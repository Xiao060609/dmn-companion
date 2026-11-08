"""start() 第 ③ 步「别再盲目 spawn」的单测（全 mock：不启引擎、不查真实进程表）。

背景（H2 复发，QA 实测）：``breeze-server`` 的启动顺序是
``loading <model>.gguf`` → ``listening on 8849``，也就是**先吃 3.5GB 显存、
几十秒后才 bind 端口**。旧逻辑只看 /health，这期间判定「没引擎」就 spawn，
于是两个实例各占 3.5GB（8188MiB 的卡会顶满），新 exe 最后还会因端口被占退出，
supervisor 每轮再重试一次 —— 反复显存尖峰。

这里钉住三条新分支：
    1. 端口被**非引擎**进程占用 → 直接抛错，绝不 spawn
    2. 已有同款引擎进程但还没就绪，稍后就绪 → 等到它并 adopt，不 spawn、不 kill
    3. 已有引擎一直不就绪（卡死）→ 等满 load_wait 后杀掉，然后正常 spawn 一次

跑法::

    python -m pytest desktop/tests/test_tts_spawn_guard.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import tts_breeze  # noqa: E402

PORT = 18849  # 和 test_tts_adopted 一致：绝不用线上的 8849


class FakeProc:
    """subprocess.Popen 的最小替身（活着的进程）。"""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.terminated = True
        self.returncode = 1

    def wait(self, timeout=None):
        return 0


def _silent(_msg=""):
    return None


@pytest.fixture()
def make(monkeypatch, tmp_path):
    """造实例：/health 按脚本返回、端口/进程探测全部可编程、spawn 全假。"""
    monkeypatch.setattr(tts_breeze.time, "sleep", lambda _s: None)

    def _make(health_script, occupant=None, same_exe=None, exe_name="breeze-server.exe",
              load_wait=3, enum_fail=False):
        # ⚠ 2026-11-04（第二十三轮）：Breeze.start() 现在会**先检查 exe/model 存在**
        # （否则换机器时报 WinError 2，不说哪个文件）。所以这里要造真实的占位文件，
        # 否则前置检查会先抛 FileNotFoundError —— 而这些测试要验的是
        # spawn/复用/守卫逻辑，不是路径检查。
        fake_exe = tmp_path / "breeze-server.exe"
        fake_model = tmp_path / "breeze-tts-2-q8_0.gguf"
        fake_exe.write_bytes(b"")
        fake_model.write_bytes(b"")
        inst = tts_breeze.Breeze(
            server=str(fake_exe),
            model=str(fake_model),
            outdir=str(tmp_path / "tts_out"),
            port=PORT,
            load_wait=load_wait,
        )
        script = list(health_script)

        def fake_health():
            return script.pop(0) if script else None

        inst._health = fake_health
        inst._check_vram = lambda log=print: None
        inst.check_voice = lambda log=print: True
        inst._port_occupant = lambda: occupant
        if enum_fail:
            # 模拟 PowerShell / WMI 查询失败：枚举结果为「未知」而不是「空」
            inst._same_exe_pids = lambda: None
        else:
            inst._same_exe_pids = lambda: list(same_exe or [])
        inst._wait_port_free = lambda timeout=10.0: True

        spawned: list[FakeProc] = []

        def fake_popen(*args, **kwargs):
            proc = FakeProc()
            spawned.append(proc)
            return proc

        monkeypatch.setattr(tts_breeze.subprocess, "Popen", fake_popen)

        killed: list[list[str]] = []

        def fake_run_console(args, timeout=10):
            killed.append(list(args))
            return ""

        monkeypatch.setattr(tts_breeze, "_run_console", fake_run_console)
        monkeypatch.setattr(tts_breeze, "exe_name_of", lambda pid: exe_name)

        inst.spawned = spawned
        inst.taskkills = killed
        return inst

    return _make


class TestForeignOccupant:
    def test_refuses_to_spawn_when_port_held_by_other_program(self, make):
        """端口被别的程序占着：spawn 出来也绑不上，只会白吃 3.5GB 显存。"""
        b = make([None, None], occupant=4242, exe_name="python.exe")
        with pytest.raises(RuntimeError) as excinfo:
            b.start(log=_silent)
        msg = str(excinfo.value)
        assert "非引擎进程占用" in msg
        assert "4242" in msg
        assert b.spawned == []          # 关键：一个都不许 spawn
        assert b.taskkills == []        # 也不许杀别人的进程


class TestLoadingEngine:
    def test_waits_and_adopts_engine_that_finishes_loading(self, make):
        """加载中 → 稍后就绪：等到就绪就 adopt，不 spawn 不 kill。"""
        # 第一次 /health 拿不到（加载中还没 bind），之后进入等待轮询，轮询里拿到 ok
        b = make([None, None, {"status": "ok", "pid": 9408}], same_exe=[9408])
        b.start(log=_silent)
        assert b.spawned == []          # 关键：没白起第二个实例
        assert b.taskkills == []        # 也没杀掉正在加载的那个
        assert b.adopted is True
        assert b.adopted_pid == 9408

    def test_stuck_engine_is_killed_then_respawned_once(self, make):
        """一直不就绪（卡死）→ 等满 load_wait 后杀掉，再正常 spawn 一次。"""
        # load_wait=0：等待阶段立刻超时（sleep 被替换成 no-op，真等会空转烧 CPU），
        # 只验「判定卡死 → 杀 → 重新 spawn 一次」这条路径
        b = make([None, {"status": "ok"}], same_exe=[777], load_wait=0)
        b.start(log=_silent)
        assert len(b.spawned) == 1      # 只 spawn 一次
        assert b.adopted is False
        tk = [args for args in b.taskkills if args and args[0] == "taskkill"]
        assert len(tk) == 1
        assert "777" in tk[0]

    def test_stuck_engine_without_breeze_name_is_not_killed(self, make):
        """名字不像引擎的 PID 不能杀（fail-closed）。"""
        b = make([None, {"status": "ok"}], same_exe=[888], exe_name="python.exe", load_wait=0)
        b.start(log=_silent)
        assert [a for a in b.taskkills if a and a[0] == "taskkill"] == []
        assert len(b.spawned) == 1      # 仍然会起新实例


class TestCleanEnvironment:
    def test_no_occupant_no_pending_spawns_immediately(self, make):
        """端口空、进程表也空 → 立刻 spawn，不白等 load_wait。"""
        b = make([None, None, {"status": "ok"}])
        b.start(log=_silent)
        assert len(b.spawned) == 1
        assert b.taskkills == []


class TestEnumerationFailure:
    """进程枚举失败 ≠ 没有进程。

    早先 ``pids_with_exe_path`` 把「查询失败」也返回 ``[]``，调用方无法区分，
    PowerShell / WMI 一失效闸门就静默 fail-open —— 加载窗口内照样 spawn 第二个，
    白白双占 3.5GB。这三条把「未知」这条路钉住。
    """

    def test_query_failure_returns_none_not_empty(self, monkeypatch):
        """底层命令失败 → None；exe 为空 → []（这是真的没人）。"""

        def boom(*_a, **_kw):
            raise RuntimeError("powershell 退出码 1: access denied")

        monkeypatch.setattr(tts_breeze, "_run_console", boom)
        assert tts_breeze.pids_with_exe_path(r"G:\x\breeze-server.exe") is None
        assert tts_breeze.pids_with_exe_path("") == []

    def test_enum_failure_still_adopts_engine_that_binds_late(self, make):
        """枚举失败，但端口上随后出现引擎 → 复用，不许 spawn 第二个。"""
        # ① step② 的 /health：没应答
        # ② _wait_healthy 首轮就拿到 ok（不空转）
        # ③ adopt 时再取一次 health 体
        b = make([None, {"status": "ok"}, {"status": "ok"}], enum_fail=True)
        b.start(log=_silent)
        assert b.spawned == []      # 关键：没有双开
        assert b.adopted is True

    def test_enum_failure_with_no_engine_spawns_once(self, make):
        """枚举失败且确实没人：等满后仍要正常拉起一个（不能因为查不到就不出声）。"""
        b = make([None, None, {"status": "ok"}], enum_fail=True, load_wait=0)
        b.start(log=_silent)
        assert len(b.spawned) == 1
        assert b.adopted is False

    def test_enum_failure_with_occupant_waits_only_once(self, make):
        """枚举失败但端口已有人占 → 只等一轮 load_wait。

        「有没有引擎」这时候根本不需要枚举（端口持有者就是），再多等一轮
        load_wait 就是纯浪费：默认 45s，两轮 + /health 3s 超时 ≈ 一分半哑巴期。
        """
        b = make([None, {"status": "ok"}], enum_fail=True, load_wait=0)
        calls = {"n": 0}

        def occupant():
            # ③ 的快照：端口有人；④ 的重探：杀掉之后端口空了
            calls["n"] += 1
            return 7000 if calls["n"] == 1 else None

        b._port_occupant = occupant
        waits = {"n": 0}
        real_wait = b._wait_healthy

        def counting_wait(timeout):
            waits["n"] += 1
            return real_wait(timeout)

        b._wait_healthy = counting_wait
        # 只统计「spawn 之前」的等待：④ 把新实例拉起来之后本来还要等它就绪，
        # 那是正常的第二次等待，跟这里要钉的「枚举失败分支白等一轮」不是一回事。
        # _check_vram 只在 ④ spawn 之前调用一次，正好拿来当快照点。
        pre_spawn = {}
        b._check_vram = lambda log=print: pre_spawn.setdefault("waits", waits["n"])
        b.start(log=_silent)
        assert pre_spawn["waits"] == 1   # 修之前是 2
        assert len(b.spawned) == 1


class TestAdoptPid:
    def test_adopt_prefers_port_occupant_over_smallest_pid(self, make):
        """同路径有多个进程时，认端口持有者。

        退出时按 ``adopted_pid`` 强杀，认成最小 PID 会杀错人、真引擎留活占着 3.5GB。
        """
        b = make([None, {"status": "ok"}, {"status": "ok"}],
                 occupant=5000, same_exe=[100, 5000])
        b.start(log=_silent)
        assert b.adopted is True
        assert b.adopted_pid == 5000     # 不是 100
        assert b.spawned == []


class TestFailClosedWiring:
    """变异测试补的两条：光有「失败返回 None」不够，接线本身也要钉住。

    上一版用例把 ``_run_console`` 直接改成 raise，绕过了 returncode 判断那一层，
    所以把 ``check=True`` 改回 ``False`` 测试照样全绿 —— 等于 P3 白修。
    """

    def test_run_console_check_flag(self, monkeypatch):
        """check=True 必须把「非零退出码」变成异常；默认 check=False 不抛。"""

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr(tts_breeze.subprocess, "run", lambda *a, **k: FakeResult())
        assert tts_breeze._run_console(["x"]) == ""      # check=False：不抛
        with pytest.raises(RuntimeError):
            tts_breeze._run_console(["x"], check=True)   # check=True：抛

    def test_pids_with_exe_path_returns_none_on_nonzero_exit(self, monkeypatch):
        """真实调用链：命令非零退出 → None（不是 []）。

        把 ``pids_with_exe_path`` 里的 ``check=True`` 改成 ``False``，这条会变红 ——
        上一版没有它，改回 False 测试全绿（QA 变异测试实测）。
        """

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "Access is denied"

        monkeypatch.setattr(tts_breeze.subprocess, "run", lambda *a, **k: FakeResult())
        assert tts_breeze.pids_with_exe_path(r"G:\x\breeze-server.exe") is None

    def test_query_script_does_not_swallow_wmi_errors(self, monkeypatch):
        """PowerShell 脚本里不许有 -ErrorAction SilentlyContinue。

        WMI 自身报错时它会吞掉错误、退出码仍是 0，check=True 挡不住 →
        返回 [] = 「确实没人」→ 加载窗口内立刻 spawn，退回旧 bug（QA 实测）。
        """
        seen: dict = {}

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(args, **_kw):
            seen["args"] = args
            return FakeResult()

        monkeypatch.setattr(tts_breeze.subprocess, "run", fake_run)
        tts_breeze.pids_with_exe_path(r"G:\x\breeze-server.exe")
        script = seen["args"][-1]
        assert "SilentlyContinue" not in script
        assert "Get-CimInstance Win32_Process" in script

    def test_same_exe_pids_passes_none_through(self, monkeypatch, tmp_path):
        """枚举失败必须原样把 None 传上去，不能自己兜成 []（那就是 fail-open 复发）。"""
        monkeypatch.setattr(tts_breeze, "pids_with_exe_path", lambda exe: None)
        b = tts_breeze.Breeze(server="x.exe", model="m.gguf",
                              outdir=str(tmp_path / "tts_out"), port=PORT)
        assert b._same_exe_pids() is None

    def test_same_exe_pids_filters_own_pid(self, monkeypatch, tmp_path):
        """枚举成功时要把自己这个 python 进程摘掉（否则会把自己当引擎）。"""
        import os
        monkeypatch.setattr(tts_breeze, "pids_with_exe_path",
                            lambda exe: [os.getpid(), 4242])
        b = tts_breeze.Breeze(server="x.exe", model="m.gguf",
                              outdir=str(tmp_path / "tts_out"), port=PORT)
        assert b._same_exe_pids() == [4242]


class TestLateBind:
    """④ 之前重探端口：等待期间才 bind 上的实例不能被当成「没人」。"""

    def test_port_appears_right_before_spawn_is_adopted(self, make):
        b = make([None, {"status": "ok"}, {"status": "ok"}])
        # 进 ③ 时端口是空的，等到 ④ 之前才出现监听者
        calls = {"n": 0}

        def occupant():
            calls["n"] += 1
            # 第 1 次是进 ③ 时的快照（空），第 2 次是 ④ 之前的重探（出现了）
            return 6000 if calls["n"] >= 2 else None

        b._port_occupant = occupant
        b.start(log=_silent)
        assert b.spawned == []           # 没有白起第二个
        assert b.adopted is True
        assert b.adopted_pid == 6000



# --------------------------------------------------------------------------- #
# 语音引擎路径不存在时，报错要说人话（2026-11-04 第二十三轮）
# --------------------------------------------------------------------------- #
def test_语音引擎路径不存在时给出可操作的报错():
    """`Breeze.start()` 遇到不存在的 exe/model，必须说清"哪个文件 + 怎么修"。

    踩过的坑：原来直接 `subprocess.Popen`，抛
        FileNotFoundError: [WinError 2] 系统找不到指定的文件
    这个报错**不说哪个文件、也不说怎么修**。

    而最常见的触发场景恰恰是"换了台机器"——
    `desktop/config.json` 里的 `breeze_dir` 存的是**绝对路径**
    （`G:\\workplase\\Breeze-TTS-2.cpp\\...`），换机器就指向不存在的目录。
    用户看到 WinError 2 完全不知道该改什么。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))
    from tts_breeze import Breeze

    b = Breeze(
        server=r"X:\definitely\not\here\breeze-server.exe",
        model=r"X:\definitely\not\here\model.gguf",
        port=18899,
        outdir=str(Path(__file__).resolve().parent / "_tts_probe_should_not_exist"),
    )
    try:
        b.start(timeout=3.0, log=lambda _m: None)
    except FileNotFoundError as exc:
        msg = str(exc)
        assert "不存在" in msg, f"报错应该说明文件不存在，实际：{msg}"
        assert "config.json" in msg, f"报错应该指出去改哪个配置文件，实际：{msg}"
        assert "breeze_dir" in msg or "breeze_model" in msg, (
            f"报错应该点出具体的配置键，实际：{msg}"
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"应该抛 FileNotFoundError，实际抛了 {type(exc).__name__}: {exc}")
    else:
        pytest.fail("路径不存在时应该抛异常，实际没有")
