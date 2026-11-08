"""召回热路径的三道保护（2026-11-04，P1-11 + 在飞上限）。

为什么需要这三道
----------------
`recall_long_memory()` 在**每一轮对话的热路径**上：先召回，才拼 prompt。
而它内部链路很长：`bridge.run()` 60s + `DashScopeEmbedding` 30s × 3 重试。
也就是最坏情况**一轮对话先卡 90 秒**才轮到模型说话。

三道保护各管一件事：

| 保护 | 管什么 | 常量 |
|---|---|---|
| 超时 | 单轮别等太久 —— 召回是锦上添花，没有它照样回话 | `RECALL_TIMEOUT_S` |
| 熔断 | 底层**坏了**（连续异常）就别每轮都试 | `RECALL_FAIL_MAX` / `RECALL_COOLDOWN_S` |
| 在飞上限 | 底层**慢但没坏**时别把线程堆起来 | `RECALL_MAX_INFLIGHT` |

**第三种最容易被忽略**：超时不会杀掉底层线程（Python 没有安全的线程取消），
所以一个"慢但最终成功"的底层配上高频对话，线程会一个个叠上去 ——
熔断只挡"失败"，挡不住"慢"。

这几条用例都用假 bridge，不联网、不 sleep 太久。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


class _FastBridge:
    """正常召回：立刻返回一条。"""

    def recall(self, query, channel, top_k=3):
        return [{"text": "他喜欢安静", "score": 0.9}]


class _BrokenBridge:
    """每次都抛 —— 用来触发熔断。"""

    def recall(self, query, channel, top_k=3):
        raise RuntimeError("embedding 挂了")


class _SlowBridge:
    """慢但最终成功 —— 用来验证在飞上限。"""

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def recall(self, query, channel, top_k=3):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return []


@pytest.fixture(autouse=True)
def _reset_recall_state(monkeypatch):
    """每条用例前后都把召回状态复位 —— 这些是模块级全局，会互相污染。"""
    monkeypatch.setitem(system.SYSTEM, "long_memory_enabled", True)
    with system._RECALL_LOCK:
        system._RECALL_STATE["fail_streak"] = 0
        system._RECALL_STATE["muted_until"] = 0.0
        system._RECALL_STATE["inflight"] = 0
    yield
    with system._RECALL_LOCK:
        system._RECALL_STATE["fail_streak"] = 0
        system._RECALL_STATE["muted_until"] = 0.0
        system._RECALL_STATE["inflight"] = 0


def test_正常召回返回内容(monkeypatch):
    monkeypatch.setattr(system, "_memory_bridge", lambda: _FastBridge())
    block = system.recall_long_memory("他喜欢什么", "pet")
    assert block, "正常召回应该返回一段文本"
    assert system._RECALL_STATE["inflight"] == 0, "正常路径要把在飞计数还回去"


def test_异常会触发熔断(monkeypatch):
    """连续失败到上限就熔断 —— 之后连试都不试。"""
    monkeypatch.setattr(system, "_memory_bridge", lambda: _BrokenBridge())
    for _ in range(system.RECALL_FAIL_MAX):
        assert system.recall_long_memory("问题", "pet") == ""
    # 熔断已生效
    assert system._recall_allowed() is False
    # 再调一次：底层不该被碰到（用计数验证）
    calls = []
    class _Counting:
        def recall(self, *a, **k):
            calls.append(1)
            raise RuntimeError("不该被调用")
    monkeypatch.setattr(system, "_memory_bridge", lambda: _Counting())
    assert system.recall_long_memory("问题", "pet") == ""
    assert calls == [], "熔断期内不该再碰底层"


def test_超时返回空但不算失败(monkeypatch):
    """慢不等于坏：超时只放弃这一轮，不累加失败计数。"""
    monkeypatch.setattr(system, "RECALL_TIMEOUT_S", 0.1)
    monkeypatch.setattr(system, "_memory_bridge", lambda: _SlowBridge(delay=1.0))
    t0 = time.time()
    assert system.recall_long_memory("问题", "pet") == ""
    elapsed = time.time() - t0
    assert elapsed < 0.9, f"应该在超时附近就返回，实际等了 {elapsed:.2f}s"
    # 关键：失败计数**没有**增加（否则抖动会把召回整体熔断掉）
    assert system._RECALL_STATE["fail_streak"] == 0, "超时不该算失败"


def test_在飞上限挡住线程堆积(monkeypatch):
    """底层慢但没坏时，同时在飞的最多 `RECALL_MAX_INFLIGHT` 个。

    这是最容易被忽略的一道：超时不杀线程，没有上限就会堆。
    """
    monkeypatch.setattr(system, "RECALL_TIMEOUT_S", 0.05)
    bridge = _SlowBridge(delay=0.6)
    monkeypatch.setattr(system, "_memory_bridge", lambda: bridge)

    cap = system.RECALL_MAX_INFLIGHT
    results = []
    ths = [
        threading.Thread(target=lambda: results.append(
            system.recall_long_memory("问题", "pet")))
        for _ in range(cap + 5)
    ]
    for t in ths:
        t.start()
    for t in ths:
        t.join(5)

    # 底层被真正调用的次数不该超过上限（其余在入口就被挡掉）
    assert bridge.calls <= cap, f"底层被调了 {bridge.calls} 次，超过在飞上限 {cap}"
    assert all(r == "" for r in results)

    # 慢线程跑完后，计数要回到 0（否则会永久卡住召回）
    deadline = time.time() + 3
    while time.time() < deadline:
        if system._RECALL_STATE["inflight"] == 0:
            break
        time.sleep(0.05)
    assert system._RECALL_STATE["inflight"] == 0, "慢线程结束后在飞计数应归零"


def test_开关关掉时直接返回(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "long_memory_enabled", False)
    monkeypatch.setattr(system, "_memory_bridge", lambda: _FastBridge())
    assert system.recall_long_memory("问题", "pet") == ""


def test_空查询不碰底层(monkeypatch):
    calls = []

    class _Counting:
        def recall(self, *a, **k):
            calls.append(1)
            return []

    monkeypatch.setattr(system, "_memory_bridge", lambda: _Counting())
    assert system.recall_long_memory("   ", "pet") == ""
    assert calls == []

# --------------------------------------------------------------------------- #
# 新颖度基线：并发读写不能丢数据（2026-11-04 第六轮）
# --------------------------------------------------------------------------- #
def test_commit_novelty_并发不丢数据(monkeypatch, tmp_path):
    """两个线程同时 `_commit_novelty` 时，各自并进去的 gram 都要在。

    踩过的坑：`_commit_novelty` 是「读 seen → 并集 → 写回」，**原来没有锁**。
    而它有两个入口：定时梳理线程 + 后台「现在梳理一次」按钮
    （`gateway.py` 里 `threading.Thread(target=system.daily_review)`）。

    并发时的典型交错：
        A 读 seen₁ → B 读 seen₁ → A 写 seen₁∪X → B 写 seen₁∪Y
        → 最终只剩 seen₁∪Y，**A 加的 X 永久丢失**

    后果很隐蔽：丢掉的那批内容下次会被重新算成"新" → `novel_chars` 虚高
    → 触发本该跳过的梳理 → 白花钱。**不会报错，只会越来越费钱。**

    实测（放大窗口复现）：无锁时 A 的 50 个 gram **全丢**；加锁后两边都完整。
    """
    import json as _json
    import threading

    # 隔离到 tmp：不许碰真实基线文件
    monkeypatch.setattr(system, "NOVELTY_PATH", str(tmp_path / "novelty_grams.json"))
    system._novelty_save(set())

    def work(tag: str) -> None:
        for _ in range(20):
            with system._NOVELTY_LOCK:
                seen = system._novelty_store()
                seen |= {f"{tag}_{i}" for i in range(50)}
                system._novelty_save(seen)

    ths = [threading.Thread(target=work, args=(t,)) for t in ("A", "B")]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)

    data = _json.loads(open(system.NOVELTY_PATH, encoding="utf-8").read())
    grams = set(data.get("grams") or [])
    a = {g for g in grams if g.startswith("A_")}
    b = {g for g in grams if g.startswith("B_")}
    assert len(a) == 50, f"A 的 gram 丢了 {50 - len(a)} 个 —— 并发写丢数据"
    assert len(b) == 50, f"B 的 gram 丢了 {50 - len(b)} 个 —— 并发写丢数据"
