"""QQ 托管：从轮盘挪到后台之后，起停逻辑落在 8848 这个服务端。

要钉住三件事：
1. 在线判定和轮盘同一个口径（6185 能连上就算在跑）；
2. 停的时候只杀 QQ 那几个端口，**绝不能碰 8848 / 8898**；
3. 启动脚本不在时要说人话，不能假装拉起成功。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))


@pytest.fixture()
def srv():
    import server

    return server


class DummySock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestOnline:
    def test_连不上就是没在跑(self, srv, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("refused")

        monkeypatch.setattr(srv.socket, "create_connection", boom)
        assert srv.qq_online() is False

    def test_连得上就是在跑(self, srv, monkeypatch):
        monkeypatch.setattr(srv.socket, "create_connection", lambda *_a, **_k: DummySock())
        assert srv.qq_online() is True


class TestCommands:
    def test_停托管只杀qq端口(self, srv, monkeypatch):
        # 2026-11-04（第二十五轮）：qq_stop 拆成了两段调用
        # （1 端口段 / 2 孤儿段），所以要收集**所有**命令再看。
        seen = []

        def fake(args, wait=False, timeout=20):
            seen.append({"args": args, "wait": wait, "timeout": timeout})
            return 0

        monkeypatch.setattr(srv, "_run_powershell", fake)

        assert srv.qq_stop() == 0
        assert seen, "应该至少调一次 PowerShell"

        # 第一段必须是端口段 —— 退出路径也只跑它，所以它必须在最前面
        first = seen[0]["args"][1]
        for port in ("6185", "6199", "5099", "3000", "3001"):
            assert str(port) in first, f"{port} 应该出现在端口段里"
        assert seen[0]["wait"] is True, "停止要等结果，否则接口会先回成功"

        # 任何一段都不能碰桌宠/网关的端口
        for s in seen:
            script = s["args"][1]
            assert "8848" not in script, "不能碰桌宠端口"
            assert "8898" not in script, "不能碰网关端口"

    def test_停托管要补杀端口已死的孤儿(self, srv, monkeypatch):
        """SnowLuma 崩过之后端口全没监听、进程还活着（项目地图坑 #25）。"""
        seen = {}

        def fake(args, wait=False, timeout=20):
            seen["args"] = args
            return 0

        monkeypatch.setattr(srv, "_run_powershell", fake)

        srv.qq_stop()

        script = seen["args"][1]
        assert "Win32_Process" in script, "只按端口杀会漏掉孤儿"
        assert "SnowLuma" in script and "astrbot" in script
        # 过滤条件必须是项目路径，不能按解释器名字（会误伤 IDE / 别的项目）
        assert "8848" not in script and "8898" not in script

    def test_启动脚本缺失要说清楚(self, srv, monkeypatch, tmp_path):
        monkeypatch.setattr(srv, "QQ_SCRIPT", tmp_path / "不存在.ps1")

        result = srv.qq_start()

        assert result["ok"] is False
        assert "找不到" in result["error"]

    def test_启动脚本存在就拉起来(self, srv, monkeypatch, tmp_path):
        script = tmp_path / "启动.ps1"
        script.write_text("# test", encoding="utf-8")
        monkeypatch.setattr(srv, "QQ_SCRIPT", script)
        called = {}

        def fake(args, wait=False, timeout=20):
            called["args"] = args
            return 0

        monkeypatch.setattr(srv, "_run_powershell", fake)

        result = srv.qq_start()

        assert result["ok"] is True
        assert "-File" in called["args"]
        assert str(script) in called["args"]


class TestShutdownStopsQq:
    """退出 = 整套退出。

    AstrBot 的 provider 指着网关 8898，网关归桌宠管。桌宠一退网关就没了，
    QQ 留着只会让群里每条消息都失败（用户看到的是「她不回话」）。
    """

    def test_qq在跑就停掉(self, srv, monkeypatch):
        called = {}
        monkeypatch.setattr(srv, "qq_any_running", lambda: True)

        def fake_stop(timeout=20, orphans=True):
            called["stop"] = True
            return 0

        monkeypatch.setattr(srv, "qq_stop", fake_stop)

        assert srv._stop_qq_for_exit() is True
        assert called.get("stop"), "退出必须真的调 qq_stop"

    def test_qq没跑就不折腾(self, srv, monkeypatch):
        called = {}
        monkeypatch.setattr(srv, "qq_any_running", lambda: False)

        def fake_stop(timeout=20, orphans=True):
            called["stop"] = True
            return 0

        monkeypatch.setattr(srv, "qq_stop", fake_stop)

        assert srv._stop_qq_for_exit() is False
        assert not called, "没在跑就不该启动一次 PowerShell"

    def test_停qq失败不能挡住退出(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "qq_any_running", lambda: True)

        def boom(timeout=20):
            raise OSError("powershell 没了")

        monkeypatch.setattr(srv, "qq_stop", boom)

        # 不抛，只返回 False：清理 QQ 是顺带，挡住退出就是灾难
        assert srv._stop_qq_for_exit() is False

    def test_半截状态也算在跑(self, srv, monkeypatch):
        """AstrBot 死了、SnowLuma 还活着：qq_online 是 False，但退出时也得清。"""
        monkeypatch.setattr(srv, "_port_open", lambda port, timeout=0.4: port == 5099)

        assert srv.qq_online() is False
        assert srv.qq_any_running() is True


class TestRoutes:
    @pytest.fixture()
    def base(self, srv):
        from http.server import ThreadingHTTPServer
        import threading

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield "http://127.0.0.1:%d" % port
        httpd.shutdown()
        httpd.server_close()

    def _get(self, url):
        import urllib.request

        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())

    def _post(self, url, payload=None):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            url, data=json.dumps(payload or {}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_返回在线状态(self, base, srv, monkeypatch):
        monkeypatch.setattr(srv, "qq_online", lambda *_a, **_k: True)
        status, d = self._get(base + "/api/qq-hosting")
        assert status == 200
        assert d["online"] is True
        assert "script_exists" in d
        assert 6185 in d["ports"]

    def test_非法动作被拒(self, base):
        status, d = self._post(base + "/api/qq-hosting", {"action": "boom"})
        assert status == 400
        assert d["ok"] is False

    def test_启动脚本缺失返回错误(self, base, srv, monkeypatch, tmp_path):
        monkeypatch.setattr(srv, "QQ_SCRIPT", tmp_path / "nope.ps1")
        status, d = self._post(base + "/api/qq-hosting", {"action": "start"})
        assert status == 400
        assert d["ok"] is False

    def test_toggle_在线就走停止(self, base, srv, monkeypatch):
        monkeypatch.setattr(srv, "qq_online", lambda *_a, **_k: True)
        called = {}

        def fake_stop(timeout=20, orphans=True):
            called["stop"] = True
            return 0

        monkeypatch.setattr(srv, "qq_stop", fake_stop)

        status, d = self._post(base + "/api/qq-hosting", {"action": "toggle"})

        assert status == 200 and d["ok"]
        assert d["action"] == "stop"
        assert called.get("stop"), "在线时 toggle 必须是停"

    def test_toggle_离线就走启动(self, base, srv, monkeypatch, tmp_path):
        monkeypatch.setattr(srv, "qq_online", lambda *_a, **_k: False)
        script = tmp_path / "启动.ps1"
        script.write_text("# test", encoding="utf-8")
        monkeypatch.setattr(srv, "QQ_SCRIPT", script)
        called = {}
        monkeypatch.setattr(srv, "_run_powershell", lambda *a, **k: called.setdefault("start", True) or 0)

        status, d = self._post(base + "/api/qq-hosting", {"action": "toggle"})

        assert status == 200 and d["ok"]
        assert d["action"] == "start"
        assert called.get("start")


# --------------------------------------------------------------------------- #
# qq_stop 必须在超时预算内完成（2026-11-04 第二十五轮）
# --------------------------------------------------------------------------- #
def test_qq_stop_退出路径只跑端口段():
    """`qq_stop(orphans=False)` 必须能在 6 秒内返回。

    踩过的坑：整条命令（端口查询 + Get-CimInstance 全量扫 + taskkill 循环）
    **单纯查询部分**实测就要 4.2 秒，加上 taskkill 必然超过退出路径的
    `timeout=6` → 日志里一堆 `subprocess.TimeoutExpired`，而且
    **超时后什么都没做**，QQ 原地留着。

    修法：拆成两段。退出路径只跑端口段（实测 2.54 秒），
    孤儿扫描留给「停 QQ」按钮或下次启动时收。
    """
    import inspect
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))
    import server as _srv

    sig = inspect.signature(_srv.qq_stop)
    assert "orphans" in sig.parameters, (
        "qq_stop 应该有 orphans 参数，让退出路径能只跑快的端口段"
    )
    assert sig.parameters["orphans"].default is True, "默认应该包含孤儿扫描（手动停 QQ 时更彻底）"

    # 源码里退出路径必须是 orphans=False
    src = inspect.getsource(_srv)
    assert "qq_stop(timeout=6, orphans=False)" in src, (
        "退出路径应该只跑端口段（orphans=False）—— "
        "整条命令会超过 6 秒预算，超时后什么都没做"
    )


def test_qq_stop_端口段用单次查询不是循环():
    """端口段要用一次 `Get-NetTCPConnection` + `Where LocalPort -in`，
    不要 foreach 逐个端口查。

    实测：循环 5 个端口 ≈ 3.8 秒，单次查询 ≈ 1.9 秒（快一倍）。
    这个差别直接决定能不能在退出路径的 6 秒预算内完成。
    """
    import inspect
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))
    import server as _srv

    src = inspect.getsource(_srv.qq_stop)
    assert "-LocalPort -in" in src or "LocalPort -in" in src, (
        "端口段应该用 `Where-Object { $_.LocalPort -in ... }` 一次查完，"
        "而不是 foreach 逐个 Get-NetTCPConnection -LocalPort（慢一倍）"
    )
