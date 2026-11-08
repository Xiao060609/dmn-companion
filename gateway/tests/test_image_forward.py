"""图片转发（多模态）的单测。

为什么要有这个文件
------------------
2026-11-01 从 ``gateway/data/v1.log`` 抓到的真实记录长这样：

    "in": "[图片][Image 1: original path G:\\\\...\\\\media_image_xxx.png",
    "out": "[语音]这张图我这边看不到内容呀，晓你再跟我说说是什么。"

也就是说**图片从头到尾就没进过模型**。链条是这样的：

1. ``cmd_config.json`` 里主 provider 的 ``modalities`` 含 ``image``，指向网关
   ``127.0.0.1:8898``；
2. ``astr_main_agent.py`` 的图片描述只在 ``not main_provider_supports_image``
   时才跑，所以**描述服务根本没被调用**（配了也是死代码）；
3. AstrBot 于是把图塞进 ``image_url`` 段发过来；
4. 网关 ``_openai_user_text`` 只读 ``text`` 段，图被整段丢掉，只剩占位符。

结论：光把 ``[Image N: path]`` 洗干净没用，必须把图原样转给上游。
本文件钉住第 4 步之后的行为。

跑法::

    python -m pytest gateway/tests/test_image_forward.py -v
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@pytest.fixture()
def gateway_mod(tmp_path, monkeypatch):
    """导入 gateway，并把库指到临时目录，绝不碰真实记忆库。"""
    import gateway

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "gateway.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """create table turns(
               id integer primary key autoincrement,
               session text not null,
               role text not null,
               content text not null,
               path text,
               ts real not null,
               channel text)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(gateway, "DB_PATH", str(db_path))
    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    return gateway


def _user_msg(parts):
    return [{"role": "user", "content": parts}]


class TestExtractImageUrls:
    def test_抠出_data_url(self, gateway_mod):
        msgs = _user_msg([
            {"type": "text", "text": "[图片]"},
            {"type": "image_url", "image_url": {"url": PNG}},
        ])
        assert gateway_mod._extract_image_urls(msgs) == [PNG]

    def test_抠出_http_url(self, gateway_mod):
        url = "https://example.com/a.png"
        msgs = _user_msg([{"type": "image_url", "image_url": {"url": url}}])
        assert gateway_mod._extract_image_urls(msgs) == [url]

    def test_拒绝本地路径(self, gateway_mod):
        """file:// 上游不认，带着会让整条请求 400 —— 宁可丢这张图。"""
        msgs = _user_msg([
            {"type": "image_url", "image_url": {"url": "file:///C:/x.png"}},
            {"type": "image_url", "image_url": {"url": PNG}},
        ])
        assert gateway_mod._extract_image_urls(msgs) == [PNG]

    def test_没有图返回空(self, gateway_mod):
        msgs = _user_msg([{"type": "text", "text": "在吗"}])
        assert gateway_mod._extract_image_urls(msgs) == []

    def test_字符串内容没有图(self, gateway_mod):
        assert gateway_mod._extract_image_urls([{"role": "user", "content": "在吗"}]) == []

    def test_只看最后一条用户消息(self, gateway_mod):
        """历史里的图不该被重新发给模型（既贵又会把旧图当成本轮输入）。"""
        msgs = [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://old.example.com/1.png"}}]},
            {"role": "assistant", "content": "嗯"},
            {"role": "user", "content": [{"type": "text", "text": "这张呢"}]},
        ]
        assert gateway_mod._extract_image_urls(msgs) == []


class TestBuildMessagesWithImages:
    def test_有图时用多模态_content_数组(self, gateway_mod):
        msgs = gateway_mod.build_messages("s1", "[图片]主人：这是什么", images=[PNG])
        last = msgs[-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list), "有图时必须发数组，发字符串图就丢了"
        assert last["content"][0] == {"type": "text", "text": "[图片]主人：这是什么"}
        assert last["content"][1]["type"] == "image_url"
        assert last["content"][1]["image_url"]["url"] == PNG

    def test_多张图按顺序都带上(self, gateway_mod):
        msgs = gateway_mod.build_messages("s1", "[图片]", images=[PNG, "https://e.com/b.png"])
        urls = [p["image_url"]["url"] for p in msgs[-1]["content"][1:]]
        assert urls == [PNG, "https://e.com/b.png"]

    def test_没图时仍是纯字符串(self, gateway_mod):
        """不能因为加了多模态就把所有请求都变成数组：老客户端/工具链按字符串处理。"""
        msgs = gateway_mod.build_messages("s1", "在吗")
        assert msgs[-1]["content"] == "在吗"

    def test_空文本但有图也要给_text_段(self, gateway_mod):
        """有些服务商不接受「只有图、没有 text」的 content 数组。"""
        msgs = gateway_mod.build_messages("s1", "", images=[PNG])
        parts = msgs[-1]["content"]
        assert parts[0]["type"] == "text" and parts[0]["text"].strip()
        assert parts[1]["type"] == "image_url"
