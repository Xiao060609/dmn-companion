"""自定义触摸区域：图形的存取、清洗、命中。

这个功能最容易翻车的不是画不出来，是「画完和判定对不上」：
- 坐标必须相对命中框归一化，前后端用同一份 hit_box；
- 存盘要能挡住越界/超量/非法 id，否则一个坏文件让桌宠整个摸不到人；
- 椭圆必须按外接矩形套椭圆方程判，不能退化成矩形（看着圆、点边角也算）。
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

    monkeypatch.setattr(server, "TOUCH_REGIONS_PATH", tmp_path / "touch_regions.json")
    monkeypatch.setattr(server, "TOUCH_REGIONS", server._default_regions())
    return server


class TestNormalize:
    def test_坐标越界会被夹回_0_1(self, srv):
        shape = srv._normalize_shape({"type": "rect", "x": -0.5, "y": 0.8, "w": 0.9, "h": 0.9})
        assert shape["x"] == 0
        assert shape["y"] == 0.8
        # 超出右边/下边的部分被裁掉，不能留下 x+w>1
        assert round(shape["x"] + shape["w"], 4) <= 1
        assert round(shape["y"] + shape["h"], 4) <= 1

    def test_太小的图形丢掉(self, srv):
        assert srv._normalize_shape({"type": "rect", "x": 0.1, "y": 0.1, "w": 0.001, "h": 0.5}) is None
        assert srv._normalize_shape("不是字典") is None

    def test_椭圆类型保留_其它一律矩形(self, srv):
        assert srv._normalize_shape({"type": "ellipse", "x": 0, "y": 0, "w": .2, "h": .2})["type"] == "ellipse"
        assert srv._normalize_shape({"type": "star", "x": 0, "y": 0, "w": .2, "h": .2})["type"] == "rect"

    def test_非法id和重复id丢掉(self, srv):
        raw = [
            {"id": "head", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": .3, "h": .3}]},
            {"id": "head", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": .3, "h": .3}]},
            {"id": "Bad Id!", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": .3, "h": .3}]},
        ]
        out = srv._normalize_regions(raw)
        assert [r["id"] for r in out] == ["head"]

    def test_每个区域图形数量有上限(self, srv):
        shapes = [{"type": "rect", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}
                  for _ in range(srv.TOUCH_SHAPES_MAX + 5)]
        out = srv._normalize_regions([{"id": "head", "shapes": shapes}])
        assert len(out[0]["shapes"]) == srv.TOUCH_SHAPES_MAX

    def test_台词从内置表继承(self, srv):
        """只传 shapes 时，台词/动作不能丢 —— 丢了就不说话了。"""
        out = srv._normalize_regions([{"id": "head", "shapes": []}])
        assert out[0]["lines"], "台词被清空了"
        assert out[0]["motion"]


class TestHit:
    def test_矩形命中(self, srv):
        shape = {"type": "rect", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}
        assert srv._shape_hit(shape, 0.3, 0.3)
        assert not srv._shape_hit(shape, 0.5, 0.3)

    def test_椭圆边角不算命中(self, srv):
        """外接矩形 (0,0,0.4,0.4) 的椭圆：中心算，四角不算。"""
        shape = {"type": "ellipse", "x": 0.0, "y": 0.0, "w": 0.4, "h": 0.4}
        assert srv._shape_hit(shape, 0.2, 0.2)
        assert not srv._shape_hit(shape, 0.01, 0.01)
        assert not srv._shape_hit(shape, 0.39, 0.39)

    def test_按区域顺序取第一个命中(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "TOUCH_REGIONS", [
            {"id": "a", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 1, "h": 1}]},
            {"id": "b", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 1, "h": 1}]},
        ])
        assert srv.touch_region_at(0.5, 0.5)["id"] == "a"

    def test_没命中返回None(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "TOUCH_REGIONS", [
            {"id": "a", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 0.1, "h": 0.1}]},
        ])
        assert srv.touch_region_at(0.9, 0.9) is None


class TestStore:
    def test_存了能读回来(self, srv):
        shapes = [{"type": "ellipse", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}]
        srv.save_touch_regions([{"id": "head", "label": "头", "shapes": shapes}])
        assert srv.TOUCH_REGIONS_PATH.is_file()
        loaded = srv.load_touch_regions()
        assert loaded[0]["id"] == "head"
        assert loaded[0]["shapes"][0]["type"] == "ellipse"

    def test_坏文件退回默认(self, srv):
        srv.TOUCH_REGIONS_PATH.write_text("{ 这不是 json", encoding="utf-8")
        loaded = srv.load_touch_regions()
        assert len(loaded) == len(srv.TOUCH_REGIONS_DEFAULT)

    def test_空表不许存(self, srv):
        with pytest.raises(ValueError):
            srv.save_touch_regions([])

    def test_恢复默认会删掉文件(self, srv):
        srv.save_touch_regions([{"id": "head", "shapes": []}])
        assert srv.TOUCH_REGIONS_PATH.is_file()
        srv.reset_touch_regions()
        assert not srv.TOUCH_REGIONS_PATH.exists()

    def test_命中框纵向够到脚(self, srv):
        """0.10~1.0：框底就是模型底。以前 0.64，腿脚挤在框底 4% 里、脚根本够不到。"""
        box = srv.TOUCH_HIT_BOX
        assert box["y1"] == 1.0
        assert box["y1"] - box["y0"] >= 0.75

    def test_下发的hit_box和桌宠一致(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "TOUCH_REGIONS", srv._default_regions())
        payload = srv.regions_payload()
        assert payload["hit_box"] == srv.TOUCH_HIT_BOX
        assert len(payload["regions"]) == len(srv.TOUCH_REGIONS_DEFAULT)
        # 兼容字段：老前端只认 x0..y1
        for region in payload["regions"]:
            assert 0 <= region["x0"] <= region["x1"] <= 1


class TestRoutes:
    """接口层：GET 下发 → POST 保存 → GET 读回 → reset 还原。"""

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

    def test_get_带hit_box和区域(self, base, srv):
        status, d = self._get(base + "/api/touch-regions")
        assert status == 200
        assert d["hit_box"] == srv.TOUCH_HIT_BOX
        assert d["custom"] is False
        assert any(r["id"] == "head" for r in d["regions"])

    def test_post_保存后_get_能读到(self, base, srv):
        regions = srv._default_regions()
        for region in regions:
            if region["id"] == "head":
                region["shapes"] = [{"type": "ellipse", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4}]
        status, d = self._post(base + "/api/touch-regions", {"regions": regions})
        assert status == 200 and d["ok"]
        _, got = self._get(base + "/api/touch-regions")
        head = next(r for r in got["regions"] if r["id"] == "head")
        assert head["shapes"][0]["type"] == "ellipse"
        assert got["custom"] is True

    def test_post_空表被拒(self, base):
        status, d = self._post(base + "/api/touch-regions", {"regions": []})
        assert status == 400
        assert not d["ok"]

    def test_reset_回到默认(self, base, srv):
        regions = srv._default_regions()
        regions[0]["shapes"] = []
        self._post(base + "/api/touch-regions", {"regions": regions})
        status, d = self._post(base + "/api/touch-regions/reset")
        assert status == 200 and d["ok"]
        assert d["custom"] is False
