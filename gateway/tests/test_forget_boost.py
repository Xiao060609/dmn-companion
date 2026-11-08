"""遗忘的「拉高稳定性」必须真的落盘（P0-6，2026-11-07 审计）。

现场
----
`forgetting.plan_prune()` 会给一部分「永不删」条目算出
`boost_to_stability_days`（拉高稳定性，下次扫描 R 就回到门槛上），
`stats["boosted"]` 也在计数。但 `memory_bridge.forget_plan()` 原来
**只读计数、从不写回 metadata** —— 于是：

  * 「不删」是生效的；
  * 「越久越稳固」是空转的 —— 下次扫描 R 还是低，每次都重算一遍。

净效果：这条保护只剩半条。当前生产 `boosted` 恒 0（记忆都还年轻）
所以暂未出事，但那是**假保护**。

⚠ 先纠正清单 06 的描述（以代码为准）
------------------------------------
清单写「对 `keep_forever=True` 且 `R <= drop_below` 的条目算出 boost」。
**实测不符**（见 `audit\\_probe_boost_trigger.py`）：

  * 代码分支是 `else:`，条件是 **R < keep_above**，不只是 ≤ drop_below；
  * `kind=promise/birthday` 走默认表时 `S=None` → `effective=None`
    → **R 恒为 1.0** → 结构上永远走不到 boost 分支。

真正会 boost 的是：`importance>=0.85`、`never_forget=true`、
或显式给了 `stability_days=<数值>` 的条目。

本文件钉住四件事
----------------
1. **真的写进库了**（`metadata.stability_days` 变大），且只改 metadata 列；
2. **闭环成立**：再扫一次 R 回到 `keep_above` 之上，不再需要 boost；
3. **不误伤**：普通 `observation` 掉到 `drop_below` 下面照旧**删**，
   **绝不能**也给它 boost —— 否则遗忘就永远不删东西了；
4. **`dry_run` 一根毛都不写**。
"""
from __future__ import annotations

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

import forgetting  # noqa: E402
import memory_bridge  # noqa: E402

DIM = 1024
DAY = 86400.0


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
    """一个用假 embedding 的记忆桥（不花 API 额度、不联网）。"""
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


def _meta(store, doc_id):
    for d in store.list_documents():
        if str(d["doc_id"]) == doc_id:
            return d.get("metadata") or {}
    return None


def _add_old_memory(store, doc_id, text, extra: dict):
    """造一条「400 天前」的记忆。"""
    meta = {"create_time": time.time() - 400 * DAY, "session_id": "owner"}
    meta.update(extra)
    store.add(text, meta, doc_id=doc_id)


# --------------------------------------------------------------------------- #
# 1. 真的落盘
# --------------------------------------------------------------------------- #
def test_boost_to_真的写进了metadata(bridge):
    """核心：`plan` 算出的 `boost_to_stability_days` 必须落到 `metadata`。

    造一条「promise 类型 + 显式 stability_days=30」——这是最真实的形态：
    给了有限稳定性，同时要求永不删。400 天后 R 早就掉到门槛下，
    `plan_prune` 会算出 boost，这里验它有没有真的写回去。
    """
    store = bridge.store()
    assert store is not None
    _add_old_memory(store, "d-boost", "他说过要带我去看海",
                    {"kind": "promise", "stability_days": 30.0})

    before = _meta(store, "d-boost")
    assert float(before["stability_days"]) == 30.0

    res = bridge.forget_plan()

    after = _meta(store, "d-boost")
    assert after is not None, "这条不该被删"
    assert float(after["stability_days"]) > 30.0, (
        f"稳定性没被拉高：{after.get('stability_days')!r}"
        "（P0-6 的原始病：算出来了但从不落盘）"
    )
    assert "stability_boosted_at" in after, "应该记下什么时候拉高的"
    assert res["boosted"] == 1, f"boosted 应该是真的写进去的条数，实际 {res['boosted']}"

    # 只改 metadata 列：正文一个字都不该动
    doc = next(d for d in store.list_documents() if d["doc_id"] == "d-boost")
    assert doc["text"] == "他说过要带我去看海", "拉高稳定性不该改正文（也就不该重建 FTS）"


def test_拉高之后R回到门槛之上_闭环(bridge):
    """闭环：再扫一次，R 必须回到 `keep_above` 之上、且不再需要 boost。

    只验「字段变了」是不够的 —— 字段变了但算法没读它，等于白写。
    这条验的是「下一次判定真的不一样了」。
    """
    store = bridge.store()
    _add_old_memory(store, "d-boost", "他说过要带我去看海",
                    {"kind": "promise", "stability_days": 30.0})

    cfg = forgetting.thresholds({})
    keep_above = cfg["keep_above"]

    # 拉高之前：R 应该在门槛以下（否则这条用例本身没意义）
    before_row = forgetting.plan_prune(
        store.list_documents(), **cfg
    )["rows"][0]
    assert before_row["retrievability"] < keep_above, (
        f"造的数据不够旧：R={before_row['retrievability']:.4f} 已经在门槛上"
    )

    bridge.forget_plan()
    store2 = bridge.store()
    after_row = forgetting.plan_prune(
        store2.list_documents(), **cfg
    )["rows"][0]

    assert after_row["retrievability"] > keep_above, (
        f"R 没回到门槛上：{after_row['retrievability']:.4f}"
    )
    assert after_row["boost_to_stability_days"] is None, "已经够稳了，不该再要求 boost"
    assert after_row["decision"] == "keep"


def test_第三次扫描不再重复boost(bridge):
    """拉高之后就该停了 —— 不然每天都白算一遍、还反复写库。"""
    store = bridge.store()
    _add_old_memory(store, "d-boost", "他说过要带我去看海",
                    {"kind": "promise", "stability_days": 30.0})

    first = bridge.forget_plan()
    assert first["boosted"] == 1

    second = bridge.forget_plan()
    assert second["boosted"] == 0, "已经拉高过了，不该再写一次"
    assert second["boost_planned"] == 0


# --------------------------------------------------------------------------- #
# 2. 闸不误伤：普通 observation 照旧删
# --------------------------------------------------------------------------- #
def test_普通observation该删就删_不给boost(bridge):
    """**边界（最容易错的一条）**：`boost` 只该作用于「永不删」类型。

    普通 `observation` 掉到 `drop_below` 下面是**该删**。要是也给它 boost，
    遗忘就永远不删东西了 —— 那等于把整个遗忘机制废掉。
    """
    store = bridge.store()
    _add_old_memory(store, "d-obs", "今天中午吃了面条", {"kind": "observation"})

    res = bridge.forget_plan()
    assert res["deleted"] == 1, "400 天前的普通观察应该被删"
    assert res["boosted"] == 0, "普通 observation 绝不能被 boost"
    assert _meta(store, "d-obs") is None, "应该真的被删掉了"


def test_promise走默认表时不boost(bridge):
    """`kind=promise` 走默认表（S=None）→ R 恒 1.0 → 结构上不 boost。

    这条钉住一个容易误解的点：**不是所有「永不删」都会 boost**。
    S=None 意味着"根本不衰减"，也就不需要拉高。清单 06 把这一点写错了。
    """
    store = bridge.store()
    _add_old_memory(store, "d-never", "他答应生日那天陪我", {"kind": "promise"})

    res = bridge.forget_plan()
    after = _meta(store, "d-never")
    assert after is not None, "永不删类型不该被删"
    assert after.get("stability_days") is None, (
        "S=None 的条目不该被写成有限数值 —— 那会让它开始衰减"
    )
    assert res["boosted"] == 0


# --------------------------------------------------------------------------- #
# 3. dry_run 一根毛都不写
# --------------------------------------------------------------------------- #
def test_dry_run不写库(bridge):
    """`dry_run` 的全部意义就是"只出计划不动库"，包括不 boost。"""
    store = bridge.store()
    _add_old_memory(store, "d-boost", "他说过要带我去看海",
                    {"kind": "promise", "stability_days": 30.0})
    _add_old_memory(store, "d-obs", "今天中午吃了面条", {"kind": "observation"})

    res = bridge.forget_plan(dry_run=True)
    assert res["dry_run"] is True
    assert float(_meta(store, "d-boost")["stability_days"]) == 30.0, "dry_run 不许写"
    assert _meta(store, "d-obs") is not None, "dry_run 也不许删"
    assert res["boosted"] == 0
    # 但计划里仍然应该算出来（不然 dry_run 就没法"预览"了）
    assert res["boost_planned"] >= 1


# --------------------------------------------------------------------------- #
# 4. store 层的方法契约
# --------------------------------------------------------------------------- #
def test_bump_stability_只往上拉不压低(bridge):
    """已经比目标更高时不要压低它 —— boost 是「拉高」，不是「赋值」。"""
    store = bridge.store()
    store.add("测试", {"stability_days": 500.0}, doc_id="d-high")
    assert store.bump_stability("d-high", 100.0) is False, "不该往下压"
    assert float(_meta(store, "d-high")["stability_days"]) == 500.0


def test_bump_stability拒绝非法参数(bridge):
    store = bridge.store()
    store.add("测试", {"stability_days": 30.0}, doc_id="d-x")
    assert store.bump_stability("d-x", 0) is False
    assert store.bump_stability("d-x", -5) is False
    assert store.bump_stability("d-x", None) is False
    assert store.bump_stability("", 100) is False
    assert store.bump_stability("不存在的id", 100) is False
    assert float(_meta(store, "d-x")["stability_days"]) == 30.0, "非法参数不该动库"
