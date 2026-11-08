# -*- coding: utf-8 -*-
"""P0-3：语音决策权归模型（2026-11-07）。

改之前的行为：`_speakable()` 判断「≤28 字且无数字字母」就**自动**给回复加
`[语音]`，她写不写标记都一样。而人格要求「一次 1~3 句、不超过 30 字」——
两个数字几乎重合，实测 307 条有效回复里 229 条（74.6%）被强制念出来。

改之后：语音是**她手上的一张卡片**。
  * 她写 `[语音]` → 照办（主路径）
  * 她不写        → 就是文字，代码绝不替她补标记
  * 兜底的唯一例外：她写了标记但内容语音根本念不了（含数字/字母/链接、超长）
    → 降级成文字，并记 `voice_degrade.log` 方便排查

这几条用例守的就是「决策权在她手上」这条线，以及兜底不会反过来变成强制。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gateway  # noqa: E402


def _spoken(**result) -> str:
    """跑一遍 _as_spoken，返回最终 text。"""
    return gateway._as_spoken(dict(result), "在吗")["text"]


# ---------------------------------------------------------------- 决策权归属

def test_她没写标记就不该有语音_短句():
    """**核心**：短句不再自动变成语音。这是 74.6% 覆盖率的直接原因。"""
    text = _spoken(text="我在这儿呀。")
    assert not text.startswith(gateway.VOICE_MARKER)
    assert text == "我在这儿呀。"


def test_她没写标记就不该有语音_长句():
    text = _spoken(text="这是一段很长的回复，" * 3)
    assert not text.startswith(gateway.VOICE_MARKER)


def test_她写了标记就必须照办():
    """主路径：她决定用语音，代码不许拦。"""
    text = _spoken(text="我在这儿呀。", voice=True)
    assert text.startswith(gateway.VOICE_MARKER)
    assert "我在这儿呀。" in text


def test_她写了标记且正文已含标记时不重复加():
    text = _spoken(text=gateway.VOICE_MARKER + "我在这儿呀。", voice=True)
    assert text.count(gateway.VOICE_MARKER) == 1


# ---------------------------------------------------------------- 兜底降级

def test_写了标记但含数字_降级为文字():
    """兜底：语音念不了数字，硬念会念成乱码。降级比硬发诚实。"""
    text = _spoken(text="现在9点了。", voice=True)
    assert not text.startswith(gateway.VOICE_MARKER)
    assert text == "现在9点了。"


def test_写了标记但超长_降级为文字():
    text = _spoken(text="字" * (gateway.VOICE_MAX_CHARS + 20), voice=True)
    assert not text.startswith(gateway.VOICE_MARKER)


def test_写了标记但含链接_降级为文字():
    text = _spoken(text="看这个 https://example.com", voice=True)
    assert not text.startswith(gateway.VOICE_MARKER)


def test_降级会留痕而不是静默():
    """静默降级会让"为什么没声音"无从排查。"""
    import json
    import tempfile

    log_path = os.path.join(tempfile.mkdtemp(prefix="voicedeg-"), "voice_degrade.log")
    real = gateway.DATA_DIR
    try:
        gateway.DATA_DIR = os.path.dirname(log_path)
        _spoken(text="现在9点了。", voice=True)
        assert os.path.isfile(log_path)
        rec = json.loads(open(log_path, encoding="utf-8").readline())
        assert rec["reason"] == "not_speakable"
    finally:
        gateway.DATA_DIR = real


# ---------------------------------------------------------------- 不能误伤

def test_观望标记原样返回():
    """[观望] 加任何前缀插件就认不出来，会把「观望」念给全群。"""
    out = gateway._as_spoken({"text": "[观望]", "silent": True}, "在吗")
    assert out["text"] == "[观望]"


def test_表情包标记不加语音前缀():
    """meme_manager 认的是裸文本，加前缀它就对不上。"""
    text = _spoken(text="哈哈哈", meme_markers=["happy"])
    assert not text.startswith(gateway.VOICE_MARKER)


def test_边界常量与插件默认值一致():
    """提示词里写的上限必须等于插件实际值，否则"说了 100、实际 28"。"""
    import re

    plugin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "qq", "astrbot", "data", "plugins", "astrbot_plugin_voice_reply", "main.py",
    )
    if not os.path.isfile(plugin):
        pytest.skip("插件源码不在预期位置")
    src = open(plugin, encoding="utf-8").read()
    m = re.search(r'_cfg\("max_chars",\s*(\d+)\)', src)
    assert m, "插件里找不到 max_chars 默认值"
    assert int(m.group(1)) == gateway.VOICE_MAX_CHARS, (
        f"网关 VOICE_MAX_CHARS={gateway.VOICE_MAX_CHARS} 与插件默认 "
        f"max_chars={m.group(1)} 不一致 —— 提示词会撒谎"
    )


def test_提示词里写明的上限来自常量():
    """提示词是 f-string 拼的，改常量应自动同步。"""
    assert str(gateway.VOICE_MAX_CHARS) in gateway.VOICE_NOTE
    assert "你自己看着办" in gateway.VOICE_NOTE or "自己看着办" in gateway.VOICE_NOTE
