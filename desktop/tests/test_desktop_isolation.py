"""守门用例：证明 `conftest.py` 的 autouse 兜底**真的生效**（2026-11-04，P1-7）。

设计思路和 `gateway/tests/test_data_isolation.py` 一致：
**故意不自己 monkeypatch**，因为要验的正是「autouse 已经兜住了」。
自己再改一遍就等于在测自己。

这一条如果红了，说明 conftest 的隔离破了 —— 那么所有 desktop 测试
都可能已经在悄悄改写主人真实的 `touch_regions.json` / `tts_cache`。
"""
from __future__ import annotations

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import server  # noqa: E402


def test_触摸区域路径被指到临时目录():
    """`TOUCH_REGIONS_PATH` 绝不能指向 `desktop/data/`（那是主人手画的）。"""
    p = Path(server.TOUCH_REGIONS_PATH)
    real = (DESKTOP_DIR / "data" / "touch_regions.json").resolve()
    assert p.resolve() != real, (
        f"TOUCH_REGIONS_PATH 仍指向生产文件 {real} —— conftest 隔离失效"
    )
    assert "isolated_desktop" in str(p), f"应该在 tmp 隔离目录里，实际 {p}"


def test_tts缓存目录被指到临时目录():
    p = Path(server.TTS_OUTDIR).resolve()
    real = (DESKTOP_DIR / "tts_cache").resolve()
    assert p != real, f"TTS_OUTDIR 仍指向生产目录 {real} —— conftest 隔离失效"


def test_写触摸区域不会碰到生产文件(tmp_path):
    """真写一次，确认落在 tmp（不碰生产）。

    只读断言不够：路径对了但函数里硬编码了别的路径，也是会出事的。
    """
    real = (DESKTOP_DIR / "data" / "touch_regions.json").resolve()
    before = real.stat().st_mtime if real.exists() else None

    server.save_touch_regions(server._default_regions())

    after = real.stat().st_mtime if real.exists() else None
    assert before == after, "生产 touch_regions.json 被改写了！"
    assert Path(server.TOUCH_REGIONS_PATH).exists(), "应该写到了隔离目录里"
