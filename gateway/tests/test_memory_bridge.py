"""DMN 事实 → 长期记忆库 的桥接测试（阶段 2）。

钉住四件事：
1. **文本形状**：`叙述句；关键词` —— 直接塞原文会被稀释（插件 13 条空壳的教训）
2. **会话键归一**：pet + 主人私聊 → `owner`（同一个人，可跨场合）；
   群聊保留全名（绝不跨群）
3. **只写长期事实**：`observation`/`promise` 进库；`care_rejected`/`deferred_care`
   是状态机的事，不进（它们有时效性，过期就该忘）
4. **失败不抛穿**：记忆库打不开时巩固不能跟着死
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = GATEWAY_DIR / "memory"
for p in (str(GATEWAY_DIR), str(MEMORY_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import memory_bridge  # noqa: E402

DIM = 1024


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

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def get_embeddings_batch(self, texts, batch_size: int = 32):
        return await self.get_embeddings(texts)


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """一个用假 embedding 的记忆桥（不花 API 额度）。"""
    import store as store_mod

    real_open = store_mod.open_store

    def fake_open(db_path, index_path=None, dim=DIM, embedding=None):
        return real_open(
            db_path=db_path, index_path=index_path, dim=DIM, embedding=FakeEmbedding()
        )

    monkeypatch.setattr(store_mod, "open_store", fake_open)
    b = memory_bridge.MemoryBridge(db_path=str(tmp_path / "记忆.db"))
    yield b
    b.close()


# --------------------------------------------------------------------------- #
# 1. 文本形状
# --------------------------------------------------------------------------- #
def test_文本形状是叙述句加分号关键词():
    text = memory_bridge.build_memory_text(
        {"kind": "observation", "subject": "主人", "evidence": "常熬夜到两三点"}
    )
    assert "；" in text, "必须有分号分隔，前面叙述句后面关键词"
    head = text.split("；", 1)[0]
    assert head == "主人：常熬夜到两三点", "叙述句要能读成一句回忆"


def test_promise类用答应过措辞():
    text = memory_bridge.build_memory_text(
        {"kind": "promise", "subject": "主人", "evidence": "明天把 skill 系统弄好"}
    )
    assert "答应过" in text
    assert text.startswith("主人答应过")


def test_没有evidence返回空串():
    assert memory_bridge.build_memory_text({"kind": "observation", "evidence": ""}) == ""


def test_关键词去重():
    text = memory_bridge.build_memory_text(
        {
            "kind": "observation",
            "subject": "主人",
            "evidence": "常熬夜",
            "approach_hint": "常熬夜",  # 和 evidence 重复
        }
    )
    # 同一段文字不该在关键词区出现两次
    tail = text.split("；", 1)[1]
    assert tail.count("常熬夜") == 1


# --------------------------------------------------------------------------- #
# 2. 会话键归一
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("channel", ["pet", "qq_private"])
def test_主人渠道归一成owner(channel):
    """桌宠和主人私聊是同一个人——桌宠记的事 QQ 私聊要能想起来。"""
    assert memory_bridge.memory_session_key(channel) == "owner"


def test_群聊保留全名不跨群():
    assert (
        memory_bridge.memory_session_key("qq_group:示例群") == "qq_group:示例群"
    )


def test_空渠道回落owner():
    assert memory_bridge.memory_session_key("") == "owner"


# --------------------------------------------------------------------------- #
# 3. 只写长期事实
# --------------------------------------------------------------------------- #
def test_只写observation和promise(bridge):
    events = [
        {"kind": "observation", "subject": "主人", "evidence": "常熬夜"},
        {"kind": "promise", "subject": "主人", "evidence": "明天弄好 skill"},
        {"kind": "care_rejected", "subject": "主人", "evidence": "别管我"},
        {"kind": "deferred_care", "subject": "主人", "evidence": "过两天再说"},
    ]
    n = bridge.write_events(events, "pet")
    assert n == 2, "状态机类型不该进长期记忆"
    assert bridge.store().count() == 2


def test_写入后能召回(bridge):
    bridge.write_events(
        [{"kind": "observation", "subject": "主人", "evidence": "讨厌人群和应酬"}],
        "pet",
    )
    hits = bridge.recall("主人讨厌什么", "qq_private", top_k=3)
    assert hits, "主人在私聊里应该能想起桌宠记的事（owner 归一）"
    assert any("人群" in str(h.get("text")) for h in hits)


def test_群聊记忆不串到主人私聊(bridge):
    bridge.write_events(
        [{"kind": "observation", "subject": "群友", "evidence": "群里说的秘密"}],
        "qq_group:某群",
    )
    assert bridge.recall("群里说的秘密", "qq_private", top_k=3) == [], "跨群串味"


def test_本群记忆能召回(bridge):
    bridge.write_events(
        [{"kind": "observation", "subject": "群友", "evidence": "群里说的秘密"}],
        "qq_group:某群",
    )
    hits = bridge.recall("群里说的秘密", "qq_group:某群", top_k=3)
    assert hits


# --------------------------------------------------------------------------- #
# 4. 失败不抛穿
# --------------------------------------------------------------------------- #
def test_库打不开时返回0不抛(tmp_path):
    """记忆库坏了不能拖死巩固。"""
    b = memory_bridge.MemoryBridge(db_path=str(tmp_path / "坏路径" / "\0bad" / "x.db"))
    try:
        assert b.write_events([{"kind": "observation", "evidence": "x"}], "pet") == 0
        assert b.recall("x", "pet") == []
    finally:
        b.close()


def test_开关关掉后是空操作(tmp_path):
    b = memory_bridge.MemoryBridge(db_path=str(tmp_path / "x.db"), enabled=False)
    try:
        assert b.write_events([{"kind": "observation", "evidence": "x"}], "pet") == 0
        assert b.recall("x", "pet") == []
        assert b.store() is None
    finally:
        b.close()


# --------------------------------------------------------------------------- #
# 5. 召回块的措辞
# --------------------------------------------------------------------------- #
def test_召回块只留叙述句不露关键词():
    block = memory_bridge.build_recall_block(
        [{"text": "主人：讨厌人群和应酬；主人；讨厌人群"}]
    )
    assert "讨厌人群和应酬" in block
    # 关键词段不该原样吐出来（读起来像检索结果，不像回忆）
    assert block.count("讨厌人群") == 1
    assert "这是你记得的事" in block or "记得" in block


def test_空命中返回空串():
    assert memory_bridge.build_recall_block([]) == ""
    assert memory_bridge.build_recall_block([{"text": ""}]) == ""
