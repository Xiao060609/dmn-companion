"""联网搜索工具的单测（全 mock，不联网）。

背景：网关原先只给了 5 个本地工具（天气/文件），用户让鱼上网查东西时
它只能说「我做不到」。现在加了 `web_search`。

**后端优先级（2026-11-03 调整）**：

    ① Tavily    —— 首选，响应自带 `answer`（已综合的短答案）
    ② Brave     —— 有 key 才走
    ③ Bing HTML → ④ Bing RSS —— 免 key 兜底

这里钉住四件事：
1. 兜底链的顺序和「一个挂了还能继续」；
2. Tavily 的 `answer` 要单独成块，且**不影响**来源列表格式；
3. 结果格式（截断长度），别把整页 HTML 塞进上下文；
4. **搜索失败不能把整轮对话炸掉** —— 曾经 `_web_search` 抛 ValueError 直接
   冒到 handle_chat，用户收到的是 500 而不是「没搜到」。

⚠ 测试进程里必须把 Tavily 关掉：`secrets.json` 里现在有**真的** key，
不 mock 的话每个用例都会发真实请求（慢、花钱、结果不确定）。
见下面的 `_no_tavily` autouse fixture。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import tools  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例都从空缓存开始，别让上一个用例的结果串味。"""
    tools._SEARCH_CACHE.clear()
    yield
    tools._SEARCH_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_tavily(monkeypatch):
    """默认关掉**所有真 key**，让老用例继续验 Brave/Bing 兜底链。

    `secrets.json` 里现在有真的 Tavily 和 Brave key。
    不 mock 的话用例会发真实请求 —— 慢、花钱、结果还不确定
    （实测：`随便` 这条查询真的打到了百度百科）。
    要测某个后端的用例自己再 monkeypatch 覆盖（后写的赢）。
    """
    monkeypatch.setattr(tools, "_tavily_key", lambda: "")
    monkeypatch.setattr(tools, "_brave_key", lambda: "")


ROWS = [
    ("标题一", "摘要一" * 60, "https://a.example.com/1"),
    ("标题二", "", "https://a.example.com/2"),
]


class TestTavily:
    """Tavily 是首选后端（2026-11-03 加）。"""

    def test_tavily_成功就不走后面(self, monkeypatch):
        monkeypatch.setattr(tools, "_tavily_key", lambda: "tk")
        monkeypatch.setattr(
            tools, "_search_tavily", lambda q, n: (list(ROWS), "这是综合答案")
        )

        def boom(*_a):
            raise AssertionError("Tavily 成功了不该再走 Brave/Bing")

        monkeypatch.setattr(tools, "_brave_key", boom)
        monkeypatch.setattr(tools, "_search_bing", boom)
        out = tools._web_search("随便", 2)
        assert "标题一" in out

    def test_answer_单独成块且排在来源前(self, monkeypatch):
        """`answer` 是已综合过的答案，比一堆待拼的摘要省事得多。"""
        monkeypatch.setattr(tools, "_tavily_key", lambda: "tk")
        monkeypatch.setattr(
            tools, "_search_tavily", lambda q, n: (list(ROWS), "这是综合答案")
        )
        out = tools._web_search("随便", 2)
        assert out.startswith("【综合】这是综合答案"), "综合答案要在最前面"
        assert "标题一" in out, "来源列表也要保留，便于核对"

    def test_没有answer时格式回到纯列表(self, monkeypatch):
        """Tavily 不返回 answer 时，不该留个空的【综合】块。"""
        monkeypatch.setattr(tools, "_tavily_key", lambda: "tk")
        monkeypatch.setattr(tools, "_search_tavily", lambda q, n: (list(ROWS), ""))
        out = tools._web_search("随便", 2)
        assert "【综合】" not in out
        assert out.startswith("1. ")

    def test_tavily_挂了退到_brave(self, monkeypatch):
        monkeypatch.setattr(tools, "_tavily_key", lambda: "tk")

        def dead(*_a):
            raise OSError("tavily 502")

        monkeypatch.setattr(tools, "_search_tavily", dead)
        monkeypatch.setattr(tools, "_brave_key", lambda: "bk")
        monkeypatch.setattr(tools, "_search_brave", lambda q, n: list(ROWS))
        monkeypatch.setattr(
            tools, "_search_bing", lambda q, n: (_ for _ in ()).throw(AssertionError("不该到 Bing"))
        )
        assert "标题一" in tools._web_search("随便", 2)

    def test_没key就跳过_tavily(self, monkeypatch):
        monkeypatch.setattr(tools, "_tavily_key", lambda: "")
        # ⚠ Brave 的 key 也得 mock：`secrets.json` 里有真的，
        # 不 mock 就会发真实请求（这条用例第一版就是这么联网的）。
        monkeypatch.setattr(tools, "_brave_key", lambda: "")

        def boom(*_a):
            raise AssertionError("没 key 不该调 Tavily")

        monkeypatch.setattr(tools, "_search_tavily", boom)
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: list(ROWS))
        assert "标题一" in tools._web_search("随便", 2)


class TestBackendFallback:
    def test_有_key_且_brave_成功就不走_bing(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "k")
        monkeypatch.setattr(tools, "_search_brave", lambda q, n: list(ROWS))

        def boom(*_a):
            raise AssertionError("不该退到 Bing")

        monkeypatch.setattr(tools, "_search_bing", boom)
        out = tools._web_search("随便", 2)
        assert "标题一" in out

    def test_brave_挂了退到_bing_html(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "k")

        def dead(*_a):
            raise OSError("502")

        monkeypatch.setattr(tools, "_search_brave", dead)
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: list(ROWS))
        monkeypatch.setattr(
            tools, "_search_bing_rss", lambda q, n: (_ for _ in ()).throw(AssertionError("不该到 RSS"))
        )
        assert "标题一" in tools._web_search("随便", 2)

    def test_html_空结果再退到_rss(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: [])
        monkeypatch.setattr(tools, "_search_bing_rss", lambda q, n: list(ROWS))
        assert "标题二" in tools._web_search("随便", 2)

    def test_全挂才抛错(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: [])
        monkeypatch.setattr(tools, "_search_bing_rss", lambda q, n: [])
        with pytest.raises(ValueError):
            tools._web_search("随便", 2)


class TestFormat:
    def test_摘要被截断(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: list(ROWS))
        out = tools._web_search("随便", 2)
        line = out.splitlines()[0]
        assert line.count("｜") == 2, "格式应是 标题｜摘要｜链接"
        assert len(line) < 260, "摘要没截断，会把上下文撑爆"

    def test_没摘要也给出链接(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: [ROWS[1]])
        out = tools._web_search("随便", 1)
        assert out == "1. 标题二｜https://a.example.com/2"

    def test_同一个词走缓存(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        calls = {"n": 0}

        def counting(q, n):
            calls["n"] += 1
            return list(ROWS)

        monkeypatch.setattr(tools, "_search_bing", counting)
        tools._web_search("同一个问题", 2)
        tools._web_search("同一个问题", 2)
        assert calls["n"] == 1

    def test_条数被夹到_1_8(self, monkeypatch):
        seen = {}

        def spy(q, n):
            seen["n"] = n
            return list(ROWS)

        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", spy)
        tools._web_search("x", 99)
        assert seen["n"] == 8


class TestRunNeverRaises:
    def test_搜不到返回错误而不是抛异常(self, monkeypatch):
        """这条是回归：以前 ValueError 会冒到 handle_chat，整轮对话 500。"""
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: [])
        monkeypatch.setattr(tools, "_search_bing_rss", lambda q, n: [])
        out = tools.run("web_search", {"query": "搜不到的东西"})
        assert out["ok"] is False
        assert "没有返回结果" in out["error"]

    def test_空查询给提示(self):
        out = tools.run("web_search", {"query": "   "})
        assert out["ok"] is False and "搜什么" in out["error"]

    def test_正常返回_ok(self, monkeypatch):
        monkeypatch.setattr(tools, "_brave_key", lambda: "")
        monkeypatch.setattr(tools, "_search_bing", lambda q, n: list(ROWS))
        out = tools.run("web_search", {"query": "重庆天气", "count": 2})
        assert out["ok"] is True and "标题一" in out["result"]

    def test_工具清单里确实有它(self):
        names = [t["function"]["name"] for t in tools.TOOLS]
        assert "web_search" in names
