"""系统层（DMN）的三个手动开关：tick / consolidate / 清空内心OS。

这几个都会花模型调用，所以：
1. 必须带桌宠 token，没 token 一律 401；
2. 真正干活放后台线程，HTTP 立刻返回，不能把后台页面卡住；
3. 清空只清队列，不动状态文件里的其它东西。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    import gateway

    calls = {}
    monkeypatch.setattr(gateway, "PET_TOKEN", "test-token")
    monkeypatch.setattr(gateway.system, "tick", lambda: calls.__setitem__("tick", True))
    monkeypatch.setattr(gateway.system, "consolidate", lambda: calls.__setitem__("consolidate", True))
    monkeypatch.setattr(gateway.system, "clear_inner_os", lambda: 3)
    monkeypatch.setattr(gateway.system, "system_status", lambda: {
        "emotion": "calm", "inner_os": [], "inner_os_count": 0,
    })
    return gateway, calls


@pytest.fixture()
def base(gw):
    from http.server import ThreadingHTTPServer
    import threading

    gateway, _calls = gw
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % port
    httpd.shutdown()
    httpd.server_close()


def _post(url, payload=None, token="test-token"):
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Gateway-Token"] = token
    req = urllib.request.Request(
        url, data=json.dumps(payload or {}).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url, token="test-token"):
    import urllib.error
    import urllib.request

    headers = {"X-Gateway-Token": token} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestAuth:
    def test_没有token一律401(self, base):
        for path in ("/admin/system/tick", "/admin/system/consolidate",
                     "/admin/system/inner-os/clear"):
            status, d = _post(base + path, token="")
            assert status == 401, path
            assert d["ok"] is False

    def test_错token也401(self, base):
        status, _d = _post(base + "/admin/system/tick", token="wrong")
        assert status == 401


class TestActions:
    def test_手动心跳会起后台线程(self, base, gw):
        _gateway, calls = gw
        status, d = _post(base + "/admin/system/tick")
        assert status == 200 and d["ok"]
        for _ in range(50):
            if calls.get("tick"):
                break
            time.sleep(0.02)
        assert calls.get("tick"), "tick 没被调用"

    def test_手动巩固会起后台线程(self, base, gw):
        _gateway, calls = gw
        status, d = _post(base + "/admin/system/consolidate")
        assert status == 200 and d["ok"]
        for _ in range(50):
            if calls.get("consolidate"):
                break
            time.sleep(0.02)
        assert calls.get("consolidate"), "consolidate 没被调用"

    def test_清空内心OS返回条数(self, base):
        status, d = _post(base + "/admin/system/inner-os/clear")
        assert status == 200 and d["ok"]
        assert d["cleared"] == 3

    def test_未知动作404(self, base):
        status, d = _post(base + "/admin/system/boom")
        assert status == 404 and d["ok"] is False


class TestStatus:
    def test_状态要带token(self, base):
        status, _d = _get(base + "/admin/system", token="")
        assert status == 401

    def test_状态返回系统层摘要(self, base):
        status, d = _get(base + "/admin/system")
        assert status == 200
        assert "emotion" in d
