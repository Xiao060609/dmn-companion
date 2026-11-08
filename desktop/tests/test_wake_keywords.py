"""群聊唤醒关键词的单测（不碰真实文件）。

背景：关键词原本写在 AstrBot 插件配置里，`__init__` 时读一次进全局，
改文件既不会热重载（监听只覆盖插件目录内）、也不会生效。
现在真源改成 `plugin_data/group_wake_keywords.json`，桌宠后台写它，
插件每条消息按 mtime 现读 —— 改完立刻生效，不用重启 AstrBot。

本文件钉住：
1. 清洗规则（去空白/去重/限个数/限长度）；
2. 文件不存在时回落到插件配置里的旧值，界面不会空着；
3. 写入是原子的（先写 .tmp 再 replace），并且能读回来；
4. 两个路由的 GET/POST 行为，包括空列表这个合法选择。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "WAKE_KEYWORDS_PATH", tmp_path / "wake" / "kw.json")
    monkeypatch.setattr(server, "WAKE_PLUGIN_CONFIG", tmp_path / "plugin_cfg.json")
    return server


class TestClean:
    def test_去空白和去重(self, srv):
        assert srv._clean_keywords([" 小鱼 ", "", "小鱼", "小菲鱼", "  "]) == ["小鱼", "小菲鱼"]

    def test_非列表返回空(self, srv):
        assert srv._clean_keywords(None) == []
        assert srv._clean_keywords("小鱼") == []

    def test_限制个数(self, srv):
        words = ["w%d" % i for i in range(srv.WAKE_KEYWORDS_MAX + 5)]
        assert len(srv._clean_keywords(words)) == srv.WAKE_KEYWORDS_MAX

    def test_限制长度(self, srv):
        long_word = "阿" * (srv.WAKE_KEYWORD_LEN_MAX + 10)
        out = srv._clean_keywords([long_word])
        assert len(out[0]) == srv.WAKE_KEYWORD_LEN_MAX


class TestLoad:
    def test_没有文件时回落到插件配置(self, srv, tmp_path):
        cfg = tmp_path / "plugin_cfg.json"
        # 真实插件配置带 BOM，这里照抄这个坑
        cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"keywords": ["小菲鱼", "小鱼"]}).encode("utf-8"))
        words, source = srv.load_wake_keywords()
        assert words == ["小菲鱼", "小鱼"]
        assert source == "plugin-config"

    def test_文件优先于插件配置(self, srv, tmp_path):
        srv.save_wake_keywords(["阿鱼"])
        (tmp_path / "plugin_cfg.json").write_text(
            json.dumps({"keywords": ["旧值"]}), encoding="utf-8")
        words, source = srv.load_wake_keywords()
        assert words == ["阿鱼"]
        assert source == "file"

    def test_什么都没有就是空(self, srv):
        words, source = srv.load_wake_keywords()
        assert words == []
        assert source == "none"


class TestSave:
    def test_写入后能读回(self, srv):
        srv.save_wake_keywords(["小菲鱼", "小鱼"])
        assert srv.load_wake_keywords() == (["小菲鱼", "小鱼"], "file")

    def test_不留临时文件(self, srv):
        srv.save_wake_keywords(["小鱼"])
        leftovers = list(srv.WAKE_KEYWORDS_PATH.parent.glob("*.tmp"))
        assert leftovers == [], "原子写入不该留下 .tmp"

    def test_空列表也能存(self, srv):
        """清空是合法选择：那就只靠 @ 唤醒。"""
        srv.save_wake_keywords([])
        words, source = srv.load_wake_keywords()
        assert words == []
        assert source == "none", "空文件会被当成没有，回落到插件配置"

    def test_文件是合法_utf8_json(self, srv):
        srv.save_wake_keywords(["小鱼"])
        data = json.loads(srv.WAKE_KEYWORDS_PATH.read_text(encoding="utf-8"))
        assert data["keywords"] == ["小鱼"]


class TestRoutes:
    @pytest.fixture()
    def base(self, srv):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield "http://127.0.0.1:%d" % port
        httpd.shutdown()
        httpd.server_close()

    def _get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, url, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_返回上限供界面使用(self, base, srv):
        status, d = self._get(base + "/api/wake-keywords")
        assert status == 200
        assert d["max"] == srv.WAKE_KEYWORDS_MAX
        assert d["max_len"] == srv.WAKE_KEYWORD_LEN_MAX

    def test_post_保存后_get_能读到(self, base):
        status, d = self._post(base + "/api/wake-keywords", {"keywords": ["小鱼", "阿鱼"]})
        assert status == 200 and d["ok"]
        _, got = self._get(base + "/api/wake-keywords")
        assert got["keywords"] == ["小鱼", "阿鱼"]

    def test_post_空列表给不同提示(self, base):
        status, d = self._post(base + "/api/wake-keywords", {"keywords": []})
        assert status == 200 and d["ok"]
        assert "@" in d["message"], "清空要明确告诉她只靠 @ 唤醒"

    def test_post_会清洗脏输入(self, base):
        self._post(base + "/api/wake-keywords", {"keywords": [" 小鱼 ", "小鱼", ""]})
        _, got = self._get(base + "/api/wake-keywords")
        assert got["keywords"] == ["小鱼"]
