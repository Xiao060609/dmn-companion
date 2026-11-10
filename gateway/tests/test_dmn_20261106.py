"""2026-11-06 DMN 改造的集成回归：心情 / 计划闭环 / system_log。

这些用例测的是**接线**，不是单模块逻辑（单模块在 test_mood.py /
test_plan.py / test_forgetting.py / test_cognition.py 里）：

1. 心情：`mood_updated_ts` 不会因为"读一次"就被推进（衰减不会重复扣）；
   被动回复真的会推动心情；群聊里的话不会动她和主人之间的心情。
2. 计划：10 天沉默期满了才触发、只触发一次、群渠道不触发；
   他的回答被判成成败之后**销毁 + 留痕**；判不出来就**不销毁**。
3. `system_log`：触发/完成都写进流水账；它**进巩固素材**但**不进对话历史**。

🔧 2026-11-06 的修复（详见 `data_dir` / `db` / `frozen_clock` 的注释）：

1. **`db` fixture 的隔离是假的**。原来是
   `monkeypatch.setattr(gateway, "DB_PATH", str(gateway.DB_PATH))` ——
   拿自己赋自己（`str()` 包一层而已），**等于什么都没改**；注释却写着
   「干净库」。现在改成 `data_dir` + `db` 两个 fixture，显式把两个模块的
   **每一个** `*_PATH` / `*_DIR` 常量都指到 `tmp_path`。

   ⚠ 但是：**它并不是本次不稳定失败的成因**。`conftest.py` 的 autouse
   兜底 fixture 早就把路径指走了（实测 5 次运行前后 `gateway/data/` 的
   SHA-256 完全一致、`turns=604 / events=21` 未变），所以"写脏生产库"
   这一半在**全量跑**时不成立。真正需要修的是下面第 2 条 ——
   只有把路径隔离做对之后，才能放心地把那 3 条用例的夹具改成显式时间戳。
   `db` fixture 仍然是坏的（它自称"干净库"，实际是兜底的功劳），
   换掉它是为了**不再依赖兜底**：本文件单跑、或将来兜底被改掉时，
   数据也不会落到生产目录。

2. **真正的不稳定来源：3 条用例依赖「墙钟别走」**。它们把 `mood_updated_ts`
   存成 0（`_default_state()` 的默认值），而 `system._normalize_mood()`
   把 `<= 0` 当成「旧格式待迁移」，**每次 `load_state()` 都重新取一个
   `time.time()`**。于是「同一份状态读两次」拿到两个不同时刻 ——
   只要中间跨过一个 ~1 ms 的时钟刻度就不相等（实测约 4%）。
   心情衰减同理：`-8` 走 3 分钟 ≈ `-7.97`，`mood.decay()` 保留 6 位小数，
   1 ms 就差 1e-5，`assert first == second` 红成 `-7.97 != -7.969999`。
   三条用例（读心情 / 群聊 / 静默）踩的是**同一个坑**，谁先撞上谁红 ——
   这就是"每次失败的用例不同"的来源。修法是 `frozen_clock` + 显式写一个
   真实的、位于过去的 `mood_updated_ts`。

3. 顺手修了两处**断言过松**：`pytest.approx` 不带 `abs` 时走相对容差
   （rel=1e-6），在 1.79e9 量级的时间戳上等于 ±1793 秒 —— 注入
   「读一次就把时间戳推到 now」的坏实现后，那两条断言**依然通过**。
   已改成精确相等（详见行内注释）。

数据隔离现在有两层：`conftest.py` 的 autouse 兜底，以及本文件自己的
`data_dir` fixture。两层不冲突 —— autouse 先执行，本文件的 fixture 后执行，
按 monkeypatch 语义后写的赢。
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
import migrate_channel  # noqa: E402
import mood  # noqa: E402
import plan as plan_rules  # noqa: E402
import system  # noqa: E402

PET = migrate_channel.CHANNEL_PET
PRIVATE = migrate_channel.CHANNEL_QQ_PRIVATE
SYSLOG = migrate_channel.CHANNEL_SYSTEM_LOG
GROUP = "qq_group:示例群"
DAY = 86400.0


# --------------------------------------------------------------------------- #
# 路径隔离
# --------------------------------------------------------------------------- #
#: `gateway` 里所有落盘常量的（属性名, 隔离目录下的文件名）。
#:
#: ⚠ **必须逐个改，只改 `DATA_DIR` 没用**：`gateway.py` 在 import 期就把
#: `DB_PATH = os.path.join(DATA_DIR, "gateway.db")` 算成了**字符串常量**，
#: 之后再也不看 `DATA_DIR`。所以 `monkeypatch DATA_DIR` 对 `init_db()` /
#: `add_turn()` 毫无影响 —— 它们用的是 `gateway.DB_PATH` 这个常量。
_GATEWAY_DATA_FILES = (
    ("DB_PATH", "gateway.db"),
    ("AUDIT_PATH", "audit.jsonl"),
    ("ROLE_PATH", "role_model.json"),
    ("TOKEN_PATH", "gateway.token"),
    ("KEYS_PATH", "keys.json"),
)

#: `system` 里所有落盘常量。同一个道理，而且这里更要命：
#: `system.load_state()` / `save_state()` 直接用**模块级常量**
#: `SYSTEM_STATE_PATH` 拼 `.tmp` 再 `os.replace` —— 它们从不重新读 `DATA_DIR`。
#: 只改 `DATA_DIR` 的话，库里是隔离的、**状态文件仍然写真生产**，
#: 表现为"这条用例明明没碰心情，下一条读到的心情就被改了"。
#:
#: `mood.py` / `plan.py` / `forgetting.py` / `cognition.py` 已确认是纯函数模块
#: （不 `open()`、不 `sqlite3`、不持任何路径常量），所以这里不用管它们。
_SYSTEM_DATA_FILES = (
    ("SYSTEM_STATE_PATH", "system_state.json"),
    ("SYSTEM_MODEL_PATH", "system_model.json"),
    ("SYSTEM_AUDIT_PATH", "system_audit.jsonl"),
    ("INNER_OS_PATH", "inner_os.json"),
    ("NOVELTY_PATH", "novelty_grams.json"),
    ("HEALTH_REPORT_PATH", "health_report.json"),
)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """给本条用例一个干净的 data 目录，并把两个模块的路径常量全部指过去。

    为什么用独立子目录 `dmn_data/` 而不是直接 `tmp_path`：`conftest.py` 的
    autouse 兜底 fixture 已经在 `tmp_path/isolated_data/` 里建过一套 schema，
    分开目录能让"本文件自己隔离"这件事独立成立、也方便排查时一眼看清谁写的。
    """
    d = Path(tmp_path) / "dmn_data"
    d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(gateway, "DATA_DIR", str(d))
    for name, filename in _GATEWAY_DATA_FILES:
        monkeypatch.setattr(gateway, name, str(d / filename))

    monkeypatch.setattr(system, "DATA_DIR", str(d))
    for name, filename in _SYSTEM_DATA_FILES:
        monkeypatch.setattr(system, name, str(d / filename))
    monkeypatch.setattr(system, "PEOPLE_DIR", str(d / "people"))

    # 长期记忆库是 `system.SYSTEM` 里的**配置键**（不是模块常量），
    # `conftest.py` 的兜底已经把值换成临时路径；这里再显式钉一遍，
    # 免得哪天兜底被改掉之后这条线又漏回 `gateway/data/livingmemory.db`。
    monkeypatch.setitem(system.SYSTEM, "long_memory_db_path", str(d / "livingmemory.db"))
    # 进程级单例：不清的话会沿用上一个用例打开的库（跨用例串味）。
    monkeypatch.setattr(system, "_MEMORY_BRIDGE", None, raising=False)
    return d


@pytest.fixture()
def db(data_dir):
    """干净库 + 干净状态 + 干净 inner_os（全部在 `data_dir` 里）。

    🔧 原来的实现是：
        monkeypatch.setattr(gateway, "DB_PATH", str(gateway.DB_PATH))
        gateway.init_db()
    第一行把 `DB_PATH` 赋成它自己（`str()` 只做了类型转换），**什么都没改**。
    它之所以看不出来坏，是因为 `conftest.py` 的 autouse 兜底早就把路径指到
    tmp 了 —— 一旦本文件被单独拷出去跑、或兜底被改掉，`init_db()` 就会去
    真实的 `gateway/data/gateway.db` 上建表并跑 `migrate_channel`。
    现在由 `data_dir` fixture 先改写常量，这里再建库，行为不变但真的隔离。
    """
    gateway.init_db()
    system.clear_inner_os()
    return gateway.DB_PATH


@pytest.fixture()
def frozen_clock(monkeypatch):
    """把「现在」钉死在某一刻，返回该时间戳。

    为什么需要（这是本文件"每次失败的用例不同"的真正原因）
    --------------------------------------------------
    `TestMoodWiring` 里有几条断言是「同一份状态读两次必须相等」：

        before = system.load_state()["mood_updated_ts"]
        gateway._note_reply_mood(...)          # 群聊 / 静默，本来就不该改
        assert system.load_state()["mood_updated_ts"] == before

    这些用例原先把状态存成 `_default_state()`，其中 `mood_updated_ts == 0`。
    而 `system._normalize_mood()` 把 `<= 0` 解释成**旧格式待迁移**，于是
    **每一次 `load_state()` 都重新取一个 `time.time()`** —— 两次读拿到的是
    两个不同时刻，只要中间跨过一个 ~1 ms 的时钟刻度就不相等（实测约 4%）。
    心情衰减同理：`-8` 衰减 3 分钟约等于 `-7.97`，而 `mood.decay()` 保留 6 位，
    1 ms 就差 1e-5，`assert first == second` 会红成 `-7.97 != -7.969999`。

    只冻结 `time.time`、不动 `sleep`：`system` / `gateway` / `mood` import 的
    都是同一个 `time` 模块对象，改一处三处都生效，`monkeypatch` 用完自动还原。

    ⚠ 冻结之后**别把状态的时间戳也写成 `now`**：那样「被误改了」和「没被改」
    会得到同一个值，断言永远绿，等于把用例测废。要写成
    `frozen_clock - 60` 这种**明确的过去某刻** —— 万一真被误改，时间戳会跳到
    `frozen_clock`，断言才抓得到。
    """
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    return now


def q(sql: str, args=()) -> list[tuple]:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        return conn.execute(sql, args).fetchall()


def x(sql: str, args=()) -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(sql, args)
        conn.commit()


def add_turn(channel: str, role: str, content: str, ts: float | None = None) -> int:
    x(
        "insert into turns(session,role,content,path,ts,channel) values(?,?,?,'chat',?,?)",
        (gateway.SHARED_SESSION, role, content, time.time() if ts is None else ts, channel),
    )
    return int(q("select max(id) from turns")[0][0])


def add_plan(event_kind: str = "promise", *, created_days_ago: float = 11,
             channel: str = PET, evidence: str = "给我加升级",
             attempts: int = 0) -> int:
    """造一条计划（events 表里 status=open 的 promise/deferred_care）。"""
    created = time.time() - created_days_ago * DAY
    payload = json.dumps({"evidence": evidence}, ensure_ascii=False)
    x(
        "insert into events(ts, kind, subject, payload, status, due_ts, attempts, channel)"
        " values(?,?,?,?,?,NULL,?,?)",
        (created, event_kind, "主人", payload, "open", attempts, channel),
    )
    return int(q("select max(id) from events")[0][0])


def plan_row(event_id: int) -> tuple:
    return q(
        "select status, attempts, kind, payload from events where id=?", (event_id,)
    )[0]


def syslog_texts() -> list[str]:
    return [r[0] for r in q("select content from turns where channel=? order by id", (SYSLOG,))]


def consolidate_with_verdicts(verdicts, *, insights=None, channel: str = PET):
    """跑一轮**真的** `_consolidate_channel`，让模型回指定的 `plan_verdicts`。

    为什么用真链路而不是直接调 `apply_plan_verdicts()`（2026-11-07 改）
    -----------------------------------------------------------------
    审计 H.1 选的方案 C 是「巩固时让模型判」，所以这条线上有**四段**，
    任何一段断掉都会让计划判不了：

        ① `_pending_plan_candidates()` 取到候选（含"他说了什么"）
        ② `_build_consolidate_messages()` 把它放进**独立的** `pending_plans` 字段
        ③ `_consolidate_tools()` 把 `plan_verdicts` 拼进工具 schema
        ④ 模型回的值被 `apply_plan_verdicts()` 落库

    只测 ④ 的话，①②③ 任何一处退化成"没接上"都测不出来 —— 而这正是
    审计里 `review_candidates()` 断头路的成因（函数写对了，没人调）。
    所以这里用桩替掉**模型**（唯一的不可控边界），其余全走真代码。

    ⚠ **别拿返回值当"成功与否"**：`_consolidate_channel()` 返回 `"skip"`
    有两种完全不同的含义 —— `no_new_turns`（没走到模型）和 `no_insight`
    （走到模型了、只是这轮没有观点要沉淀）。而 `no_insight` 是**最常见**
    的生产结果（实测 `normal` 5 / `no_insight` 2），计划裁决恰恰必须在这种
    情况下照样执行。所以断言要看 `called` / 数据库，不要看返回值。

    Args:
        verdicts: 模型要回的 `plan_verdicts`（原样塞进 tool_call 的 arguments）。
        insights: 同时回的观点；默认空（**故意**：见上）。
        channel: 巩固哪个渠道。默认 pet —— 和主人私聊同属 owner 那一路
            （`_inner_channel` 把两者都归一成 owner），所以两者都会带
            `pending_plans`。要测"群渠道不带"的话直接调
            `_build_consolidate_messages(..., GROUP)`。

    Returns:
        `{"result": _consolidate_channel 的返回值,
          "called": 模型这一轮被调用了没有,
          "tools": 这一轮的工具 schema,
          "msgs": 这一轮的 messages,
          "payload": 第二段 system 里那段 JSON（解析好的）}`
    """
    args: dict = {"insights": insights or []}
    if verdicts is not None:
        args["plan_verdicts"] = verdicts
    seen: dict = {"called": False, "tools": [], "msgs": []}

    def fake_call_system(msgs, *, tool_choice="auto", tools=None, max_tokens=None):
        # 只认 `system_consolidate` 那一次调用。同一轮里前面还有
        # `system_extract_events`（抽事件）和画像调用，它们的 tools 里
        # 没有 system_consolidate —— 别把那些也算成"巩固那次"。
        names = [((t or {}).get("function") or {}).get("name") for t in (tools or [])]
        if "system_consolidate" in names:
            seen["called"] = True
            seen["tools"] = tools or []
            seen["msgs"] = msgs
            return None, {}, [{"id": "1", "function": {
                "name": "system_consolidate",
                "arguments": json.dumps(args, ensure_ascii=False),
            }}]
        return None, {}, []

    gate = pytest.MonkeyPatch()
    try:
        gate.setattr(system, "call_system", fake_call_system)
        # 增量门槛默认 8，测试里造 8 条对话太啰嗦，调到 1。
        # ⚠ 这个门槛是**真实存在的守卫**（`not_enough_new`），所以调它而不是
        #   绕过 `_consolidate_channel()` —— 我们要测的正是"走到模型那一步之后"
        #   的行为，前面的守卫照原样留着。
        gate.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
        st = system.load_state()
        # 钉一个"刚刚巩固过"的时间戳，让「最长等待旁路」不生效 —— 否则
        # 测的就不是门槛而是旁路（见 TestSystemLogConsolidate._fresh_state）。
        st["last_consolidate_ts"] = time.time()
        system.save_state(st)
        result = system._consolidate_channel(system.load_state(), channel)
    finally:
        gate.undo()
    payload: dict = {}
    if seen["msgs"]:
        try:
            payload = json.loads(seen["msgs"][1]["content"])
        except (IndexError, json.JSONDecodeError, TypeError):
            payload = {}
    return {**seen, "result": result, "payload": payload}


def raw_state() -> dict:
    """直接读 `system_state.json` 的原始内容（**不经过** `load_state()`）。

    为什么需要：本文件有几条用例要验「迁移到底有没有落盘」。而 `load_state()`
    自己会在迁移时顺手 `save_state()` 一次 —— 拿它当探针等于让被测者自己
    证明自己没问题。所以这里直接读文件。
    """
    with open(system.SYSTEM_STATE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 一、心情
# --------------------------------------------------------------------------- #
class TestMoodWiring:
    """心情接线。

    ⚠ 写这个类的新用例时请守一条规矩：**别拿 `_default_state()` 直接喂给
    `save_state()`**。它的 `mood_updated_ts` 是 0，而 `system._normalize_mood()`
    把 `<= 0` 当成「旧格式待迁移」，于是**每次 `load_state()` 都重新取一次
    `time.time()`**（迁移结果不会被 `load_state()` 落盘 —— 它只读）。
    结果就是「同一份状态读两次」拿到两个不同时刻，断言偶尔不相等。
    要造状态就显式写一个真实的 `mood_updated_ts`（见下面几条用例）。
    """

    def test_读心情不会推进更新时间(self, db, frozen_clock):
        """最危险的那个坑：`mood_updated_ts` 必须是上次**真正更新**的时间。

        如果读一次就把它推到现在，衰减永远算成 0 分钟 —— 心情会永久停在
        最后一次事件的值上，看起来"有心情"，其实再也不变。

        🔧 为什么要 `frozen_clock`：这条的核心断言是「同样的落盘状态，
        连读两次结果必须一模一样」。而心情衰减是**连续函数**，
        `mood.decay()` 只保留 6 位小数 —— 两次读之间只要真实时间跨过
        一个 ~1 ms 的时钟刻（本机 `time.time()` 实测就是这个粒度），
        `-7.97` 就会变成 `-7.969999`，断言红成 `assert -7.97 == -7.969999`。
        冻结时钟后"两次读"才真的发生在同一时刻，这条用例测的才是
        「读操作不改状态」，而不是「这台机器跑得够快」。
        """
        st = system._default_state()
        st["mood_value"] = -8.0
        st["mood_updated_ts"] = frozen_clock - 3 * 60  # 3 分钟前
        system.save_state(st)

        first = system.mood_state()
        second = system.mood_state()
        assert first["mood_value"] == second["mood_value"]
        # -8 走 3 分钟 → -8 + 0.03
        assert first["mood_value"] == pytest.approx(-7.97, abs=1e-6)
        # 落盘的时间戳没有被读操作改动。
        # 🔧 这里原来写的是 `pytest.approx(st[...])` —— 不带 `abs` 时走的是
        # **相对**容差（默认 rel=1e-6），而时间戳量级是 1.79e9，于是容差
        # 高达 ±1793 秒：读操作把时间戳推进 1 秒、甚至 20 分钟，这条断言
        # 照样是绿的（实测：注入「读一次就推到 now」的坏实现后依然通过）。
        # 这条要钉的正是"一个字节都没动"，所以必须用**精确相等**。
        assert system.load_state()["mood_updated_ts"] == st["mood_updated_ts"]

    def test_记一次事件会落盘并推进时间(self, db):
        system.save_state(system._default_state())
        new = system.record_mood_event("中度高兴", reason="主人刚刚夸我可爱")
        assert new["mood_label"] in ("愉悦", "中度高兴")
        assert new["mood_reason"] == "主人刚刚夸我可爱"
        st = system.load_state()
        assert st["mood_value"] == pytest.approx(new["mood_value"])
        assert st["mood_updated_ts"] > 0

    def test_旧emotion会在读的时候迁移成新档位(self, db):
        """升级到新格式时，旧 `emotion` 枚举要翻一次，只翻一次。"""
        st = system._default_state()
        st["emotion"] = "happy"
        st["mood_value"] = 0.0
        st["mood_label"] = "平静"
        st["mood_updated_ts"] = 0          # 旧格式没有这个字段
        system.save_state(st)
        migrated = system.load_state()
        assert migrated["mood_label"] == "愉悦"
        assert migrated["mood_value"] == pytest.approx(2.0)
        assert migrated["mood_updated_ts"] > 0

    def test_被动回复会推动心情(self, db, frozen_clock):
        """改造前 `emotion` 只有主动说话会写，主人在私聊回一句她立刻失忆。

        🔧 这里显式写真实时间戳（不再用 `_default_state()` 的 0）：
        `0` 会被 `_normalize_mood()` 当成旧格式、每次读都重新打时间戳，
        `before` / `after` 就落在两个不同的"现在"上，比较失去意义。
        """
        st = system._default_state()
        st["mood_updated_ts"] = frozen_clock - 60   # 明确的"一分钟前"，不是"现在"
        system.save_state(st)
        before = system.mood_state()["mood_value"]
        gateway._note_reply_mood(
            {"status": "ok", "text": "好呀", "emotion": "happy"}, owner=True
        )
        after = system.mood_state()["mood_value"]
        assert after > before, f"高兴的话该把心情往上推：{before} → {after}"

    def test_群聊里的话不动她和主人的心情(self, db, frozen_clock):
        """心情是私人的：群聊里她的话是为群友说的。

        🔧 原来这里（以及下面 `test_静默和报错不动心情`）是
        `save_state(_default_state())` 之后比 `load_state()["mood_updated_ts"]`。
        那个字段是 **0**，而 `_normalize_mood()` 对 `<= 0` 走「旧格式迁移：
        取 `time.time()`」——**每次读都重新取**。所以 `before` 和断言里的
        那次读拿到的是两个不同时刻，只要中间跨过一个 ~1 ms 刻度就不相等
        （实测约 4% 概率红）。这就是"每次失败的用例不同"的来源：
        三条用例都在同一个坑上，谁先撞上谁红。

        时间戳写 `frozen_clock - 60`（明确的过去某刻）而不是 `frozen_clock`：
        万一 `_note_reply_mood()` 真被改坏了、把时间戳推到"现在"，
        断言会看到 `frozen_clock != frozen_clock - 60` 而变红 ——
        写成同一个值就永远绿了。
        """
        st = system._default_state()
        st["mood_updated_ts"] = frozen_clock - 60
        system.save_state(st)
        before = system.load_state()["mood_updated_ts"]
        gateway._note_reply_mood(
            {"status": "ok", "text": "哈哈", "emotion": "happy"}, owner=False
        )
        assert system.load_state()["mood_updated_ts"] == before

    def test_静默和报错不动心情(self, db, frozen_clock):
        """静默观望 / 模型报错都不是"她说了句话"，不该算一次心情事件。

        🔧 同上：`_default_state()["mood_updated_ts"]` 是 0，会被当成旧格式
        每次读重新打时间戳；先写一个真实时间戳（`frozen_clock - 60`，
        与"现在"不同，误改才抓得到），这条才测得到
        「`_note_reply_mood()` 什么都没写」。
        """
        st = system._default_state()
        st["mood_updated_ts"] = frozen_clock - 60
        system.save_state(st)
        before = system.load_state()["mood_updated_ts"]
        gateway._note_reply_mood({"status": "silent", "text": "", "emotion": "calm"}, owner=True)
        gateway._note_reply_mood({"status": "model_error", "text": "我卡了", "emotion": "sad"}, owner=True)
        assert system.load_state()["mood_updated_ts"] == before

    def test_心情注入只给主人且平静时不塞空块(self, db):
        st = system._default_state()
        st["focus"] = ""
        system.save_state(st)
        assert system.mood_prompt_line() == "", "平静且没有挂着的话，不该注入空块"

        # ⚠ 注意 0.5/0.5 加权的直接后果（主人定的权重，不是 bug）：
        #   从「平静」出发，一次事件只兑现一半 —— 「轻度悲伤」(-4) → -2「伤感」。
        #   要真的到「轻度悲伤」需要连续几次同向事件，或者旧值本来就在这一侧。
        # 这条顺便把加权语义钉住：以后谁改了权重，这里会红。
        new = system.record_mood_event("轻度悲伤", reason="主人三天没理我")
        assert new["mood_value"] == pytest.approx(-2.0, abs=1e-6)
        assert new["mood_label"] == "伤感"

        # 🔧 2026-11-07 修一条**偶发红**（和本次改动无关，但它现在必须绿）：
        #   `-2.0` 正好是「伤感」的**右端点**（`label_of` 是
        #   `if v <= -2.0: return "伤感"`），而 `decay()` 从那个时刻开始
        #   每分钟往 0 靠 0.01。于是 `record_mood_event()` 与
        #   `mood_prompt_line()` 之间只要真实时间跨过一个刻度，
        #   `-2.0 - 1e-6` 就掉进「无聊」—— 实测就是这么红的
        #   （`assert '伤感' in '...你现在的心情是「无聊」...'`）。
        #
        #   这和 `frozen_clock` 修的那三条是**同一个病**（"墙钟别走"），
        #   只是这条上次漏了。修法不是挪阈值，而是把时钟冻住：
        #   这条用例验的是"注入形状"，不是"衰减速度"（那在 test_mood.py 里）。
        gate = pytest.MonkeyPatch()
        try:
            gate.setattr(time, "time", lambda: new["mood_updated_ts"])
            line = system.mood_prompt_line()
        finally:
            gate.undo()
        assert "伤感" in line, (
            f"衰减刚过端点就换了档位（真实时钟在走）—— 该用冻结时钟：{line!r}"
        )
        assert "主人三天没理我" in line
        # 不给"该怎么表现"的指令
        for bad in ("你应该", "请表现", "要用"):
            assert bad not in line

    # ---------------- 心情迁移必须落盘（项目地图坑 #61）----------------
    def test_心情迁移后连续读三次时间戳必须一致(self, db):
        """修坑 #61 的**后果 A**：迁移只在内存里做、`load_state()` 不写盘。

        修前的实测：连续三次 `load_state()` 拿到
        `...4326.7132497` / `...4326.723944` / `...4326.7346344`（各差约 10ms）。
        后果是「第一次 `save_state()` 之前，`decay()` 的 `elapsed_minutes`
        恒为 0，衰减算不出来」——靠心跳捎带落盘是碰运气，不是保证。

        修法：`_normalize_mood()` 返回"是否发生了迁移"，`load_state()`
        看到 True 就立刻 `save_state()` 一次。迁移后 ts 必须**稳定不变**。

        ⚠ 这里**不要**用 `frozen_clock`：要证明的正是「不冻结时钟时也稳定」。
          冻结之后三次读天然相等，这条用例就测不出任何东西了。
        """
        st = system._default_state()
        st["emotion"] = "happy"
        st["mood_updated_ts"] = 0          # 旧格式：没有这个字段
        system.save_state(st)

        first = system.load_state()["mood_updated_ts"]
        assert first > 0, "迁移必须补一个真实时间戳"
        second = system.load_state()["mood_updated_ts"]
        third = system.load_state()["mood_updated_ts"]
        assert first == second == third, (
            f"迁移后时间戳每次都重取：{first} / {second} / {third} —— "
            f"说明迁移结果没落盘"
        )
        # 而且必须真的写进文件（不是只有内存里对）
        assert raw_state()["mood_updated_ts"] == first, "迁移结果没有写回 system_state.json"

    def test_迁移不该吃掉真实心情值(self, db):
        """修坑 #61 的**后果 B**（无时间边界的那个）。

        修前：`ts<=0` 就走「旧格式迁移」，**无条件**用旧 `emotion` 枚举覆盖
        四字段 —— 于是 `-8.0 / 重度悲伤 / ts=0` 读一次变成 `0.0 / 平静`，
        **真的心情值被吃掉**。触发路径不止一条：从旧备份恢复、手工删掉
        `mood_updated_ts`、旧版本写过这份文件。

        修后：有真实值就**原值保留**，只补时间戳。
        """
        st = system._default_state()
        st["emotion"] = "calm"            # 旧的枚举说是平静
        st["mood_value"] = -8.0           # 但真实值是重度悲伤
        st["mood_label"] = "重度悲伤"
        st["mood_reason"] = "主人已经三天没理我了"
        st["mood_updated_ts"] = 0         # ← 关键：时间戳丢了
        system.save_state(st)

        got = system.load_state()
        assert got["mood_value"] == pytest.approx(-8.0), (
            f"真实心情值被迁移吃掉了：-8.0 → {got['mood_value']}"
        )
        assert got["mood_label"] == "重度悲伤"
        assert got["mood_reason"] == "主人已经三天没理我了"
        assert got["mood_updated_ts"] > 0, "原值保留的同时必须补上时间戳"

    def test_只有档位名没有数值也不被吃掉(self, db):
        """半迁移状态（`mood_label` 有、`mood_value` 缺）同样要保住档位。

        这是一条更窄的路径：`mood_value` 被写成 0/缺失、但 `mood_label`
        明确是「中度悲伤」。修前 `value=0` 会被当成"没值"，直接走旧枚举覆盖。
        """
        st = system._default_state()
        st["emotion"] = "calm"
        st["mood_value"] = 0.0
        st["mood_label"] = "中度悲伤"
        st["mood_updated_ts"] = 0
        system.save_state(st)

        got = system.load_state()
        assert got["mood_label"] == "中度悲伤"
        assert got["mood_value"] == pytest.approx(-6.0), "档位表反推出来的数值"

    def test_已经迁移过的状态不会每次读都写盘(self, db):
        """反向守护：**不能**变成"每次 `load_state()` 都写一次文件"。

        那会毁性能（`load_state()` 在每条热路径上），也会让
        「读操作不改状态」这条纪律失效。只允许迁移那一次写。
        """
        st = system._default_state()
        st["mood_updated_ts"] = time.time() - 60   # 已经是新格式
        system.save_state(st)
        path = Path(system.SYSTEM_STATE_PATH)
        mtime_before = path.stat().st_mtime_ns

        for _ in range(3):
            system.load_state()

        assert path.stat().st_mtime_ns == mtime_before, (
            "新格式的状态读三次不该写盘一次 —— 只有迁移才允许写"
        )


# --------------------------------------------------------------------------- #
# 二、计划闭环（能力 5）
# --------------------------------------------------------------------------- #
class TestPlanLifecycle:
    def test_沉默期没满不触发(self, db):
        eid = add_plan(created_days_ago=3)
        assert system._fire_due_plans() == 0
        assert plan_row(eid)[0] == "open"
        assert plan_row(eid)[1] == 0

    def test_沉默期满10天就触发并写内心OS和流水账(self, db):
        eid = add_plan(created_days_ago=11, evidence="给我加升级")
        assert system._fire_due_plans() == 1

        status, attempts, _kind, _payload = plan_row(eid)
        assert status == "open", "触发不等于销毁，还没成败"
        assert attempts == 1

        items = system._read_inner_os()
        assert any("该回头看看了" in str(i.get("text")) for i in items)
        texts = syslog_texts()
        assert any("计划触发" in t and "给我加升级" in t for t in texts), texts

    def test_只触发一次不反复催(self, db):
        add_plan(created_days_ago=11)
        assert system._fire_due_plans() == 1
        assert system._fire_due_plans() == 0, "已经送过的不该再催第二遍"
        assert len(syslog_texts()) == 1

    def test_群渠道的计划不触发(self, db):
        """群渠道没有投递通道：触发只会变成永远 pending 的死数据。"""
        eid = add_plan(created_days_ago=11, channel=GROUP)
        assert system._fire_due_plans() == 0
        status, attempts, _k, _p = plan_row(eid)
        assert status == "closed"
        assert attempts == 1
        assert not [i for i in system._read_inner_os() if "该回头看看了" in str(i.get("text"))]

    # ---------------- 成败由**模型判**（2026-11-07，审计 H.1 方案 C）----------------
    #
    # 🔧 这一组原来测的是 `_close_plans_from_reply()` + `plan.judge_outcome()`
    # 的三套词表。那套东西**整套删掉了**（词表判不准：「我做完了」「完工了」
    # 「升级好了」全判 unknown，实测见审计 H.1）。
    #
    # 但**断言一条都没减**，全部换成测新机制（没人调的词表函数留着没意义，
    # 而「模型判 success/failure 才销毁、unknown 不销毁」这个不变量必须钉死）：
    #
    #   旧：_close_plans_from_reply() 读他的话 → 词表猜 → 落库
    #   新：巩固时喂 pending_plans → 模型在 plan_verdicts 里给 → apply_plan_verdicts 落库
    #
    # ⚠ 下面每条用例都走**真的** `_consolidate_channel()`（只把模型换成桩），
    #   所以连带验了：候选取对了、payload 字段独立、工具 schema 拼上了、落库没错。
    #   只测 `apply_plan_verdicts()` 的话，①②③ 任何一段断掉都测不出来。

    def test_模型判成功就销毁并留痕(self, db):
        """模型说 success → 计划销毁 + 留痕事件 + 流水账。

        对应旧用例 `test_他说升级好了就销毁并留痕`（词表时代）。
        """
        eid = add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PET, "user", "主人: 升级好了，你看", time.time() + 1)

        run = consolidate_with_verdicts(
            [{"id": eid, "verdict": "success", "evidence": "升级好了，你看"}]
        )
        assert run["called"], "没走到模型 —— 待判清单没接上"

        status, _attempts, _kind, _payload = plan_row(eid)
        assert status == "closed", "模型判 success 就该销毁计划"

        # 留痕：是一条**新的**事件，不是把原计划改成"完成"
        rows = q("select kind, payload, status from events where id<>?", (eid,))
        assert rows, "成功必须留痕（记入事件）"
        assert any("兑现" in r[1] for r in rows), rows
        # 模型的依据也存进去了（将来回答「她凭什么说成了」不用重读流水账）
        assert any("升级好了" in r[1] for r in rows), rows
        assert any("计划完成" in t for t in syslog_texts())

    def test_模型判失败也销毁并留痕(self, db):
        """对应旧用例 `test_他说忘了也销毁并留痕`。"""
        eid = add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PET, "user", "主人: 哎我忘了", time.time() + 1)

        run = consolidate_with_verdicts(
            [{"id": eid, "verdict": "failure", "evidence": "哎我忘了"}]
        )
        assert run["called"], "没走到模型"
        assert plan_row(eid)[0] == "closed"
        rows = q("select payload from events where id<>?", (eid,))
        assert any("没有兑现" in r[0] for r in rows), rows

    def test_模型判unknown绝不销毁(self, db):
        """**最重要的一条**：unknown = 什么都不做，计划原样留着。

        对应旧用例 `test_判不出成败就不销毁`。这条守的是项目地图坑 #64 那条铁律：
        一次误判 = 一条真实计划被静默销毁，以后再也不提。

        ⚠ 断言里那句「一条留痕都不许有」很关键：如果哪天有人"顺手"给 unknown
        也写一条 `observation` 留痕，体检和人物画像就会多出一条
        「主人提到过…，结果不明」的**假事实** —— 等于用垃圾数据确认了一次误判。
        """
        eid = add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PET, "user", "主人: 嗯，今天天气不错", time.time() + 1)

        run = consolidate_with_verdicts([{"id": eid, "verdict": "unknown"}])
        assert run["called"], "没走到模型"
        assert plan_row(eid)[0] == "open", "unknown 不许销毁"
        assert plan_row(eid)[1] == 1, "attempts 也不该被再推一次"
        assert q("select id from events where id<>?", (eid,)) == [], "unknown 不许留痕"
        assert not [t for t in syslog_texts() if "计划完成" in t]

    def test_模型什么都没给也不销毁(self, db):
        """模型这轮没填 `plan_verdicts`（或填了脏值）→ 等于全 unknown。

        这是**最常见**的失败模式（模型偶尔只关心 insights），必须安全。
        """
        eid = add_plan(created_days_ago=11, attempts=1)
        add_turn(PET, "user", "主人: 嗯嗯", time.time() + 1)
        for verdicts in (None, [], "不是数组", [{"id": eid, "verdict": "说不清"}]):
            run = consolidate_with_verdicts(verdicts)
            assert run["called"], verdicts
            assert plan_row(eid)[0] == "open", f"{verdicts!r} 不该销毁计划"
            assert q("select id from events where id<>?", (eid,)) == []

    def test_还没触发的计划不进候选(self, db):
        """`attempts=0` 说明提醒根本还没送到她面前，他没被问过 → 不进候选。

        对应旧用例 `test_还没触发的计划不去判成败`。现在这条约束在
        `_pending_plan_candidates()` 的 SQL 里（`coalesce(attempts,0) >= 1`），
        所以断言改成直接看候选：**它压根不该被喂给模型**。
        """
        eid = add_plan(created_days_ago=11, attempts=0)
        add_turn(PET, "user", "主人: 升级好了", time.time() + 1)
        assert system._pending_plan_candidates() == [], "没触发过的计划不该进候选"

    def test_候选带上触发之后他说了什么(self, db):
        """这是判定能准的**前提**：词表时代失败就因为只看文本、不知道计划内容。

        对应旧用例 `test_私聊里的回答也算` 的一半 —— 原来验「能取到他的话」，
        现在验证的话**真的进了候选**（而且只取同一主体的渠道）。
        """
        eid = add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PRIVATE, "user", "【私聊：10000001】示例用户（主人）： 弄好了", time.time() + 1)
        add_turn(GROUP, "user", "【群聊·示例群：10000002】路人（路人）： 弄好了", time.time() + 2)

        cands = system._pending_plan_candidates()
        assert len(cands) == 1
        assert cands[0]["id"] == eid
        assert cands[0]["plan"] == "给我加升级"
        assert "弄好了" in cands[0]["said_after_trigger"]
        # 群聊里别人的话**绝不能**混进来（群渠道不是他）
        assert "路人" not in cands[0]["said_after_trigger"]
        # 时间换成了天数，不给模型原始时间戳（它没有时钟）
        assert cands[0]["created_days_ago"] == pytest.approx(11.0, abs=0.1)

    def test_群里别人接话不算他答应(self, db):
        """群聊的话不构成证据：群里别人接话不代表他答应了什么。

        对应旧用例 `test_群里别人接话不算他答应`。现在这条约束在候选取话的
        `CARE_RESPONSE_SIBLINGS` 里 —— 群里的话压根不会出现在
        `said_after_trigger` 里，所以模型没有"被误导"的原料。
        """
        add_plan(created_days_ago=11, channel=PET, attempts=1)
        add_turn(GROUP, "user", "【群聊·示例群：10000002】路人（路人）： 弄好了", time.time() + 1)
        cands = system._pending_plan_candidates()
        assert cands and cands[0]["said_after_trigger"] == ""

    def test_群渠道的计划不进候选(self, db):
        """群渠道的计划本来就不触发（`_fire_due_plans` 直接落 closed）。

        但历史数据万一有 `attempts>=1` 的群计划，也**不能**拿群友的闲话
        去判主人有没有兑现 —— 那是替别人记账。这条守的是那个兜底。
        """
        add_plan(created_days_ago=11, channel=GROUP, attempts=1)
        assert system._pending_plan_candidates() == []

    def test_候选有封顶(self, db):
        """一轮最多判 `PLAN_VERDICT_MAX` 条：超出的下一轮再判（不是丢）。

        封顶的理由：巩固那一次调用的产出上限是 500 token，待判清单一长，
        模型要么答不全（剩下的一律 unknown → 计划永远收不了尾），
        要么把 insights 挤没。
        """
        for _ in range(plan_rules.PLAN_VERDICT_MAX + 5):
            add_plan(created_days_ago=11, attempts=1)
        assert len(system._pending_plan_candidates()) == plan_rules.PLAN_VERDICT_MAX

    def test_幻觉id不会误伤别的计划(self, db):
        """模型编一个不存在的 id —— 绝不能因此去改一条真计划。

        这条替代了旧用例 `test_取回后按时间正序拼而不是倒序` 守的那个方向：
        旧用例防的是「窗口取错导致判错条」，新链路里同类的风险是「id 对错」。
        """
        real = add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PET, "user", "主人: 搞定了", time.time() + 1)
        run = consolidate_with_verdicts(
            [{"id": 9999, "verdict": "success"},
             {"id": "0", "verdict": "failure"}]
        )
        assert run["called"], "没走到模型"
        assert plan_row(real)[0] == "open", "幻觉 id 居然改到了真计划"
        assert q("select id from events where id<>?", (real,)) == []

    def test_没有候选时工具schema里没有plan_verdicts(self, db):
        """**没候选就不该出现那个字段**：一个永远为空的数组会诱导模型编内容。

        这是 `_consolidate_tools()` 的全部存在意义（`system_policy.md`
        第 4 条「不猜测，不补全」）。
        """
        add_turn(PET, "user", "主人: 随便说一句", time.time())
        run = consolidate_with_verdicts(None)
        assert run["called"]
        props = run["tools"][0]["function"]["parameters"]["properties"]
        assert "plan_verdicts" not in props
        # 巩固本身的字段照旧
        assert "insights" in props

    def test_有候选时工具schema里带plan_verdicts(self, db):
        eid = add_plan(created_days_ago=11, attempts=1)
        add_turn(PET, "user", "主人: 嗯", time.time())
        run = consolidate_with_verdicts([{"id": eid, "verdict": "unknown"}])
        props = run["tools"][0]["function"]["parameters"]["properties"]
        assert "plan_verdicts" in props
        assert props["plan_verdicts"]["items"]["properties"]["verdict"]["enum"] == [
            "success", "failure", "unknown"
        ]

    def test_候选只喂给主人那一路(self, db):
        """群渠道那一路不该带 `pending_plans`（跨场合 + 重复计费）。"""
        add_plan(created_days_ago=11, attempts=1)
        msgs = system._build_consolidate_messages(
            system.load_state(), [{"role": "user", "content": "群里的话"}], GROUP,
            pending_plans=[{"id": 1, "plan": "x"}],
        )
        payload = json.loads(msgs[1]["content"])
        assert "pending_plans" not in payload

    def test_计划清单绝不混进new_turns(self, db):
        """坑 #59 的同一条铁律：混进 `new_turns` 会被 `_extract_events` 抽成承诺。

        待判清单里带着计划内容和系统记账。混进对话历史 → 模型会以为
        `[系统] 计划触发：…` 是某个人说的话，抽出一条**没人答应过的承诺**。
        """
        add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PET, "user", "主人: 你好", time.time())
        cands = system._pending_plan_candidates()
        msgs = system._build_consolidate_messages(
            system.load_state(), [{"role": "user", "content": "你好"}], PET,
            pending_plans=cands,
        )
        payload = json.loads(msgs[1]["content"])
        assert payload["new_turns"] == [{"role": "user", "content": "你好"}]
        assert payload["pending_plans"][0]["plan"] == "给我加升级"

    def test_内部字段channel不进prompt但落库还在(self, db):
        """`channel` 是**内部字段**：喂模型之前摘掉，落留痕事件时还要用。

        两件事必须同时成立，所以单独一条：
          ① 进 prompt 的那份没有 `channel`（场合对「成没成」没有信息量，
             说了反而会诱导模型按场合猜）；
          ② `_consolidate_channel()` 手上那份**还带着** `channel` ——
             否则留痕事件会落到错误的渠道（那等于把私聊的事记到群里）。
        """
        eid = add_plan(created_days_ago=11, evidence="给我加升级",
                       attempts=1, channel=PRIVATE)
        cands = system._pending_plan_candidates()
        assert cands[0]["channel"] == PRIVATE, "候选里必须留着真实渠道"
        msgs = system._build_consolidate_messages(
            system.load_state(), [{"role": "user", "content": "你好"}], PET,
            pending_plans=cands,
        )
        payload = json.loads(msgs[1]["content"])
        assert "channel" not in payload["pending_plans"][0], (
            "内部字段漏进 prompt 了"
        )
        # 而原 list 没被改坏（摘的是副本）
        assert cands[0]["channel"] == PRIVATE

        # 真的走一遍：留痕事件必须落在**原渠道**
        add_turn(PRIVATE, "user", "【私聊：10000001】示例用户（主人）： 弄好了", time.time())
        # 只能巩固私聊那一路 —— 计划在 `PRIVATE` 渠道上，巩固 `PET` 的话
        # 水位/增量对不上（`_read_channel_turns()` 按渠道精确读）。
        run = consolidate_with_verdicts(
            [{"id": eid, "verdict": "success"}], channel=PRIVATE
        )
        assert run["called"]
        rows = q("select channel from events where id<>?", (eid,))
        assert rows and rows[0][0] == PRIVATE, (
            f"留痕事件落到别的渠道了：{rows}"
        )

    def test_insights为空也照样判计划(self, db):
        """**关键顺序**：`no_insight` 是最常见的生产结果，计划裁决不能跟着丢。

        生产实测：`normal` 5 次 / `no_insight` 2 次 —— 也就是说三分之一的
        巩固轮次里 insights 是空的。如果裁决写在 `if not insights: return`
        之后，那三分之一的轮次里计划永远判不了。
        """
        eid = add_plan(created_days_ago=11, evidence="给我加升级", attempts=1)
        add_turn(PET, "user", "主人: 弄好了", time.time())
        run = consolidate_with_verdicts(
            [{"id": eid, "verdict": "success"}], insights=[]
        )
        assert run["called"], "没走到模型"
        assert run["result"] == "skip", (
            "这轮没有观点要沉淀 → `_consolidate_channel` 返回 skip（no_insight）。"
            "计划裁决**不能**被这个 return 带走"
        )
        assert plan_row(eid)[0] == "closed", "insights 为空时计划裁决被跳过了"

    def test_没有增量的一轮不碰计划(self, db):
        """没有新对话 → 巩固提前 return → 计划也不该被判（省一次 SQL，也不该动）。

        计划不是「每轮都判」，而是「跟着巩固判」。没有巩固素材时保持原样，
        下一轮有增量时再判 —— 这符合主人的「整理流水账时一起判断」。
        """
        eid = add_plan(created_days_ago=11, attempts=1)
        assert system._pending_plan_candidates()  # 候选本身是有的
        run = consolidate_with_verdicts([{"id": eid, "verdict": "success"}])
        assert run["result"] == "skip", "没增量时该走 no_new_turns 分支"
        assert not run["called"], "没有新对话就不该调模型（这一步纯代码守卫）"
        assert plan_row(eid)[0] == "open", "没巩固就不该判计划"


# --------------------------------------------------------------------------- #
# 三、system_log（能力 5 第四节的全局渠道）
# --------------------------------------------------------------------------- #
class TestSystemLog:
    def test_流水账带统一前缀(self, db):
        assert system.append_system_log("[系统] 测试一下") is True
        assert syslog_texts() == ["[系统] 测试一下"]

    def test_空正文不写(self, db):
        assert system.append_system_log("   ") is False
        assert syslog_texts() == []

    def test_流水账不进对话历史(self, db):
        """它不是谁说的话（role=system），塞进对话历史会让模型以为有人这么讲。"""
        add_turn(PET, "user", "主人: 你好")
        add_turn(PET, "assistant", "你好呀")
        system.append_system_log("[系统] 计划触发：问他升级的事")

        hist = gateway.recent_turns(gateway.SHARED_SESSION, 50, channel="", local_pet=True)
        joined = " ".join(str(h.get("content")) for h in hist)
        assert "计划触发" not in joined
        assert "你好呀" in joined

    def test_流水账会喂给巩固(self, db):
        add_turn(PET, "user", "主人: 你好")
        system.append_system_log("[系统] 计划触发：问他升级的事")
        st = system.load_state()
        msgs = system._build_consolidate_messages(st, [{"role": "user", "content": "你好"}], PET)
        blob = json.dumps(msgs, ensure_ascii=False)
        assert "system_log" in blob
        assert "计划触发" in blob
        # 但**不能**混进 new_turns（那会把它当对话去抽事实）
        payload = json.loads(msgs[1]["content"])
        assert payload["new_turns"] == [{"role": "user", "content": "你好"}]

    def test_群渠道不带流水账(self, db):
        """全局流水账只喂 owner 那一路：喂给每个渠道等于重复计费。"""
        system.append_system_log("[系统] 计划触发：问他升级的事")
        msgs = system._build_consolidate_messages(
            system.load_state(), [{"role": "user", "content": "群里的话"}], GROUP
        )
        payload = json.loads(msgs[1]["content"])
        assert "system_log" not in payload

    def test_流水账水位只许前进(self, db):
        system.append_system_log("[系统] 一")
        system.append_system_log("[系统] 二")
        st = system.load_state()
        rows = system._pending_system_log(st)
        assert len(rows) == 2
        system._advance_system_log_watermark(rows[-1]["id"])
        assert system._pending_system_log(system.load_state()) == []
        # 回退不许生效
        system._advance_system_log_watermark(1)
        assert system.load_state()["last_system_log_id"] == rows[-1]["id"]

    def test_系统层记录的role是system(self, db):
        system.append_system_log("[系统] 体检：发现 2 个问题")
        role = q("select role from turns where channel=?", (SYSLOG,))[0][0]
        assert role == "system"

    # ---------------- 流水账不能被当成"对话"（坑 #59 的另一半）----------------
    #
    # 这一组是修「问题 4」时**发现的新连带问题**：`system_log` 写进 turns 之后，
    # `_seconds_since_last_turn()` / `_absence_summary()` / `_latest_turn_id()` /
    # `_day_timeline()` 这些查询只看 session，于是系统自己写的流水账
    # 会被当成"主人刚说过话"。实测代价：
    #   * 每日梳理永远被 `not_idle` 挡住（`test_体检的节律状态与后台卡片一致` 变红）；
    #   * 每写一条流水账就多一次 `new_turns` 变化 → 白花一次模型调用；
    #   * 「他多久没说话了」被系统重置，场景 C 的素材归零。
    def test_流水账不算他刚说过话(self, db):
        add_turn(PET, "user", "主人: 三天前那句", ts=time.time() - 3 * DAY)
        before = system._seconds_since_last_turn()
        assert before is not None and before > 2 * DAY

        system.append_system_log("[系统] 体检：发现 2 个问题")

        after = system._seconds_since_last_turn()
        assert after == pytest.approx(before, abs=5), (
            f"系统流水账被当成对话了：{before} → {after}（应该几乎不变）"
        )

    def test_流水账不算新对话(self, db):
        """`_latest_turn_id()` 不能把流水账当成"有新内容"，否则每次记账都白调模型。"""
        add_turn(PET, "user", "主人: 你好")
        before = system._latest_turn_id()
        system.append_system_log("[系统] 巩固完成：写入 3 条记忆")
        assert system._latest_turn_id() == before

    def test_流水账不算缺席结束(self, db):
        """`_absence_summary()` 是每日梳理「他三天没来」那条腿的素材，不能被系统重置。"""
        add_turn(PET, "user", "主人: 三天前那句", ts=time.time() - 3 * DAY)
        before = system._absence_summary()["absent_hours"]
        assert before >= 70

        system.append_system_log("[系统] 主动说话：主人刚回来")

        after = system._absence_summary()["absent_hours"]
        assert after == pytest.approx(before, abs=0.1), (
            f"系统流水账把缺席时长清零了：{before} → {after}"
        )

    def test_流水账不算对话节奏(self, db):
        """`_day_timeline()` 的疏密节奏不能被记账行为污染。

        ⚠ 这里**不能**传 `now=`：`append_system_log()` 写入用的是真实的
        `time.time()`，传一个过去的 `now` 会让流水账落在 `ts <= end` 窗口外，
        旧实现也照样通过 —— 用例就测了个寂寞。所以用默认的"现在"当窗口终点，
        并把两条真对话放在过去 1 小时。
        """
        now = time.time()
        add_turn(PET, "user", "主人: 一句", ts=now - 3600)
        add_turn(PET, "assistant", "嗯", ts=now - 3500)
        before = system._day_timeline(hours=24)
        assert before["turns_total"] == 2

        system.append_system_log("[系统] 体检：发现 2 个问题")
        system.append_system_log("[系统] 巩固完成：写入 3 条记忆")

        after = system._day_timeline(hours=24)
        assert after["turns_total"] == before["turns_total"], (
            f"流水账混进对话节奏了：{before['turns_total']} → {after['turns_total']}"
        )
        assert after["active_hours"] == before["active_hours"]


# --------------------------------------------------------------------------- #
# 四、事件的创建时间以对话时间为准（能力 8 第 ④ 条）
# --------------------------------------------------------------------------- #
def test_事件带上对话时间而不是提炼时间(db):
    """主人 2026-11-06 定：「创建时间肯定是以对话时间为准」。

    攒了 4 天才巩固的那批如果按提炼时间算，在遗忘算法眼里会显得很新，
    于是永远删不掉它。
    """
    old = time.time() - 4 * DAY
    turns = [
        {"role": "user", "content": "主人: 我最近在弄记忆系统", "ts": old},
        {"role": "assistant", "content": "听起来好难", "ts": old + 60},
    ]
    evs = system._validate_events(
        {"events": [{"kind": "observation", "subject": "主人", "evidence": "在弄记忆系统"}]},
        turns,
    )
    assert evs
    # 🔧 用精确相等，不用 `pytest.approx`：`_validate_events()` 是
    # `float(t["ts"])` 原样透传，没有任何浮点运算。而 approx 不带 `abs`
    # 走的是**相对**容差（默认 rel=1e-6），时间戳量级 1.7e9 → 容差 ±1700 秒，
    # 意味着"取错成第一条(t=old)"或"用提炼时间(now)"都可能照样绿。
    # 这条断言的全部价值就在"到底取了哪一个时刻"，必须钉死。
    assert evs[0]["ts"] == old + 60, "取这一批里最新的那条对话时间"


def test_没给对话时间就不带ts(db):
    evs = system._validate_events(
        {"events": [{"kind": "observation", "evidence": "在弄记忆系统"}]}
    )
    assert "ts" not in evs[0]


# --------------------------------------------------------------------------- #
# 五、遗忘：判据换成指数衰减之后，门槛真的能跨过去
# --------------------------------------------------------------------------- #
def test_遗忘扫描用新判据而不是旧的importance门槛(db, monkeypatch):
    """旧判据「够旧 且 importance<0.3」在生产上 matched 恒为 0。

    因为 importance 是写死常数（最低 0.55），门槛 0.3 永远跨不过去。
    新判据只看 R=e^(-t/S)。
    """
    import forgetting

    # 一条 200 天的"一般观察"（S=30）→ R≈0.0013，早该删
    r = forgetting.retrievability(30.0, 200.0)
    assert r < 0.05
    assert forgetting.classify(30.0, 200.0) == "drop"

    # 一条 200 天的承诺 → 永不删
    assert forgetting.stability_for("promise") is None
    assert forgetting.classify(None, 200.0, keep_forever=True) == "keep"


# --------------------------------------------------------------------------- #
# 六、主动说话读新心情字段（项目地图坑 #63 / 问题 3）
# --------------------------------------------------------------------------- #
class TestActiveSpeakMood:
    """主动说话的 prompt 必须读新的 `mood_*` 四字段，不是旧的 `emotion` 枚举。

    修前：`role_speak.maybe_speak()` 拼的是 `f"你现在的心情：{st.get('emotion')}"`
    —— 那个 8 值旧枚举。于是**她主动开口时"知道"的还是旧 emotion，
    被动回话时才知道新心情**（`build_messages` 走 `mood_prompt_line()`）。
    """

    def _capture_prompt(self, monkeypatch, st) -> str:
        """跑一次 maybe_speak，把拼好的第二段 system 抓回来。"""
        import role_speak

        captured = {}
        monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
        monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})

        def fake_call(msgs, use_tools=False):
            captured["msgs"] = msgs
            return "[不说]", {}, None

        monkeypatch.setattr(gateway, "call_model", fake_call)
        monkeypatch.setattr(role_speak, "deliver_to_pet", lambda r: True)
        role_speak.maybe_speak({"now": "2026-11-06 22:00"}, st)
        return captured["msgs"][1]["content"]

    def test_主动说话读到新心情档位和理由(self, db, monkeypatch):
        st = system._default_state()
        st["emotion"] = "happy"          # 旧枚举（会被忽略）
        # ⚠ 取 -7.0（不是正好 -6.0）：`-6.0` 落在「中度悲伤」的**右端点**
        #   （`(-8, -6]`），而衰减是连续函数 —— 30 秒的衰减就把它推过 -6.0
        #   变成「轻度悲伤」，用例会红在"档位名不对"这种假问题上。
        st["mood_value"] = -7.0
        st["mood_label"] = "中度悲伤"
        st["mood_reason"] = "主人三天没理我"
        st["mood_updated_ts"] = time.time() - 30
        system.save_state(st)

        prompt = self._capture_prompt(monkeypatch, system.load_state())
        assert "中度悲伤" in prompt, f"主动说话没读到新档位：{prompt!r}"
        assert "主人三天没理我" in prompt
        assert "你现在的心情：" not in prompt, "旧那行 `你现在的心情：{emotion}` 还在"
        assert "happy" not in prompt, "旧枚举漏进 prompt 了"

    def test_主动说话不注入focus(self, db, monkeypatch):
        """坑 #63 的明确警告：主动说话注入 `focus` 会让她**复读自己**。

        所以不能用 `mood_prompt_line()` 本体（它带「你心里还挂着刚才那句」），
        只能用 `mood.prompt_line()`（只读四字段）。
        """
        st = system._default_state()
        st["focus"] = "早点睡"           # 她上一句说过的话
        st["mood_value"] = 2.0
        st["mood_label"] = "愉悦"
        st["mood_updated_ts"] = time.time() - 30
        system.save_state(st)

        prompt = self._capture_prompt(monkeypatch, system.load_state())
        assert "早点睡" not in prompt, f"主动说话把 focus 注入进去了（会复读自己）：{prompt!r}"
        assert "挂着刚才那句" not in prompt

    def test_主动说话平静时也给出心情(self, db, monkeypatch):
        """平静时**照常注入一行**（和 `mood_prompt_line()` 的"不注入"不同）。

        主动说话必须知道自己此刻是平静的，否则只能靠猜。
        这里同时守住「不会拿到空串」这条。
        """
        st = system._default_state()
        st["focus"] = ""
        st["mood_value"] = 0.0
        st["mood_label"] = "平静"
        st["mood_updated_ts"] = time.time() - 30
        system.save_state(st)

        prompt = self._capture_prompt(monkeypatch, system.load_state())
        assert "平静" in prompt, f"平静时也该告诉她「你现在的心情是「平静」」：{prompt!r}"

    def test_主动说话的心情按时间衰减(self, db, monkeypatch):
        """读的是**衰减后**的值：+8 放 100 分钟 → +7（0.01/分钟）。"""
        st = system._default_state()
        st["mood_value"] = 8.0
        st["mood_label"] = "极度高兴"
        st["mood_updated_ts"] = time.time() - 100 * 60   # 100 分钟前
        system.save_state(st)

        prompt = self._capture_prompt(monkeypatch, system.load_state())
        # +8 - 100*0.01 = +7 → 「极度高兴」（(4,8] 区间）
        assert "极度高兴" in prompt, f"没读到衰减后的心情：{prompt!r}"
        assert "超级高兴" not in prompt, "没衰减（+8 原样落进 >8 的「超级高兴」）"


# --------------------------------------------------------------------------- #
# 七、system_log 接入三处（问题 4）
# --------------------------------------------------------------------------- #
class _BridgeStub:
    """`_memory_bridge()` 的替身：只数写了几条，不碰真库、不算向量。"""

    def __init__(self, written: int = 3, boom: bool = False) -> None:
        self.written = written
        self.boom = boom
        self.calls = 0

    def write_events(self, events, channel):
        self.calls += 1
        if self.boom:
            raise RuntimeError("记忆库坏了")
        return self.written


class TestSystemLogConsolidate:
    """巩固完成 —— **只在真的写入了记忆时**记，空转一律不记。"""

    def _prep(self, monkeypatch, written=3, events=None, min_new=8):
        monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", min_new)
        # 「最长等待旁路」会把「上次巩固很久以前 + 至少 2 条增量」当成够格，
        # 于是门槛测试里 1 条对话也会跑。钉一个刚刚巩固过的时间戳，
        # 让 `idle_bypass` 不生效 —— 否则测的就不是门槛了。
        monkeypatch.setitem(system.SYSTEM, "consolidate_max_wait_hours", 12)
        monkeypatch.setattr(
            system, "_extract_events",
            lambda turns: events if events is not None else
            [{"kind": "observation", "subject": "主人", "evidence": "在弄记忆"}],
        )
        monkeypatch.setattr(system, "update_people_profile", lambda ch, turns: False)
        monkeypatch.setattr(system, "_memory_bridge", lambda: _BridgeStub(written=written))
        monkeypatch.setattr(
            system, "call_system",
            lambda msgs, **kw: (None, {}, [{"id": "1", "function": {
                "name": "system_consolidate",
                "arguments": json.dumps({"insights": [{"text": "一条观点", "confidence": 0.8}]}),
            }}]),
        )

    def _fresh_state(self) -> dict:
        """刚巩固过的状态：把 `idle_bypass` 关掉，测的才是增量门槛。"""
        st = system.load_state()
        st["last_consolidate_ts"] = time.time()
        system.save_state(st)
        return system.load_state()

    def test_真写入记忆时记一条(self, db, monkeypatch):
        self._prep(monkeypatch, written=3)
        for i in range(10):
            add_turn(PET, "user", f"主人: 第{i}句")

        assert system._consolidate_channel(self._fresh_state(), PET) == "ok"
        texts = syslog_texts()
        assert any("巩固完成" in t and "3" in t for t in texts), texts

    def test_没写进去就不记(self, db, monkeypatch):
        """`written=0`（抽出来的是 observation，长期记忆不收）不该记 —— 
        没结果的操作记了只会让流水账变成噪音。"""
        self._prep(monkeypatch, written=0, min_new=1)
        for i in range(10):
            add_turn(PET, "user", f"主人: 第{i}句")

        system._consolidate_channel(self._fresh_state(), PET)
        assert not [t for t in syslog_texts() if "巩固完成" in t], syslog_texts()

    def test_没增量不记(self, db, monkeypatch):
        """`no_new_turns` / `not_enough_new` 一律跳过（主人明确说的）。"""
        self._prep(monkeypatch, written=3)
        assert system._consolidate_channel(self._fresh_state(), PET) == "skip"
        assert syslog_texts() == []

    def test_增量不够门槛也不记(self, db, monkeypatch):
        self._prep(monkeypatch, written=3, min_new=8)
        add_turn(PET, "user", "主人: 只有一句")   # 低于门槛 8
        assert system._consolidate_channel(self._fresh_state(), PET) == "skip"
        assert syslog_texts() == []

    def test_记忆库炸了不记(self, db, monkeypatch):
        """写长期记忆抛异常 = 没结果，不记（`consolidate` 自己会落审计）。"""
        self._prep(monkeypatch, min_new=1)
        monkeypatch.setattr(system, "_memory_bridge",
                            lambda: _BridgeStub(boom=True))
        for i in range(10):
            add_turn(PET, "user", f"主人: 第{i}句")

        system._consolidate_channel(self._fresh_state(), PET)
        assert not [t for t in syslog_texts() if "巩固完成" in t], syslog_texts()


class TestSystemLogSpeak:
    """主动说话 —— **只在真的送出去时**记，被额度拦下的不记。"""

    def _patch_tick(self, monkeypatch):
        """心跳的隔离替身。

        ⚠ 路径**不用再改**：`db` fixture 已经把 `SYSTEM_STATE_PATH` 等全部
        指到 `data_dir` 了。这里只桩掉「读库 / 判变化 / 刹车」，让
        `_tick_body` 能走到主动说话那一段。
        """
        monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH",
                            str(Path(system.DATA_DIR) / "system_audit.jsonl"))
        monkeypatch.setattr(system, "_detect_change",
                            lambda st: {"new_turns": 1, "last_turn_id": 1,
                                        "presence": "present"})
        monkeypatch.setattr(system, "_read_recent_chat", lambda n=5: [])
        monkeypatch.setattr(system, "_read_recent_chat_multi", lambda n=5: [])
        monkeypatch.setattr(system, "_seconds_since_last_turn", lambda: None)
        monkeypatch.setattr(system, "_allow_speak", lambda st: True)
        monkeypatch.setattr(system, "_fire_due_schedules", lambda: 0)
        monkeypatch.setattr(system, "_fire_due_plans", lambda: 0)
        # ⚠ 这里原来还桩了 `_close_plans_from_reply`。那个函数 2026-11-07 删掉了
        # （成败判定改由模型在巩固时判），心跳里只剩 `_fire_due_plans`。
        # 别再补回来 —— 补回来会让本类用例以为心跳还是多一步的。

    def test_真送出去时记一条(self, db, monkeypatch):
        self._patch_tick(monkeypatch)
        monkeypatch.setattr(
            system.role_speak, "maybe_speak",
            lambda signals, st: ({"text": "你回来啦", "emotion": "happy", "tone": ""}, "spoken"),
        )
        system._tick_body()
        texts = syslog_texts()
        assert any(t == "[系统] 主动说话：主人刚回来" for t in texts), texts

    def test_她选择不说就不记(self, db, monkeypatch):
        self._patch_tick(monkeypatch)
        monkeypatch.setattr(system.role_speak, "maybe_speak",
                            lambda signals, st: (None, "declined"))
        system._tick_body()
        assert syslog_texts() == []

    def test_投递失败不记(self, db, monkeypatch):
        """`delivery_failed` 是没送出去 —— 记进流水账会让「她今天找过主人几次」失真。"""
        self._patch_tick(monkeypatch)
        monkeypatch.setattr(system.role_speak, "maybe_speak",
                            lambda signals, st: (None, "delivery_failed"))
        system._tick_body()
        assert syslog_texts() == []

    def test_被额度拦住不记(self, db, monkeypatch):
        """主人明确要求：**被额度拦住的不要记**。"""
        self._patch_tick(monkeypatch)
        monkeypatch.setattr(system, "_allow_speak", lambda st: False)
        called = []
        monkeypatch.setattr(system.role_speak, "maybe_speak",
                            lambda signals, st: called.append(1) or (None, "declined"))
        system._tick_body()
        assert called == [], "刹车拦住时根本不该问角色层"
        assert syslog_texts() == []


class TestSystemLogHealthCheck:
    """体检 —— **只在真的发现问题时**记，全绿的跳过。"""

    def _ok_db(self):
        return {"total": 10, "last_24h": 1, "null_channel": 0, "sources": {},
                "unknown_24h": 0, "known_24h": 1}

    def test_有问题时记一条(self, db, monkeypatch):
        monkeypatch.setattr(system, "_db_stats",
                            lambda: {"total": 0, "last_24h": 0, "null_channel": 0,
                                     "sources": {}})
        rep = system.health_check()
        assert rep["problems"], "这条用例需要至少一个问题"
        assert any("体检：发现" in t for t in syslog_texts()), syslog_texts()

    def test_没问题不记(self, db, monkeypatch):
        """全绿的体检没有"值得未来参考的结果"，记了只会让流水账变噪音。"""
        monkeypatch.setattr(system, "_db_stats", self._ok_db)
        monkeypatch.setattr(system, "_read_inner_os", lambda: [])
        monkeypatch.setattr(system, "_read_persona", lambda: "人" * 500)
        monkeypatch.setattr(system, "_audit_stats",
                            lambda: {"errors": 0, "by_event": {}, "soft_fails": 0})
        monkeypatch.setattr(system, "_long_memory_stats", lambda: None)
        monkeypatch.setattr(system, "_model_error_stats",
                            lambda hours=24.0: {"total": 0, "unauthorized": 0,
                                                "server_error": 0, "timeout": 0,
                                                "chat_errors": 0})
        flaky = system._default_state()
        flaky["last_tick_ts"] = time.time()      # 心跳活着，别报 warn
        monkeypatch.setattr(system, "load_state", lambda: flaky)
        monkeypatch.setattr(system, "_plan_health_stats",
                            lambda: {"open": 0, "overdue": 0, "triggered": 0,
                                     "pending_close": 0, "closed_24h": 0})
        # 每日梳理期开着、但一次都没跑过 → 会报一条 warn（真实配置就是开着）。
        # 这条用例要的是"全绿"，所以显式关掉梳理 —— 它和本用例无关。
        monkeypatch.setitem(system.SYSTEM, "daily_review", {"enabled": False})

        rep = system.health_check()
        assert rep["problems"] == [], f"这条用例要求全绿：{rep['problems']}"
        assert not [t for t in syslog_texts() if "体检" in t], syslog_texts()

# --------------------------------------------------------------------------- #
# 能力 2 · 遗忘的**中间档**：接上 `review_candidates()` 这条断头路
#
# 审计 G.2 第 1 条 / 板块 J ② / 板块 I 第 5 条：
#
#     `MemoryBridge.review_candidates()` 有完整实现（`memory_bridge.py:482`），
#     但生产代码零调用点。
#     → 遗忘只有「硬删」和「保留」两档，「问模型」那一档永远不执行。
#
# 修法：把它接进**巩固那一次模型调用**（零额外调用），候选放独立的
# `forget_review` 字段，答案走 `system_consolidate.forget_drop`。
# 为什么选巩固而不是每日清理 / 独立调用 —— 见 `system._maybe_forget_review()`
# 的 docstring（三条理由：省钱 / 复用「一起判断」/ 巩固本来就在读记忆）。
# --------------------------------------------------------------------------- #
class _ForgetBridgeStub:
    """`_memory_bridge()` 的替身：只提供遗忘中间档需要的那两个方法。

    为什么不接真 `MemoryStore`：真库要 faiss + embedding，测试进程里
    `conftest.py` 的出网点守门会直接把 embedding 判红，而且每条用例建一次
    faiss 索引要几百毫秒。这里要验的是**接线**（候选有没有被喂给模型、
    模型说删的有没有真删），不是 faiss 本身 —— 那一层在 test_memory_store.py。
    """

    def __init__(self, items, *, applied=None, boom=False):
        self.items = items
        self.boom = boom
        self.applied = applied if applied is not None else {"dropped": 0,
                                                            "kept": 0, "ok": False}
        self.apply_calls = []
        self.forget_plan_deleted = 0

    # 中间档候选（`_maybe_forget_review()` 调的）
    def forget_review_items(self, limit: int = 10):
        if self.boom:
            raise RuntimeError("记忆库坏了")
        return list(self.items[:limit])

    # 模型的「删」名单 → 真删（`apply_forget_review()` 调的）
    def apply_review(self, reply, candidate_ids):
        self.apply_calls.append((reply, list(candidate_ids)))
        return dict(self.applied)

    # 每日清理那条路（keep / drop 两档，不受本次改动影响）
    def forget_plan(self, dry_run: bool = False):
        if self.boom:
            raise RuntimeError("记忆库坏了")
        return {"scanned": 0, "drop": 0, "deleted": self.forget_plan_deleted,
                "review": len(self.items), "keep": 0, "boosted": 0,
                "dry_run": bool(dry_run)}

    def write_events(self, events, channel):
        return 0


def _forget_candidates(n=2):
    return [{"id": f"doc{i}", "text": f"第{i}条旧记忆"} for i in range(n)]


def _run_consolidate_forget(monkeypatch, stub, forget_drop=None, *,
                            insights="[]"):
    """跑一轮真巩固，把 models 换成"回指定 forget_drop"的桩。

    返回 `(called, tools_props, payload)` —— 和计划那组同一个形状。
    """
    monkeypatch.setattr(system, "_memory_bridge", lambda: stub)
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    args = {"insights": [] if insights == "[]" else [
        {"text": "一条观点", "confidence": 0.8}]}
    if forget_drop is not None:
        args["forget_drop"] = forget_drop
    seen = {"called": False, "tools": [], "msgs": []}

    def fake(msgs, *, tool_choice="auto", tools=None, max_tokens=None):
        names = [((t or {}).get("function") or {}).get("name") for t in (tools or [])]
        if "system_consolidate" in names:
            seen["called"] = True
            seen["tools"] = tools or []
            seen["msgs"] = msgs
            return None, {}, [{"id": "1", "function": {
                "name": "system_consolidate",
                "arguments": json.dumps(args, ensure_ascii=False),
            }}]
        return None, {}, []

    monkeypatch.setattr(system, "call_system", fake)
    st = system.load_state()
    st["last_consolidate_ts"] = time.time()
    system.save_state(st)
    system._consolidate_channel(system.load_state(), PET)
    payload = {}
    if seen["msgs"]:
        payload = json.loads(seen["msgs"][1]["content"])
    return seen["called"], seen["tools"], payload


class TestForgetReviewWiring:
    """`review_candidates()` 从"零调用"变成"每轮巩固都在问"。"""

    def test_中间档候选被喂给模型(self, db, monkeypatch):
        """**这条就是断头路的验收**：候选真的进了巩固 payload。"""
        add_turn(PET, "user", "主人: 随便说点什么", time.time())
        stub = _ForgetBridgeStub(_forget_candidates())
        called, _tools, payload = _run_consolidate_forget(monkeypatch, stub)
        assert called, "没走到模型"
        assert "forget_review" in payload, "候选没进 payload —— 断头路还是断的"
        assert [c["id"] for c in payload["forget_review"]] == ["doc0", "doc1"]
        assert payload["forget_review"][0]["text"] == "第0条旧记忆"

    def test_没有候选时字段不出现也不多花一次调用(self, db, monkeypatch):
        """中间档为空（生产现状：13 条记忆 R 都 >0.89）→ 一切照旧。"""
        add_turn(PET, "user", "主人: 随便说点什么", time.time())
        stub = _ForgetBridgeStub([])
        called, tools, payload = _run_consolidate_forget(monkeypatch, stub)
        assert called
        assert "forget_review" not in payload
        props = tools[0]["function"]["parameters"]["properties"]
        assert "forget_drop" not in props, (
            "没有候选就不该出现这个字段 —— 一个永远为空的数组会诱导模型编 id"
        )
        assert stub.apply_calls == [], "没候选就不该调 apply_review"

    def test_模型说删就真删(self, db, monkeypatch):
        add_turn(PET, "user", "主人: 嗯", time.time())
        stub = _ForgetBridgeStub(_forget_candidates(3),
                                 applied={"dropped": 1, "kept": 2, "ok": True})
        called, tools, payload = _run_consolidate_forget(
            monkeypatch, stub, forget_drop=["doc1"]
        )
        assert called
        # 工具 schema 里出现了 forget_drop，且是字符串数组
        props = tools[0]["function"]["parameters"]["properties"]
        assert props["forget_drop"]["type"] == "array"
        assert props["forget_drop"]["items"]["type"] == "string"
        # 真删调用发生了，而且只带着**候选里**的 id
        assert stub.apply_calls, "模型说删，但根本没调 apply_review"
        _reply, candidate_ids = stub.apply_calls[0]
        assert candidate_ids == ["doc0", "doc1", "doc2"]

    def test_模型没说删就一条也不动(self, db, monkeypatch):
        """`forget_drop` 不出现 / 空数组 → **全留**（删是不可逆的）。"""
        add_turn(PET, "user", "主人: 嗯", time.time())
        stub = _ForgetBridgeStub(_forget_candidates(2))
        for drop in (None, []):
            stub.apply_calls.clear()
            called, _tools, _payload = _run_consolidate_forget(
                monkeypatch, stub, forget_drop=drop
            )
            assert called
            assert stub.apply_calls == [], f"{drop!r} 居然触发了删除"

    def test_模型编的id被白名单挡住(self, db, monkeypatch):
        """模型幻觉出候选之外的 doc_id → 不进 `apply_review`。"""
        add_turn(PET, "user", "主人: 嗯", time.time())
        stub = _ForgetBridgeStub(_forget_candidates(2),
                                 applied={"dropped": 0, "kept": 2, "ok": False})
        _run_consolidate_forget(monkeypatch, stub, forget_drop=["doc99", "不存在的"])
        assert stub.apply_calls == [], "幻觉 id 没被拦住"

    def test_记忆库炸了不拦巩固(self, db, monkeypatch):
        """`_maybe_forget_review()` 抛异常 → 记审计，巩固照常跑完。"""
        add_turn(PET, "user", "主人: 嗯", time.time())
        stub = _ForgetBridgeStub(_forget_candidates(2), boom=True)
        called, _tools, payload = _run_consolidate_forget(monkeypatch, stub)
        assert called, "遗忘读失败不该拖死巩固"
        assert "forget_review" not in payload
        audits = [json.loads(line) for line in
                  open(system.SYSTEM_AUDIT_PATH, encoding="utf-8")]
        assert any(a.get("event") == "long_memory_forget_review"
                   and a.get("ok") is False for a in audits), audits[-5:]

    def test_候选只喂给主人那一路(self, db, monkeypatch):
        """群渠道不碰：长期记忆是全局的，拿群的调用去删主人的记忆是隔离事故。"""
        stub = _ForgetBridgeStub(_forget_candidates(2))
        monkeypatch.setattr(system, "_memory_bridge", lambda: stub)
        msgs = system._build_consolidate_messages(
            system.load_state(), [{"role": "user", "content": "群里的话"}], GROUP,
            forget_review=_forget_candidates(2),
        )
        payload = json.loads(msgs[1]["content"])
        assert "forget_review" not in payload

    def test_候选绝不混进new_turns(self, db):
        """坑 #59 的同一条铁律：记忆库正文混进对话历史会被当成"他刚说的话"。"""
        msgs = system._build_consolidate_messages(
            system.load_state(), [{"role": "user", "content": "你好"}], PET,
            forget_review=_forget_candidates(2),
        )
        payload = json.loads(msgs[1]["content"])
        assert payload["new_turns"] == [{"role": "user", "content": "你好"}]
        assert payload["forget_review"][0]["text"] == "第0条旧记忆"

    def test_候选里不含时间信息(self, db):
        """3.7 末节：模型没有时钟，问法里**绝不能**出现时间/分数。

        这条守的是 `forgetting.review_prompt()` 那套口径 —— 现在答案走
        tool schema 了，但**问法**（候选载荷）也必须守住同一条规矩。
        """
        add_turn(PET, "user", "主人: 嗯", time.time())
        stub = _ForgetBridgeStub(_forget_candidates(3))
        import pytest as _pytest  # noqa: PLC0415

        gate = _pytest.MonkeyPatch()
        try:
            _called, _tools, payload = _run_consolidate_forget(gate, stub)
        finally:
            gate.undo()
        blob = json.dumps(payload.get("forget_review") or [], ensure_ascii=False)
        for word in ("天前", "个月", "多久", "时间", "R=", "retrievability"):
            assert word not in blob, f"候选里出现了 {word!r}：模型没有真实时钟"
        # 每条只有 id + 内容
        for item in payload["forget_review"]:
            assert set(item) == {"id", "text"}, item


class TestForgetReviewBridge:
    """`MemoryBridge.forget_review_items()` 本身（纯数据层，不用模型）。"""

    def test_没有中间档候选时返回空(self):
        import memory_bridge as mb

        b = mb.MemoryBridge(db_path="/nonexistent/x.db", enabled=False)
        assert b.forget_review_items() == []
        assert b.review_candidates() == ("", [])

    def test_候选形状只有id和text(self, tmp_path):
        """用假 store 直接验形状：**不带 metadata、不带时间、不带分数**。"""
        import memory_bridge as mb

        class _Store:
            def list_documents(self, limit=2000):
                return [
                    {"doc_id": "d1", "text": "他说最近在忙项目；主人；忙",
                     "metadata": {"create_time": 1.0}},
                ]

        b = mb.MemoryBridge(db_path=str(tmp_path / "x.db"), enabled=True)
        b._store = _Store()
        # `forget_review_items()` 复用的是 `review_candidates()` 的候选筛选，
        # 所以这里得让 `forgetting.plan_prune()` 把 d1 判成 review。
        import forgetting as fg

        real_prompt = fg.review_prompt
        real_plan_prune = fg.plan_prune
        try:
            fg.plan_prune = lambda docs, **kw: {"keep": [], "drop": [],
                                                "review": ["d1"], "stats": {},
                                                "rows": []}
            fg.review_prompt = lambda docs, max_items=10: ("提示词", ["d1"])
            items = b.forget_review_items()
        finally:
            fg.review_prompt = real_prompt
            fg.plan_prune = real_plan_prune
        assert items == [{"id": "d1", "text": "他说最近在忙项目"}], (
            "只该给 id + 叙述句（分号后的关键词段要剥掉）"
        )
        b._store = None   # 别让 close() 去调假 store 的 close（它没有）

# --------------------------------------------------------------------------- #
# 十、死代码清理的**验收**（审计 G.1）
#
# 四条里三条是"删"，一条是"统一"。这里把**判断依据**也做成断言 ——
# 因为"删了没删"好验，"为什么这么判"不好验，而这恰恰是最容易过期的东西。
# --------------------------------------------------------------------------- #
class TestDeadCodeDecisions:
    """逐条钉住 2026-11-07 的处置结论（详见报告里的理由）。"""

    def test_mood_state成了唯一的心情读入口(self, db, frozen_clock):
        """审计 G.1 第 4 条「🔴 要查」：**不删，让它统一**。

        查证结论：真正在算衰减的只有 `mood.decayed_state()` 一份
        （`mood_prompt_line` 和 `role_speak._mood_line` 都间接调它），
        所以数值口径本来是一致的；重复的是
        「`load_state()` + `decayed_state()`」这个**组合**。

        现在：`mood_prompt_line()` 改成调 `mood_state(st)`，
        于是"读心情"只剩一个入口。
        """
        st = system._default_state()
        st["mood_value"] = -8.0
        st["mood_updated_ts"] = frozen_clock - 3 * 60
        system.save_state(st)

        # 传 st 进去和让 mood_state() 自己读，结果必须一模一样
        # （这就是"统一"的意思：同一个函数、同一个口径）
        assert system.mood_state(system.load_state()) == system.mood_state()

    def test_心情读接口真的有人调了(self, db, frozen_clock):
        """修掉"生产零调用"：`system_status()` 后台卡片里带了衰减后的新心情。"""
        st = system._default_state()
        st["mood_value"] = 2.0
        st["mood_updated_ts"] = frozen_clock - 60
        system.save_state(st)

        status = system.system_status()
        assert "mood" in status, "后台卡片没带新心情 —— mood_state() 还是零调用"
        assert set(status["mood"]) == {
            "mood_value", "mood_label", "mood_reason", "mood_updated_ts"
        }
        assert status["mood"]["mood_label"] == "愉悦"
        # 旧字段保留（前端 DMN_EMOTION 映射还读它，切换是独立一步）
        assert "emotion" in status

    def test_mood_prompt_line和mood_state口径一致(self, db, frozen_clock):
        """`mood_prompt_line()` 里的数值必须来自 `mood_state()`，不能自己再算一遍。

        注入「自己调 decayed_state」的坏实现时这条不会红（两处算法相同），
        但它会守住**换实现**的情况：以后 `mood_state()` 里加任何后处理
        （比如四舍五入、加兜底），`mood_prompt_line()` 会跟着变。
        """
        st = system._default_state()
        st["mood_value"] = -4.0
        st["mood_updated_ts"] = frozen_clock - 400 * 60
        system.save_state(st)

        line = system.mood_prompt_line()
        eff = system.mood_state()
        assert eff["mood_label"] in line, (
            f"mood_prompt_line 用的档位和 mood_state 不一致：{line!r} vs {eff!r}"
        )

    def test_is_local_trusted删了但性质还在(self):
        """审计 G.1 第 1 条（🟢 可删）：删了，但**判据本身要留下**。"""
        import migrate_channel as mc

        assert not hasattr(mc, "is_local_trusted")
        # 生产真正在用的那个函数必须还能判出 pet
        assert mc.channel_key("", local=True) == mc.CHANNEL_PET
        assert mc.channel_key("私聊") == mc.CHANNEL_QQ_PRIVATE

    def test_parse_review_thoughts删了payload还在(self):
        """审计 G.1 第 3 条：删的是那个一行转发，真正干活的函数保留。"""
        assert not hasattr(system, "_parse_review_thoughts")
        assert system._parse_review_payload('["念头"]') == (["念头"], {})
        assert system._parse_review_hint('{"thoughts": [], "profile_hint": {"a": 1}}') \
            == {"a": 1}

    def test_plan_summary留下了而且有人用(self, db):
        """审计 G.1 第 2 条（🟡 可能未来给后台卡片用）：**留**，并接上体检。

        判断依据：它算的是「这条计划还有几天临期 / 该不该催」，那正是
        `_plan_health_stats()` 里 `pending_close` 那一段在手算的东西。
        留着不用 = 同一件事两处实现（审计 G.3 的口径）；接上就只剩一处。
        """
        now = time.time()
        eid = add_plan(created_days_ago=11, attempts=1)
        stats = system._plan_health_stats()
        assert stats["open"] == 1
        assert stats["triggered"] == 1
        # 11 天前创建 + 沉默期 10 天 → 已经过了 `due`，算 pending_close
        assert stats["pending_close"] == 1, (
            f"`_plan_health_stats()` 没用 plan_summary 的 due 判据：{stats}"
        )
        # 直接对一下两边是不是同一个结论
        row = q("select ts, status from events where id=?", (eid,))[0]
        assert system.plan_rules.plan_summary(
            {"kind": "promise", "status": row[1], "ts": row[0]}, now
        )["due"] is True

    def test_计划判定函数只剩三个(self):
        """`plan.py` 里跟"成败"有关的公开入口只该有这三个。

        多一个 `judge_outcome` 式的字符串判定，就会出现"两处都在判、
        谁说了算不清楚"—— 那正是这次要纠正的设计偏差。
        """
        import plan as plan_rules

        for name in ("normalize_verdict", "parse_verdicts", "should_close"):
            assert callable(getattr(plan_rules, name, None)), name
        for gone in ("judge_outcome", "SUCCESS_WORDS", "FAILURE_WORDS",
                     "DENY_WORDS", "_has_negated"):
            assert not hasattr(plan_rules, gone), (
                f"{gone} 又回来了 —— 成败判定已经交给模型，别再留字符串启发式"
            )
