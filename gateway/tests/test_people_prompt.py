"""阶段 5 收口：人物画像从「只写」改成「真的用上」。

`_read_people_profile()` 以前只有 `role_speak.maybe_speak()`（主动说话）一个调用点，
而且写死 `pet`——被动回复这条路（`gateway.build_messages`）完全不读画像。
于是群友画像建了也用不上，主人画像 QQ 对话时也用不上：验收只达了「存」。

本文件守的就是「真的用上 + 不越界」：

1. 群 A 的画像只进群 A 的 prompt，不进群 B / 私聊 / 桌宠
2. owner 画像同时进 `pet` 和主人私聊（主人可跨场合）
3. 陌生人私聊不建模、也不注入
4. `updated_ts` 太旧要带降权提示，新鲜的不带
5. 没画像返回空串，prompt 里不留空 system 块
6. `people_channels` 支持 `qq_group:*` 通配（来新群不用改配置）
7. 画像超长要截断，但不能把「可能不准」这句挤掉
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import system  # noqa: E402

GROUP_A = "qq_group:王家"
GROUP_B = "qq_group:李家"


def _system_text(**kwargs) -> str:
    """跑一次 build_messages，把里面所有 system 块拼成一段，方便断言。"""
    msgs = gateway.build_messages("main", "测试一句", **kwargs)
    return "\n".join(
        str(m.get("content") or "") for m in msgs if m.get("role") == "system"
    )


def _profile(channel: str, profile: dict, days_ago: float = 0.0) -> None:
    system._write_people_profile(
        channel,
        {
            "channel": channel,
            "updated_ts": time.time() - days_ago * 86400,
            "profile": profile,
        },
    )


# ---------------------------------------------------------------- 1. 群隔离
def test_群A的画像只进群A的prompt():
    _profile(GROUP_A, {"称呼": "老王", "在意的事": ["猫"]})
    a = _system_text(channel="群聊·王家", speaker="老王")
    assert "你对这个群里的人的了解" in a, "群聊要用「这个群里的人」的措辞"
    assert "老王" in a
    # 关键：不能写成「他」——模型会当成在说主人
    assert "你对他的了解" not in a

    b = _system_text(channel="群聊·李家", speaker="小李")
    assert "老王" not in b and "你对这个群里的人的了解" not in b

    priv = _system_text(channel="私聊", speaker="主人")
    assert "老王" not in priv
    pet = _system_text(channel="", local_pet=True)
    assert "老王" not in pet


def test_群画像不会冒充主人画像():
    """把群画像写成「他」，模型会拿群里的印象去猜主人——措辞是隔离的一部分。"""
    _profile(GROUP_A, {"称呼": "老王"})
    a = _system_text(channel="群聊·王家", speaker="老王")
    assert "你对他的了解" not in a


# ---------------------------------------------------------------- 2. 主人跨场合
def test_owner画像同时进桌宠和主人私聊():
    _profile("pet", {"称呼": "主人", "作息": "常熬夜"})
    pet = _system_text(channel="", local_pet=True)
    assert "你对他的了解" in pet and "常熬夜" in pet
    priv = _system_text(channel="私聊", speaker="主人")
    assert "你对他的了解" in priv and "常熬夜" in priv
    # 不能顺带漏进群聊
    group = _system_text(channel="群聊·王家", speaker="老王")
    assert "常熬夜" not in group


# ---------------------------------------------------------------- 3. 陌生人私聊
def test_陌生人私聊不建模也不注入(monkeypatch):
    called: list = []
    monkeypatch.setattr(
        system, "call_system", lambda msgs, **kw: called.append(msgs) or (None, {}, None)
    )
    ok = system.update_people_profile(
        "qq_private", [{"role": "user", "content": "【私聊：10000002】张三（路人）： 在吗", "ts": 0}]
    )
    assert ok is False, "qq_private 分不清主人/陌生人，必须拒绝建模"
    assert called == [], "拒绝建模时不该浪费一次模型调用"
    assert system._read_people_profile("qq_private") == {}
    assert system.people_prompt_line("qq_private") == ""


def test_混合私聊只拿主人那几句去建模(monkeypatch):
    """同一批 qq_private 里混着陌生人：建模输入里一个字都不能有张三。"""
    captured: dict = {}

    def fake(msgs, **kw):
        captured["msgs"] = msgs
        return '{"称呼":"主人"}', {}, None

    monkeypatch.setattr(system, "call_system", fake)
    mixed = [
        {"id": 1, "role": "user", "content": "【私聊：10000001】示例用户（主人）： 今天好累", "ts": 0},
        {"id": 2, "role": "assistant", "content": "那早点睡", "ts": 0},
        {"id": 3, "role": "user", "content": "【私聊：10000002】张三（路人）： 在吗在吗", "ts": 0},
    ]
    routed = system._filter_owner_turns(mixed)
    assert system.update_people_profile("pet", routed) is True
    blob = json.dumps(captured["msgs"], ensure_ascii=False)
    assert "今天好累" in blob
    assert "张三" not in blob


# ---------------------------------------------------------------- 4. 陈旧降权
def test_三十天前的画像带降权提示():
    _profile("pet", {"称呼": "主人"}, days_ago=30)
    line = system.people_prompt_line("pet")
    assert "30 天前的印象" in line
    assert "不准" in line
    assert "不准" in _system_text(channel="", local_pet=True)


def test_当天更新的画像不带降权提示():
    _profile("pet", {"称呼": "主人"}, days_ago=0)
    line = system.people_prompt_line("pet")
    assert "你对他的了解" in line
    assert "天前的印象" not in line


# ---------------------------------------------------------------- 5. 没画像
def test_没画像返回空串且prompt不留空块():
    assert system.people_prompt_line("pet") == ""
    assert system.people_prompt_line(GROUP_A) == ""
    msgs = gateway.build_messages("main", "测试一句", channel="", local_pet=True)
    for m in msgs:
        assert str(m.get("content") or "").strip(), f"不许出现空 system 块：{m}"
    joined = "\n".join(str(m.get("content") or "") for m in msgs)
    assert "\n\n\n" not in joined


# ---------------------------------------------------------------- 6. 通配配置
def test_people_channels支持群通配():
    assert system._people_enabled(GROUP_A, ["pet", "qq_group:*"]) is True
    assert system._people_enabled(GROUP_A, ["pet"]) is False
    # qq_private 不是可建模的主体，两种配置下都不能命中
    assert system._people_enabled("qq_private", ["pet", "qq_group:*"]) is False
    assert system._people_enabled("qq_private", ["pet"]) is False
    # 通配只到「同一渠道族」，不许跨族
    assert system._people_enabled("pet", ["qq_group:*"]) is False
    assert system._people_enabled("", ["pet", "qq_group:*"]) is False


# ---------------------------------------------------------------- 7. 长度上限
def test_画像太长会截断但保住降权提示():
    _profile("pet", {"称呼": "很长" * 200, "作息": "熬夜" * 100}, days_ago=30)
    line = system.people_prompt_line("pet")
    assert len(line) <= system.PEOPLE_PROMPT_MAX_LINE == 400
    assert "不准" in line, "降权提示不能被截断挤掉"
    assert "天前的印象" in line


# ---------------------------------------------------------------- 8. 新群自动建模
def _add_turns(channel: str, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        for role, content in rows:
            conn.execute(
                "insert into turns(session, role, content, path, ts, channel) values(?,?,?,?,?,?)",
                (gateway.SHARED_SESSION, role, content, "chat", time.time(), channel),
            )
        conn.commit()


def test_新群不改配置也会建模并进prompt(monkeypatch):
    """`people_channels` 里的 `qq_group:*` 要真的生效，否则来一个新群就永远没画像。"""
    _add_turns(
        GROUP_A,
        [
            ("user", "【群聊·王家：10000002】老王（路人）： 我家猫又拆家了"),
            ("assistant", "那你收拾起来挺麻烦吧。"),
        ] * 5,
    )
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    monkeypatch.setattr(system, "_extract_events", lambda turns: [])

    def fake(msgs, *, tool_choice=None, tools=None, max_tokens=None):
        # 建模那次不带工具；巩固那次带两个工具 → 让他什么都不选
        if not tools:
            return '{"称呼":"老王","在意的事":["猫"]}', {}, None
        return None, {}, None

    monkeypatch.setattr(system, "call_system", fake)
    system._consolidate_body()

    assert system._read_people_profile(GROUP_A).get("profile", {}).get("称呼") == "老王"
    line = system.people_prompt_line(GROUP_A)
    assert "你对这个群里的人的了解" in line and "老王" in line
    # 只进本群
    assert "老王" not in system.people_prompt_line(GROUP_B)
    assert "老王" not in system.people_prompt_line("pet")
