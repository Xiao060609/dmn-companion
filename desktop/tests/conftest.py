"""desktop 测试的公共兜底（2026-11-04 加，P1-7）。

为什么需要这一层
----------------
`desktop/tests/` 原来**没有 conftest、没有 autouse**，隔离完全靠每条用例
自己 `monkeypatch.setattr(server, "TOUCH_REGIONS_PATH", tmp_path / ...)`。

这是 `gateway/tests/conftest.py` 顶部亲口否定过的模式：
「根因不是某一条用例写错，而是默认路径常量指向生产目录……靠人工逐条检查靠不住」。
gateway 侧已经为此付过两次学费（11-02 往 `events` 写 12 条 `care_given`、
11-03 往 `livingmemory.db` 灌 49 条 stub）。

desktop 侧的危险常量是真实存在的：
  * `server.TOUCH_REGIONS_PATH` = `desktop/data/touch_regions.json`
    —— 那是**主人亲手画的触摸区域**，被测试改写就要重画；
  * `server.TTS_OUTDIR` = `desktop/tts_cache`
  * `server.WAKE_KEYWORDS_PATH` / `WAKE_PLUGIN_CONFIG`
    —— 改错会动到 AstrBot 插件的配置。

所以这里加一层 autouse：默认全部指到 tmp_path。
**用例自己再 setattr 会覆盖这里（同一个 monkeypatch 实例，后写胜）**，
现有的显式隔离照旧优先，不会有任何行为变化。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))


@pytest.fixture(autouse=True)
def _isolate_desktop_paths(tmp_path, monkeypatch):
    """把 desktop 侧所有「会落盘到项目目录」的常量指到 tmp_path。

    找不到某个常量就跳过它（模块版本之间有增减，不该因此让整套测试红）。
    """
    try:
        import server  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— 有的测试不需要 server（比如纯脚本测试）
        yield
        return

    data_dir = Path(tmp_path) / "isolated_desktop"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 每个都带默认值，避免常量改名后这里 KeyError
    targets = {
        "TOUCH_REGIONS_PATH": data_dir / "touch_regions.json",
        "TTS_OUTDIR": data_dir / "tts_cache",
        "TOUCH_VOICE_DIR": data_dir / "touch_voice",
        "WAKE_KEYWORDS_PATH": data_dir / "wake_keywords.json",
        "WAKE_PLUGIN_CONFIG": data_dir / "plugin_config.json",
        "AUDIT_LOG": data_dir / "audit.log",
    }
    for name, value in targets.items():
        if hasattr(server, name):
            monkeypatch.setattr(server, name, value, raising=False)

    # `TOUCH_REGIONS` 是模块级加载出来的内存副本，也要复位，否则上一个用例
    # 改过它（有测试会直接 setattr 一个自定义区域表）会带到下一条。
    if hasattr(server, "TOUCH_REGIONS") and hasattr(server, "_default_regions"):
        try:
            monkeypatch.setattr(server, "TOUCH_REGIONS", server._default_regions())
        except Exception:  # noqa: BLE001
            pass

    yield
