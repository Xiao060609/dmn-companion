"""改台词后的语音缓存：先删旧的，再按新台词重合成。

这条链路最容易出错的地方不是合成，是「删了没补」——旧文件按 hash 命名，
台词一改就再也命中不到，留着只是垃圾；但要是合成失败又删了，那块就彻底哑了。
所以：引擎没就绪时不许假装成功，状态要能被后台查到。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "TOUCH_REGIONS_PATH", tmp_path / "touch_regions.json")
    monkeypatch.setattr(server, "TOUCH_REGIONS", server._default_regions())
    monkeypatch.setattr(server, "TOUCH_VOICE_DIR", tmp_path / "touch")
    with server._TOUCH_VOICE_LOCK:
        server._TOUCH_VOICE_STATUS.clear()
    return server


class FakeTTS:
    """假引擎：写个假 wav，记录合成过什么。"""

    ready = True
    voice_id = "voice-a"

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.calls = []

    def speak_wav(self, text, instruction=None):
        self.calls.append((text, instruction or ""))
        path = self.tmp / ("wav-%d.wav" % len(self.calls))
        path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        return path


class DownTTS:
    ready = False
    voice_id = ""


def _head(srv):
    return next(r for r in srv._default_regions() if r["id"] == "head")


class TestSynthesize:
    def test_合成会写进缓存(self, srv, tmp_path, monkeypatch):
        fake = FakeTTS(tmp_path)
        monkeypatch.setattr(srv, "tts", fake)
        region = _head(srv)

        result = srv.synthesize_touch_voices(region)

        assert result["error"] == ""
        assert result["ready"] == len(region["lines"])
        assert len(srv.touch_voice_files("head")) == len(region["lines"])
        # 情绪要变成语气指令，不是干念
        assert all(instr for _text, instr in fake.calls)

    def test_已有缓存不重复合成(self, srv, tmp_path, monkeypatch):
        fake = FakeTTS(tmp_path)
        monkeypatch.setattr(srv, "tts", fake)
        region = _head(srv)

        srv.synthesize_touch_voices(region)
        first = len(fake.calls)
        srv.synthesize_touch_voices(region)

        assert len(fake.calls) == first

    def test_引擎没就绪不假装成功(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "tts", DownTTS())
        result = srv.synthesize_touch_voices(_head(srv))
        assert result["ready"] == 0
        assert "引擎" in result["error"]


class TestClear:
    def test_清掉这个区域的旧语音(self, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "tts", FakeTTS(tmp_path))
        region = _head(srv)
        srv.synthesize_touch_voices(region)

        removed = srv.clear_touch_voices("head")

        assert removed == len(region["lines"])
        assert srv.touch_voice_files("head") == []

    def test_只清自己那个区域(self, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "tts", FakeTTS(tmp_path))
        regions = srv._default_regions()
        for region in regions[:2]:
            srv.synthesize_touch_voices(region)
        keep = regions[0]["id"]

        srv.clear_touch_voices(regions[1]["id"])

        assert srv.touch_voice_files(keep), "别的区域不该被误删"


class TestRebuild:
    def test_先删旧再建新(self, srv, tmp_path, monkeypatch):
        fake = FakeTTS(tmp_path)
        monkeypatch.setattr(srv, "tts", fake)
        region = dict(_head(srv))
        srv.synthesize_touch_voices(region)
        old_files = set(srv.touch_voice_files("head"))
        assert old_files

        region["lines"] = [{"text": "换了句新的", "emotion": "happy"}]
        srv.rebuild_touch_voices(region)

        new_files = set(srv.touch_voice_files("head"))
        assert len(new_files) == 1
        assert not (new_files & old_files), "旧文件必须删掉，不能留着"
        assert srv.touch_voice_status(region)["state"] == "done"

    def test_失败时状态是_error(self, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "tts", DownTTS())
        region = dict(_head(srv))

        srv.rebuild_touch_voices(region)

        status = srv.touch_voice_status(region)
        assert status["state"] == "error"
        assert status["error"]


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

    def _get(self, url):
        import urllib.request

        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())

    def _wait_done(self, srv, region, timeout=5.0):
        """等后台合成结束。不能只等「有文件」——旧文件还在，会被误判成已完成。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if srv.touch_voice_status(region)["state"] == "done":
                return True
            time.sleep(0.05)
        return False

    def test_改台词会删旧语音并重合成(self, base, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "tts", FakeTTS(tmp_path))
        regions = srv._default_regions()
        head = next(r for r in regions if r["id"] == "head")
        srv.synthesize_touch_voices(head)
        old = set(srv.touch_voice_files("head"))
        assert old

        head["lines"] = [{"text": "新的一句", "emotion": "happy"}]
        status, d = self._post(base + "/api/touch-regions", {"regions": regions})

        assert status == 200 and d["ok"]
        assert "head" in d["rebuilding"]
        assert self._wait_done(srv, head)
        new = set(srv.touch_voice_files("head"))
        assert len(new) == 1
        assert not (new & old)

    def test_只改形状不会重合成(self, base, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "tts", FakeTTS(tmp_path))
        regions = srv._default_regions()
        regions[0]["shapes"] = [{"type": "rect", "x": 0, "y": 0, "w": 0.2, "h": 0.2}]

        status, d = self._post(base + "/api/touch-regions", {"regions": regions})

        assert status == 200 and d["ok"]
        assert d["rebuilding"] == []

    def test_引擎没好时给出错误不假装(self, base, srv, monkeypatch):
        monkeypatch.setattr(srv, "tts", DownTTS())
        regions = srv._default_regions()
        head = next(r for r in regions if r["id"] == "head")
        head["lines"] = [{"text": "新的一句", "emotion": "happy"}]

        status, d = self._post(base + "/api/touch-regions", {"regions": regions})

        assert status == 200 and d["ok"]
        assert d["voice_errors"] and d["voice_errors"][0]["id"] == "head"
        assert d["rebuilding"] == []

    def test_状态接口能查进度(self, base, srv):
        status, d = self._get(base + "/api/touch-voice-status")
        assert status == 200
        assert "head" in d["regions"]
        assert set(d["regions"]["head"]) >= {"total", "ready", "state"}

    def test_列表带情绪选项和语音状态(self, base, srv):
        status, d = self._get(base + "/api/touch-regions")
        assert status == 200
        assert d["emotions"], "后台下拉框要能拿到情绪表"
        head = next(r for r in d["regions"] if r["id"] == "head")
        assert set(head["voice"]) >= {"total", "ready", "state"}
