"""H2「语音引擎复用」分支单测（全 mock：不启动引擎、不监听端口、不杀进程）。

背景见 ``desktop/tts_breeze.py`` 模块 docstring 的「引擎归属」一节，以及
架构评审报告 H2：``breeze-server.exe`` 在 Electron ``TerminateProcess`` 之后
会变成孤儿，继续占着 8849 和 3.5GB 显存。修复策略是「复用而不是杀掉重建」，
于是 ``adopted`` 这个状态必须同时影响三处：``start()`` / ``ready`` / ``stop()``。
任何一处漏改都会退化成「supervisor 每 3 秒白拉一个必死的 exe」。

这里只验证**分支决策**：
    * adopted 时 start() 不 spawn、stop() 不 terminate、ready 只看 /health
    * 没有引擎时才 spawn，且只 spawn 一次
    * 自有进程的行为与修复前一致（回归）
    * spawn 出来的进程秒退时错误信息可读（指向端口冲突）

跑法::

    python -m pytest desktop/tests/test_tts_adopted.py -v
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import tts_breeze  # noqa: E402

PORT = 18849  # 故意不用 8849：万一有人手滑改成真连，也碰不到线上引擎


class FakeProc:
    """subprocess.Popen 的最小替身。"""

    def __init__(self, alive: bool = True):
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.returncode = None if alive else 1

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.terminated = True
        self._alive = False
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode or 0


@pytest.fixture()
def breeze_factory(monkeypatch, tmp_path):
    """造 Breeze 实例的工厂：/health 按脚本返回，spawn 全部替换成假进程。"""
    monkeypatch.setattr(tts_breeze.time, "sleep", lambda _s: None)

    def _make(health_script, spawn_dies: bool = False):
        # ⚠ 2026-11-04（第二十三轮）：Breeze.start() 现在会先检查 exe/model 存在。
        # 这里要验的是 spawn/复用/concurrency 逻辑，所以造真实占位文件，
        # 别让前置检查先抛 FileNotFoundError。
        fake_exe = tmp_path / "breeze-server.exe"
        fake_model = tmp_path / "breeze-tts-2-q8_0.gguf"
        fake_exe.write_bytes(b"")
        fake_model.write_bytes(b"")
        inst = tts_breeze.Breeze(
            server=str(fake_exe),
            model=str(fake_model),
            outdir=str(tmp_path / "tts_out"),
            port=PORT,
        )
        script = list(health_script)

        def fake_health():
            return script.pop(0) if script else None

        inst._health = fake_health
        inst._check_vram = lambda log=print: None
        inst.check_voice = lambda log=print: True
        # start() 第 ③ 步会探测「端口上/系统里是不是已经有个引擎」。这两个探测
        # 在生产里走 netstat / PowerShell，单测里必须关掉，否则会查真实进程表
        # （而且它的 subprocess.run 会命中下面被替换的 Popen，把 spawn 次数算错）。
        # 默认语义 = 「端口没人、也没有同款进程」，即老用例的「干净环境」。
        inst._port_occupant = lambda: None
        inst._same_exe_pids = lambda: []

        spawned: list[FakeProc] = []

        def fake_popen(*args, **kwargs):
            proc = FakeProc(alive=not spawn_dies)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(tts_breeze.subprocess, "Popen", fake_popen)
        inst.spawned = spawned
        return inst

    return _make


def _silent(_msg=""):
    return None


class TestAdoptedBranch:
    def test_existing_engine_is_adopted_without_spawn(self, breeze_factory):
        b = breeze_factory([{"status": "ok"}])
        b.start(log=_silent)
        assert b.adopted is True
        assert b.proc is None
        assert b.spawned == []

    def test_adopted_engine_reports_ready_when_healthy(self, breeze_factory):
        b = breeze_factory([{"status": "ok"}, {"status": "ok"}])
        b.start(log=_silent)
        assert b.ready is True

    def test_adopted_engine_reports_not_ready_when_gone(self, breeze_factory):
        """复用的引擎退出后 ready 必须变 False，否则 supervisor 永远不重拉。"""
        b = breeze_factory([{"status": "ok"}, None])
        b.start(log=_silent)
        assert b.ready is False

    def test_stop_does_not_terminate_adopted_engine(self, breeze_factory):
        b = breeze_factory([{"status": "ok"}])
        b.start(log=_silent)
        watcher = FakeProc()
        b.proc = watcher          # 就算有引用，adopted 分支也不该碰它
        b.stop()
        assert watcher.terminated is False
        assert b.adopted is False   # 只解除引用

    def test_adopted_pid_is_recorded_when_health_exposes_it(self, breeze_factory):
        b = breeze_factory([{"status": "ok", "pid": 9408}])
        b.start(log=_silent)
        assert b.adopted_pid == 9408

    def test_adopted_pid_is_none_when_health_has_no_pid(self, breeze_factory):
        """线上引擎 /health 只回 status/sample_rate/ws_port，不能瞎猜。"""
        b = breeze_factory([{"status": "ok"}])
        b.start(log=_silent)
        assert b.adopted_pid is None


class TestSpawnBranch:
    def test_spawns_exactly_once_when_no_engine(self, breeze_factory):
        b = breeze_factory([None, None, {"status": "ok"}])
        b.start(log=_silent)
        assert len(b.spawned) == 1
        assert b.adopted is False

    def test_own_process_is_not_marked_adopted(self, breeze_factory):
        b = breeze_factory([None, None, {"status": "ok"}, {"status": "ok"}])
        b.start(log=_silent)
        assert b.adopted is False
        assert b.ready is True

    def test_own_process_is_terminated_on_stop(self, breeze_factory):
        b = breeze_factory([None, None, {"status": "ok"}])
        b.start(log=_silent)
        proc = b.proc
        b.stop()
        assert proc is not None and proc.terminated is True

    def test_immediate_exit_error_mentions_port(self, breeze_factory):
        """刚 spawn 就退出 = 端口被别的实例占了，报错要能看出这一点。"""
        b = breeze_factory([None], spawn_dies=True)
        with pytest.raises(RuntimeError) as excinfo:
            b.start(log=_silent)
        assert str(PORT) in str(excinfo.value)


class TestConcurrencyContract:
    def test_start_is_not_reentrant(self, breeze_factory):
        """start() 本身不加锁（docstring 写明「调用方负责加锁」）。

        两个调用点（server.boot_tts / server._tts_supervisor）都持有
        ``_tts_lock``，所以线上安全；这条用例把这个**前提**钉住：
        一旦有人绕过锁直接并发调 start()，会双 spawn。

        用 Barrier 卡在 spawn 之前，让「两个线程都已经越过探测、都还没 spawn」
        这个时刻确定发生 —— 否则谁先 spawn 完，另一个就会在分支 ① 看到
        「自己的进程活着且健康」直接返回，断言变成靠调度运气。
        """
        b = breeze_factory([None, None, None, None, {"status": "ok"}, {"status": "ok"}])
        barrier = threading.Barrier(2, timeout=10)
        b._check_vram = lambda log=print: barrier.wait()
        threads = [threading.Thread(target=lambda: b.start(log=_silent)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(b.spawned) == 2
