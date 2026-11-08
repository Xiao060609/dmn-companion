"""P0-5：`inner_os` 的「读过之后」语义 —— 分流落地。

问题（2026-11-03 21:40 实测）：`delivered` 之后**没有任何代码再读它**。
`mark_inner_os_state()` 只有 4 个调用点，全在 `_apply_one_suggestion()` 里，
也就只有 `persona_suggestion` 会走到 acted/declined；其余类型的终态永远是
`delivered`，一直躺到 7 天后过期。

所以 `inner_os` 的实际语义是：**一次性提醒**。
梳理期如果只管往 inner_os 扔念头，等于「每天重新想一遍同样的事」。

定下来的分工（计划 P0-5 的 B + C）：
  * **「他是什么样的人」** → people 画像（每轮都带，不会漏）
  * **「该怎么说 / 什么时候别说」** → 长期记忆（按相关度召回）
  * inner_os 保持一次性，只做**当下的提醒**
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
import memory_bridge  # noqa: E402
import system  # noqa: E402


# --------------------------------------------------------------------------- #
# B 路：profile_hint 合并进 people 画像
# --------------------------------------------------------------------------- #
def _read_owner() -> dict:
    return system._read_people_profile(system.migrate_channel.CHANNEL_PET)


def test_profile_hint合并进画像():
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": time.time(),
        "profile": {"称呼": "晓", "作息": "常忙到深夜"},
    })
    ok = system._apply_profile_hint({"作息": "一两点还在弄代码"})
    assert ok is True
    prof = _read_owner()["profile"]
    assert prof["作息"] == "一两点还在弄代码"
    assert prof["称呼"] == "晓", "没提到的字段必须原样保留（只做字段级合并）"


def test_profile_hint不许整体覆盖():
    """这是画像最容易烂掉的方式：用今天的印象抹掉一个月的观察。"""
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": time.time(),
        "profile": {
            "称呼": "晓", "作息": "常忙到深夜", "情绪基调": "平静自持",
            "在意的事": ["给角色定生日"], "互动习惯": "喜欢试探记忆",
        },
    })
    system._apply_profile_hint({"情绪基调": "这几天有点低落"})
    prof = _read_owner()["profile"]
    assert len(prof) == 5, prof
    assert prof["作息"] == "常忙到深夜"
    assert prof["情绪基调"] == "这几天有点低落"


def test_非法字段被丢弃():
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": time.time(), "profile": {"称呼": "晓"},
    })
    ok = system._apply_profile_hint({"姓名": "张三", "手机号": "13800000000"})
    assert ok is False, "画像只有 5 个固定字段，别的键一律不进"
    assert _read_owner()["profile"] == {"称呼": "晓"}


def test_空hint不写入():
    assert system._apply_profile_hint({}) is False
    assert system._apply_profile_hint(None) is False


def test_写入hint会刷新updated_ts():
    """更新过就算「新印象」，`people_prompt_line` 的降权计时要从头算。"""
    old = time.time() - 30 * 86400
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": old, "profile": {"称呼": "晓"},
    })
    system._apply_profile_hint({"作息": "改了"})
    assert _read_owner()["updated_ts"] > old + 86400


def test_hint留下查证痕迹():
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": time.time(), "profile": {"称呼": "晓"},
    })
    system._apply_profile_hint({"作息": "x"})
    h = _read_owner().get("review_hints")
    assert isinstance(h, list) and h
    assert h[-1]["fields"] == ["作息"]


def test_在意的事是列表时保留列表():
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": time.time(), "profile": {},
    })
    system._apply_profile_hint({"在意的事": ["讨厌人群", "讨厌愚蠢"]})
    assert _read_owner()["profile"]["在意的事"] == ["讨厌人群", "讨厌愚蠢"]


# --------------------------------------------------------------------------- #
# C 路：分寸类念头沉淀进长期记忆
# --------------------------------------------------------------------------- #
#: 假 embedding 维度，与 `memory/store.DEFAULT_DIM` 一致。
DIM = 1024


class FakeEmbedding:
    """不花 API 额度的向量化实现（照抄 `test_memory_bridge.py` 的做法）。

    ⚠ 2026-11-04 修的一条**真问题**：本文件原来直接
    `memory_bridge.MemoryBridge(db_path=...)`，而 `MemoryBridge.store()` 不传
    `embedding` 时，`store.py:99` 会默认建一个**真的** `DashScopeEmbedding`，
    它读 `gateway/secrets.json` 里的真 key、打真实阿里云端点。

    后果：跑一次这个文件 = 用主人的真金白银打一次 DashScope embedding，
    而且测试文本「他不喜欢被追问」会离开本机发给阿里云；网络抖动/欠费还会让
    这条用例红 —— 也就是说测试基线依赖外网可用性。

    conftest 的隔离管得住**文件路径**（库指到 tmp，不污染真实记忆库），
    管不住**出网点**。所以必须在用例里把 embedding 换掉。
    """

    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim

    def get_dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> list[float]:
        import numpy as np

        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.random(self._dim).astype(np.float32)
        v /= np.linalg.norm(v)
        return v.tolist()

    async def get_embedding(self, text: str) -> list[float]:
        return self._vec(text)

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def get_embeddings_batch(self, texts, batch_size: int = 32):
        return await self.get_embeddings(texts)


@pytest.fixture()
def offline_memory(monkeypatch):
    """把 `open_store` 换成本地假 embedding —— 整个文件都不许联网。"""
    import store as store_mod

    real_open = store_mod.open_store

    def fake_open(db_path, index_path=None, dim=DIM, embedding=None):
        return real_open(
            db_path=db_path,
            index_path=index_path,
            dim=DIM,
            embedding=FakeEmbedding(),
        )

    monkeypatch.setattr(store_mod, "open_store", fake_open)
    return fake_open


def _memory_rows() -> list[dict]:
    """直接读库，确认真的落盘了（不走召回，避免检索细节干扰断言）。"""
    import sqlite3 as _sq
    path = Path(system.SYSTEM.get("long_memory_db_path") or "")
    if not path.exists():
        return []
    conn = _sq.connect(str(path))
    try:
        try:
            rows = conn.execute("select text, metadata from documents").fetchall()
        except _sq.Error:
            return []
    finally:
        conn.close()
    out = []
    for text, meta in rows:
        try:
            m = json.loads(meta or "{}")
        except ValueError:
            m = {}
        out.append({"text": text, "meta": m})
    return out


def test_分寸念头写进长期记忆(offline_memory):
    bridge = memory_bridge.MemoryBridge(
        db_path=system.SYSTEM.get("long_memory_db_path"), enabled=True
    )
    n = bridge.write_thoughts(
        ["他不喜欢被追问，问了就沉默"], system.migrate_channel.CHANNEL_PET
    )
    assert n == 1
    bridge.close()

    rows = _memory_rows()
    assert len(rows) == 1
    assert "不喜欢被追问" in rows[0]["text"]
    # 形状必须是「叙述句；关键词1；关键词2」，否则 FTS 侧召不回来
    assert "；" in rows[0]["text"]
    assert rows[0]["meta"]["memory_origin"] == "daily_review"
    assert rows[0]["meta"]["session_id"] == "owner"


def test_空念头不写():
    bridge = memory_bridge.MemoryBridge(
        db_path=system.SYSTEM.get("long_memory_db_path"), enabled=True
    )
    assert bridge.write_thoughts([], "pet") == 0
    assert bridge.write_thoughts(["", "   "], "pet") == 0
    bridge.close()
    assert _memory_rows() == []


def test_记忆总开关关掉时不写():
    bridge = memory_bridge.MemoryBridge(
        db_path=system.SYSTEM.get("long_memory_db_path"), enabled=False
    )
    assert bridge.write_thoughts(["x"], "pet") == 0
    bridge.close()


# --------------------------------------------------------------------------- #
# 端到端分流：一次梳理把三类产出放到三个地方
# --------------------------------------------------------------------------- #
@pytest.fixture()
def review_on(monkeypatch):
    cfg = {
        "enabled": True, "earliest_hour": 0, "day_start_hour": 6,
        "idle_after_minutes": 0, "min_absence_hours": 0,
        "daily_model_calls_cap": 5,
        "budget": {"ruminate": 1, "prospect": 1,
                   "theory_of_mind": 1, "self_narrative": 1},
    }
    monkeypatch.setitem(system.SYSTEM, "daily_review", cfg)
    return cfg


def test_梳理期三类产出各归各位(review_on, monkeypatch, offline_memory):
    """反刍的分寸念头 → 长期记忆；心智理论的 hint → 画像；念头 → inner_os。"""
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','今天好累','chat',?,'pet')", (time.time() - 600,),
        )
        conn.commit()
    system._write_people_profile(system.migrate_channel.CHANNEL_PET, {
        "channel": "pet", "updated_ts": time.time(), "profile": {"称呼": "晓"},
    })
    monkeypatch.setattr(system, "_system_key", lambda: "k")

    def _fake(msgs, **kw):
        # 从 prompt 里认出是哪个活动。
        # ⚠ 必须匹配**中文标题**（【反刍】/【心智理论】）：
        # 素材块里含有 `"activity": "ruminate"` 这种英文字段，
        # 用英文名匹配会把所有活动都误判成同一个。
        blob = json.dumps(msgs, ensure_ascii=False)
        if "【心智理论】" in blob:
            return json.dumps({
                "thoughts": ["他可能在硬撑着不说"],
                "profile_hint": {"情绪基调": "这几天偏低落"},
            }, ensure_ascii=False), {}, None
        if "【反刍】" in blob:
            return '["以后别追问，等他自己说"]', {}, None
        return "[]", {}, None

    monkeypatch.setattr(system, "call_system", _fake)
    r = system.daily_review()

    # ① inner_os：所有念头都在
    os_items = [i for i in system._read_inner_os() if i.get("source") == "daily_review"]
    assert os_items
    # ② 画像：hint 落了
    assert _read_owner()["profile"]["情绪基调"] == "这几天偏低落"
    assert _read_owner()["profile"]["称呼"] == "晓"
    # ③ 长期记忆：反刍那条落了
    assert r["memory_written"] >= 1
    texts = " ".join(x["text"] for x in _memory_rows())
    assert "别追问" in texts


def test_自我叙事不写长期记忆(review_on, monkeypatch):
    """「我是谁」属于人格层，不该当事实记忆召回。"""
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','在吗','chat',?,'pet')", (time.time() - 600,),
        )
        conn.commit()
    review_on["budget"] = {"ruminate": 0, "prospect": 0,
                           "theory_of_mind": 0, "self_narrative": 1}
    monkeypatch.setitem(system.SYSTEM, "daily_review", review_on)
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(system, "call_system",
                        lambda *a, **k: ('["我还是我"]', {}, None))

    r = system.daily_review()
    assert r["memory_written"] == 0
    assert _memory_rows() == []


def test_inner_os有配额上限(review_on, monkeypatch):
    """**2026-11-03 22:35 实测踩到的坑**。

    第一次真跑就产出 12 条（四类活动各 3 条），而 `INNER_OS_MAX=20`、
    当时队列里已有 5 条 —— 一次梳理把队列挤到 17/20，体检立刻报「积压接近上限」。
    梳理期不该垄断这个队列：它是「当下的提醒」，一天能进 prompt 的本来就只有几条。

    超出的部分不丢：C 路会沉淀进长期记忆，B 路进了画像。
    """
    review_on["inner_os_quota_per_review"] = 3
    monkeypatch.setitem(system.SYSTEM, "daily_review", review_on)
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','在吗','chat',?,'pet')", (time.time() - 600,),
        )
        conn.commit()
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    # 每个活动都产 3 条 → 四类活动共 12 条
    monkeypatch.setattr(
        system, "call_system",
        lambda *a, **k: ('["念头一","念头二","念头三"]', {}, None),
    )

    before = len([i for i in system._read_inner_os() if i.get("source") == "daily_review"])
    r = system.daily_review()
    after = [i for i in system._read_inner_os() if i.get("source") == "daily_review"]
    assert len(after) - before <= 3, f"进 inner_os 超过配额：{len(after) - before}"
    assert r["thoughts"] == 12, "产出的念头总数照实统计（只是不全进 inner_os）"


def test_配额外的念头仍然沉淀进长期记忆(review_on, monkeypatch, offline_memory):
    """inner_os 装不下 ≠ 白想了。C 路拿全部正文。

    ⚠ 2026-11-06 改：这里原来用「甲」「乙」「丙」这种占位词，而按新的
    `plan/DMN审核.md` 1.9「认知录入准则」，那种词既没有主谓宾、也不可验证，
    会被判成噪音**不落库**。用例要验的是"配额只限制 inner_os、不限制记忆沉淀"，
    所以材料必须换成真正符合准入标准的句子——否则测的就不是配额，而是准入。
    """
    review_on["inner_os_quota_per_review"] = 1
    review_on["budget"] = {"ruminate": 1, "prospect": 0,
                           "theory_of_mind": 0, "self_narrative": 0}
    monkeypatch.setitem(system.SYSTEM, "daily_review", review_on)
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','在吗','chat',?,'pet')", (time.time() - 600,),
        )
        conn.commit()
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(
        system, "call_system",
        lambda *a, **k: (
            '["他喜欢在深夜聊天","他住在上海","他讨厌人群"]', {}, None
        ),
    )

    r = system.daily_review()
    assert r["memory_written"] == 3, "配额只限制 inner_os，不限制记忆沉淀"


def test_场景推断不落库只进inner_os(review_on, monkeypatch, offline_memory):
    """`plan/DMN审核.md` 1.9 的新架构：**想法不落库**。

    旧代码把反刍/预演的全部产出都写进记忆库，而那两个活动的 prompt 明确要求
    产出场景推断（反刍的示例就写着「他今天话说得少，可能累了」——这是想法，
    不是认知）。按新架构，这类内容不该进长期记忆，只走 inner_os 用完即销毁。

    这条是把"新架构"钉住的：以后谁把 `cognition_only` 关掉或改成默认不判，
    它就会红。
    """
    review_on["budget"] = {"ruminate": 1, "prospect": 0,
                           "theory_of_mind": 0, "self_narrative": 0}
    monkeypatch.setitem(system.SYSTEM, "daily_review", review_on)
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','在吗','chat',?,'pet')", (time.time() - 600,),
        )
        conn.commit()
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(
        system, "call_system",
        lambda *a, **k: ('["他今天话说得少，可能累了"]', {}, None),
    )

    r = system.daily_review()
    assert r["memory_written"] == 0, "场景推断（带推断词）不该进长期记忆"
    assert _memory_rows() == []
    # 但念头本身仍然进了 inner_os —— 不落库 ≠ 白想
    assert r["thoughts"] >= 1


def test_关掉memory_sink后不写长期记忆(review_on, monkeypatch):
    review_on["budget"] = {"ruminate": 1, "prospect": 0,
                           "theory_of_mind": 0, "self_narrative": 0}
    review_on["memory_sink_enabled"] = False
    monkeypatch.setitem(system.SYSTEM, "daily_review", review_on)
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','在吗','chat',?,'pet')", (time.time() - 600,),
        )
        conn.commit()
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(system, "call_system", lambda *a, **k: ('["念头"]', {}, None))

    r = system.daily_review()
    assert r["memory_written"] == 0
    assert _memory_rows() == []


# --------------------------------------------------------------------------- #
# 解析 profile_hint
# --------------------------------------------------------------------------- #
def test_解析对象形状的thoughts和hint():
    raw = json.dumps({"thoughts": ["他累了"], "profile_hint": {"作息": "晚睡"}},
                     ensure_ascii=False)
    texts, hint = system._parse_review_payload(raw)
    assert texts == ["他累了"]
    assert hint == {"作息": "晚睡"}


def test_解析对象形状时hint可缺省():
    raw = json.dumps({"thoughts": ["他累了"]}, ensure_ascii=False)
    texts, hint = system._parse_review_payload(raw)
    assert texts == ["他累了"]
    assert hint == {}


def test_只有hint没有thoughts也算有产出():
    """心智理论可能只想更新认知，不想留念头 —— 那也是有效产出。"""
    raw = json.dumps({"thoughts": [], "profile_hint": {"作息": "晚睡"}},
                     ensure_ascii=False)
    texts, hint = system._parse_review_payload(raw)
    assert texts == []
    assert hint == {"作息": "晚睡"}


def test_解析hint单独函数():
    raw = json.dumps({"thoughts": [], "profile_hint": {"作息": "晚睡"}},
                     ensure_ascii=False)
    assert system._parse_review_hint(raw) == {"作息": "晚睡"}
    assert system._parse_review_hint('["纯数组"]') == {}
