"""图片消息清洗的单测（纯函数、无网络、秒级可跑）。

背景：AstrBot 会把收到的每张图转成一条文字标签塞进请求
（``astrbot/core/utils/image_input.py:182``）：

    [Image 1: original path G:\\...\\media_image_xxx.png; sent to image-captioning model; ...]

这段话对角色层没用，而且**本机路径会被写进记忆库**。开了
``provider_settings.default_image_caption_provider_id`` 之后，AstrBot 会额外附一段
``<image_caption>描述</image_caption>``，那才是真正的信息。

``_clean_image_notes`` 的职责：丢掉路径标签、把描述换成一句人话。
用例里的原始字符串全部来自 2026-11-01 在真实 ``gateway/data/v1.log`` 抓到的请求。

跑法::

    python -m pytest gateway/tests/test_image_notes.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GATEWAY_PY = Path(__file__).resolve().parents[1] / "gateway.py"


@pytest.fixture(scope="module")
def clean():
    """只取 gateway.py 里的清洗函数，不启动服务、不连数据库。

    gateway.py 在 import 时会读 config.json / 建 DB 连接，所以这里用
    ast 抽出目标函数单独执行，避免副作用。
    """
    import ast
    import re as _re

    src = GATEWAY_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"_clean_image_notes"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
    # 依赖的模块级正则常量
    consts = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") in {"IMAGE_LABEL_RE", "IMAGE_CAPTION_RE"} for t in n.targets)
    ]
    assert nodes, "gateway.py 里找不到 _clean_image_notes"
    assert len(consts) == 2, "缺少 IMAGE_LABEL_RE / IMAGE_CAPTION_RE"

    ns: dict = {"re": _re}
    exec(compile(ast.Module(body=consts + nodes, type_ignores=[]), "<gateway>", "exec"), ns)
    return ns["_clean_image_notes"]


def test_未开描述时只留图片占位_路径被丢掉(clean):
    """真实抓到的请求：开了描述之前的样子。"""
    raw = (
        "[图片][Image 1: original path "
        "G:\\小鱼\\qq\\astrbot\\data\\temp\\media_image_20261101131234.png; "
        "not included in this request]"
    )
    out = clean(raw)
    assert out == "[图片]"
    assert "original path" not in out
    assert "astrbot" not in out


def test_开了描述时换成一句人话(clean):
    raw = (
        "[图片][Image 1: original path G:\\a\\b.png; "
        "sent to image-captioning model; description included as text]"
        "<image_caption>一只橘猫躺着，表情无语</image_caption>"
    )
    assert clean(raw) == "[图：一只橘猫躺着，表情无语]"


def test_多张图各自保留描述(clean):
    raw = (
        "[Image 1: original path C:\\a.png; sent to image-captioning model]"
        "<image_caption>猫</image_caption>"
        "[Image 2: original path C:\\b.png]"
        "<image_caption>狗</image_caption>"
    )
    assert clean(raw) == "[图：猫][图：狗]"


def test_纯文字不受影响(clean):
    for text in ("今天天气怎么样", "在吗", ""):
        assert clean(text) == text


def test_描述里的换行被压平(clean):
    raw = "<image_caption>第一行\n第二行</image_caption>"
    out = clean(raw)
    assert "original path" not in out
    assert "第一行" in out and "第二行" in out


def test_空描述不留空壳(clean):
    """描述为空时不该产出 [图：]，退回图片占位。"""
    out = clean("[图片][Image 1: original path C:\\a.png]<image_caption>   </image_caption>")
    assert "[图：]" not in out
    assert out == "[图片]"
