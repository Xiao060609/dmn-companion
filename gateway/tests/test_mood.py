"""心情引擎（`gateway/mood.py`）的单元测试。

全部是纯函数，不联网、不读写 `gateway/data/`，所以没有 fixture 依赖 ——
连 `conftest.py` 的隔离夹具都不需要关心（它们照常跑，只是本文件用不上）。

数值口径（避免以后改代码时踩边界）
--------------------------------
* 档位 = 「不超过该值的最高档位」：-100 归「创伤」而不是「抑郁」，
  0 归「平静」，2 归「愉悦」。
* 衰减 0.01 / 分钟，往 0 靠，地板 ±0.5。
* 事件加权：`旧值(已衰减) × 0.5 + 新事件值 × 0.5`。

★ 规格正文里有一处自查不一致，本测试按**公式**钉死并在此记录：
  正文说「-4 经过 180 分钟到地板 -0.5」，但按公式 180 分钟只能走
  180 × 0.01 = 1.8，即 -4 → -2.2；真正落到 -0.5 需要 (4 - 0.5) / 0.01
  = 350 分钟。`test_负值衰减到地板需要_350_分钟` 把两个数都写清楚了。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import mood  # noqa: E402

#: 一个固定的「上次更新时间」，避免用例被真实时间影响。
T0 = 1_800_000_000.0


def _minutes(n: float) -> float:
    """n 分钟后的时间戳。"""
    return T0 + n * 60.0


def _state(value: float, updated_ts: float = T0, reason: str = "") -> dict:
    """按落盘格式造一个状态字典。"""
    return {
        "mood_value": value,
        "mood_label": mood.label_of(value),
        "mood_reason": reason,
        "mood_updated_ts": updated_ts,
    }


# --------------------------------------------------------------------------- #
# 1. 档位映射的边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 边界值本身归「不超过该值的最高档位」= 更低的那一档
        (-200.0, "抑郁"),
        (-100.0, "创伤"),
        (-10.0, "绝望"),
        (-8.0, "重度悲伤"),
        (-6.0, "中度悲伤"),
        (-4.0, "轻度悲伤"),
        (-2.0, "伤感"),
        (-0.5, "无聊"),
        (0.0, "平静"),
        (2.0, "愉悦"),
        (4.0, "中度高兴"),
        (8.0, "极度高兴"),
        (10.0, "超级高兴"),
    ],
)
def test_档位边界值(value: float, expected: str):
    """13 个档位的代表值必须映射到自己那一档。"""
    assert mood.label_of(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 区间内部
        (-500.0, "抑郁"),
        (-150.0, "创伤"),
        (-50.0, "绝望"),
        (-9.0, "重度悲伤"),
        (-7.0, "中度悲伤"),
        (-5.0, "轻度悲伤"),
        (-3.0, "伤感"),
        (-1.0, "无聊"),
        (1.0, "愉悦"),
        (3.0, "中度高兴"),
        (6.0, "极度高兴"),
        (9.0, "超级高兴"),
        (100.0, "超级高兴"),
        # 边界外一点点（往更轻的一档走）
        (-10.0001, "绝望"),   # 仍在 (-100, -10]
        (-9.9999, "重度悲伤"),  # 跨过 -10
        (-2.0001, "伤感"),
        (-1.9999, "无聊"),
        (-0.001, "无聊"),
        (-0.0001, "无聊"),
        (0.0001, "愉悦"),
        (2.0001, "中度高兴"),
        (4.0001, "极度高兴"),
        (8.0001, "超级高兴"),
    ],
)
def test_档位区间内部与边界外一点点(value: float, expected: str):
    """区间内部 + 边界两侧 ±0.0001，确认没有 off-by-one。"""
    assert mood.label_of(value) == expected


def test_档位名和数值可以双向往返():
    """每个档位名转成数值后必须还能转回同名（映射表自洽）。"""
    for label, value in mood.LABEL_VALUES.items():
        assert mood.value_of(label) == value
        assert mood.label_of(value) == label


def test_value_of_不认识就返回_None():
    """不认识的档位名返回 None —— 绝不模糊匹配或猜一个近似值。"""
    for bad in ["", "  ", "悲伤", "超开心", "happy", "HUGE", "轻松", None]:
        assert mood.value_of(bad) is None  # type: ignore[arg-type]


def test_value_of_允许前后空格():
    """模型输出常带空格，strip 之后要认。"""
    assert mood.value_of("  愉悦 ") == 2.0


def test_档位表覆盖_13_个档位():
    """档位数 = 13（含地板档「无聊」），少一个就说明表被改坏了。"""
    assert len(mood.LABEL_VALUES) == 13
    assert mood.LABEL_VALUES["无聊"] == -mood.FLOOR


# --------------------------------------------------------------------------- #
# 2. 衰减速率
# --------------------------------------------------------------------------- #
def test_正值_60_分钟衰减_0_6():
    """+8 走 60 分钟：8 - 60 × 0.01 = 7.4。"""
    assert mood.decay(8.0, T0, _minutes(60)) == pytest.approx(7.4)


def test_负值_180_分钟走到_负_2_2():
    """-4 走 180 分钟：-4 + 180 × 0.01 = -2.2（还没到地板）。"""
    assert mood.decay(-4.0, T0, _minutes(180)) == pytest.approx(-2.2)


def test_负值衰减到地板需要_350_分钟():
    """真正落到 -0.5 要 (4 - 0.5) / 0.01 = 350 分钟；再多也是 -0.5。

    规格正文写的是「-4 经过 180 分钟到地板」，与 0.01/分钟 的速率不符；
    这里按公式钉死，并顺手验证「衰减过头仍停在地板」。
    """
    assert mood.decay(-4.0, T0, _minutes(350)) == pytest.approx(-mood.FLOOR)
    assert mood.decay(-4.0, T0, _minutes(10_000)) == pytest.approx(-mood.FLOOR)


@pytest.mark.parametrize("days", [1, 7, 30, 365])
def test_长时间停在地板不归零(days: int):
    """30 天（乃至一年）没事件：-8 停在 -0.5，档位「无聊」，绝不精确归零。"""
    later = _minutes(days * 24 * 60)
    value = mood.decay(-8.0, T0, later)
    assert value == pytest.approx(-mood.FLOOR)
    assert value != 0.0
    assert mood.label_of(value) == "无聊"

    eff = mood.decayed_state(_state(-8.0), later)
    assert eff["mood_value"] == pytest.approx(-mood.FLOOR)
    assert eff["mood_label"] == "无聊"
    assert eff["mood_reason"] == mood.FLOOR_REASON


def test_正值长时间停在地板_正_0_5():
    """正值按公式只夹到 +0.5（不会穿过 0 变成负）—— 规格的地板是对称的。"""
    value = mood.decay(8.0, T0, _minutes(30 * 24 * 60))
    assert value == pytest.approx(mood.FLOOR)
    assert mood.label_of(value) == "愉悦"


def test_衰减是线性的():
    """同样时长、不同起点，走的距离一样（都是 0.01 × 分钟）。"""
    assert mood.decay(6.0, T0, _minutes(100)) - mood.decay(2.0, T0, _minutes(100)) == (
        pytest.approx(4.0)
    )
    assert mood.decay(-6.0, T0, _minutes(100)) - mood.decay(-2.0, T0, _minutes(100)) == (
        pytest.approx(-4.0)
    )


def test_零保持零():
    """0 不动：不做任何漂移。"""
    assert mood.decay(0.0, T0, _minutes(60)) == 0.0
    assert mood.decay(0.0, T0, _minutes(30 * 24 * 60)) == 0.0


def test_时间倒流或时间戳缺失都算_0_分钟():
    """updated_ts 在未来 / 缺失 / 是坏值 → 视为 0 分钟，只回归不反向外推。"""
    assert mood.decay(4.0, T0, T0 - 3600) == pytest.approx(4.0)
    assert mood.decay(4.0, None, _minutes(60)) == pytest.approx(4.0)  # type: ignore[arg-type]
    assert mood.decay(4.0, "坏值", _minutes(60)) == pytest.approx(4.0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 3. 地板不往外推
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [0.2, 0.4, 0.5, -0.2, -0.4, -0.5])
def test_地板内的小值衰减后不动(value: float):
    """本来就在地板区间（|v| ≤ 0.5）的值，衰减不能把它往外推。"""
    assert mood.decay(value, T0, _minutes(60)) == pytest.approx(value)
    assert mood.decay(value, T0, _minutes(30 * 24 * 60)) == pytest.approx(value)


def test_地板不会让正值穿越零点():
    """+0.2 走一天仍是 +0.2，符号不变。"""
    value = mood.decay(0.2, T0, _minutes(24 * 60))
    assert value == pytest.approx(0.2)
    assert value > 0


# --------------------------------------------------------------------------- #
# 4. 事件加权
# --------------------------------------------------------------------------- #
def test_加权_地板旧值_加_中度高兴():
    """旧 -4 衰减三天到 -0.5，再来一个「中度高兴」(+4)：

        0.5 × 4 + 0.5 × (-0.5) = 2.0 - 0.25 = +1.75

    档位落在 (0, 2] → 「愉悦」。
    """
    state = _state(-4.0, reason="主人说了句谢谢")
    new = mood.apply_event(state, "中度高兴", _minutes(3 * 24 * 60), reason="主人夸我了")

    assert new["mood_value"] == pytest.approx(1.75)
    assert new["mood_label"] == "愉悦"
    assert new["mood_reason"] == "主人夸我了"
    assert new["mood_updated_ts"] == pytest.approx(_minutes(3 * 24 * 60))


def test_加权_两个负向事件不会互相抵消():
    """旧 -2（不衰减）+ 新「轻度悲伤」(-4)：0.5×(-4) + 0.5×(-2) = -3 → 「伤感」。"""
    state = _state(-2.0)
    new = mood.apply_event(state, "轻度悲伤", T0)
    assert new["mood_value"] == pytest.approx(-3.0)
    assert new["mood_label"] == "伤感"


def test_加权_平静加轻微事件只走一半():
    """旧 0 + 新「愉悦」(+2)：0.5×2 + 0.5×0 = +1 → 「愉悦」。"""
    new = mood.apply_event(_state(0.0), "愉悦", T0)
    assert new["mood_value"] == pytest.approx(1.0)
    assert new["mood_label"] == "愉悦"


def test_加权权重可调():
    """权重是参数：全给新事件时结果等于事件值本身。"""
    new = mood.apply_event(
        _state(-6.0), "极度高兴", T0, reason="", old_weight=0.0, new_weight=1.0
    )
    assert new["mood_value"] == pytest.approx(8.0)
    assert new["mood_label"] == "极度高兴"


def test_apply_event_不改入参():
    """加权是纯函数：调用方的字典不能被动过。"""
    state = _state(-4.0, reason="旧理由")
    before = dict(state)
    mood.apply_event(state, "愉悦", _minutes(60))
    assert state == before


def test_apply_event_空理由时地板档自动给理由():
    """落在负向地板区间、又没给理由 → 用 FLOOR_REASON 兜住。

    旧 -0.5（已在负向地板，不衰减）+ 新「无聊」(-0.5)：
        0.5 × (-0.5) + 0.5 × (-0.5) = -0.5 → 「无聊」
    """
    new = mood.apply_event(_state(-0.5), "无聊", T0, reason="")
    assert new["mood_value"] == pytest.approx(-0.5)
    assert new["mood_label"] == "无聊"
    assert new["mood_reason"] == mood.FLOOR_REASON


# --------------------------------------------------------------------------- #
# 5. 未知档位名
# --------------------------------------------------------------------------- #
def test_未知_label不改数值但推进时间戳():
    """模型给了不认识的档位名：不猜、不改数值，只把时间戳更新到 now。"""
    state = _state(-4.0, reason="刚才有点难过")
    now = _minutes(180)
    new = mood.apply_event(state, "超～级～开心", now, reason="瞎编的")

    assert new["mood_value"] == pytest.approx(-2.2)  # 只发生了衰减
    assert new["mood_label"] == "伤感"
    assert new["mood_updated_ts"] == pytest.approx(now)


def test_未知_label连理由都不编():
    """未知档位名时给出的 reason 不该被当成新事件的理由写进去。"""
    now = _minutes(180)
    new = mood.apply_event(_state(-4.0, reason="原来那句话"), "happy", now, reason="瞎编的")
    assert new["mood_value"] == pytest.approx(-2.2)
    # -2.2 不在负向地板区间，沿用衰减后的展示理由（原有理由）
    assert new["mood_reason"] == "原来那句话"


# --------------------------------------------------------------------------- #
# 6. decayed_state 容错 + 「不落盘」
# --------------------------------------------------------------------------- #
def test_decayed_state_四个字段齐全():
    """返回值必须是完整四字段，方便直接喂给 prompt_line / 展示层。"""
    eff = mood.decayed_state(_state(8.0))
    assert set(eff) == set(mood.MOOD_FIELDS)


def test_decayed_state_算到_now():
    """+8 读的时候按 now 一次性算到 7.4（不用定时器）。"""
    eff = mood.decayed_state(_state(8.0), _minutes(60))
    assert eff["mood_value"] == pytest.approx(7.4)
    assert eff["mood_label"] == "极度高兴"
    assert eff["mood_updated_ts"] == pytest.approx(_minutes(60))


@pytest.mark.parametrize(
    "state",
    [
        {},                                   # 全缺
        {"mood_value": None},
        {"mood_value": "abc", "mood_updated_ts": "xyz"},
        {"mood_value": 4.0},                  # 缺 ts
        {"mood_updated_ts": T0},              # 缺 value
        {"mood_value": float("nan"), "mood_updated_ts": float("inf")},
        {"mood_value": True, "mood_reason": None},
        None,                                 # 连 dict 都不是
    ],
)
def test_decayed_state_坏输入不抛异常(state):
    """缺字段 / 坏值 / 根本不是 dict 都要能容错，返回可用的四字段。"""
    eff = mood.decayed_state(state)  # type: ignore[arg-type]
    assert set(eff) == set(mood.MOOD_FIELDS)
    assert isinstance(eff["mood_value"], float)
    assert isinstance(eff["mood_label"], str)
    assert isinstance(eff["mood_reason"], str)
    assert isinstance(eff["mood_updated_ts"], float)


def test_decayed_state_不修改入参():
    """展示态不能就地改落盘状态（否则 updated_ts 被读一次就变一次）。"""
    state = _state(-4.0, reason="刚才有点难过")
    before = dict(state)
    mood.decayed_state(state, _minutes(60))
    assert state == before


def test_decayed_state_衰减进地板时理由变成地板理由():
    """没新事件、衰减到地板 → 理由换成「无聊（最近没发生什么）」。"""
    eff = mood.decayed_state(_state(-4.0, reason="刚才有点难过"), _minutes(400))
    assert eff["mood_value"] == pytest.approx(-mood.FLOOR)
    assert eff["mood_label"] == "无聊"
    assert eff["mood_reason"] == mood.FLOOR_REASON


def test_apply_event_写回的状态可以继续用():
    """落盘 → 再读：时间戳是 now，所以再衰减不会重复扣。"""
    t1 = _minutes(60)
    saved = mood.apply_event(_state(8.0), "愉悦", t1)
    assert saved["mood_updated_ts"] == pytest.approx(t1)
    # 同一时刻再读，值不变
    assert mood.decayed_state(saved, t1)["mood_value"] == pytest.approx(saved["mood_value"])


# --------------------------------------------------------------------------- #
# 7. prompt_line
# --------------------------------------------------------------------------- #
def test_prompt_line_地板时说无聊并带上理由():
    """「无聊」档必须出现 FLOOR_REASON，否则角色不知道自己正闲着。"""
    line = mood.prompt_line(_state(-4.0), _minutes(400))
    assert "无聊" in line
    assert mood.FLOOR_REASON in line


def test_prompt_line_长度不超过_200_且不是命令句():
    """注入 system 的一行：短、给出档位名、不用祈使句命令模型表演。"""
    line = mood.prompt_line(_state(-4.0, reason="主人今天没怎么理我"), _minutes(400))
    assert len(line) <= 200
    assert "「无聊」" in line
    # 不写成状态报告/命令：不出现这类命令式字样
    for banned in ("请表现", "必须", "你应该", "输出", "回复我"):
        assert banned not in line


def test_prompt_line_平静档给档位名但不啰嗦():
    """平静（0 ≤ v < 2）只给档位名，不解释理由。"""
    line = mood.prompt_line(_state(0.0))
    assert "「平静」" in line
    assert len(line) <= 200
    # 平静档即便状态里带着旧理由，也不念出来
    line_with_reason = mood.prompt_line(_state(0.0, reason="刚才有点难过"))
    assert "刚才有点难过" not in line_with_reason


def test_prompt_line_高愉悦点出档位名和理由():
    """+8 档位是「极度高兴」，理由要念出来。"""
    line = mood.prompt_line(_state(8.0, reason="主人夸我了"))
    assert "「极度高兴」" in line
    assert "主人夸我了" in line


def test_prompt_line_坏状态也能出字():
    """没有任何字段时退回「平静」，不抛异常。"""
    line = mood.prompt_line({})
    assert "「平静」" in line
    assert len(line) <= 200


# --------------------------------------------------------------------------- #
# 8. 旧枚举兼容
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("happy", "愉悦"),
        ("sad", "轻度悲伤"),
        ("angry", "中度悲伤"),
        ("shy", "愉悦"),
        ("love", "愉悦"),
        ("surprised", "愉悦"),
        ("confused", "伤感"),
        ("calm", "平静"),
    ],
)
def test_旧枚举八个都能翻(legacy: str, expected: str):
    """`system.EMOTIONS` 里的 8 个旧枚举一个都不能漏。"""
    label = mood.from_legacy_emotion(legacy)
    assert label == expected
    assert label in mood.LABEL_VALUES  # 翻出来的名字必须是真档位


@pytest.mark.parametrize("bad", ["", None, "excited", "EMO", "开心", "  "])
def test_旧枚举未知返回_None(bad):
    """未知枚举返回 None，调用方自己决定兜底（别替它猜）。"""
    assert mood.from_legacy_emotion(bad) is None  # type: ignore[arg-type]


def test_旧枚举大小写与空格容错():
    """旧数据里可能存成 'Happy ' / 'CALM'。"""
    assert mood.from_legacy_emotion(" Happy ") == "愉悦"
    assert mood.from_legacy_emotion("CALM") == "平静"


def test_初始状态是平静():
    """首次迁移：0 / 平静 / 空理由 / 时间戳给 now。"""
    st = mood.initial_state(T0)
    assert st == {
        "mood_value": 0.0,
        "mood_label": "平静",
        "mood_reason": "",
        "mood_updated_ts": T0,
    }
    assert set(st) == set(mood.MOOD_FIELDS)


# --------------------------------------------------------------------------- #
# 9. 纯函数保证 / 常量
# --------------------------------------------------------------------------- #
def test_常量与规格一致():
    """FLOOR / 速率 / 地板理由都是定稿数值，改动必须改测试。"""
    assert mood.FLOOR == 0.5
    assert mood.DECAY_PER_MIN == 0.01
    assert mood.FLOOR_REASON == "无聊（最近没发生什么）"


def test_模块不碰文件系统也不碰网络():
    """mood.py 必须是纯函数模块：不 import 落盘/网络相关模块。"""
    source = (GATEWAY_DIR / "mood.py").read_text(encoding="utf-8")
    for banned in ("import os", "import json", "import socket", "requests", "sqlite3"):
        assert banned not in source, f"mood.py 不该出现 {banned!r}"
