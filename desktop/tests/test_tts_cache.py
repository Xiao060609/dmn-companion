"""语音缓存（保留最近 N 个 + 打开文件夹）的单测。

背景：以前 `/api/tts` 生成完立刻 `unlink()`，`tts_cache` 永远是空的，
后台想回听刚才那句根本没文件。现在留着，但只保留最近若干个，别无限长。

本文件钉住：
1. 只保留最新的 N 个，旧的删掉；按 mtime 判新旧；
2. 不碰 `touch/` 子目录（触摸语音是预合成的，删了要重新合成）；
3. 打开文件夹只允许本机调用，且不会在测试里真的弹出资源管理器。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
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

    cache = tmp_path / "tts_cache"
    cache.mkdir()
    monkeypatch.setattr(server, "TTS_OUTDIR", cache)
    return server


def _make(cache: Path, n: int, prefix: str = "breeze") -> list[Path]:
    """造 n 个文件，mtime 依次递增（越大越新）。"""
    out = []
    base = time.time() - n
    for i in range(n):
        p = cache / f"{prefix}_{i:05d}.wav"
        p.write_bytes(b"RIFF")
        ts = base + i
        os.utime(p, (ts, ts))
        out.append(p)
    return out


class TestPrune:
    def test_只留最新的几个(self, srv, tmp_path):
        files = _make(tmp_path / "tts_cache", 10)
        srv._prune_tts_cache(keep=3)
        left = sorted(p.name for p in (tmp_path / "tts_cache").glob("*.wav"))
        assert left == sorted([files[-1].name, files[-2].name, files[-3].name])

    def test_不足_keep_时不动(self, srv, tmp_path):
        _make(tmp_path / "tts_cache", 3)
        srv._prune_tts_cache(keep=10)
        assert len(list((tmp_path / "tts_cache").glob("*.wav"))) == 3

    def test_不碰_touch_子目录(self, srv, tmp_path):
        cache = tmp_path / "tts_cache"
        touch = cache / "touch"
        touch.mkdir()
        (touch / "head-0-abc.wav").write_bytes(b"RIFF")
        _make(cache, 5)
        srv._prune_tts_cache(keep=1)
        assert (touch / "head-0-abc.wav").is_file(), "预合成的触摸语音不能被清掉"
        assert len(list(cache.glob("*.wav"))) == 1

    def test_目录不存在也不抛(self, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "TTS_OUTDIR", tmp_path / "not-there")
        srv._prune_tts_cache(keep=1)  # 不抛就算过


class TestStartupCleanup:
    def test_启动不再整锅倒掉(self, srv, tmp_path, monkeypatch):
        """以前启动会全删，结果「语音缓存」点开永远是空的。

        现在只收一下：留着最近的，淘汰超出的。重启前生成的那几句要还在。
        """
        files = _make(tmp_path / "tts_cache", 5)
        monkeypatch.setattr(srv, "TTS_KEEP_FILES", 60)
        srv._cleanup_tts_cache()
        left = sorted(p.name for p in (tmp_path / "tts_cache").glob("*.wav"))
        assert left == sorted(p.name for p in files), "启动不该把缓存清空"

    def test_启动也会淘汰超量的(self, srv, tmp_path, monkeypatch):
        _make(tmp_path / "tts_cache", 10)
        monkeypatch.setattr(srv, "TTS_KEEP_FILES", 3)
        srv._cleanup_tts_cache()   # 走真实启动路径
        assert len(list((tmp_path / "tts_cache").glob("*.wav"))) == 3

    def test_启动时目录不存在会建出来(self, srv, tmp_path, monkeypatch):
        target = tmp_path / "new-cache"
        monkeypatch.setattr(srv, "TTS_OUTDIR", target)
        srv._cleanup_tts_cache()
        assert target.is_dir()


class TestOpenFolder:
    @pytest.fixture()
    def base(self, srv):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield "http://127.0.0.1:%d" % port
        # ⚠ 2026-11-04 修（偶发 `ConnectionAbortedError` [WinError 10053]）：
        #
        # 原来这里直接 `shutdown()` + `server_close()`。`shutdown()` 只保证
        # **不再接受新连接**，不管已经建立的连接；`server_close()` 会关掉监听
        # socket，而此时如果某个请求线程还在 `handle_one_request` 里等着读
        # 下一个请求（`protocol_version = "HTTP/1.1"` → keep-alive），
        # 客户端那边正在 `getresponse()` 的 socket 就会被 RST ——
        # 表现成 `ConnectionAbortedError: 你的主机中的软件中止了一个已建立的连接`。
        #
        # 实测：单独跑这条 10/10 通过，但整个类连跑是 **1/8 失败**，
        # 全量跑是 **2/5 失败** —— 典型的时序竞态，不是功能错。
        #
        # 修法：`block_on_close = True` + 显式 join 处理线程，让"关服务器"这件事
        # 等所有在飞请求收尾。`daemon_threads` 仍然保留（万一有卡死的线程，
        # 进程退出时不会被拖住）。
        httpd.shutdown()
        httpd.block_on_close = True
        try:
            # ThreadingMixIn 会把 worker 线程记录在 `_threads` 里
            for t in list(getattr(httpd, "_threads", [])):
                t.join(2.0)
        except Exception:  # noqa: BLE001 —— 收尾失败不该让测试红
            pass
        httpd.server_close()

    def _post(self, url):
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_本机调用会打开文件夹(self, base, srv, tmp_path, monkeypatch):
        opened = []
        monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
        _make(tmp_path / "tts_cache", 2)
        status, d = self._post(base + "/api/open-tts-cache")
        assert status == 200 and d["ok"]
        assert d["count"] == 2
        assert opened == [str(srv.TTS_OUTDIR)], "应该把缓存目录交给资源管理器"

    def test_非本机被拒(self, base, srv, monkeypatch):
        monkeypatch.setattr(srv.Handler, "_client_is_local", lambda self: False)
        status, d = self._post(base + "/api/open-tts-cache")
        assert status == 403
        assert "本机" in d["error"]
