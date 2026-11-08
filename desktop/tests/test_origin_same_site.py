# -*- coding: utf-8 -*-
"""后台白屏回归：同源子资源的 Origin 写法差异不能被判 403。

背景（2026-11-08 实测，证据是 data/rejected_requests.log 里抄下来的请求头）：
某浏览器给 /console.css、/console.js 这类**同源子资源**发的 origin 是
`http://127.0.0.1`（不带端口），而白名单只认 `http://127.0.0.1:8848`。
结果是 HTML 正常（导航请求不带 Origin）、CSS/JS 全 403，
页面变成一张没样式的白页 —— 而且从服务器侧怎么测都"正常"。

这条测试把那组真实请求头原样发一遍，防止以后又被收紧回去。
"""
from __future__ import annotations

import http.client
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))


@pytest.fixture()
def base():
    """起一个真实的 HTTP 服务实例（端口 0 由系统分配）。

    不用 app 的生产端口，也不碰任何落盘状态。
    """
    import server

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, path: str, headers: dict) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# 白屏现场的真实请求头（来自 server.py 的 rejected_requests.log）
BLANK_PAGE_HEADERS = {
    "Connection": "keep-alive",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"),
    "Accept": "text/css,*/*;q=0.1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Dest": "style",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "origin": "http://127.0.0.1",   # ← 不带端口，当年就是它触发的 403
}


@pytest.mark.parametrize("path", ["/console.css", "/console.js"])
def test_同源子资源_origin不带端口也要放行(base, path):
    """真实白屏请求头复现：必须 200，不能 403。"""
    status, body = _request(base, path, BLANK_PAGE_HEADERS)
    assert status == 200, "同源子资源被判 403，后台会白屏"
    assert len(body) > 1000, "返回内容过短，可能是错误页"


def test_css内容确实是样式(base):
    status, body = _request(base, "/console.css", BLANK_PAGE_HEADERS)
    assert status == 200
    assert b"box-sizing" in body


def test_js内容确实是脚本(base):
    status, body = _request(base, "/console.js", BLANK_PAGE_HEADERS)
    assert status == 200
    assert b"function" in body or b"=>" in body


@pytest.mark.parametrize("path", ["/api/system-model", "/api/daily-review",
                                  "/api/system"])
def test_跨站读受保护的api仍然拒绝(base, path):
    """放宽同源判定，不能顺手把 CSRF 防护拆了。"""
    status, _ = _request(base, path, {"Origin": "http://evil.example"})
    assert status == 403, f"{path} 对跨站 Origin 没拒绝"


def test_本机其它端口不算同源(base):
    status, _ = _request(base, "/api/system-model",
                         {"Origin": "http://127.0.0.1:9999"})
    assert status == 403


def test_localhost无端口也算同源(base):
    status, _ = _request(base, "/console.css", {"Origin": "http://localhost"})
    assert status == 200
