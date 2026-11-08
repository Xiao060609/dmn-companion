"""群渠道内心OS 只写不读 + 挤占主人队列（P0 回归）。

问题（QA 端到端实测）：
1. `gateway.build_messages` 只在 `is_owner()` 分支里 `consume_inner_os()`，
   所以**群渠道产生的 insight 永远没人消费**：一直是 pending，7 天后 expired，
   中途还参与 `INNER_OS_MAX` 的封顶淘汰。
2. 于是群渠道既写死数据，又把主人的容量挤掉。

为什么不能「群里也 consume」：`_item_visible()` 对 `channel=""` 的全局项一律可见，
而 `persona_suggestion` 和历史遗留条目都是全局的——群里一 consume，人格修改建议
和主人私密观点就会进群聊 prompt。红线。产品决策不变：内心OS 只注入主人私密空间。

所以修法是**源头不写**：群渠道只抽事件 + 更新群画像，不蒸馏观点。

覆盖：
1. 只有群增量时：events 有新增、群画像生成、inner_os 一条都没多、没调蒸馏
2. 主人队列有 18 条 pending 时跑一轮群巩固：一条不少
3. owner 渠道行为不变：仍能蒸馏出 insight 并 push（channel=owner）
4. `recent_care_hint("pet")` 读不到别的渠道的 care_given
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

GROUP = "qq_group:示例群"


def _add(channel: str, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        for role, content in rows:
            conn.execute(
                "insert into turns(session, role, content, path, ts, channel) values(?,?,?,?,?,?)",
                (gateway.SHARED_SESSION, role, content, "chat", time.time(), channel),
            )
        conn.commit()


def _q(sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        return list(conn.execute(sql, params))


def _audit_records() -> list[dict]:
    try:
        with open(system.SYSTEM_AUDIT_PATH, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


class _Stub:
    """按被请求的工具名分派，并记下每次调用。"""

    def __init__(self, events: list[dict] | None = None, insights: list[dict] | None = None):
        self.calls: list[list[str]] = []
        self.events = events or []
        self.insights = insights or []
        self.profile = {"称呼": "群友", "在意的事": ["猫"]}

    def __call__(self, msgs, tools=None, tool_choice=None, max_tokens=None, **kw):
        names = [((t or {}).get("function") or {}).get("name") for t in (tools or [])]
        self.calls.append(names)
        if "system_extract_events" in names:
            if not self.events:
                return None, {}, []
            return None, {}, _tool("system_extract_events", {"events": self.events})
        if "system_consolidate" in names:
            if not self.insights:
                return None, {}, []
            return None, {}, _tool("system_consolidate", {"insights": self.insights})
        return json.dumps(self.profile, ensure_ascii=False), {}, None

    def n_distill(self) -> int:
        return sum(1 for c in self.calls if "system_consolidate" in c)

    def n_extract(self) -> int:
        return sum(1 for c in self.calls if "system_extract_events" in c)


def _tool(name: str, args: dict) -> list[dict]:
    return [{"id": "call_1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]


def _group_rows(n: int = 10) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for i in range(n):
        rows.append(("user", f"[群聊·示例群] 黑崎小雪: 群里第{i}句"))
        rows.append(("assistant", "嗯。"))
    return rows


# ---------------------------------------------------------------- 1. 群渠道只做两件事
def test_群渠道只抽事件和画像_不写内心OS(monkeypatch):
    _add(GROUP, _group_rows())
    stub = _Stub(events=[{"kind": "promise", "subject": "", "evidence": "群里聊了猫"}])
    monkeypatch.setattr(system, "call_system", stub)
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)

    system._consolidate_body()

    # 抽了事件
    assert [r[0] for r in _q("select distinct channel from events")] == [GROUP]
    # 建了群画像
    assert system._read_people_profile(GROUP).get("profile", {}).get("称呼") == "群友"
    # 但一条内心OS 都没写
    assert system._read_inner_os() == [], "群渠道不该再往内心OS 写东西（没人消费）"
    # 也没调蒸馏那一次模型
    assert stub.n_distill() == 0, "群渠道跳过 system_consolidate，省掉每群每小时一次调用"
    # 审计要能看出「干活了」，不是静默跳过
    acts = [a for a in _audit_records() if a.get("action") == "events+profile_only"]
    assert acts and acts[0]["channel"] == GROUP
    # 水位照样前进（不然下轮重读同一段）
    assert GROUP in (system.load_state().get("consolidate_wm") or {})


def test_群渠道跑完整轮不报no_channel_ready(monkeypatch):
    """群渠道现在 `_CH_OK` 返回，不该再被记成「没渠道可处理」。"""
    _add(GROUP, _group_rows())
    stub = _Stub(events=[{"kind": "promise", "subject": "", "evidence": "x"}])
    monkeypatch.setattr(system, "call_system", stub)
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    system._consolidate_body()
    assert not [a for a in _audit_records() if a.get("reason") == "no_channel_ready"]


# ---------------------------------------------------------------- 2. 不挤占主人队列
def test_群渠道巩固不会挤掉主人的pending(monkeypatch):
    """QA 实测的那个回归：主人 18 条 pending 跑一轮群巩固后必须一条不少。"""
    owner_items = [
        {
            "id": f"owner_{i}", "type": "insight", "text": f"主人的想法{i}",
            "channel": "owner", "state": "pending",
            "created_ts": time.time() - 7200 + i, "expires_ts": time.time() + 86400,
        }
        for i in range(18)
    ]
    system._write_inner_os(owner_items)
    _add(GROUP, _group_rows())
    stub = _Stub(events=[{"kind": "promise", "subject": "", "evidence": "x"}])
    monkeypatch.setattr(system, "call_system", stub)
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)

    system._consolidate_body()

    kept = {i["id"] for i in system._read_inner_os()}
    assert len(kept) == 18, "主人的 18 条 pending 一条都不能掉"
    assert all(f"owner_{i}" in kept for i in range(18))


def test_封顶时主人pending优先于群渠道pending(monkeypatch):
    """就算真有别的渠道条目（老数据/未来渠道），封顶也不能拿主人的 pending 去腾位置。"""
    system._write_inner_os(
        [{
            "id": "owner_old", "type": "insight", "text": "主人的旧想法",
            "channel": "owner", "state": "pending",
            "created_ts": time.time() - 7200, "expires_ts": time.time() + 86400,
        }]
        + [
            {
                "id": f"g{i}", "type": "insight", "text": f"群里的观点{i}",
                "channel": GROUP, "state": "pending",
                "created_ts": time.time() - 60 + i, "expires_ts": time.time() + 86400,
            }
            for i in range(system.INNER_OS_MAX)
        ]
    )
    system.push_inner_os([{"type": "insight", "text": "压垮骆驼的一条", "channel": GROUP}])

    kept = {i["id"] for i in system._read_inner_os()}
    assert len(kept) == system.INNER_OS_MAX
    assert "owner_old" in kept, "主人的 pending 不许被群渠道的条目挤掉"


# ---------------------------------------------------------------- 3. owner 行为不变
def test_owner渠道仍然蒸馏出观点并带owner渠道(monkeypatch):
    _add("pet", [("user", f"主人: 第{i}句") for i in range(10)])
    stub = _Stub(insights=[{"text": "主人有点累", "emotion": "sad", "confidence": 0.8}])
    monkeypatch.setattr(system, "call_system", stub)
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)

    system._consolidate_body()

    assert stub.n_distill() == 1, "owner 渠道该照常蒸馏"
    items = system._read_inner_os()
    assert [(i["channel"], i["text"]) for i in items] == [("owner", "主人有点累")]
    assert items[0]["state"] == "pending"


# ---------------------------------------------------------------- 4. care 按渠道
def _care(channel: str, status: str, ts: float) -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into events(ts,kind,subject,payload,status,due_ts,attempts,channel)"
            " values(?,?,?,?,?,NULL,0,?)",
            (ts, "care_given", "", json.dumps({"text": "关心"}, ensure_ascii=False), status, channel),
        )
        conn.commit()


def test_关心提示只读本渠道的care事件():
    now = time.time()
    _care(GROUP, "rejected", now)          # 群里的拒绝：更新、更近
    assert system.recent_care_hint("pet") == "", "桌上的她不该被群里的拒绝影响"
    assert "拒绝" in system.recent_care_hint(GROUP)

    _care("pet", "responded", now - 10)
    assert "拒绝" not in system.recent_care_hint("pet")
    assert "回应" in system.recent_care_hint("pet")
