"""触摸分区 + 语音预合成的单测（假引擎，不联网、不启真 TTS）。

两件事要钉住：

1. **分区判定**：摸头/胸/腹/手臂/腿/脚要落到不同区域，而且区域是**归一化坐标**
   （相对命中框 0~1），这样窗口缩放、换分辨率都不用改数字。
2. **语音预合成**：摸她的语音必须提前合成好（现算要好几秒，反馈感全没）。
   缓存文件名带 hash，换声线自动失效；引擎没就绪或合成失败都不能影响别的功能。
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

    monkeypatch.setattr(server, "TOUCH_VOICE_DIR", tmp_path / "touch")
    # 别读本机那份自定义区域：用例只该验内置行为和判定逻辑
    monkeypatch.setattr(server, "TOUCH_REGIONS_PATH", tmp_path / "touch_regions.json")
    monkeypatch.setattr(server, "TOUCH_REGIONS", server._default_regions())
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    return server


class FakeTTS:
    ready = True
    voice_id = "voice-a"

    def __init__(self, tmp_path, fail=False):
        self.tmp = tmp_path
        self.fail = fail
        self.calls = []

    def speak_wav(self, text, instruction=None):
        self.calls.append((text, instruction))
        if self.fail:
            raise RuntimeError("引擎忙")
        p = self.tmp / ("wav-%d.wav" % len(self.calls))
        p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        return p


def _ready(server, tts):
    server.tts = tts
    server.tts_ready.set()


def _total_lines(srv):
    return sum(len(r["lines"]) for r in srv.TOUCH_REGIONS)


class TestRegions:
    def test_区域齐全(self, srv):
        ids = [r["id"] for r in srv.TOUCH_REGIONS]
        for want in ("head", "chest", "belly", "arm_l", "arm_r", "legs", "feet"):
            assert want in ids, "缺区域 %s" % want

    def test_坐标都在_0_1_内且不越界(self, srv):
        """按图形校验，别按 x0..y1 —— 自定义区域存的是 shapes，没有那两个字段。"""
        for region in srv.TOUCH_REGIONS:
            for shape in region.get("shapes") or []:
                assert 0 <= shape["x"] <= 1, region["id"]
                assert 0 <= shape["y"] <= 1, region["id"]
                assert 0 < shape["w"] <= 1, region["id"]
                assert 0 < shape["h"] <= 1, region["id"]
                assert shape["x"] + shape["w"] <= 1.0001, region["id"]
                assert shape["y"] + shape["h"] <= 1.0001, region["id"]

    def test_按高度分头胸腹(self, srv):
        assert srv.touch_region_at(0.5, 0.10)["id"] == "head"
        assert srv.touch_region_at(0.5, 0.40)["id"] == "chest"
        assert srv.touch_region_at(0.5, 0.62)["id"] == "belly"
        assert srv.touch_region_at(0.5, 0.80)["id"] == "legs"
        assert srv.touch_region_at(0.5, 0.97)["id"] == "feet"

    def test_两侧算手臂(self, srv):
        assert srv.touch_region_at(0.08, 0.50)["id"] == "arm_l"
        assert srv.touch_region_at(0.92, 0.50)["id"] == "arm_r"

    def test_框外不再冒充左臂(self, srv):
        """以前没命中会退回 TOUCH_REGIONS[0]（左臂），点空白处会说「手臂也不行啦」。

        现在返回 None，由前端决定「只做动作、不出声」。
        """
        assert srv.touch_region_at(-1, -1) is None
        assert srv.touch_region_at(0.5, 0.5) is not None

    def test_每个区域都有动作和台词(self, srv):
        for r in srv.TOUCH_REGIONS:
            assert r.get("motion"), r["id"]
            assert r["lines"], r["id"]
            for line in r["lines"]:
                assert line.get("text") and line.get("emotion"), r["id"]


class TestCacheKey:
    def test_不同区域不同句路径不同(self, srv):
        paths = set()
        for r in srv.TOUCH_REGIONS:
            for i in range(len(r["lines"])):
                paths.add(srv._touch_voice_path(r["id"], i))
        assert len(paths) == _total_lines(srv)

    def test_换声线会换文件名(self, srv):
        class TTS:
            ready = True

        tts = TTS()
        tts.voice_id = "voice-a"
        srv.tts = tts
        before = srv._touch_voice_path("head", 0)
        tts.voice_id = "voice-b"
        assert srv._touch_voice_path("head", 0) != before, "换声线必须让旧缓存失效"

    def test_非法区域或索引返回_None(self, srv):
        assert srv._touch_voice_path("nope", 0) is None
        assert srv._touch_voice_path("head", -1) is None
        assert srv._touch_voice_path("head", 999) is None


class TestPrewarm:
    def test_所有区域的台词都合成(self, srv, tmp_path):
        tts = FakeTTS(tmp_path)
        _ready(srv, tts)
        srv.prewarm_touch_voices()
        assert len(tts.calls) == _total_lines(srv)
        for r in srv.TOUCH_REGIONS:
            for i in range(len(r["lines"])):
                assert srv._touch_voice_path(r["id"], i).is_file()

    def test_已有缓存不重复合成(self, srv, tmp_path):
        tts = FakeTTS(tmp_path)
        _ready(srv, tts)
        srv.prewarm_touch_voices()
        first = len(tts.calls)
        srv.prewarm_touch_voices()
        assert len(tts.calls) == first

    def test_引擎没就绪直接返回(self, srv, tmp_path, monkeypatch):
        tts = FakeTTS(tmp_path)
        srv.tts = tts

        class NotReady:
            def wait(self, timeout=None):
                return False

            def is_set(self):
                return False

        monkeypatch.setattr(srv, "tts_ready", NotReady())
        srv.prewarm_touch_voices()
        assert tts.calls == []

    def test_合成失败不抛出(self, srv, tmp_path):
        tts = FakeTTS(tmp_path, fail=True)
        _ready(srv, tts)
        srv.prewarm_touch_voices()
        assert tts.calls
        assert not srv._touch_voice_path("head", 0).exists()


class TestRoutes:
    @pytest.fixture()
    def base(self, srv, tmp_path):
        tts = FakeTTS(tmp_path)
        _ready(srv, tts)
        srv.prewarm_touch_voices()
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
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_区域接口(self, base):
        status, body = self._get(base + "/api/touch-regions")
        assert status == 200
        regions = json.loads(body)["regions"]
        assert len(regions) == 7
        first = regions[0]
        for key in ("id", "label", "x0", "x1", "y0", "y1", "motion", "lines"):
            assert key in first, key

    def test_语音接口(self, base):
        status, body = self._get(base + "/api/touch-voice?r=head&i=0")
        assert status == 200
        assert body.startswith(b"RIFF")

    def test_非法参数_404(self, base):
        assert self._get(base + "/api/touch-voice?r=nope&i=0")[0] == 404
        assert self._get(base + "/api/touch-voice?r=head&i=99")[0] == 404
        assert self._get(base + "/api/touch-voice")[0] == 404

    def test_没合成时_404(self, srv, tmp_path):
        srv.tts = FakeTTS(tmp_path)
        srv.tts_ready.clear()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            status, _ = self._get("http://127.0.0.1:%d/api/touch-voice?r=head&i=0" % port)
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert status == 404
