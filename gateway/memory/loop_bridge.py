"""常驻事件循环桥：让同步代码调用 async 记忆层。

为什么需要
----------
网关是 `ThreadingHTTPServer` + 同步 sqlite3 + 同步 urllib。
而记忆层（`document_storage` / `FaissVecDB`）是 **async**（SQLAlchemy async + aiosqlite）。

实验数据（`tmp/exp_async_sql.py`，2026-11-03 实测）：

    每次 asyncio.run()          40.4 ms/次
    常驻事件循环 + threadsafe    9.38 ms/次   ← 快 4.3 倍

对话热路径上每次多 30ms 不可接受，所以必须用常驻循环。

并发安全性
----------
一个后台线程跑 `loop.run_forever()`，其他线程用
`asyncio.run_coroutine_threadsafe()` 投递协程。
实验验证：3 个线程并发写 30 条，零错误。

注意：**所有记忆层的对象（engine / index）都只能在那个循环线程里创建和使用**。
跨线程直接 await 是不允许的，必须通过 `run()`。
"""
from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class AsyncLoopBridge:
    """一个常驻事件循环 + 同步投递入口。

    用法：

        bridge = AsyncLoopBridge()
        n = bridge.run(store.count_documents({}))

    关闭：`bridge.close()`，或者进程退出时由 atexit 兜底。
    """

    def __init__(self, name: str = "xiaoyu-memory-loop") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._closed = threading.Event()
        self._started = threading.Event()
        self._thread.start()
        # 等循环真的跑起来再返回，避免第一批调用抢跑
        if not self._started.wait(10):
            raise RuntimeError("记忆事件循环没能在 10 秒内启动")
        atexit.register(self.close)

    # ---------------------------------------------------------------- 内部
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._started.set)
        try:
            self._loop.run_forever()
        finally:
            # 收尾：取消所有未完成的协程，再关循环
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001 —— 收尾失败不该拖垮退出流程
                pass
            finally:
                self._loop.close()

    # ---------------------------------------------------------------- 对外
    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 60.0) -> T:
        """把协程投递到循环线程并等结果（同步阻塞）。

        Args:
            coro: 要执行的协程。
            timeout: 等待上限（秒）。超时抛 `TimeoutError`。

        Returns:
            协程的返回值。

        Raises:
            RuntimeError: 桥已关闭。
            TimeoutError: 超时（底层 future 会被取消）。
        """
        if self._closed.is_set():
            # 协程没人消费会告警，这里显式关掉
            coro.close()
            raise RuntimeError("记忆事件循环已关闭")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def close(self, timeout: float = 5.0) -> None:
        """停循环并等线程退出。可重复调用（幂等）。"""
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            # 循环已经关了
            return
        self._thread.join(timeout)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


#: 进程级单例。记忆层全项目共用一个循环 —— 多个循环会各持一份 faiss 索引，
    #: 那是真正的数据错乱来源。
_BRIDGE: AsyncLoopBridge | None = None
_LOCK = threading.Lock()


def get_bridge() -> AsyncLoopBridge:
    """取全局桥（懒建、线程安全）。"""
    global _BRIDGE
    if _BRIDGE is not None and not _BRIDGE.closed:
        return _BRIDGE
    with _LOCK:
        if _BRIDGE is None or _BRIDGE.closed:
            _BRIDGE = AsyncLoopBridge()
    return _BRIDGE


def run(coro: Coroutine[Any, Any, T], timeout: float = 60.0) -> T:
    """便捷入口：`loop_bridge.run(store.insert(...))`。"""
    return get_bridge().run(coro, timeout=timeout)
