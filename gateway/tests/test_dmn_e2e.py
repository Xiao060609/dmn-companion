"""DMN 五阶段的端到端验收：turns → events → schedules → inner_os → 触发重判。

和 `test_dmn_channels.py`（工程师的自测）分工：
- 那边测「多渠道路由」的单元行为；
- 这里走**整条链路**：真库（临时库）+ 真函数，只把模型调用换成计数桩，
  逐条对应 `plan\小鱼-总计划.md` 第 2.2 节的验收标准，外加渠道隔离红线、
  模型成本、冷启动时序。

为什么这些必须在「一条链」上测：单点都对、串起来错，是这个项目已经发生过的事
（events 抽出来了但渠道标错、内心OS 投递了但群聊和私聊串味、到点任务带了别的
渠道的上下文）。所以这里不 mock 中间层，只 mock 模型。

数据隔离由 `conftest.py` 的 autouse fixture 兜住（DB_PATH / DATA_DIR / inner_os
全部指向 tmp），本文件不碰生产数据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import migrate_channel  # noqa: E402
import role_speak  # noqa: E402
import system  # noqa: E402

PET = migrate_channel.CHANNEL_PET
PRIVATE = migrate_channel.CHANNEL_QQ_PRIVATE
GROUP_A = "qq_group:示例群"
GROUP_B = "qq_group:王家"


# --------------------------------------------------------------------------- #
# 基础设施：计数桩 + 造数
# --------------------------------------------------------------------------- #
def _tool(name: str, args: dict) -> list[dict]:
    return [{"id": "call_1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]


class ModelStub:
    """`system.call_system` 的替身：按被请求的工具名分派，并记下每次调用。

    默认什么都不给（`(None, {}, [])`），需要产出的用例自己填 `events` / `insights`。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.events: list[dict] = []
        self.insights: list[dict] = []
        self.profile: dict = {}
        self.broken: set[str] = set()   # 这些渠道的「蒸馏」调用直接抛异常

    def __call__(self, msgs, tools=None, tool_choice=None, max_tokens=None, **kw):
        names = [((t or {}).get("function") or {}).get("name") for t in (tools or [])]
        self.calls.append(names)
        blob = json.dumps(msgs, ensure_ascii=False)
        if "system_extract_events" in names:
            if not self.events:
                return None, {}, []
            return None, {}, _tool("system_extract_events", {"events": self.events})
        if "system_consolidate" in names:
            for channel in self.broken:
                if channel in blob:
                    raise RuntimeError("模拟该渠道蒸馏炸了")
            if not self.insights:
                return None, {}, []
            return None, {}, _tool("system_consolidate", {"insights": self.insights})
        return json.dumps(self.profile, ensure_ascii=False), {}, None

    # -- 统计口径 ---------------------------------------------------------- #
    def n_extract(self) -> int:
        return sum(1 for c in self.calls if "system_extract_events" in c)

    def n_distill(self) -> int:
        return sum(1 for c in self.calls if "system_consolidate" in c)


@pytest.fixture()
def env(monkeypatch):
    """干净库 + 计数桩。所有模型调用都走桩，绝不发真请求。"""
    gateway.init_db()
    system.clear_inner_os()
    stub = ModelStub()
    monkeypatch.setattr(system, "call_system", stub)
    return stub


def add_turn(channel: str, role: str, content: str, ts: float | None = None) -> int:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        cur = conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values(?,?,?,'chat',?,?)",
            (gateway.SHARED_SESSION, role, content, time.time() if ts is None else ts, channel),
        )
        conn.commit()
        return int(cur.lastrowid)


def add_pet_turns(n: int = 10, prefix: str = "今天特别累") -> None:
    for i in range(n // 2):
        add_turn(PET, "user", f"主人: {prefix}（{i}）")
        add_turn(PET, "assistant", f"辛苦啦（{i}）")


def add_group_turns(channel: str, name: str, n: int = 10, prefix: str = "说话") -> None:
    for i in range(n // 2):
        add_turn(channel, "user", f"【群聊·{name}：10000002】某人（路人）： {prefix}{i}")
        add_turn(channel, "assistant", f"嗯嗯{i}")


def q(sql: str, args=()) -> list[tuple]:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        return conn.execute(sql, args).fetchall()


def audit_records() -> list[dict]:
    out = []
    try:
        with open(system.SYSTEM_AUDIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# A1 阶段 1：事件表 —— turns → events
# --------------------------------------------------------------------------- #
class TestStage1Events:
    def test_巩固一轮产出事件_evidence非空_渠道复用原值(self, env):
        add_pet_turns(10)
        # 用 promise 而不是 emotion：2026-11-03 起 events 只当状态机用，
        # emotion/observation 不再落库（长期事实归插件的 livingmemory）。
        # 本用例验的是"落库那部分的形状"，所以挑一个状态机类型。
        env.events = [
            {"kind": "promise", "subject": "主人", "evidence": "今天特别累，不想说话"},
            {"kind": "deferred_care", "subject": "主人", "evidence": "说别管我",
             "due_hours": 48, "approach_hint": "轻一点"},
        ]
        env.insights = [{"text": "主人这阵子很累", "emotion": "sad", "confidence": 0.8}]

        assert system._consolidate_channel(system.load_state(), PET) == "ok"

        rows = q("select kind,payload,status,due_ts,channel from events order by id")
        assert [r[0] for r in rows] == ["promise", "deferred_care"]
        assert all(json.loads(r[1]).get("evidence") for r in rows), "payload.evidence 不能为空"
        assert {r[4] for r in rows} == {PET}, "events.channel 必须复用 turns 的原值"
        deferred = [r for r in rows if r[0] == "deferred_care"][0]
        assert deferred[2] == "deferred" and deferred[3], "deferred_care 要带 due_ts 并置 deferred"
        assert len(q("select id from schedules")) == 1, "deferred_care 应顺带排一条定时任务"

    def test_抽取失败时原文完整_水位不前进_下一轮还能重抽(self, env):
        """`call_system` 返回 (None, {}, []) —— 模型什么都没给。

        这时必须：① turns 一行不少；② 水位不动（否则这段对话永远读不到了）；
        ③ 模型恢复后，同一段对话还能被重新抽一次。
        """
        add_pet_turns(10)
        before = len(q("select id from turns"))
        assert system._consolidate_channel(system.load_state(), PET) == "skip"

        assert len(q("select id from turns")) == before == 10, "抽取失败不许丢原文"
        assert q("select id from events") == [], "没内容就不许写假事件"
        assert "pet" not in (system.load_state().get("consolidate_wm") or {}), \
            "抽取失败时水位不能前进（否则这段对话永久丢失）"
        assert env.n_extract() == 1, "失败那轮应该真的问过模型"

        env.events = [{"kind": "promise", "subject": "主人", "evidence": "今天特别累"}]
        env.insights = [{"text": "累", "emotion": "sad", "confidence": 0.7}]
        assert system._consolidate_channel(system.load_state(), PET) == "ok"
        assert len(q("select id from events")) == 1, "水位没动，所以下一轮能重抽到同一段对话"


# --------------------------------------------------------------------------- #
# A2 阶段 2：内心OS 状态机
# --------------------------------------------------------------------------- #
class TestStage2InnerOs:
    def test_消费后不消失且状态变delivered(self, env):
        system.push_inner_os([{"type": "insight", "text": "观点A", "channel": PET},
                              {"type": "insight", "text": "观点B", "channel": PET}])
        got = system.consume_inner_os(PET)

        assert len(got) == 2
        items = system._read_inner_os()
        assert len(items) == 2, "读后清空是老行为，现在必须留着"
        assert [i["state"] for i in items] == ["delivered", "delivered"]
        assert system.consume_inner_os(PET) == [], "已投递的不能重复投"

    def test_过期自动转expired且不投递(self, env):
        old = time.time() - 9 * 86400
        system._write_inner_os([{
            "id": "os_old", "type": "insight", "text": "过期观点", "channel": "",
            "state": "pending", "created_ts": old, "expires_ts": time.time() - 86400,
        }])
        assert system.consume_inner_os(PET) == [], "过期项不该被投递"
        assert system._read_inner_os()[0]["state"] == "expired"

    def test_封顶优先踢已投递的_不踢pending(self, env):
        many = ([{"id": f"p{i}", "type": "insight", "text": f"待办{i}", "channel": "",
                  "state": "pending", "created_ts": time.time() - 1000 + i,
                  "expires_ts": time.time() + 86400} for i in range(15)]
                + [{"id": f"d{i}", "type": "insight", "text": f"已投递{i}", "channel": "",
                    "state": "delivered", "created_ts": time.time() - 5000 + i,
                    "expires_ts": time.time() + 86400} for i in range(15)])
        system._write_inner_os(many)
        system.push_inner_os([{"type": "insight", "text": "触发一次封顶", "channel": ""}])

        kept = system._read_inner_os()
        assert len(kept) <= system.INNER_OS_MAX
        assert sum(1 for i in kept if i["state"] == "pending") >= 15, "pending 一条都不该被挤掉"

    def test_limit限制一轮注入几条_剩下的保持pending(self, env):
        """**2026-11-03 22:40 实测加的**。

        梳理期一次能产 12 条，队列堆到 15 条 —— 全塞进每轮 prompt 会变成
        一段几百字的「自我提醒墙」，把真正要紧的那条稀释掉。
        分层取：**主人的 pending 优先**，其余留到下一轮（不是丢弃）。
        """
        system.push_inner_os([
            {"type": "insight", "text": f"想法{i}", "channel": PET} for i in range(10)
        ])
        got = system.consume_inner_os(PET, limit=3)
        assert len(got) == 3
        states = [i["state"] for i in system._read_inner_os()]
        assert states.count("delivered") == 3
        assert states.count("pending") == 7, "没取走的必须还在队列里，不能吞掉"

        # 下一轮还能继续取
        got2 = system.consume_inner_os(PET, limit=3)
        assert len(got2) == 3

    def test_limit为0时不限条数(self, env):
        system.push_inner_os([
            {"type": "insight", "text": f"想法{i}", "channel": PET} for i in range(5)
        ])
        assert len(system.consume_inner_os(PET)) == 5

    def test_限额时主人条目优先于群渠道(self, env):
        """群聊条目再新也不能挤掉主人的 —— 只有主人会消费，群条目是死数据。"""
        system.push_inner_os([
            {"type": "insight", "text": "群里的旧想法", "channel": GROUP_A},
            {"type": "insight", "text": "主人的想法", "channel": PET},
        ])
        # 手工把群那条的 created_ts 调到最新，验证排序不是纯按时间
        items = system._read_inner_os()
        for it in items:
            if it["text"] == "群里的旧想法":
                it["created_ts"] = time.time() + 100
        system._write_inner_os(items)

        got = system.consume_inner_os(PET, limit=1)
        assert [i["text"] for i in got] == ["主人的想法"]


# --------------------------------------------------------------------------- #
# A3 阶段 3：定时任务 → 内心OS（触发重判）
# --------------------------------------------------------------------------- #
class TestStage3Schedules:
    def _event(self, channel: str) -> int:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            cur = conn.execute(
                "insert into events(ts,kind,subject,payload,status,due_ts,attempts,channel)"
                " values(?,?,?,?,'deferred',?,0,?)",
                (time.time() - 7200, "deferred_care", "主人", '{"evidence": "说别管"}',
                 time.time() - 60, channel),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _schedule(self, event_id: int, due: float, note: str = "两天后再看") -> None:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            conn.execute("insert into schedules(due_ts,kind,event_id,note,fired)"
                         " values(?,?,?,?,0)", (due, "recheck_care", event_id, note))
            conn.commit()

    def test_到点任务被捞起并带本渠道当前状态(self, env):
        """触发时带的必须是**这条任务所属渠道**的对话，不能拿别的渠道的话当上下文。

        注意：群渠道的延后关心已被产品口径砍掉（群里没有投递通道，见下面
        `test_群渠道到点任务被跳过…`），所以「带本渠道上下文」这条只能在
        主人渠道上验；反向仍然要验——群聊的对话不能混进主人的牵挂里。
        """
        add_pet_turns(4, prefix="桌宠这边说的话")
        add_group_turns(GROUP_A, "示例群", 4, prefix="群里说的话")
        event_id = self._event(PET)
        self._schedule(event_id, time.time() - 60, "主人之前说别管")

        assert system._fire_due_schedules() == 1

        items = system._read_inner_os()
        assert len(items) == 1 and items[0]["state"] == "pending"
        ctx = items[0]["context"]
        assert "minutes_since_last_turn" in ctx
        texts = json.dumps(ctx["recent_chat"], ensure_ascii=False)
        assert "桌宠这边说的话" in texts, "上下文应该来自任务所属渠道"
        assert "群里说的话" not in texts, "群聊的对话不能混进主人的牵挂里"
        assert items[0]["channel"] == "owner", "主人渠道的内心OS 走 owner 归一"
        assert q("select fired from schedules where event_id=?", (event_id,))[0][0] == 1
        assert system._fire_due_schedules() == 0, "已 fired 的不许重复触发"
        assert env.calls == [], "触发是纯代码，一次模型都不该调"

    def test_未到点不触发且零模型调用(self, env):
        add_pet_turns(4)
        event_id = self._event(PET)
        self._schedule(event_id, time.time() + 48 * 3600)

        assert system._fire_due_schedules() == 0
        assert system._read_inner_os() == []
        assert env.calls == [], "没到点的任务不能产生任何模型调用"

    def test_群渠道到点任务被跳过_不产生死数据(self, env):
        """群里没有投递通道（`role_speak` 只推桌宠 /api/say），所以群渠道的延后关心
        不排班；历史数据里残留的也要在触发时跳过并标记 fired，否则会变成
        永远 pending、7 天后过期的死数据。
        """
        add_group_turns(GROUP_A, "示例群", 4, prefix="群里说的话")
        event_id = self._event(GROUP_A)
        self._schedule(event_id, time.time() - 60, "群里那位说别管他")

        assert system._fire_due_schedules() == 0, "跳过不算触发"
        assert system._read_inner_os() == [], "不许为群渠道写内心OS（没人读）"
        assert q("select fired from schedules where event_id=?", (event_id,))[0][0] == 1, \
            "跳过的排班也要标记 fired，否则每次心跳都重扫一遍"
        skipped = [a for a in audit_records() if a.get("event") == "schedules_skipped"]
        assert skipped and skipped[0].get("reason") == "group_no_delivery", skipped[:1]
        assert env.calls == [], "跳过不调模型"

    def test_群渠道的deferred_care只记事实不排班(self, env):
        """源头上就不排班：事实照记（以后能从 events 查到），但不产生 schedule。"""
        add_group_turns(GROUP_A, "示例群", 10, prefix="说别管他")
        env.events = [{"kind": "deferred_care", "subject": "某人",
                       "evidence": "说别管他", "due_hours": 48}]
        env.insights = []

        assert system._consolidate_channel(system.load_state(), GROUP_A) == "ok"

        rows = q("select payload, status, due_ts from events where kind='deferred_care'")
        assert rows, "事实必须照记"
        payload = json.loads(rows[0][0])
        assert payload.get("skip_reason") == "group_no_delivery"
        assert rows[0][1] == "closed", "不排班就没有后续动作，状态该是 closed 而不是 deferred"
        assert rows[0][2] is None, "不排班就不该有到期时间：closed + 未来 due_ts 会让人以为欠着事"
        assert q("select id from schedules") == [], "群渠道不许排班"
        skipped = [a for a in audit_records() if a.get("event") == "deferred_care_skipped"]
        assert skipped and skipped[0].get("reason") == "group_no_delivery", skipped[:1]

    def test_主人渠道的deferred_care正常排班(self, env):
        """对照：主人渠道的延后关心必须照旧排班（别把群渠道的限制误伤到主人）。"""
        add_pet_turns(10, prefix="说别管我")
        env.events = [{"kind": "deferred_care", "subject": "主人",
                       "evidence": "说别管我", "due_hours": 48}]
        env.insights = [{"text": "观点", "emotion": "calm", "confidence": 0.6}]

        assert system._consolidate_channel(system.load_state(), PET) == "ok"

        assert len(q("select id from schedules")) == 1, "主人渠道的延后关心不能一起被砍"
        payload, status, due_ts = q(
            "select payload, status, due_ts from events where kind='deferred_care'")[0]
        assert "skip_reason" not in json.loads(payload)
        assert status == "deferred" and due_ts, "主人渠道要照旧是 deferred + 有到期时间"


# --------------------------------------------------------------------------- #
# A4 阶段 4：安慰三态
# --------------------------------------------------------------------------- #
class TestStage4CareStates:
    def _care(self, ts: float, text: str = "要不要陪你聊聊", channel: str = PET) -> None:
        """直接落一条 care_given（时间戳可控，避免被测函数内部取 now 导致顺序不可控）。"""
        with sqlite3.connect(gateway.DB_PATH) as conn:
            conn.execute(
                "insert into events(ts,kind,subject,payload,status,due_ts,attempts,channel)"
                " values(?,?,?,?,'open',NULL,0,?)",
                (ts, "care_given", "",
                 json.dumps({"text": text, "emotion": "care"}, ensure_ascii=False), channel),
            )
            conn.commit()

    def test_三态可分别查到且提示不同(self, env):
        base = time.time() - 1000      # 放在过去，后续对话用显式的「之后」时间戳
        self._care(base, "要不要陪你聊聊")
        assert q("select status from events where kind='care_given'") == [("open",)]
        assert system._mark_care_outcomes() == 0, "没回应就保持 open"

        add_turn(PET, "user", "主人: 别管我，烦着呢", base + 1)
        system._mark_care_outcomes()
        assert q("select status from events where kind='care_given'") == [("rejected",)]
        hint_rejected = system.recent_care_hint()
        assert "拒绝" in hint_rejected and "别" in hint_rejected, "被拒后要给出具体约束，不只是改时间"

        self._care(base + 2, "今天想跟你说句话")
        add_turn(PET, "user", "主人: 谢谢你呀", base + 3)
        system._mark_care_outcomes()
        statuses = [r[0] for r in q("select status from events where kind='care_given' order by id")]
        assert statuses == ["rejected", "responded"], "三种结果必须各自可查"
        hint_responded = system.recent_care_hint()
        assert hint_responded != hint_rejected
        assert "回应" in hint_responded and "拒绝" not in hint_responded

    def test_record_care_given写入open且渠道恒为桌宠(self, env):
        system.record_care_given({"text": "在吗", "emotion": "calm"})
        assert q("select status,channel from events where kind='care_given'") == [("open", PET)]
        system.record_care_given({"text": "   "})   # 空台词不记
        assert len(q("select id from events where kind='care_given'")) == 1


# --------------------------------------------------------------------------- #
# A5 阶段 5：人物建模
# --------------------------------------------------------------------------- #
class TestStage5People:
    def _prompt(self, monkeypatch) -> str:
        """跑一次 role_speak.maybe_speak，把拼好的 prompt 抓回来。"""
        captured = {}
        monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
        monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})

        def fake_call(msgs, use_tools=False):
            captured["msgs"] = msgs
            return "[不说]", {}, None

        monkeypatch.setattr(gateway, "call_model", fake_call)
        monkeypatch.setattr(role_speak, "deliver_to_pet", lambda r: True)
        role_speak.maybe_speak({"now": "2026-11-02 22:00"}, system._default_state())
        return captured["msgs"][1]["content"]

    def test_群聊画像不进桌宠prompt_主人画像进(self, env, monkeypatch):
        system._write_people_profile(GROUP_A, {
            "channel": GROUP_A, "updated_ts": time.time(),
            "profile": {"称呼": "避难所里的阿明"}})
        system._write_people_profile(PET, {
            "channel": PET, "updated_ts": time.time(),
            "profile": {"称呼": "主人"}})

        prompt = self._prompt(monkeypatch)
        assert "主人" in prompt, "当前场合（桌宠）的画像应该进 prompt"
        assert "阿明" not in prompt, "别处的群友画像绝不能漏进桌宠 prompt"

    def test_主人画像跨pet与主人私聊(self, env):
        """主人私聊的对话要能更新**同一份** owner 画像（跨场合），而不是另起一份。"""
        assert system._people_channel(PRIVATE) == PET == system._people_channel(PET)
        assert system._people_key(PET) == "owner"

        for i in range(5):
            add_turn(PRIVATE, "user", f"【私聊：10000001】示例用户（主人）： 我在加班（{i}）")
            add_turn(PRIVATE, "assistant", f"辛苦啦（{i}）")
        env.profile = {"称呼": "主人", "作息": "常熬夜"}
        env.insights = [{"text": "主人最近在加班", "emotion": "calm", "confidence": 0.6}]

        assert system._consolidate_channel(system.load_state(), PRIVATE) == "ok"
        owner = system._read_people_profile(PET)
        assert owner.get("profile", {}).get("称呼") == "主人", "主人私聊应该并进 pet 那份画像"
        assert owner.get("updated_ts"), "画像要带 updated_ts"

    def test_陈旧画像带降权提示_新鲜画像不带(self, env, monkeypatch):
        system._write_people_profile(PET, {
            "channel": PET, "updated_ts": time.time() - 30 * 86400,
            "profile": {"称呼": "主人"}})
        stale = self._prompt(monkeypatch)
        assert "天前的印象" in stale and "不准" in stale

        system._write_people_profile(PET, {
            "channel": PET, "updated_ts": time.time(), "profile": {"称呼": "主人"}})
        assert "天前的印象" not in self._prompt(monkeypatch), "新鲜画像不该被降权"

    def test_people_channels支持群通配_来新群不用改配置(self, env, monkeypatch):
        """`people_channels` 里写 `qq_group:*` 就该覆盖所有群，且各群画像是分开的。"""
        monkeypatch.setitem(system.SYSTEM, "people_channels", [PET, "qq_group:*"])
        env.profile = {"称呼": "群友"}
        add_group_turns(GROUP_A, "示例群", 10)
        add_group_turns(GROUP_B, "王家", 10)

        system._consolidate_channel(system.load_state(), GROUP_A)
        system._consolidate_channel(system.load_state(), GROUP_B)

        # 2026-11-04 改（P1-10）：文件名规则换成「可读前缀__哈希」。
        # 断言改成「前缀对得上 + 两个群是两个不同文件」，语义比原来更准：
        # 原来只验"两个名字都在"，验不出撞名（撞名时两个名字会变成同一个）。
        names = set(os.listdir(system.PEOPLE_DIR))
        a_key = system._people_key(GROUP_A) + ".json"
        b_key = system._people_key(GROUP_B) + ".json"
        assert a_key in names and b_key in names, names
        assert a_key != b_key, "两个群不能落到同一个文件"
        assert a_key.startswith("qq_group_示例群__"), a_key
        assert b_key.startswith("qq_group_王家__"), b_key

    def test_people_channels没配就不建模(self, env, monkeypatch):
        """默认只配了 pet —— 群画像不该被偷偷建出来（省模型调用，也少一份隐私面）。"""
        monkeypatch.setitem(system.SYSTEM, "people_channels", [PET])
        env.profile = {"称呼": "群友"}
        add_group_turns(GROUP_A, "示例群", 10)

        system._consolidate_channel(system.load_state(), GROUP_A)
        # P1-10：文件名规则改了，用 `_people_key()` 算，别硬编码旧格式
        assert not os.path.isfile(
            os.path.join(system.PEOPLE_DIR, system._people_key(GROUP_A) + ".json")
        )

    def test_people_enabled只认精确值和前缀通配(self, env):
        """裸 `*` 必须不生效：通配开太宽等于把渠道隔离开了个洞。"""
        assert system._people_enabled("qq_group:王家", [PET, "qq_group:*"]) is True
        assert system._people_enabled("qq_group:王家", [PET]) is False
        assert system._people_enabled("qq_group:王家", ["*"]) is False, "裸星不能开一切"
        assert system._people_enabled(PET, ["qq_group:*"]) is False, "前缀通配不许跨族"
        assert system._people_enabled(PRIVATE, ["qq_group:*"]) is False, "qq_private 不在群族里"
        assert system._people_enabled("", [PET]) is False, "空渠道不建模"
        # 前缀匹配的边界：`qq_group:`（不带群名）也能命中前缀通配。
        # 线上出现不了——`migrate_channel.channel_key` 取不到群名时给的是 `qq_group`（不带冒号）。
        assert system._people_enabled("qq_group:", ["qq_group:*"]) is True


# --------------------------------------------------------------------------- #
# B 隔离红线（新的安全边界）
# --------------------------------------------------------------------------- #
class TestChannelIsolation:
    def test_群聊的内心OS取不到别的群也取不到桌宠(self, env):
        system.push_inner_os([{"type": "insight", "text": "避难所里的秘密", "channel": GROUP_A}])

        assert system.consume_inner_os(GROUP_B) == [], "别的群不能看到"
        assert system.consume_inner_os(PET) == [], "桌宠不能看到"
        assert system.consume_inner_os(PRIVATE) == [], "主人私聊也不能看到（那是群里的隐私）"
        assert system._read_inner_os()[0]["state"] == "pending", "没被取走就该还在队列里"

    def test_被私聊消费后群聊那条还在且本群仍能取到(self, env):
        """反向验证：隔离不能变成「谁先读谁吃掉」。"""
        system.push_inner_os([
            {"type": "insight", "text": "主人私聊的想法", "channel": PRIVATE},
            {"type": "insight", "text": "王家群里的想法", "channel": GROUP_B},
        ])

        got = system.consume_inner_os(PRIVATE)
        assert [i["text"] for i in got] == ["主人私聊的想法"], "私聊只该拿到自己那条（owner 归一）"

        remaining = {i["text"]: i["state"] for i in system._read_inner_os()}
        assert remaining["王家群里的想法"] == "pending", "被别处消费过，群聊那条必须还在"

        got2 = system.consume_inner_os(GROUP_B)
        assert [i["text"] for i in got2] == ["王家群里的想法"], "本群仍能正常取到"

    def test_events存原始群名不截断不折成pet(self, env):
        add_group_turns(GROUP_A, "示例群", 10)
        env.events = [{"kind": "promise", "subject": "", "evidence": "群里说话"}]
        env.insights = [{"text": "群里的观点", "emotion": "calm", "confidence": 0.5}]

        assert system._consolidate_channel(system.load_state(), GROUP_A) == "ok"
        channels = {r[0] for r in q("select distinct channel from events")}
        assert channels == {GROUP_A}, f"events.channel 必须是原始全名，实际 {channels}"
        assert "qq_group" not in channels and PET not in channels

    def test_陌生人私聊与插件注入一条都不进抽取(self, env):
        # 陌生人私聊 + 插件注入 + 助手自言自语
        add_turn(PRIVATE, "user", "【私聊：10000002】张三（路人）： 你好呀")
        add_turn(PRIVATE, "assistant", "你好")
        add_turn(PRIVATE, "user", "[系统任务：主动对话] 说点什么")
        raw = system._read_channel_turns(PRIVATE, 0, 200)
        assert len(raw) == 3
        assert system._route_turns(PRIVATE, raw) == [], "判不出主体就必须 fail-closed"

        env.events = [{"kind": "promise", "subject": "", "evidence": "不该出现"}]
        system._consolidate_channel(system.load_state(), PRIVATE)
        assert q("select id from events") == [], "陌生人的话不许变成事件"
        assert env.n_extract() == 0, "没主可判时连模型都不该问"

    def test_无群名群聊与qq_unknown不进巩固渠道(self, env):
        add_turn("qq_group", "user", "没有群名的群消息")
        add_turn("qq_unknown", "user", "来源不明")
        add_turn("internal", "user", "内部注入")
        add_group_turns(GROUP_A, "示例群", 4)

        channels = system._consolidate_channels()
        assert channels == [PET, GROUP_A], f"只允许 pet / 主人私聊 / 有群名的群聊，实际 {channels}"


# --------------------------------------------------------------------------- #
# C 成本与冷启动
# --------------------------------------------------------------------------- #
class TestCostAndBoot:
    def test_渠道无增量时零模型调用(self, env):
        assert system._consolidate_channel(system.load_state(), PET) == "skip"
        assert env.calls == [], "没增量就不许调模型"

    def test_增量不够门槛时也不调模型(self, env):
        add_pet_turns(2)   # 低于 consolidate_min_new_turns(8)
        assert system._consolidate_channel(system.load_state(), PET) == "skip"
        assert env.calls == []

    def test_冷启动只等boot_delay而不是整个周期(self, monkeypatch):
        """以前是「先睡一整个 consolidate_s 再干活」，86400 秒意味着首轮永远等不到。"""
        boot = float(system.SYSTEM.get("consolidate_boot_delay_s", 90))
        interval = float(system.SYSTEM.get("consolidate_s", 3600))
        sleeps, rounds = [], []

        class _Stop(Exception):
            pass

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise _Stop()

        monkeypatch.setattr(system.time, "sleep", fake_sleep)
        monkeypatch.setattr(system, "consolidate", lambda: rounds.append(1))
        with pytest.raises(_Stop):
            system._consolidate_loop()

        assert sleeps[0] == pytest.approx(boot), f"首轮该等 {boot}s，实际 {sleeps[0]}"
        assert sleeps[0] < interval, "首轮绝不能等一整个周期"
        assert len(rounds) == 2, "首轮 sleep 之后就该干活，之后按周期跑"
        assert interval * 0.9 <= sleeps[1] <= interval * 1.1, "后续才是周期（含 ±10% 抖动）"

    def test_单渠道异常不影响其它渠道(self, env):
        """一个渠道的模型调用炸了，别的渠道必须照常跑完（水位各自独立）。

        炸点只能落在 owner 渠道上：群渠道从 P0 起不再调 `system_consolidate`
        （只抽事件 + 建画像，见 `action=events+profile_only`），没有「群蒸馏」可炸。
        所以这里让桌宠的蒸馏炸，验证群渠道仍然完整跑完。
        """
        add_pet_turns(10)
        add_group_turns(GROUP_B, "王家", 10)
        env.events = [{"kind": "promise", "subject": "", "evidence": "摘录"}]
        env.insights = [{"text": "观点", "emotion": "calm", "confidence": 0.6}]
        env.broken = {PET}   # 只在蒸馏桌宠时炸

        system._consolidate_body()

        assert (GROUP_B,) == tuple(r[0] for r in q(
            "select distinct channel from events where channel=?", (GROUP_B,))), "群渠道必须照常跑完"
        assert GROUP_B in (system.load_state().get("consolidate_wm") or {}), "群渠道水位该前进"
        broken_audit = [a for a in audit_records()
                        if a.get("event") == "consolidate" and a.get("reason", "").startswith("exception")]
        assert broken_audit, "炸掉的渠道要留下 exception 审计"

    def test_群渠道炸了不影响主人渠道(self, env, monkeypatch):
        """反向覆盖：「一个渠道炸了别的照常」必须两个方向都测。

        P0 之后群渠道不再调 `system_consolidate`，所以「炸群渠道」只能落在
        读取/路由这一层（`_consolidate_channel` 里对 `_route_turns` 没有 try）。
        原用例只覆盖「炸 owner、验群跑完」，这里补上「炸群、验 owner 跑完」，
        否则这个不变量只被验了一半。
        """
        add_pet_turns(10)
        add_group_turns(GROUP_B, "王家", 10)
        env.events = [{"kind": "promise", "subject": "", "evidence": "摘录"}]
        env.insights = [{"text": "观点", "emotion": "calm", "confidence": 0.6}]

        real_read = system._read_channel_turns

        def flaky_read(channel, since_id, limit):
            if channel == GROUP_B:
                raise RuntimeError("模拟群渠道读取炸了")
            return real_read(channel, since_id, limit)

        monkeypatch.setattr(system, "_read_channel_turns", flaky_read)

        system._consolidate_body()

        assert PET in {r[0] for r in q("select distinct channel from events")}, \
            "群渠道炸了，主人渠道必须照常跑完"
        assert PET in (system.load_state().get("consolidate_wm") or {}), "主人水位该前进"
        broken = [a for a in audit_records() if a.get("channel") == GROUP_B
                  and str(a.get("reason", "")).startswith("exception")]
        assert broken, "炸掉的群渠道要留下 exception 审计"

    def test_群渠道不蒸馏不写内心OS_只抽事件和画像(self, env):
        """P0 边界：群渠道只抽事件 + 建画像，不蒸馏、不写内心OS。

        为什么必须钉：群聊的观点没人消费（内心OS 只注入主人私密空间），写进去
        只会一直 pending、7 天后过期，还要参与封顶淘汰。删掉这段提前返回，回归
        照样全绿——只有这条用例会红。
        """
        add_group_turns(GROUP_A, "示例群", 10)
        env.events = [{"kind": "promise", "subject": "", "evidence": "群里说的话"}]
        env.insights = [{"text": "本不该被写进去的观点", "emotion": "calm", "confidence": 0.6}]

        assert system._consolidate_channel(system.load_state(), GROUP_A) == "ok"

        assert q("select id from events") != [], "事件照抽"
        assert env.n_distill() == 0, "群渠道不许有蒸馏调用（省一次模型，也不产生死数据）"
        assert system._read_inner_os() == [], "群渠道不许写内心OS"
        actions = [a for a in audit_records() if a.get("channel") == GROUP_A]
        assert any(a.get("action") == "events+profile_only" for a in actions), \
            "审计要能看出这里是「有意跳过蒸馏」，否则以后没人敢动这行"


# --------------------------------------------------------------------------- #
# D 当年的遗留风险（P0 已修，这里改成钉住**修好之后**的期望行为）
# --------------------------------------------------------------------------- #
class TestKnownRisks:
    def test_INNER_OS_MAX是全局共享_但主人的pending不会被群聊挤掉(self, env):
        """封顶还是全局 20 条，但**主人的 pending 优先级高于别的渠道**。

        改之前：pending 之间只比 created_ts，旧的先被踢 → 群聊刷出 20+ 条
        pending 时，主人那条更早的 pending 会被挤掉（死数据挤活数据）。
        现在 `_trim_inner_os` 给 owner/全局的 pending 加了一级权重。
        注：P0 之后群渠道已经不再写内心OS，这里是给老数据/未来渠道兜底。
        """
        old_owner = {"id": "owner_old", "type": "care_suggestion", "text": "主人的牵挂",
                     "channel": "owner", "state": "pending",
                     "created_ts": time.time() - 7200, "expires_ts": time.time() + 86400}
        old_global = {"id": "global_old", "type": "insight", "text": "老的内置观点",
                      "channel": "", "state": "pending",
                      "created_ts": time.time() - 7100, "expires_ts": time.time() + 86400}
        groups = [{"id": f"g{i}", "type": "insight", "text": f"群里的观点{i}",
                   "channel": GROUP_A, "state": "pending",
                   "created_ts": time.time() - 60 + i, "expires_ts": time.time() + 86400}
                  for i in range(system.INNER_OS_MAX)]
        system._write_inner_os([old_owner, old_global] + groups)

        system.push_inner_os([{"type": "insight", "text": "压垮骆驼的一条", "channel": GROUP_A}])

        kept = {i["id"] for i in system._read_inner_os()}
        assert "owner_old" in kept, (
            "主人的 pending 不许被别的渠道挤掉（_trim_inner_os 里 owner/全局 pending 加权）"
        )
        assert "global_old" in kept, "无渠道的全局 pending 同样受保护（老数据都归这一类）"
        assert len(kept) == system.INNER_OS_MAX, "封顶仍然是全局 20 条"
        assert any(g["id"] not in kept for g in groups), (
            "被淘汰的必须是群渠道的旧条目，否则「主人保住了」可能只是碰巧"
        )

    def test_recent_care_hint按渠道取_care_given目前只由桌宠产出(self, env):
        """两个事实一起钉住：

        1. `record_care_given()` 写死的 channel 是 pet —— 线上不可能出现群聊的
           care 事件；
        2. `recent_care_hint(channel="pet")` 现在按渠道过滤 —— 将来群聊也走主动
           关心时，群里的拒绝不会串到桌宠身上。
        """
        system.record_care_given({"text": "在吗", "emotion": "calm"})
        assert q("select distinct channel from events where kind='care_given'") == [(PET,)], \
            "关心事件只来自桌宠渠道"

        # 手工构造一条「群聊里的拒绝」：渠道过滤之后，桌宠这边读不到它
        with sqlite3.connect(gateway.DB_PATH) as conn:
            conn.execute(
                "insert into events(ts,kind,subject,payload,status,due_ts,attempts,channel)"
                " values(?,?,?,?,'rejected',NULL,0,?)",
                (time.time(), "care_given", "", '{"text": "群里的关心"}', GROUP_A),
            )
            conn.commit()
        assert system.recent_care_hint() == "", (
            "默认按 pet 取：群聊的拒绝不能影响桌宠语气（跨场合串味）"
        )
        assert "拒绝" in system.recent_care_hint(GROUP_A), "但按群渠道取就能看到自己的那条"

    def test_群渠道根本不会消费内心OS_所以群里的牵挂永远不会被投递(self, env, monkeypatch):
        """机制说明 + 历史数据防线（产品口径已定：群渠道不做延后关心）。

        决定是「群渠道不产内心OS」（不蒸馏、不排班）。这条钉的是**为什么**能这么砍：
        `gateway.build_messages()` 只在 `is_owner()` 时调 `consume_inner_os()`，
        群聊轮次永远不会消费内心OS —— 给群渠道写条目必然变成没人读、只能等
        7 天过期的死数据。

        留着它的价值：① 以后有人「顺手」给群渠道 push 条目时，这里说明没有投递通道；
        ② `_fire_due_schedules` 跳过历史残留群排班的依据，就是这个事实。
        """
        system.push_inner_os([{"type": "care_suggestion", "text": "群里的牵挂",
                               "channel": GROUP_A}])
        seen: list[str] = []
        # limit 是 2026-11-03 22:40 加的参数（一轮最多注入几条），stub 要跟着收。
        monkeypatch.setattr(system, "consume_inner_os",
                            lambda channel="", limit=0: seen.append(channel) or [])
        monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
        monkeypatch.setattr(system, "people_prompt_line", lambda channel: "")

        gateway.build_messages(gateway.SHARED_SESSION, "阿明: 在吗",
                               speaker="阿明", channel="群聊·示例群", local_pet=False)
        assert seen == [], "群聊轮次不会去消费内心OS → 群里那条牵挂没人读"

        gateway.build_messages(gateway.SHARED_SESSION, "主人: 在吗",
                               speaker="主人", channel="", local_pet=True)
        assert seen, "桌宠轮次才会消费"

        assert system._read_inner_os()[0]["state"] == "pending", "群渠道那条永远停在 pending"
