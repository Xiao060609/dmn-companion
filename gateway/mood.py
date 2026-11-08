"""心情引擎：一个连续数值 + 档位名 + 理由，随时间线性回归平静。

为什么是纯函数模块（2026-11-06 定稿）
------------------------------------
心情以前是 `system_state.json` 里的 `emotion` 枚举（happy/sad/...），
只有「此刻是什么」没有「有多深、多久会过去」。这次换成连续数值：

* **按需计算，不跑定时器**。只存 4 个字段：
  `mood_value` / `mood_label` / `mood_reason` / `mood_updated_ts`。
  读的时候用 `mood_updated_ts` 一次性算到 now（`decayed_state`）。
* `mood_updated_ts` 必须是**上次真正更新心情的时间**，不是上次读取的时间，
  否则每次读都会把衰减重复算一遍。
* **地板 ±0.5**：正值不会衰减到 0.5 以下，负值不会衰减到 -0.5 以上。
  不做「绝对归零」——长期没事件时停在 -0.5 附近，档位是「无聊」。

本模块不 import `system` / `gateway`，不读写任何文件，全部是纯函数，
方便单测和将来被 `system.py` / `role_speak.py` 直接调用。

档位表（数值 → 名称，见 `LABEL_VALUES`）
--------------------------------------
    ≤ -200  抑郁      (-200, -100]  创伤     (-100, -10]  绝望
    (-10, -8]  重度悲伤   (-8, -6]  中度悲伤   (-6, -4]  轻度悲伤
    (-4, -2]  伤感      (-2, 0)  无聊        0  平静
    (0, 2]  愉悦        (2, 4]  中度高兴      (4, 8]  极度高兴
    (8, +∞)  超级高兴（8~10 与 >10 同名）

衰减公式（逐字照抄定稿，避免边界歧义）
------------------------------------
    v = value
    若 v > 0: v = max(v - elapsed_min * 0.01, min(v, +0.5))
    若 v < 0: v = min(v + elapsed_min * 0.01, max(v, -0.5))
    v == 0 保持 0

`min(v, FLOOR)` / `max(v, -FLOOR)` 这一项是「地板不能把本来就在地板内
（|v| < 0.5）的值往外推」：v = +0.2 衰减 60 分钟仍然是 +0.2。

事件加权
--------
    新心情 = 旧心情(已按 now 衰减) × 旧权重 + 新事件值 × 新权重   （默认 0.5 / 0.5）

新事件由模型给**档位名**（不让模型直接给数值），用 `value_of` 换成数值；
未知档位名不猜，数值保持不变（`apply_event` 直接返回衰减后的状态）。

注意：规格正文里「-4 经过 180 分钟到地板 -0.5」与上面的公式不符
（180 分钟只走到 -4 + 1.8 = -2.2，真正到地板需要 (4-0.5)/0.01 = 350 分钟）。
本模块以公式为准，测试里把两个数都钉住了。
"""
from __future__ import annotations

import math
import time

#: 地板：长期无事件时心情停住的边界（正负对称）。
FLOOR = 0.5
#: 衰减速率：每分钟 0.01，往 0 靠。
DECAY_PER_MIN = 0.01
#: 停在地板上（长期没事件）时的理由文案。
FLOOR_REASON = "无聊（最近没发生什么）"

#: 落盘用的四个字段名（给集成方对齐 `system_state.json` 用）。
MOOD_FIELDS = ("mood_value", "mood_label", "mood_reason", "mood_updated_ts")

#: 档位表：档位名 → 该档代表值。`value_of` 查这张表，
#: 也是模型给档位名时唯一的取值来源（不让模型直接给数值）。
LABEL_VALUES: dict[str, float] = {
    "抑郁": -200.0,
    "创伤": -100.0,
    "绝望": -10.0,
    "重度悲伤": -8.0,
    "中度悲伤": -6.0,
    "轻度悲伤": -4.0,
    "伤感": -2.0,
    "无聊": -FLOOR,
    "平静": 0.0,
    "愉悦": 2.0,
    "中度高兴": 4.0,
    "极度高兴": 8.0,
    "超级高兴": 10.0,
}

#: 旧枚举（`system.EMOTIONS`）→ 新档位名。只给迁移期用，不做长期 API。
LEGACY_EMOTION: dict[str, str] = {
    "happy": "愉悦",
    "sad": "轻度悲伤",
    "angry": "中度悲伤",
    "shy": "愉悦",
    "love": "愉悦",
    "surprised": "愉悦",
    "confused": "伤感",
    "calm": "平静",
}

#: 浮点比较容差（只用于「是不是在地板区间里」这类判断）。
_EPS = 1e-9
#: prompt_line 的硬上限：注入 system 的一行字，太长会变成状态报告。
_PROMPT_MAX_CHARS = 200
#: prompt_line 的固定抬头，措辞与 `system.mood_prompt_line` 保持一致的口径：
#: 是「你自己此刻的心情」，不是报告，也不命令模型表演。
_PROMPT_HEAD = (
    "（这是你自己此刻的心情，不是别人告诉你的。自然地带着它说话，不用解释、不用报告）"
)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _as_float(value: object, default: float | None = 0.0) -> float | None:
    """把任意值当数字读；读不出来（None / 字符串 / NaN / inf）返回 default。

    容错的理由：`system_state.json` 是人工也可能手改的文件，
    历史版本还可能缺字段，读心情不该因为一个坏值整条链路炸掉。
    """
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _now(now: float | None) -> float:
    """`now=None` 表示「就是现在」；传了坏值也退回 `time.time()`。"""
    if now is None:
        return time.time()
    resolved = _as_float(now, None)
    return time.time() if resolved is None else resolved


def _elapsed_minutes(updated_ts: object, now: float) -> float:
    """从 updated_ts 到 now 的分钟数；缺失/非数/时间倒流都算 0 分钟。"""
    ts = _as_float(updated_ts, None)
    if ts is None:
        return 0.0
    delta = now - ts
    if not math.isfinite(delta) or delta <= 0:
        return 0.0
    return delta / 60.0


def _in_floor_zone(value: float) -> bool:
    """是否落在「负向地板区间」[-0.5, 0)——长期没事件时就停在这里。

    ⚠ **右端必须开区间**：0 是「平静」，不是「无聊」。
    这条是集成时实测抓出来的：闭区间 `<= 0.0` 会让刚初始化的平静状态
    也被写上「无聊（最近没发生什么）」，于是每轮 prompt 都多一段
    「你现在的心情是「平静」——无聊」的自相矛盾文本，
    并且 `mood_prompt_line()` 的「平静且没什么可说就不注入」整条失效。

    正向地板 +0.5 不算：那时档位是「愉悦」，理由不该被写成「无聊」。
    """
    return -FLOOR - _EPS <= value < 0.0


def _default_reason(value: float) -> str:
    """没有事件理由时，照档位给一句（只有地板区间有话说）。"""
    return FLOOR_REASON if _in_floor_zone(value) else ""


def _display_reason(value: float, stored_reason: str, moved_to_floor: bool) -> str:
    """展示用理由：衰减进地板了就换回地板理由，否则沿用已存理由。

    规则（对应规格「长期无事件、衰减到地板且没有新事件时 reason 应变成
    FLOOR_REASON」）：

    * 已经在地板区间，且（这次是衰减进来的 / 压根没存理由）→ `FLOOR_REASON`；
    * 其它情况沿用 `stored_reason`（不覆盖主人或模型给的具体理由）。
    """
    if _in_floor_zone(value) and (moved_to_floor or not stored_reason):
        return FLOOR_REASON
    return stored_reason


def _with_period(text: str) -> str:
    """句末补一个中文句号；已经有句末标点就不重复加。"""
    if not text:
        return ""
    if text[-1] in "。！？!?.…":
        return text
    return text + "。"


# --------------------------------------------------------------------------- #
# 档位映射
# --------------------------------------------------------------------------- #
def label_of(value: float) -> str:
    """数值 → 档位名。判定顺序按严重度从高到低匹配区间。

    「档位名 = 不超过该值的最高档位」，所以边界值归**更低**的那一档：
    -100 是「创伤」、-10 是「绝望」、-2 是「伤感」、0 是「平静」、
    2 是「愉悦」；超过 10 仍是「超级高兴」。

    坏值（None / 非数 / NaN）按 0.0 处理，返回「平静」。
    """
    v = _as_float(value, 0.0)
    if v is None:  # 理论上到不了，防御一下
        v = 0.0
    if v <= -200.0:
        return "抑郁"
    if v <= -100.0:
        return "创伤"
    if v <= -10.0:
        return "绝望"
    if v <= -8.0:
        return "重度悲伤"
    if v <= -6.0:
        return "中度悲伤"
    if v <= -4.0:
        return "轻度悲伤"
    if v <= -2.0:
        return "伤感"
    if v < 0.0:
        return "无聊"
    if v == 0.0:
        return "平静"
    if v <= 2.0:
        return "愉悦"
    if v <= 4.0:
        return "中度高兴"
    if v <= 8.0:
        return "极度高兴"
    return "超级高兴"


def value_of(label: str) -> float | None:
    """档位名 → 数值；不认识的档位名返回 None（调用方不要瞎猜）。

    只做 strip，不做模糊匹配：「轻度悲伤」和「悲伤」是两回事，
    模糊匹配会让模型换个说法就悄悄改掉数值。
    """
    if label is None:
        return None
    key = str(label).strip()
    if not key:
        return None
    return LABEL_VALUES.get(key)


# --------------------------------------------------------------------------- #
# 衰减
# --------------------------------------------------------------------------- #
def decay(value: float, updated_ts: float, now: float | None = None) -> float:
    """把 value 从 `updated_ts` 线性衰减到 `now`，结果夹在地板内。

    * 速率 `DECAY_PER_MIN`（0.01 / 分钟），往 0 靠；
    * 正值不低于 +FLOOR，负值不高于 -FLOOR；
    * 本来就在地板内（|v| < FLOOR）的值**不动**（地板不会把它往外推）；
    * `updated_ts` 缺失 / 非数 / 在未来 → 视为 0 分钟（只回归，不反向外推）。

    Returns:
        衰减后的数值（保留 6 位小数只为清掉浮点噪声，不改公式）。
    """
    v = _as_float(value, 0.0)
    if v is None:
        v = 0.0
    minutes = _elapsed_minutes(updated_ts, _now(now))
    if v > 0.0:
        v = max(v - minutes * DECAY_PER_MIN, min(v, FLOOR))
    elif v < 0.0:
        v = min(v + minutes * DECAY_PER_MIN, max(v, -FLOOR))
    return round(v, 6)


def decayed_state(state: dict, now: float | None = None) -> dict:
    """把落盘状态衰减到 `now`，返回**不落盘**的展示态。

    Args:
        state: `{"mood_value", "mood_label", "mood_reason", "mood_updated_ts"}`。
            允许缺字段、允许是 None、允许坏值（按「平静 / 0 分钟」兜底）。
        now:  计算基准时间；None 表示 `time.time()`。

    Returns:
        新的四字段字典（不修改入参）。`mood_updated_ts` 填的是这次计算的
        `now`——value 与 ts 必须成对，否则拿它去落盘会被再衰减一次。
        注意本函数**只做展示**，长期没事件时不要直接把它写回
        `system_state.json`（那会把「上次真正更新心情的时间」冲掉）。
    """
    st = state if isinstance(state, dict) else {}
    now_ts = _now(now)
    raw_value = st.get("mood_value", 0.0)
    v0 = _as_float(raw_value, 0.0)
    if v0 is None:
        v0 = 0.0
    v1 = decay(v0, st.get("mood_updated_ts"), now_ts)
    stored = str(st.get("mood_reason") or "").strip()
    moved_to_floor = abs(v0) > FLOOR + _EPS
    return {
        "mood_value": v1,
        "mood_label": label_of(v1),
        "mood_reason": _display_reason(v1, stored, moved_to_floor),
        "mood_updated_ts": now_ts,
    }


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
def apply_event(
    state: dict,
    event_label: str,
    now: float | None = None,
    reason: str = "",
    old_weight: float = 0.5,
    new_weight: float = 0.5,
) -> dict:
    """先按 `now` 衰减旧心情，再与新事件按权重加权，返回**新状态**。

        新心情 = 衰减后的旧值 × old_weight + 事件值 × new_weight

    Args:
        state:  当前落盘状态（缺字段容错同 `decayed_state`）。
        event_label: 模型给的**档位名**（如 "中度高兴"）。不认识时**不猜**：
            数值保持不变，只把时间戳推进到 `now`。
        now:    事件发生时间；None 表示现在。
        reason: 事件理由（给 prompt 用）。空字符串时：落在地板区间取
            `FLOOR_REASON`，否则留空——**不沿用旧理由**，因为旧理由说的是
            上一件事，新事件已经把它冲淡了。
        old_weight / new_weight: 权重，默认各 0.5。

    Returns:
        新的四字段字典：`mood_value`（加权结果）、`mood_label`（由数值重算）、
        `mood_reason`、`mood_updated_ts`（= now）。调用方负责落盘。
    """
    st = state if isinstance(state, dict) else {}
    now_ts = _now(now)
    base = decayed_state(st, now_ts)

    event_value = value_of(event_label)
    if event_value is None:
        # 未知档位名：不改变数值，也不编理由，只推进时间戳。
        return base

    weight_old = _as_float(old_weight, 0.5)
    weight_new = _as_float(new_weight, 0.5)
    if weight_old is None:
        weight_old = 0.5
    if weight_new is None:
        weight_new = 0.5

    new_value = round(base["mood_value"] * weight_old + event_value * weight_new, 6)
    given_reason = str(reason or "").strip()
    return {
        "mood_value": new_value,
        "mood_label": label_of(new_value),
        "mood_reason": given_reason or _default_reason(new_value),
        "mood_updated_ts": now_ts,
    }


# --------------------------------------------------------------------------- #
# 给角色层注入的一行
# --------------------------------------------------------------------------- #
def prompt_line(state: dict, now: float | None = None) -> str:
    """拼成一行注入 system 的中文：只描述此刻心情，不写成状态报告。

    形如：

        （这是你自己此刻的心情，不是别人告诉你的。自然地带着它说话，不用解释、不用报告）
        你现在的心情是「无聊」——无聊（最近没发生什么）。

    平静档（`0 <= value < 2`）可以不提理由，但一定要给档位名。
    """
    eff = decayed_state(state, now)
    value = eff["mood_value"]
    label = eff["mood_label"]
    reason = eff["mood_reason"]

    quiet = 0.0 <= value < 2.0
    if reason and not quiet:
        body = f"你现在的心情是「{label}」——{_with_period(reason)}"
    else:
        body = f"你现在的心情是「{label}」。"

    line = _PROMPT_HEAD + "\n" + body
    if len(line) > _PROMPT_MAX_CHARS:
        line = line[: _PROMPT_MAX_CHARS - 1] + "…"
    return line


# --------------------------------------------------------------------------- #
# 迁移 & 便利函数
# --------------------------------------------------------------------------- #
def from_legacy_emotion(emotion: str) -> str | None:
    """旧枚举（happy/sad/...）→ 新档位名；未知/空返回 None。

    迁移用：`system_state.json` 里的 `emotion` 会被一次性翻成新档位，
    之后 `emotion` 字段可以留着做兼容，也可以是 `None`。
    """
    if not emotion:
        return None
    key = str(emotion).strip().lower()
    if not key:
        return None
    return LEGACY_EMOTION.get(key)


def initial_state(now: float | None = None) -> dict:
    """出厂心情：平静、0、无理由。给「字段缺失」或首次迁移时用。"""
    return {
        "mood_value": 0.0,
        "mood_label": "平静",
        "mood_reason": "",
        "mood_updated_ts": _now(now),
    }


__all__ = [
    "DECAY_PER_MIN",
    "FLOOR",
    "FLOOR_REASON",
    "LABEL_VALUES",
    "LEGACY_EMOTION",
    "MOOD_FIELDS",
    "apply_event",
    "decay",
    "decayed_state",
    "from_legacy_emotion",
    "initial_state",
    "label_of",
    "prompt_line",
    "value_of",
]
