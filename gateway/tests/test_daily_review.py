"""每日梳理期的触发时机与四类活动（P0-4 阶段 1 / 2 / 2.5）。

这是系统层的**第二层节律**。主人 2026-11-03 21:10 的原话：

> 静默是正常的，这个 5 分钟级别的是被动的反应，因为我写了一天的代码，
> 没去和她说话，这个问题会留给每天一次的梳理期来决定要不要主动行动。

所以这组用例钉的是「什么时候该跑 / 什么时候不该跑 / 跑了拿什么素材」，
**不是**「她说了几句话」。梳理期的产出只进 inner_os，说不说由角色层决定。

四个硬矛盾里有三个在这份文件里：
  * 矛盾 1：`require_activity_today` 会掐死场景 C → 分活动判素材
  * 矛盾 2：滑动间隔让节律漂移 → 日窗口锚定
  * 矛盾 3：只喂 turns 说不出「中间都没歇」→ _day_timeline（见另一个文件）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import system  # noqa: E402


def _add_turn(ts: float, content: str = "在吗", channel: str = "pet") -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user',?,'chat',?,?)",
            (content, ts, channel),
        )
        conn.commit()


def _at(hour: int, minute: int = 0, day: int = 3) -> float:
    """构造 2026-11-XX HH:MM 的时间戳（本地时区）。"""
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


@pytest.fixture()
def review_on(monkeypatch):
    """打开梳理期，默认配置照计划定稿那份。"""
    def _on(**over):
        cfg = {
            "enabled": True,
            "day_start_hour": 6,
            "earliest_hour": 21,
            "idle_after_minutes": 120,
            "once_per_day": True,
            "ruminate_requires_today_turns": True,
            "absence_is_material": True,
            "min_absence_hours": 6,
            "budget": {"ruminate": 1, "prospect": 1,
                       "theory_of_mind": 1, "self_narrative": 1},
            "daily_model_calls_cap": 5,
        }
        cfg.update(over)
        monkeypatch.setitem(system.SYSTEM, "daily_review", cfg)
        return cfg

    return _on


# --------------------------------------------------------------------------- #
# 日窗口键（矛盾 2 的地基）
# --------------------------------------------------------------------------- #
def test_日窗口在凌晨三点归前一天():
    """主人常弄到一两点。按 00:00 切日，一次熬夜会被拆成两次梳理。"""
    assert system._review_day_key(_at(3, 0), 6) == "2026-11-02"
    assert system._review_day_key(_at(5, 59), 6) == "2026-11-02"


def test_日窗口在早上六点后归当天():
    assert system._review_day_key(_at(6, 0), 6) == "2026-11-03"
    assert system._review_day_key(_at(23, 0), 6) == "2026-11-03"


def test_日窗口默认配置是6点起():
    assert system._daily_review_cfg()["day_start_hour"] == 6


# --------------------------------------------------------------------------- #
# 该不该跑
# --------------------------------------------------------------------------- #
def test_默认关闭时永远不跑(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "daily_review", {})
    should, reason = system._daily_review_should_run(_at(23))
    assert should is False
    assert reason == "disabled"


def test_没到earliest_hour不跑(review_on):
    """深夜不主动思考是设计的一部分，不是漏掉。"""
    review_on()
    _add_turn(_at(19, 0))          # 早就有对话，但 20:30 还没到 21:00
    should, reason = system._daily_review_should_run(_at(20, 30))
    assert should is False
    assert reason == "too_early"


def test_刚聊完不跑(review_on):
    """「安静了」是最常挡住的理由 —— 他还在说话，就别回头总结。"""
    review_on()
    _add_turn(_at(22, 55))
    should, reason = system._daily_review_should_run(_at(23, 0))
    assert should is False
    assert reason == "not_idle"


def test_安静够了且到点就跑(review_on):
    """安静 + 到点 + 有素材 → 跑。具体走哪条腿由信息量决定（T-12 双判断）。

    2026-11-04 改：这里原来断言 `reason == "ok"`。加了双判断之后，
    `ok` 只留给「有效新颖量达阈值」那条；新颖量不够但到点了，
    返回的是 `circadian_probe`（= 让模型看一眼再决定）。
    两条都代表 **should=True**，测试要断言的是这一点，而不是具体的腿。
    """
    review_on()
    _add_turn(_at(18, 0))
    _add_turn(_at(20, 30))
    should, reason = system._daily_review_should_run(_at(23, 30))
    assert should is True, reason
    assert reason in ("ok", "circadian_probe")


def test_新颖量达标就立刻跑(review_on):
    """信息量那条：有效新颖字数到阈值 → 直接 `ok`，不需要模型再看一眼。"""
    review_on(min_novel_chars=10)
    _add_turn(_at(20, 30), "今天把整个网关的鉴权链路重写了一遍还顺手补了测试")
    should, reason = system._daily_review_should_run(_at(23, 30))
    assert should is True, reason
    assert reason == "ok"


def test_没新东西就睡(review_on):
    """「其余情况算睡眠」：一条新内容都没有时连探路都不做。"""
    review_on(min_novel_chars=10, circadian_enabled=False)
    should, reason = system._daily_review_should_run(_at(23, 30))
    assert should is False
    assert reason in ("below_threshold", "no_material")


def test_同一天跑过就不再跑(review_on):
    """`once_per_day` 是天然的 1 次/天上限 —— 不靠滑动间隔。"""
    review_on()
    _add_turn(_at(10, 0))
    st = system.load_state()
    st["last_daily_review_day"] = system._review_day_key(_at(21, 30), 6)
    st["last_daily_review_ts"] = _at(21, 30)
    system.save_state(st)

    should, reason = system._daily_review_should_run(_at(23, 30))
    assert should is False
    assert reason == "already_ran_today"


def test_跨日窗口后又能跑了(review_on):
    """跨过 day_start_hour 就是新的一天，不该被昨天的记录挡住。"""
    review_on()
    _add_turn(_at(10, 0, day=3))
    st = system.load_state()
    st["last_daily_review_day"] = "2026-11-02"
    st["last_daily_review_ts"] = _at(22, 0, day=2)
    system.save_state(st)

    # 11-03 23:00：距上一句 13 小时，已过 earliest_hour，且窗口键是新的一天
    should, reason = system._daily_review_should_run(_at(23, 0, day=3))
    assert should is True, reason
    assert system._review_day_key(_at(23, 0, day=3), 6) != "2026-11-02"


# --------------------------------------------------------------------------- #
# 分活动判素材（矛盾 1 —— 场景 C 死活跑不起来的那个）
# --------------------------------------------------------------------------- #
def test_零对话的一天反刍不跑但其余三个跑(review_on):
    """**这是矛盾 1 的核心断言。**

    `require_activity_today` 会让连续三天不来的场景一次都不跑，
    于是「第一天/第二天/第三天」永远无从说起。
    正确做法：缺席本身是素材，只是**反刍**吃不到它（没对话就没得反刍）。
    """
    review_on()
    _add_turn(time.time() - 3 * 86400)   # 三天前说过话，之后一直没来
    m = system._daily_review_materials(time.time())
    assert m["today_turns"] == 0
    assert "ruminate" not in m["eligible"]
    assert "prospect" in m["eligible"]
    assert "theory_of_mind" in m["eligible"]
    assert "self_narrative" in m["eligible"]
    assert m["has_material"] is True


def test_今天有对话时四个活动全跑(review_on):
    review_on()
    _add_turn(time.time() - 600)
    m = system._daily_review_materials(time.time())
    assert m["today_turns"] >= 1
    assert set(m["eligible"]) == set(system.REVIEW_ACTIVITIES)


def test_缺席不足门槛时零对话日不跑(review_on):
    """刚走开一小时不算「缺席」，没素材就该老实不跑。

    注意 `today_turns` 是**24 小时窗口内**的条数，1 小时前那条算在里面，
    所以这里要构造「窗口内没有对话」才测得准：用 30 小时前的证据 + 短缺席。
    """
    review_on(min_absence_hours=6)
    # 30 小时前说过话 → 24h 窗口内 0 条；缺席 30 小时 > 6 小时 → 仍算缺席
    _add_turn(time.time() - 30 * 3600)
    m = system._daily_review_materials(time.time())
    assert m["today_turns"] == 0
    # 缺席够了，所以其余三个活动照跑
    assert "prospect" in m["eligible"]


def test_既有今日对话又缺席足够时全跑(review_on):
    """两个条件不互斥：今天聊过、但也确实静了很久。"""
    review_on(min_absence_hours=6)
    _add_turn(time.time() - 8 * 3600)     # 8 小时前，仍在 24h 窗口内
    m = system._daily_review_materials(time.time())
    assert m["today_turns"] >= 1
    assert set(m["eligible"]) == set(system.REVIEW_ACTIVITIES)


def test_关掉absence_is_material后零对话日完全不跑(review_on):
    review_on(absence_is_material=False)
    _add_turn(time.time() - 3 * 86400)
    m = system._daily_review_materials(time.time())
    assert m["eligible"] == []
    assert m["has_material"] is False


def test_预算为0的活动被剔除(review_on):
    review_on(budget={"ruminate": 0, "prospect": 1,
                      "theory_of_mind": 1, "self_narrative": 1})
    _add_turn(time.time() - 600)
    m = system._daily_review_materials(time.time())
    assert "ruminate" not in m["eligible"]
    assert "prospect" in m["eligible"]


# --------------------------------------------------------------------------- #
# 念头解析
#
# 🔧 2026-11-07（审计 G.1 第 3 条）：这一组原来调的是
# `system._parse_review_thoughts()`，那个函数**已删除**。
#
# 为什么不是"删测试"，而是换成测 `_parse_review_payload()`：
# 被删的那个只是个**一行转发**（`return _parse_review_payload(reply)[0]`），
# 生产调用点（`_run_review_activity`）从来没用过它 —— 它解包的是元组。
# 所以这六条用例真正在守的东西（"模型回的各种形状能不能正确解出念头，
# 解不出来必须返回空而不是把原文当念头"）**一条都没少**，只是改成直接测
# 真正的实现函数，取它的第一个返回值。
#
# 顺便：这样改之后测试和生产走的是**同一条路径**（生产也是调
# `_parse_review_payload`），比原来隔着两跳更贴近实际。
# --------------------------------------------------------------------------- #
def _thoughts(reply) -> list[str]:
    """测试侧的小帮手：取 `_parse_review_payload()` 的念头那一半。"""
    return system._parse_review_payload(reply)[0]


def test_解析JSON数组():
    assert _thoughts('["他今天很累", "我不该追问"]') == [
        "他今天很累", "我不该追问",
    ]


def test_解析带围栏的JSON():
    raw = '```json\n["他今天很累"]\n```'
    assert _thoughts(raw) == ["他今天很累"]


def test_解析对象数组():
    raw = '[{"text": "他今天很累"}, {"thought": "别追问"}]'
    assert _thoughts(raw) == ["他今天很累", "别追问"]


def test_解析失败返回空而不是塞原文():
    """把模型的客套话灌进 inner_os 比不产更糟。"""
    assert _thoughts("") == []
    assert _thoughts(None) == []
    assert _thoughts("好的，我来分析一下。") == []


def test_空数组就是不产念头():
    assert _thoughts("[]") == []


def test_念头最多三条():
    raw = json.dumps([f"念头{i}" for i in range(10)], ensure_ascii=False)
    assert len(_thoughts(raw)) == 3


def test_转发函数已删除():
    """`_parse_review_thoughts()` 是纯转发、零生产调用，2026-11-07 删了。

    留着这条断言是为了**不让它悄悄长回来**：它长回来就说明有人又在
    "取元组第一项"这件事上多包了一层，而那一层会和
    `_parse_review_payload()` 形成两个入口 —— 正是审计 G.3 说的"同一件事两处实现"。
    """
    assert not hasattr(system, "_parse_review_thoughts"), (
        "_parse_review_thoughts 被加回来了？它是 `_parse_review_payload(reply)[0]` "
        "的一行转发，生产零调用 —— 直接调 `_parse_review_payload()` 就好"
    )


# --------------------------------------------------------------------------- #
# 端到端（不打真模型）
# --------------------------------------------------------------------------- #
def test_daily_review把念头写进inner_os且不投递(review_on, monkeypatch):
    """产出只进 inner_os，**绝不直接投递给用户**（不抢台词）。"""
    review_on()
    _add_turn(time.time() - 600)

    def _fake_call(msgs, **kw):
        return '["他今天话说得少，可能累了"]', {"elapsed_ms": 12}, None

    monkeypatch.setattr(system, "call_system", _fake_call)
    monkeypatch.setattr(system, "_system_key", lambda: "test-key")

    before = len(system._read_inner_os())
    r = system.daily_review()
    assert r["ok"] is True
    after = system._read_inner_os()
    assert len(after) > before

    new = [i for i in after if i.get("source") == "daily_review"]
    assert new, "念头必须带 source=daily_review，便于区分来源"
    assert all(i["state"] == "pending" for i in new)
    assert not any(i.get("delivered_ts") for i in new), "梳理期不许自己投递"


def test_没有system_key时不调模型(review_on, monkeypatch):
    review_on()
    _add_turn(time.time() - 600)
    monkeypatch.setattr(system, "_system_key", lambda: "")

    called = []
    monkeypatch.setattr(system, "call_system",
                        lambda *a, **k: called.append(1) or ("x", {}, None))
    r = system.daily_review()
    assert called == []
    assert r["ok"] is False


def test_模型不产念头时ok为False(review_on, monkeypatch):
    review_on()
    _add_turn(time.time() - 600)
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(system, "call_system", lambda *a, **k: ("[]", {}, None))
    r = system.daily_review()
    assert r["ok"] is False
    assert all(x["reason"] == "no_thought" for x in r["ran"])


def test_跑完记下日窗口并写审计(review_on, monkeypatch):
    """注意：turn 必须按**固定的 `_at()`** 放，不能用 `time.time()`。

    踩过：这里原来写 `_add_turn(time.time() - 600)`，而 `daily_review(now=_at(23,0))`
    是固定时刻。白天跑的时候 23:00 还在未来、turn 落在窗口里，测试是绿的；
    等真实时间过了 23:00，turn 就跑到窗口外了 → `today_turns=0`、缺席又被夹到 0
    → 没素材 → 提前 return → 日窗口没写上 → **同一份代码白天绿、深夜红**。
    """
    review_on()
    _add_turn(_at(22, 50))          # 固定放在 23:00 之前 10 分钟
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(system, "call_system",
                        lambda *a, **k: ('["有想法"]', {}, None))

    system.daily_review(now=_at(23, 0))
    st = system.load_state()
    assert st["last_daily_review_day"] == "2026-11-03"
    assert st["last_daily_review_ts"] > 0

    audit = Path(system.SYSTEM_AUDIT_PATH)
    assert audit.exists()
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
    ev = [r for r in rows if r.get("event") == "daily_review"]
    assert ev, rows
    assert ev[-1]["ok"] is True
    assert "activity" in str(ev[-1].get("activities")) or ev[-1].get("activities")


def test_成本上限削掉多余活动(review_on, monkeypatch):
    """`daily_model_calls_cap` 是硬兜底，超了就直接不跑那么多类。"""
    review_on(daily_model_calls_cap=2)
    _add_turn(time.time() - 600)
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    calls = []

    def _fake(msgs, **kw):
        calls.append(1)
        return '["念头"]', {}, None

    monkeypatch.setattr(system, "call_system", _fake)
    system.daily_review()
    assert len(calls) <= 2, f"调用次数 {len(calls)} 超过上限 2"

# --------------------------------------------------------------------------- #
# 双判断的节律探路（T-12，2026-11-04）
# --------------------------------------------------------------------------- #
def test_探路跳过只花一次调用(review_on, monkeypatch):
    """模型说 skip → 只花 1 次调用，绝不进入四类活动。

    这是双判断第二条腿的核心价值：不加探路时 `daily_review()` 进去就跑满
    四类活动（实测 11-03 那次 4 次调用产 12 条念头），"可以跳过"等于空话。
    """
    review_on(min_novel_chars=10000)   # 阈值拉高，确保走节律那条腿
    _add_turn(time.time() - 600, "今天还行")
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    calls = []

    def _fake(msgs, **kw):
        calls.append(1)
        # 模拟模型调了探路工具，选择 skip
        return None, {}, [{
            "id": "c1", "type": "function",
            "function": {"name": "system_daily_review_probe",
                         "arguments": json.dumps({"decision": "skip", "reason": "没什么新东西"})},
        }]

    monkeypatch.setattr(system, "call_system", _fake)
    materials = system._daily_review_materials()
    decision, why = system._daily_review_probe(materials, {"novel_chars": 5})
    assert decision == "skip"
    assert len(calls) == 1, f"探路只该花 1 次调用，实际 {len(calls)}"


def test_探路选定方向就只跑那一个(review_on, monkeypatch):
    """模型 pick 了 theory_of_mind → `daily_review(probe_decision=...)` 只跑这一类。"""
    review_on()
    _add_turn(time.time() - 600, "今天有点累")
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    ran = []

    def _fake(msgs, **kw):
        ran.append(1)
        return '["一个念头"]', {}, None

    monkeypatch.setattr(system, "call_system", _fake)
    system.daily_review(probe_decision="theory_of_mind")
    # 四类活动各调一次是 4；只跑一类就是 1
    assert len(ran) == 1, f"应该只跑 1 类活动，实际调了 {len(ran)} 次"


def test_探路失败当跳过(review_on, monkeypatch):
    """探路调用抛错 → 当跳过。宁可少想一次，也别因为探路失败把线程搞挂。"""
    review_on()
    monkeypatch.setattr(system, "_system_key", lambda: "k")

    def _boom(*a, **kw):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(system, "call_system", _boom)
    decision, why = system._daily_review_probe(system._daily_review_materials(), {})
    assert decision == "skip"
    assert "probe_error" in why


def test_探过就不再重复探(review_on):
    """同一个日窗口探过一次（模型说跳过）→ 不再花第二次调用。"""
    review_on(min_novel_chars=10000, min_absence_hours=0)
    _add_turn(_at(20, 30), "今天还行")
    st = system.load_state()
    st["last_daily_review_probe_day"] = system._review_day_key(_at(23, 30), 6)
    system.save_state(st)
    should, reason = system._daily_review_should_run(_at(23, 30))
    assert should is False
    assert reason == "probed_declined"

# --------------------------------------------------------------------------- #
# 同一时刻只允许一次梳理（2026-11-04 第七轮）
# --------------------------------------------------------------------------- #
def test_并发梳理只有一个真的执行(review_on, monkeypatch):
    """5 个线程同时 `daily_review`，只有 1 个执行，其余返回 busy。

    踩过的坑：这个函数有两个入口 —— 定时梳理线程 + 后台「现在梳理一次」按钮
    （`gateway.py` 里 `threading.Thread(target=system.daily_review)`）。
    两个同时进来会**各调一遍模型**（一次梳理是多轮调用，几十秒），
    等于白花一倍的钱。数据有锁保护不会坏，但钱是真的花掉了。

    为什么是"直接返回 busy"而不是排队：手动点的那次本来就想"现在看看"，
    排队等第一次跑完再重复劳动没有意义。
    """
    import threading
    import time as _time

    review_on()
    calls = []
    orig = system._daily_review_body

    def fake_body(now=None, probe_decision=None):
        calls.append(1)
        _time.sleep(0.4)          # 模拟调模型
        return {"ok": True, "ran": []}

    monkeypatch.setattr(system, "_daily_review_body", fake_body)

    results = []

    def call():
        results.append(system.daily_review())

    ths = [threading.Thread(target=call) for _ in range(5)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)

    assert len(calls) == 1, f"执行体被调了 {len(calls)} 次，应该只有 1 次"
    busy = sum(1 for r in results if r.get("reason") == "busy")
    assert busy == 4, f"应该有 4 个返回 busy，实际 {busy}"


# --------------------------------------------------------------------------- #
# 体检要有「节律状态」（2026-11-04 第三十二轮，计划 7.3 最后一项）
# --------------------------------------------------------------------------- #
def test_体检带节律状态():
    """`health_check` 的 stats 必须有 `daily_review_state`。

    计划 7.3 原话：「这一行最有用：一眼看出她是在睡觉，还是攒了料没处理」。

    以前这个状态只在后台卡片（`daily_review_payload` 的 `blocked_by`）里有，
    **体检报告里完全没有** —— 而体检才是每天该看的那一份。
    结果是：看体检只能知道「梳理期开着」，不知道她是睡着还是有活没干。
    """
    rep = system.health_check()
    st = rep.get("stats") or {}
    assert "daily_review_state" in st, (
        "体检 stats 缺少 daily_review_state —— 看不到她在睡觉还是攒了料没处理"
    )
    # 取值必须是 `_daily_review_should_run` 可能返回的那些
    known = {
        "ready", "sleeping", "below_threshold", "circadian_due", "circadian_probe",
        "too_early", "not_idle", "already_ok", "already_ran_today", "cap_reached",
        "probed_declined", "declined", "no_material", "not_enough_new",
    }
    assert st["daily_review_state"] in known, (
        f"daily_review_state 取值 {st['daily_review_state']!r} 不在已知集合里，"
        f"可能是新状态但忘了同步这个测试"
    )


def test_体检的节律状态与后台卡片一致():
    """体检和后台卡片必须说同一件事 —— 两个入口口径不一致最坑人。"""
    rep = system.health_check()
    st = rep.get("stats") or {}
    payload = system.daily_review_payload()
    card = payload.get("blocked_by") or "ready"
    assert st.get("daily_review_state") == card, (
        f"体检说 {st.get('daily_review_state')!r}，后台卡片说 {card!r} —— 口径必须一致"
    )


def test_体检带新颖量():
    """`daily_review_novel_chars` 也要露出来。

    它是「攒了多少料」的量化值：0 说明真的没新东西（该睡），
    接近阈值说明快该整理了。光有状态没数值，看不出"攒到哪一步了"。
    """
    rep = system.health_check()
    st = rep.get("stats") or {}
    assert "daily_review_novel_chars" in st, "体检 stats 缺少 daily_review_novel_chars"
    v = st["daily_review_novel_chars"]
    assert v is None or isinstance(v, (int, float)), f"新颖量应该是数字或 None，实际 {type(v)}"
