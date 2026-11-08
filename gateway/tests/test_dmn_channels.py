"""DMN 生产链路：多渠道事件抽取 + 渠道隔离 + 独立水位 + 巩固冷启动。

这四条是「跑通生产链路」的验收线（对照 `plan\小鱼-总计划.md` 第 2.2 节）：

1. 多渠道路由：pet / 主人私聊 / 有群名的群聊才抽，陌生私聊和 qq_unknown 不抽；
   `events.channel` 必须是**该组对话的真实渠道**（群聊要带全群名）。
2. 隔离：群聊产生的内心OS 不能在私聊/桌宠的 `consume_inner_os()` 里出现；
   主人（pet + 主人私聊）互通。
3. 水位独立：一个渠道处理完，另一个渠道的积压不受影响。
4. 冷启动：`_consolidate_loop()` 首轮不按 `consolidate_s` 整整睡一个周期。
5. 附属：deferred_care → schedules → 内心OS 的渠道要跟着事件走；
   旧的单值 `last_consolidate_id` 能迁成 pet 渠道水位。

所有用例都靠 `tmp_path` + monkeypatch 隔离 DATA_DIR / DB_PATH，绝不碰生产库。
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

GROUP = "qq_group:示例群"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DATA_DIR + 建一个干净的库（turns / events / schedules）。"""
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(tmp_path / "system_state.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    monkeypatch.setattr(system, "INNER_OS_PATH", str(tmp_path / "inner_os.json"))
    monkeypatch.setattr(system, "PEOPLE_DIR", str(tmp_path / "people"))
    db = str(tmp_path / "gateway.db")
    monkeypatch.setattr(gateway, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "create table turns(id integer primary key autoincrement, session text, role text,"
            " content text, path text, ts real, channel text)"
        )
        conn.execute(
            "create table events(id integer primary key autoincrement, ts real, kind text,"
            " subject text, payload text, status text, due_ts real, attempts integer, channel text)"
        )
        conn.execute(
            "create table schedules(id integer primary key autoincrement, due_ts real, kind text,"
            " event_id integer, note text, fired integer default 0)"
        )
        conn.commit()
    return db


def _add(db, channel, role, content, path="chat", ts=None):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "insert into turns(session, role, content, path, ts, channel) values(?,?,?,?,?,?)",
            (gateway.SHARED_SESSION, role, content, path, ts or time.time(), channel),
        )
        conn.commit()


def _channels_of_events(db):
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute("select channel from events order by id")]


def _audit_records() -> list[dict]:
    try:
        with open(system.SYSTEM_AUDIT_PATH, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


# ---------------------------------------------------------------- 1. 多渠道路由
def test_只抽能判定主体的渠道且事件渠道各自正确(env, monkeypatch):
    """pet / 主人私聊 / 群聊三类要各抽各的，陌生私聊和 qq_unknown 一条都不抽。"""
    db = env
    # 桌宠：主人直接说
    _add(db, "pet", "user", "主人: 今天有点累")
    _add(db, "pet", "assistant", "那早点睡。")
    # 主人私聊：带 `[私聊] 主人:` 前缀
    _add(db, "qq_private", "user", "[私聊] 主人: 我在忙流程优化")
    _add(db, "qq_private", "assistant", "好，你先忙。")
    # 陌生人私聊 / 插件注入：判不出主体，绝不抽
    _add(db, "qq_private", "user", "[私聊] 张三: 在吗在吗")
    _add(db, "qq_private", "user", "[系统任务：主动对话]\n你被授权发起一次主动消息")
    # 群聊：标签带群名
    _add(db, GROUP, "user", "[群聊·示例群] 黑崎小雪: 今天好冷")
    _add(db, GROUP, "assistant", "那你多穿点。")
    # 取不到来源标签 / 没群名的群：不抽
    _add(db, "qq_unknown", "user", "[系统任务：主动对话] 说点什么")
    _add(db, "qq_group", "user", "[群聊] 没群名")

    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    seen: list[list[dict]] = []

    def fake_extract(turns):
        seen.append(list(turns))
        return [{"kind": "promise", "subject": "", "evidence": "测试事件"}]

    monkeypatch.setattr(system, "_extract_events", fake_extract)
    # 巩固那一步不产生观点，避免把水搅浑（这里只验路由和渠道落库）
    monkeypatch.setattr(system, "call_system", lambda msgs, **kw: (None, {}, None))

    system._consolidate_body()

    assert len(seen) == 3, f"应该只有 pet / 主人私聊 / 群聊 三次抽取，实际 {len(seen)} 次"
    assert _channels_of_events(db) == ["pet", "qq_private", GROUP]

    # 抽进去的 turns 里不能有陌生人 / 插件注入 / 没群名的群
    joined = "\n".join(str(t["content"]) for batch in seen for t in batch)
    assert "张三" not in joined
    assert "系统任务" not in joined
    assert "[群聊] 没群名" not in joined
    # 主人私聊那批里，主人的话和她的回复都在
    private_batch = [b for b in seen if any("[私聊]" in str(t["content"]) for t in b)][0]
    assert len(private_batch) == 2
    assert "流程优化" in str(private_batch[0]["content"])


def test_群聊事件渠道保留完整群名不截断(env, monkeypatch):
    """群聊抽出来的事件被标成 pet 或截断成 qq_group，隔离就失效了。"""
    db = env
    _add(db, GROUP, "user", "[群聊·示例群] 龙: 我只是路过")
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    monkeypatch.setattr(
        system, "_extract_events",
        lambda turns: [{"kind": "promise", "subject": "", "evidence": "路过"}],
    )
    monkeypatch.setattr(system, "call_system", lambda msgs, **kw: (None, {}, None))

    system._consolidate_body()
    assert _channels_of_events(db) == [GROUP], "群聊事件必须记完整渠道名"


def test_没增量的渠道不调模型(env, monkeypatch):
    """渠道没增量就一次模型都不调——否则每轮都在花钱跑空。"""
    db = env
    _add(db, "pet", "user", "主人: 只说过一句")
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 8)  # 门槛没够
    called: list[str] = []
    monkeypatch.setattr(system, "call_system", lambda msgs, **kw: called.append("x") or (None, {}, None))
    monkeypatch.setattr(system, "_extract_events", lambda turns: called.append("extract") or [])

    system._consolidate_body()
    assert called == [], "增量不够时不该产生任何模型调用"


# ---------------------------------------------------------------- 2. 渠道隔离
def test_群聊的想法不会出现在私聊和桌宠里(env):
    system.push_inner_os([{"type": "insight", "text": "群里那点事", "channel": GROUP}])
    system.push_inner_os([{"type": "insight", "text": "桌宠里那点事", "channel": "pet"}])

    got = [i["text"] for i in system.consume_inner_os("qq_private")]
    assert got == ["桌宠里那点事"], "主人私聊只能看到主人自己的（pet 归一到 owner）"

    got_pet = [i["text"] for i in system.consume_inner_os("pet")]
    assert got_pet == [], "同样的 owner 条目已经投递过，不该重复投"

    got_group = [i["text"] for i in system.consume_inner_os(GROUP)]
    assert got_group == ["群里那点事"], "群聊的条目只能回这个群"


def test_群聊的想法不会漏进桌宠(env):
    system.push_inner_os([{"type": "insight", "text": "群里的秘密", "channel": GROUP}])
    assert system.consume_inner_os("pet") == []
    pending = [i for i in system._read_inner_os() if i.get("state") == "pending"]
    assert [i["text"] for i in pending] == ["群里的秘密"], "取不到不等于被消费掉"


def test_全局条目任何渠道都能看到(env):
    """旧 inner_os.json 里的条目没有 channel，必须当全局项兼容。"""
    system.push_inner_os([{"type": "insight", "text": "全局想法"}])
    assert [i["text"] for i in system.consume_inner_os("qq_private")] == ["全局想法"]
    system.push_inner_os([{"type": "insight", "text": "全局想法2"}])
    assert [i["text"] for i in system.consume_inner_os(GROUP)] == ["全局想法2"]
    system.push_inner_os([{"type": "insight", "text": "全局想法3"}])
    assert [i["text"] for i in system.consume_inner_os("pet")] == ["全局想法3"]


def test_旧数组格式的inner_os当全局项(env, monkeypatch):
    """线上 inner_os.json 现在是 `[]` / 旧数组条目，读进来必须当全局项，不是丢掉。"""
    path = Path(system.INNER_OS_PATH)
    path.write_text(
        json.dumps(
            [{"type": "insight", "text": "很久以前的想法", "ts": time.time()}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert [i["text"] for i in system.consume_inner_os(GROUP)] == ["很久以前的想法"]


# ---------------------------------------------------------------- 3. 水位独立
def test_一个渠道处理完不影响另一个渠道的增量(env, monkeypatch):
    db = env
    for i in range(10):
        _add(db, "pet", "user", f"主人: 第{i}句")
    for i in range(10):
        _add(db, GROUP, "user", f"[群聊·示例群] 龙: 群第{i}句")

    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    monkeypatch.setattr(system, "_extract_events", lambda turns: [])
    monkeypatch.setattr(system, "update_people_profile", lambda ch, turns: False)
    monkeypatch.setattr(
        system, "call_system",
        lambda msgs, **kw: (None, {}, [{"id": "1", "function": {
            "name": "system_consolidate",
            "arguments": json.dumps({"insights": [{"text": "一条观点", "confidence": 0.8}]}),
        }}]),
    )

    system._consolidate_body()
    wm1 = system.load_state()["consolidate_wm"]
    assert set(wm1) == {"pet", GROUP}
    assert wm1["pet"] > 0 and wm1[GROUP] > 0

    # 只有 pet 有新对话：第二轮的 pet 水位前进，群聊原封不动
    for i in range(3):
        _add(db, "pet", "user", f"主人: 新第{i}句")
    system._consolidate_body()
    wm2 = system.load_state()["consolidate_wm"]
    assert wm2[GROUP] == wm1[GROUP], "群聊没增量，水位不该动"
    assert wm2["pet"] > wm1["pet"], "pet 有新增量，水位该前进"


def test_旧单值水位迁成渠道水位(env):
    """升级前只有一个 last_consolidate_id，升级后要当作 pet 的水位起点。"""
    Path(system.SYSTEM_STATE_PATH).write_text(
        json.dumps({"last_consolidate_id": 42}, ensure_ascii=False), encoding="utf-8"
    )
    st = system.load_state()
    assert st["consolidate_wm"] == {"pet": 42}


# ---------------------------------------------------------------- 4. 冷启动
class _Stop(Exception):
    """叫停 `_consolidate_loop` 的死循环（它本来是 while True）。"""


def test_冷启动首轮不等一整个巩固周期(monkeypatch):
    """以前是「先睡满 consolidate_s 再干活」，周期还是 86400 → 一天内一次都不跑。"""
    seq: list[tuple[str, float]] = []

    def fake_sleep(sec):
        seq.append(("sleep", round(float(sec))))
        if len([x for x in seq if x[0] == "sleep"]) >= 2:
            raise _Stop

    monkeypatch.setitem(system.SYSTEM, "consolidate_s", 3600)
    monkeypatch.setitem(system.SYSTEM, "consolidate_boot_delay_s", 90)
    monkeypatch.setattr(system.time, "sleep", fake_sleep)
    monkeypatch.setattr(system, "consolidate", lambda: seq.append(("consolidate", 0)))

    with pytest.raises(_Stop):
        system._consolidate_loop()

    names = [x[0] for x in seq]
    assert names == ["sleep", "consolidate", "sleep"], "必须先短等、再干活、再按周期等"
    assert 30 <= seq[0][1] <= 120, "首轮等待要远小于周期，但要给网关启动留余量"
    assert seq[2][1] >= 3600 * 0.8, "之后才按 consolidate_s 周期"


def test_巩固线程默认周期是一小时(monkeypatch):
    """config 里配的 3600 要真的进到线程里（以前是 86400）。"""
    assert float(system.SYSTEM.get("consolidate_s")) == 3600
    monkeypatch.setattr(system, "_heartbeat_loop", lambda: None)
    monkeypatch.setattr(system, "_persona_apply_worker", lambda: None)
    monkeypatch.setattr(system, "_consolidate_loop", lambda: None)
    names = [t.name for t in system.start()]
    assert "system-consolidate" in names


# ---------------------------------------------------------------- 5. 定时任务渠道
def test_巩固失败不前进水位也不重复写事件(env, monkeypatch):
    """水位只在「抽事件 + 蒸馏观点」都成功后才走；中间失败重读同一段，事件不能翻倍。"""
    db = env
    for i in range(10):
        _add(db, "pet", "user", f"主人: 第{i}句")
    monkeypatch.setitem(system.SYSTEM, "consolidate_min_new_turns", 1)
    monkeypatch.setattr(system, "update_people_profile", lambda ch, turns: False)
    monkeypatch.setattr(
        system, "_extract_events",
        lambda turns: [{"kind": "promise", "subject": "", "evidence": "同一件事"}],
    )
    monkeypatch.setattr(system, "call_system", lambda msgs, **kw: (None, {}, None))  # 模型没回

    system._consolidate_body()
    assert system.load_state()["consolidate_wm"].get("pet", 0) == 0, "模型没回，水位不该前进"
    assert _channels_of_events(db) == ["pet"]

    system._consolidate_body()  # 水位没动 → 重读同一段
    assert _channels_of_events(db) == ["pet"], "重读同一段对话不该把事件复制一份"


def test_群渠道的deferred_care只记事实不排班(env):
    """群渠道没有投递通道 → 延后关心不排班，状态直接 closed 而不是 deferred。

    事实本身要留下（「群里他说过两天再看」是有价值的记忆），只是不产生动作。
    """
    db = env
    system._save_events([{
        "kind": "deferred_care", "subject": "群里某人", "evidence": "说过两天再看",
        "due_ts": time.time() + 3600, "approach_hint": "轻一点",
    }], GROUP)

    with sqlite3.connect(db) as conn:
        row = list(conn.execute("select status, payload, channel, due_ts from events"))[0]
        assert list(conn.execute("select count(*) from schedules"))[0][0] == 0, "群渠道不许排班"
    assert row[0] == "closed", "不排班就该是 closed，不是 deferred"
    assert row[2] == GROUP
    assert row[3] is None, "不排班的事件不该带 due_ts（否则读 events 的人以为还欠一件事）"
    assert "group_no_delivery" in row[1]
    assert "group_no_delivery" in json.loads(row[1])["skip_reason"]
    acts = [a for a in _audit_records() if a.get("event") == "deferred_care_skipped"]
    assert acts and acts[0]["channel"] == GROUP and acts[0]["reason"] == "group_no_delivery"


def test_主人渠道的deferred_care照旧排班(env):
    """owner 渠道行为不变：deferred + schedules +1 + 到点能捞起。"""
    db = env
    system._save_events([{
        "kind": "deferred_care", "subject": "主人", "evidence": "说别管",
        "due_ts": time.time() - 1, "approach_hint": "轻一点",
    }], "pet")
    with sqlite3.connect(db) as conn:
        assert list(conn.execute("select status from events"))[0][0] == "deferred"
        assert list(conn.execute("select count(*) from schedules"))[0][0] == 1
    assert system._fire_due_schedules() == 1, "owner 渠道到点必须能捞起"
    assert [i["channel"] for i in system._read_inner_os()] == ["owner"]


def test_主人的定时任务到点投递且带渠道(env):
    """deferred_care 来自桌宠，到点提醒回到桌宠（owner 归一键），不带别的渠道内容。"""
    db = env
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "insert into events(ts, kind, subject, payload, status, due_ts, attempts, channel)"
            " values(?,?,?,?,?,?,0,?)",
            (time.time() - 7200, "deferred_care", "今天好冷", "{}", "deferred",
             time.time() - 60, "pet"),
        )
        conn.execute(
            "insert into schedules(due_ts, kind, event_id, note, fired) values(?,?,?,?,0)",
            (time.time() - 60, "recheck_care", cur.lastrowid, "冷就多穿点"),
        )
        conn.commit()
        conn.execute(
            "insert into turns(session, role, content, path, ts, channel) values(?,?,?,?,?,?)",
            (gateway.SHARED_SESSION, "user", "主人: 今天确实冷", "chat", time.time(), "pet"),
        )
        conn.commit()

    assert system._fire_due_schedules() == 1
    items = system._read_inner_os()
    assert len(items) == 1
    assert items[0]["channel"] == "owner", "pet 归一到 owner（主人跨场合）"
    ctx = items[0].get("context") or {}
    assert "今天确实冷" in json.dumps(ctx, ensure_ascii=False), "要带上当前状态供角色层重判"


def test_群渠道的定时任务不投递只跳过(env):
    """群渠道没有投递通道：老数据里的群排班不许再制造 dead pending。"""
    db = env
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "insert into events(ts, kind, subject, payload, status, due_ts, attempts, channel)"
            " values(?,?,?,?,?,?,0,?)",
            (time.time() - 7200, "deferred_care", "今天好冷", "{}", "closed",
             time.time() - 60, GROUP),
        )
        conn.execute(
            "insert into schedules(due_ts, kind, event_id, note, fired) values(?,?,?,?,0)",
            (time.time() - 60, "recheck_care", cur.lastrowid, "冷就多穿点"),
        )
        conn.commit()

    assert system._fire_due_schedules() == 0, "群渠道不投递，不算 fired"
    assert system._read_inner_os() == [], "不许留下永远 pending 的群条目"
    with sqlite3.connect(db) as conn:
        assert list(conn.execute("select fired from schedules"))[0][0] == 1, "要标记 fired，别反复捞"
    skipped = [a for a in _audit_records()
               if a.get("event") == "schedules_skipped" and a.get("channel") == GROUP]
    assert skipped and skipped[0]["reason"] == "group_no_delivery"
