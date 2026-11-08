"""8848 的系统层代理：后台不该持有网关 token。

浏览器只跟 8848 说话，token 只在服务端读、只在服务端用。
这里钉住：路径转对了、方法转对了、网关挂了给 502 而不是 500。
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
def srv(tmp_path, monkeypatch):
    import server

    seen = {}

    def fake_request(path, method="GET", payload=None, timeout=15):
        seen["path"] = path
        seen["method"] = method
        if path == "/admin/system":
            return 200, {
                "emotion": "shy", "focus": "在想事情",
                "inner_os": [{"text": "他今天有点累", "ts": 1}],
                "inner_os_count": 1,
                "last_tick_ts": 2, "last_speak_ts": 0,
                "speak_count_today": 1, "max_speak_per_day": 6,
                "heartbeat_s": 300, "consolidate_s": 86400,
                "last_consolidate_ts": 0,
            }
        return 200, {"ok": True, "message": "ok", "cleared": 2}

    monkeypatch.setattr(server, "gateway_request", fake_request)
    return server, seen


@pytest.fixture()
def base(srv):
    from http.server import ThreadingHTTPServer
    import threading

    server, _seen = srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % port
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(url):
    import urllib.request

    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


class TestProxy:
    def test_状态转发到_admin_system(self, base, srv):
        _server, seen = srv
        status, d = _get(base + "/api/system")
        assert status == 200
        assert seen["path"] == "/admin/system"
        assert seen["method"] == "GET"
        assert d["emotion"] == "shy"
        assert d["inner_os_count"] == 1

    def test_手动心跳转发成POST(self, base, srv):
        _server, seen = srv
        status, d = _post(base + "/api/system/tick")
        assert status == 200 and d["ok"]
        assert seen["path"] == "/admin/system/tick"
        assert seen["method"] == "POST"

    def test_巩固转发路径(self, base, srv):
        _server, seen = srv
        status, d = _post(base + "/api/system/consolidate")
        assert status == 200 and d["ok"]
        assert seen["path"] == "/admin/system/consolidate"

    def test_清空内心OS转发路径(self, base, srv):
        _server, seen = srv
        status, d = _post(base + "/api/system/inner-os/clear")
        assert status == 200
        assert seen["path"] == "/admin/system/inner-os/clear"
        assert d["cleared"] == 2

    def test_网关挂了返回502(self, base, srv, monkeypatch):
        server, _seen = srv

        def boom(*_a, **_k):
            return 502, {"error": "网关没连上：refused"}

        monkeypatch.setattr(server, "gateway_request", boom)
        status, d = _get(base + "/api/system")
        assert status == 502
        assert "网关没连上" in d["error"]
