"""`tools._json_body`：把「上游回 HTML 错误页」变成人话（P2 技术债 #6）。

修之前三处都是裸 `json.loads(resp.read())`：
`_weather` / `_search_tavily` / `_search_brave`。

上游抽风时回的是整页 HTML（502、限流页、维护页），裸解抛 `JSONDecodeError`。
外层 `run()` 有兜底不会 500，但会**原样回给模型「ValueError: ...」**——
模型看不懂那串栈信息，这一轮对话就废了，用户只会看到她答非所问。

现在的契约：拿到非 JSON 抛 `ValueError`，消息里带**上游真说了什么**
（截一段），让模型能据此换话术（"我查不到"），而不是吐一串异常名。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import tools  # noqa: E402


class _FakeResp:
    """最小可用的 urlopen 响应替身：只需要 read()。"""

    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_正常JSON能解析成dict():
    d = tools._json_body(_FakeResp('{"weather": []}'), "天气")
    assert d == {"weather": []}


def test_HTML错误页抛ValueError且带上游原文():
    html = b"<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"
    with pytest.raises(ValueError) as ei:
        tools._json_body(_FakeResp(html), "天气")
    msg = str(ei.value)
    assert "不是 JSON" in msg
    assert "502 Bad Gateway" in msg, "要把上游真说了什么带出来，模型才知道怎么回事"
    assert "天气" in msg


def test_错误消息不是异常名():
    """关键：消息里不能只是 'JSONDecodeError' 这种模型看不懂的东西。"""
    with pytest.raises(ValueError) as ei:
        tools._json_body(_FakeResp("not json at all"), "搜索")
    msg = str(ei.value)
    assert "JSONDecodeError" not in msg
    assert "搜索" in msg


def test_空响应也能给人话():
    with pytest.raises(ValueError) as ei:
        tools._json_body(_FakeResp(b""), "搜索")
    assert "空响应" in str(ei.value)


def test_JSON但不是对象也报错():
    """上游有时会回一个裸数组/null —— 那不是我们要的接口形状。"""
    with pytest.raises(ValueError) as ei:
        tools._json_body(_FakeResp("[1,2,3]"), "天气")
    assert "不是对象" in str(ei.value)


def test_非utf8字节不会崩():
    """有些错误页是 gbk/乱码，decode 本身不能抛。"""
    with pytest.raises(ValueError):
        tools._json_body(_FakeResp(b"\xff\xfe<html>err</html>"), "搜索")


def test_长HTML被截断不刷屏():
    html = "<html>" + ("x" * 5000) + "</html>"
    with pytest.raises(ValueError) as ei:
        tools._json_body(_FakeResp(html), "天气")
    assert len(str(ei.value)) < 300, "错误消息不该把整页 HTML 塞进 prompt"


def test_天气接口真的用了_json_body(monkeypatch):
    """行为级验证：让 urlopen 回 HTML，_weather 应该抛 ValueError 而不是 JSONDecodeError。"""
    monkeypatch.setattr(
        tools.urllib.request, "urlopen",
        lambda *a, **k: _FakeResp("<html>502</html>"),
    )
    tools._WEATHER_CACHE.clear()
    with pytest.raises(ValueError) as ei:
        tools._weather("上海")
    assert "不是 JSON" in str(ei.value)
