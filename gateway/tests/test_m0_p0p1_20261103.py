"""M0 / P0-A / P0-B / P1 四项补齐的测试（2026-11-03 16:10 轮次）。

四条都是"设计前提错了"的修正：

| 项 | 错的前提 | 修正 |
|---|---|---|
| M0 巩固门槛 | 用户会高频聊天 | 低频也能巩固（最长等待旁路） |
| P0-A recent_chat | 主人在 pet 说话 | 跨 pet + 主人私聊，并带素材年龄 |
| P0-B 心境 | 心境只影响主动说话 | 被动回复也带心境 |
| P1 遗忘 | 插件会清 30 天前的 | 自建库自己清（双条件） |
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = GATEWAY_DIR / "memory"
for p in (str(GATEWAY_DIR), str(MEMORY_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import system  # noqa: E402

PET = "pet"
GROUP = "qq_group:某群"
DIM = 1024


# --------------------------------------------------------------------------- #
# M0：巩固门槛的最长等待旁路
# --------------------------------------------------------------------------- #
def _add_turns(channel: str, n: int) -> None:
    """插入 n 条**能被认成主人**的对话。

    ⚠ 私聊必须带 `【私聊：10000001】示例用户（主人）：` 前缀 —— `_read_recent_chat_multi` 靠它认人，
    不带前缀的行会被当成陌生人过滤掉（第一版测试就是这么红的）。
    """
    prefix = "【私聊：10000001】示例用户（主人）： " if channel == "qq_private" else "主人: "
    with sqlite3.connect(system_gateway().DB_PATH) as c:
        for i in range(n):
            c.execute(
                "insert into turns(session,role,content,path,ts,channel)"
                " values('main','user',?,'chat',?,?)",
                (f"{prefix}第{i}句", time.time() - 60, channel),
            )
        c.commit()


def system_gateway():
    import gateway

    return gateway


@pytest.fixture()
def gw(monkeypatch):
    """只把"抽事件"和"调用模型"这些外部依赖敲掉，状态读写保持真实。

    `save_state` **不能** mock —— 下面好几个用例靠"写进去再读出来"验证心境，
    mock 掉它 `load_state()` 读到的永远是默认值（第一版就是这么红的）。
    """
    import gateway

    monkeypatch.setattr(system, "_extract_events", lambda turns: [])
    return gateway


def _run_consolidate(channel: str, last_ts: float, n_turns: int) -> str:
    _add_turns(channel, n_turns)
    st = system.load_state()
    st["consolidate_wm"] = {}
    if last_ts:
        st["last_consolidate_ts"] = last_ts
    return system._consolidate_channel(st, channel)


def test_没攒够门槛且刚跑过就跳过(gw):
    """3 条 < 8，且 1 小时前刚成功巩固过 → 不该跑。"""
    st = _run_consolidate(PET, time.time() - 3600, 3)
    assert st == "skip"


def test_没攒够门槛但等太久了就跑(gw):
    """3 条 < 8，但距上次成功巩固 13 小时 → **门槛被绕过**，真的去调模型了。

    注意不能断言返回 "ok"：返回 ok 还要模型真的产出 insights。
    这里验的是「门槛有没有拦住它」，所以看审计里有没有 `idle_bypass`。
    """
    import json

    _run_consolidate(PET, time.time() - 13 * 3600, 3)
    rows = [
        json.loads(line)
        for line in Path(system.SYSTEM_AUDIT_PATH).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    bypass = [r for r in rows if r.get("action") == "idle_bypass"]
    assert bypass, "低频使用靠这条才能记住：门槛必须被绕过"
    assert bypass[0]["idle_hours"] > 12


def test_全新安装不会因为时间戳为0直接触发(gw):
    """`last_consolidate_ts=0` 时算出的 idle_hours 是"从1970年到现在"。

    不设 `last_ok > 0` 前置条件的话，第一次巩固就会无视门槛，
    等于门槛从来没生效过 —— 这是 2026-11-03 实测抓到的回归。
    """
    st = _run_consolidate(PET, 0, 3)
    assert st == "skip", "从没成功巩固过时，不应视为'等太久'"


def test_只有一条增量时不触发旁路(gw):
    """旁路要求 >= 2 条，1 条太少（一次单向消息不构成对话）。"""
    st = _run_consolidate(PET, time.time() - 13 * 3600, 1)
    assert st == "skip"


def test_关闭旁路后行为回到原样(gw, monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "consolidate_max_wait_hours", 0)
    st = _run_consolidate(PET, time.time() - 13 * 3600, 3)
    assert st == "skip", "max_wait=0 应该是关闭"


# --------------------------------------------------------------------------- #
# P0-A：recent_chat 跨渠道 + 素材年龄
# --------------------------------------------------------------------------- #
def test_recent_chat能读到主人私聊(gw):
    """pet 没新对话、私聊有新对话时，素材里应该有私聊那几条。"""
    _add_turns("qq_private", 3)
    got = system._read_recent_chat_multi(5)
    assert got, "私聊的对话应该被读到"
    assert any(t["channel"] == "qq_private" for t in got)


def test_recent_chat不读群聊(gw):
    """群友的话不能变成"主人刚说过什么"。"""
    _add_turns(GROUP, 3)
    got = system._read_recent_chat_multi(5)
    assert got == []


def test_recent_chat过滤陌生私聊(gw):
    """私聊里混着陌生人，只有 `【私聊：10000001】示例用户（主人）：` 才算。"""
    with sqlite3.connect(gw.DB_PATH) as c:
        c.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','【私聊：10000002】陌生人（路人）： 你是谁呀','chat',?,'qq_private')",
            (time.time(),),
        )
        c.commit()
    got = system._read_recent_chat_multi(5)
    assert got == [], "陌生人的话不该进「主人刚说过什么」"


def test_素材年龄算得对(gw):
    now = time.time()
    chat = [{"ts": now - 600}, {"ts": now - 60}]  # 最新那条 1 分钟前
    age = system._recent_chat_age_minutes(chat)
    assert age is not None
    assert 0.5 < age < 2.0


def test_没有时间戳时年龄是None():
    assert system._recent_chat_age_minutes([{"ts": 0}]) is None
    assert system._recent_chat_age_minutes([]) is None


# --------------------------------------------------------------------------- #
# P0-B：心境回流
# --------------------------------------------------------------------------- #
def test_心境能拼出来(gw):
    st = system._default_state()
    st["emotion"] = "sad"
    st["focus"] = "你还好吗"
    st["last_speak_ts"] = time.time() - 300
    system.save_state(st)
    line = system.mood_prompt_line()
    assert "低落" in line or "挂" in line


def test_平静且没focus就不注入(gw):
    st = system._default_state()
    st["emotion"] = "calm"
    st["focus"] = ""
    system.save_state(st)
    assert system.mood_prompt_line() == "", "没有可说的心境时不该塞空块"


def test_心境里带刚说过话的时间(gw):
    st = system._default_state()
    st["emotion"] = "happy"
    st["focus"] = "早点睡"
    st["last_speak_ts"] = time.time() - 600
    system.save_state(st)
    line = system.mood_prompt_line()
    assert "分钟前" in line, "刚说完就该记得自己开过口"


# --------------------------------------------------------------------------- #
# P0-B 隔离：心境只进主人私密空间
#
# 这条以前只靠 code review，没有断言。心境是从主人私聊提炼的私人状态，
# 漏进群聊就是跨场合串味 —— 和 people 画像、inner_os 同一条红线。
# --------------------------------------------------------------------------- #
def _build_msgs(speaker: str, local_pet: bool, channel: str = "") -> list[dict]:
    """调真实的 build_messages，看它到底拼了什么。"""
    import gateway

    return gateway.build_messages(
        "main",
        "你好",
        speaker=speaker,
        channel=channel,
        local_pet=local_pet,
    )


def _all_system_text(msgs: list[dict]) -> str:
    return "\n".join(
        str(m.get("content")) for m in msgs if m.get("role") == "system"
    )


def test_主人私聊会带心境(gw):
    """主人在 QQ 私聊里说话 → 应该带着心境。

    2026-11-04 改（T-11 第二阶段）：`build_messages` 现在也可以用 `identity` 传身份。
    以前这条只用 `speaker="主人"` 字符串调用 —— 那条路拼出来的身份
    **不可信**（`trusted_identity=False`），所以不再注入心境。

    这不是退步，正是 T-08 要的：一个裸字符串无法证明"我不是从用户正文里
    抠出来的"。真实链路（`/v1`）现在会带 `identity`，
    见 `test_identity.py::test_可信提醒块里的主人被认出来`。
    """
    import gateway

    st = system._default_state()
    st["emotion"] = "sad"
    st["focus"] = "他说今天很累"
    system.save_state(st)
    # 模拟真实链路：身份是**可信解析**出来的主人 + 私聊场合
    ident = gateway.Identity(
        uid="10000001", speaker="主人", group="",
        label="私聊", channel_key="qq_private",
        local_pet=False, trusted_identity=True,
    )
    msgs = gateway.build_messages("main", "你好", identity=ident)
    assert "此刻的状态" in _all_system_text(msgs), "可信的主人在私聊里应该带着心境"


def test_不可信身份不带心境(gw):
    """反面：只有 `speaker="主人"` 这个字符串、身份不可信 → **不**注入心境。

    这条钉住 T-11 的核心语义：授权看的是"身份可信不可信"，
    不是"这个字符串等不等于「主人」"。
    """
    import gateway

    st = system._default_state()
    st["emotion"] = "sad"
    st["focus"] = "他说今天很累"
    system.save_state(st)
    msgs = gateway.build_messages(
        "main", "你好", speaker="主人", channel="私聊", local_pet=False,
    )
    assert "此刻的状态" not in _all_system_text(msgs), (
        "裸字符串拼出来的身份不可信，不该拿到心境"
    )


def test_桌宠会带心境(gw):
    st = system._default_state()
    st["emotion"] = "happy"
    st["focus"] = "他夸我了"
    system.save_state(st)
    msgs = _build_msgs(speaker="主人", local_pet=True)
    assert "此刻的状态" in _all_system_text(msgs), "桌宠是主人私密空间，应该带心境"


def test_群聊不带心境(gw):
    """群友不该看到她的心境 —— 那是从主人私聊提炼的私人状态。"""
    st = system._default_state()
    st["emotion"] = "sad"
    st["focus"] = "主人说他很累"
    system.save_state(st)
    msgs = _build_msgs(speaker="某个群友", local_pet=False, channel="群聊·某群")
    assert "此刻的状态" not in _all_system_text(msgs), "心境漏进群聊就是串味"


def test_陌生人私聊不带心境(gw):
    st = system._default_state()
    st["emotion"] = "sad"
    st["focus"] = "主人说他很累"
    system.save_state(st)
    msgs = _build_msgs(speaker="陌生人", local_pet=False, channel="私聊")
    assert "此刻的状态" not in _all_system_text(msgs)


# --------------------------------------------------------------------------- #
# P1：遗忘（双条件）
# --------------------------------------------------------------------------- #
class FakeEmbedding:
    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim

    def get_dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> list[float]:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.random(self._dim).astype(np.float32)
        v /= np.linalg.norm(v)
        return v.tolist()

    async def get_embedding(self, text: str) -> list[float]:
        return self._vec(text)

    async def get_embeddings(self, texts):
        return [self._vec(t) for t in texts]

    async def get_embeddings_batch(self, texts, batch_size: int = 32):
        return await self.get_embeddings(texts)


@pytest.fixture()
def mem(tmp_path):
    import store as store_mod

    s = store_mod.open_store(
        db_path=str(tmp_path / "记忆.db"), dim=DIM, embedding=FakeEmbedding()
    )
    yield s
    s.close()


def _add_aged(mem, text: str, days_ago: float, importance: float) -> None:
    """写一条指定"年龄"和重要度的记忆。"""
    mem.add(
        text,
        {
            "importance": importance,
            "create_time": time.time() - days_ago * 86400,
            "session_id": "owner",
        },
    )


def test_旧且不重要会被清掉(mem):
    _add_aged(mem, "很久以前的一句闲聊", 200, 0.2)
    stats = mem.prune(older_than_days=90, min_importance=0.3)
    assert stats["matched"] == 1
    assert stats["deleted"] == 1
    assert mem.count() == 0


def test_旧但很重要会保留(mem):
    """生日、承诺这类不能被清 —— 这是双条件的意义。"""
    _add_aged(mem, "主人的生日是10月20日", 200, 0.9)
    stats = mem.prune(older_than_days=90, min_importance=0.3)
    assert stats["matched"] == 0
    assert stats["kept_important"] == 1
    assert mem.count() == 1


def test_不重要但很新会保留(mem):
    _add_aged(mem, "昨天聊的闲话", 1, 0.1)
    stats = mem.prune(older_than_days=90, min_importance=0.3)
    assert stats["matched"] == 0
    assert mem.count() == 1


def test_dry_run不真删(mem):
    _add_aged(mem, "该清的", 200, 0.1)
    stats = mem.prune(older_than_days=90, min_importance=0.3, dry_run=True)
    assert stats["matched"] == 1
    assert stats["deleted"] == 0
    assert mem.count() == 1


def test_拿不到时间就不删(mem):
    """fail-safe：宁可留着，也不能因为元数据缺字段误删。"""
    mem.add("没有 create_time 的记忆", {"importance": 0.1})
    with sqlite3.connect(mem.db_path) as c:
        import json

        rows = c.execute("select doc_id, metadata from documents").fetchall()
        for doc_id, meta in rows:
            d = json.loads(meta or "{}")
            d.pop("create_time", None)
            c.execute(
                "update documents set metadata=? where doc_id=?",
                (json.dumps(d, ensure_ascii=False), doc_id),
            )
        c.commit()
    stats = mem.prune(older_than_days=90, min_importance=0.3)
    assert stats["matched"] == 0

# --------------------------------------------------------------------------- #
# 系统层素材必须是双向的（2026-11-09）
# --------------------------------------------------------------------------- #
def test_她的回复要跟着主人的话一起读到(gw):
    """只读到主人说的话、读不到她接的话 —— 系统层就会把主语搞反。

    实测事故：内心OS 写成「**他**坚持要正经夸奖」，
    而要夸奖的其实是小鱼。

    ⚠ 这里不能用 `_add_turns`（它只插 user 行）。要自己插一对
    「主人说 + 她回」，才能测出回复有没有跟着进来。
    """
    import sqlite3

    owner_tag = "【私聊：10000001】示例用户（主人）： "
    with sqlite3.connect(gw.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session, role, content, path, ts, channel)"
            " values(?,?,?,?,?,?)",
            (gw.SHARED_SESSION, "user", owner_tag + "那主人夸夸我嘛",
             "chat", 100.0, "qq_private"),
        )
        conn.execute(
            "insert into turns(session, role, content, path, ts, channel)"
            " values(?,?,?,?,?,?)",
            (gw.SHARED_SESSION, "assistant", "我这么乖，什么都记得住",
             "chat", 101.0, "qq_private"),
        )
        conn.commit()

    turns = system._read_recent_chat_multi(20)
    mine = [t for t in turns if t.get("role") == "assistant"]
    theirs = [t for t in turns if t.get("role") != "assistant"]

    assert theirs, "主人的话应该读到"
    assert mine, "**她的回复也必须读到**，否则模型只看到单方面的对话"
    assert any("夸夸我" in str(t.get("content") or "") for t in theirs)
    assert any("记得住" in str(t.get("content") or "") for t in mine)


def test_陌生人的回复不会混进来(gw):
    """fail-closed：路人说的和她的回复都不能算"主人刚说过什么"。"""
    import sqlite3

    other_tag = "【私聊：10000002】张三（路人）：在吗在吗"
    with sqlite3.connect(gw.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session, role, content, path, ts, channel)"
            " values(?,?,?,?,?,?)",
            (gw.SHARED_SESSION, "user", other_tag, "chat", 9999999999.0,
             "qq_private"),
        )
        conn.execute(
            "insert into turns(session, role, content, path, ts, channel)"
            " values(?,?,?,?,?,?)",
            (gw.SHARED_SESSION, "assistant", "你是谁呀", "chat", 9999999999.1,
             "qq_private"),
        )
        conn.commit()

    turns = system._read_recent_chat_multi(20)
    joined = "\n".join(str(t.get("content") or "") for t in turns)
    assert "张三" not in joined, "路人的话不能进主人素材"
    assert "你是谁呀" not in joined, "路人之后的回复也不能跟进来"
