"""事件抽取（DMN 阶段 1）的**提示词/schema 防回退**测试。

提示词没法断言「抽得准不准」，但能钉住「这层约束还在不在」。背景是 2026-11-03
对生产 `gateway/data/` 的 11 条真实事件抽检，发现三处质量缺口，本文件逐条兜住：

1. **`approach_hint` 语义漂移**——生产里被用来存长期特征
   （「常忙到很晚，作息偏晚」「会忘记吃饭，可适时提醒」），根因是 schema 里
   `approach_hint` / `due_hours` / `subject` 只有 `maxLength` 没有 `description`，
   模型只能靠字段名猜。→ `test_schema_三个字段都有说明`
2. **主人下的规定被抽成 `observation`**——「禁止回答某类话题」是**她对小菲鱼的
   行为约束**（人格/策略层），不是「他是个什么样的人」。污染往后的心境/自我叙事。
   → `test_提示词有_observation_边界`
3. **`deferred_care` 一条都抽不出来**——生产 11 条里 `deferred_care` = 0、
   `schedules` 一直是 0，阶段 3 定时任务和阶段 4 三态回访在生产上从未被走到。
   真实对话里「迟点再修吧」「过两天」被抽成了 `promise`。
   → `test_提示词有_deferred_care_触发词` / `test_提示词写清_deferred_care_与_promise_的边界`

外加一条纯代码不变量：到期时间只属于 `deferred_care`（`promise` 带 `due_ts` 会让
「什么时候该回头」出现第二个含义）。→ 两条 `test_validate_events_*` 用例

最后是问题 3 的**第二条通路**：模型抽对了 `kind` 却漏给 `due_hours` 时，条目会变成
`status=deferred` + `due_ts` NULL + 不排班 + 永不关单的死记录。现在校验层兜底 48h，
并且端到端钉住「落库 → deferred → 排班」这条链，同时反向钉住群渠道仍不排班。
→ 三条 `test_漏给_due_hours_*` 用例

本文件只读常量、只调纯函数，不落库、不联网，不碰生产数据（conftest 另有兜底）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402

#: 真实对话里出现过的「过一阵再回头看」信号词。提示词至少要给出 3 个，
#: 否则模型又从零开始猜（问题 3 的根因）。
DEFER_SIGNALS = ("迟点", "过两天", "等忙完", "回头再看", "忙完这阵", "明天再说吧")

#: `_extract_events` 组 prompt 时用的最小 turns（只为触发一次组包）。
_TURNS = [
    {"id": 1, "role": "user", "content": "修 bug 的事迟点再说吧，这会儿不想碰", "ts": 0},
    {"id": 2, "role": "assistant", "content": "好，那我不催你", "ts": 1},
]


def _event_item_schema() -> dict:
    """取出 `EXTRACT_EVENTS_TOOL` 里单条事件的 properties。"""
    return (
        system.EXTRACT_EVENTS_TOOL["function"]["parameters"]["properties"]["events"]["items"][
            "properties"
        ]
    )


def _captured_prompt(monkeypatch) -> str:
    """跑一遍 `_extract_events`，把发给模型的 system 文本原样捞出来。

    模型调用整个打桩（不发网络请求），只记录 messages。
    """
    seen: list[list[dict]] = []

    def _fake_call_system(msgs, **_kw):
        seen.append(msgs)
        return None, {}, None

    monkeypatch.setattr(system, "call_system", _fake_call_system)
    assert system._extract_events(_TURNS) == []  # 没调工具 → 空，不抛
    assert len(seen) == 1, "抽取应该只调一次模型"
    # 第一条 system 是 SYSTEM_POLICY，这里**只**取抽取用的那一段，避免断言被
    # 人格文本里的词蒙对（那样测的就不是抽取提示词了）。
    contents = [m.get("content") or "" for m in seen[0] if m.get("role") == "system"]
    prompt = next((c for c in contents if "kind 怎么选" in c), "")
    assert prompt, "没在 messages 里找到抽取提示词"
    return prompt


# --------------------------------------------------------------------------- #
# 问题 1：三个字段没有 description，模型只能猜
# --------------------------------------------------------------------------- #
def test_schema_三个字段都有说明():
    """`approach_hint` / `due_hours` / `subject` 必须有非空 description。

    问题 1 的根因就是这个：`approach_hint` 在本意上是「下次怎么开口」，
    没有说明时模型会拿它存「常忙到很晚，作息偏晚」这类长期特征。
    """
    props = _event_item_schema()
    for name in ("approach_hint", "due_hours", "subject"):
        assert name in props, f"schema 里丢了 {name} 字段"
        desc = props[name].get("description")
        assert isinstance(desc, str) and desc.strip(), f"{name} 缺 description"


def test_approach_hint_的说明写的是下次怎么接近():
    """`approach_hint` 的定义必须是「下次接近方式」，不能退化成「观察到的特征」。"""
    desc = _event_item_schema()["approach_hint"]["description"]
    assert "下次" in desc and "接近" in desc, desc
    assert "特征" in desc, "必须显式说明「不是观察到的特征」，否则语义会再漂移"


def test_due_hours_的说明限定只有_deferred_care_用():
    desc = _event_item_schema()["due_hours"]["description"]
    assert "deferred_care" in desc and "后" in desc, desc


# --------------------------------------------------------------------------- #
# 问题 2 / 问题 3：提示词里的三条边界
# --------------------------------------------------------------------------- #
def test_提示词有_observation_边界(monkeypatch):
    """`observation` 只记「关于人的事实」；主人下的规定/禁令不算事实。"""
    prompt = _captured_prompt(monkeypatch)
    assert "observation" in prompt
    assert "规定" in prompt, "没有把「规定」这类约束挡在 observation 外面"
    assert "不算事实" in prompt, "没有直说规定/禁令「不算事实」"
    # 边界不能只说「规定」，得把同类词都点到，否则模型换个说法又漏
    for word in ("边界", "禁令"):
        assert word in prompt, f"observation 边界缺关键词：{word}"


def test_提示词有_deferred_care_触发词(monkeypatch):
    """`deferred_care` 必须给触发信号，至少 3 个（问题 3 的根因）。"""
    prompt = _captured_prompt(monkeypatch)
    assert "deferred_care" in prompt
    hit = [w for w in DEFER_SIGNALS if w in prompt]
    assert len(hit) >= 3, f"deferred_care 触发词太少（只找到 {hit}），模型会继续抽成 promise"


def test_提示词有_deferred_care_正例(monkeypatch):
    """光给词不够，还得给一个「原话 → kind + due_hours 理由」的正例。"""
    prompt = _captured_prompt(monkeypatch)
    assert "正例" in prompt, "deferred_care 没有正例"
    assert "due_hours=" in prompt, "正例里要落到具体的 due_hours 取值"


def test_提示词写清_deferred_care_与_promise_的边界(monkeypatch):
    """两者都沾时优先 deferred_care，且不重复抽两条。"""
    prompt = _captured_prompt(monkeypatch)
    assert "promise" in prompt
    assert "优先抽 deferred_care" in prompt, "没有写「优先抽 deferred_care」"
    assert "不要重复抽两条" in prompt, "没有写「不要重复抽两条」"


def test_提示词仍然保持简洁(monkeypatch):
    """项目的设计理念是「小而美」：加边界可以，别写成小作文。"""
    prompt = _captured_prompt(monkeypatch)
    before_conversation = prompt.split("[{", 1)[0]  # 去掉拼进去的对话 JSON
    assert len(before_conversation) < 1200, f"抽取提示词过长（{len(before_conversation)} 字）"


# --------------------------------------------------------------------------- #
# 纯代码不变量：到期时间只属于 deferred_care
# --------------------------------------------------------------------------- #
def test_validate_events_非_deferred_care_丢弃_due_hours():
    """`promise` 带 `due_hours` → 不许产出 `due_ts`，也不许把 `due_hours` 带下去。"""
    evs = system._validate_events({"events": [
        {"kind": "promise", "subject": "主人", "evidence": "我明天弄", "due_hours": 48},
    ]})
    assert len(evs) == 1
    assert evs[0]["kind"] == "promise"
    assert "due_ts" not in evs[0], "promise 不该有到期时间"
    assert "due_hours" not in evs[0], "原始 due_hours 不该透传到落库层"


def test_validate_events_deferred_care_保留_due_ts():
    """`deferred_care` 带 `due_hours` → 必须有 `due_ts`，且落在未来 24 小时附近。"""
    now = time.time()
    evs = system._validate_events({"events": [
        {"kind": "deferred_care", "subject": "主人", "evidence": "说迟点再修",
         "due_hours": 24, "approach_hint": "先问她累不累，别提 bug"},
    ]})
    assert len(evs) == 1
    assert "due_ts" in evs[0], "deferred_care 没有 due_ts 就永远不会排班"
    assert now + 23 * 3600 < evs[0]["due_ts"] < now + 25 * 3600
    assert evs[0]["approach_hint"] == "先问她累不累，别提 bug"


# --------------------------------------------------------------------------- #
# 第二刀：模型抽对了 kind 但**漏给 due_hours** —— 兜底 48h，绝不留死数据
# --------------------------------------------------------------------------- #
def test_漏给_due_hours_的_deferred_care_兜底_48h():
    """缺 `due_hours` 也不许丢事件，兜底 `DEFER_DEFAULT_HOURS`（48h）。

    不兜底的话这条会变成 `status=deferred` + `due_ts` NULL + 不排班 + 永不关单
    的死记录。事件本身是真的（她确实说过「过两天再看」），只是模型没给时间。
    """
    now = time.time()
    evs = system._validate_events({"events": [
        {"kind": "deferred_care", "subject": "主人", "evidence": "说过两天再看"},
    ]})
    assert len(evs) == 1, "兜底不等于丢事件"
    assert system.DEFER_DEFAULT_HOURS == 48.0
    assert "due_ts" in evs[0]
    assert now + 47 * 3600 < evs[0]["due_ts"] < now + 49 * 3600


def test_漏给_due_hours_端到端_要排班不留死数据():
    """端到端钉「不留死数据」这个不变量：校验层兜底 → 落库 deferred → 排一条班。"""
    import sqlite3

    import gateway

    # 模型只给了 kind，没给 due_hours（就是生产上抽对 kind 却漏时间的情形）
    evs = system._validate_events({"events": [
        {"kind": "deferred_care", "subject": "主人", "evidence": "说迟点再修 bug"},
    ]})
    assert system._save_events(evs, "pet") == 1

    with sqlite3.connect(gateway.DB_PATH) as c:
        status, due_ts = c.execute(
            "select status, due_ts from events where kind='deferred_care'"
        ).fetchone()
        assert status == "deferred"
        assert due_ts is not None and due_ts > time.time(), "due_ts 空 = 死数据"
        assert c.execute("select count(*) from schedules").fetchone()[0] == 1, \
            "没排班 = 永远不会被回头看一眼"


def test_漏给_due_hours_群渠道仍不排班():
    """反向断言：群渠道「只记事实不排班」的限制不能被兜底逻辑弄坏。"""
    import sqlite3

    import gateway

    evs = system._validate_events({"events": [
        {"kind": "deferred_care", "subject": "群里某人", "evidence": "说过两天再看"},
    ]})
    assert system._save_events(evs, "qq_group") == 1

    with sqlite3.connect(gateway.DB_PATH) as c:
        status, due_ts = c.execute(
            "select status, due_ts from events where kind='deferred_care'"
        ).fetchone()
        assert status == "closed", "群渠道不排班 → 直接 closed（不是 deferred）"
        assert due_ts is None, "不排班就不该带未来到期时间"
        assert c.execute("select count(*) from schedules").fetchone()[0] == 0
