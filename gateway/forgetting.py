"""遗忘算法（能力 2）：**指数衰减 + 越用越熟**，纯函数、免费、不调模型。

为什么要这个文件（2026-11-06 定稿，依据 `plan/DMN审核.md` 能力 2 + 板块三 3.7）
--------------------------------------------------------------------------
现在 `memory/store.py` 的 `prune()` 是**双条件硬门槛**：够旧 **且**
`importance < 0.3`。而 `importance` 根本不是模型打的，是写死的常数
（对话事件 0.6 / 承诺 0.7 / 每日念头 0.55，见 `memory_bridge.py`）——
最低的 0.55 也比 0.3 高 83%，**门槛永远跨不过去**。
实测 29 次 prune、扫了 7 条，`matched` 永远是 0。

所以这里换一套真正会"淡"的算法：抄人类遗忘机制的**两条原则**（主人原话），
但**不照抄 FSRS**。

为什么不照抄 FSRS
-----------------
FSRS 是幂函数 `R = (1 + F·t/S) ** C`（F=19/81, C=-0.5），它的目标是让人
**记住**，所以衰减极慢：S=30 天时 R 掉到 0.1 要 **34.7 年**。
我们的目标是让**没用的自己消失**，方向相反。

换成指数衰减：`R = e^(-t/S)`
    t = S   → R ≈ 0.368
    t = 3S  → R ≈ 0.0498
S=30 天时 R 掉到 0.1 只要 **69 天**；180 天时 R≈0.0025（早就该删）。
半年没人碰的记忆一年多淡掉，这才符合直觉。

两条原则怎么落地
----------------
1. **随时间下滑**：`retrievability()` = e^(-t/S)，t 由系统层代码算（模型没有
   真实时间体验，时间判断绝不能交给它，见 3.7 末节）。
2. **越用越熟**：每次召回 `use_count += 1`、`last_used = now`、
   `stability *= 1.5`。初值 7 天，召回 5 次 → 7×1.5^5 ≈ 53.2 天；
   再 5 次 → 403.9 天。见 `decayed_stability()` / `apply_recall()`。

每日扫描三档（`classify()` / `plan_prune()`）
    R > 0.5            → 留
    R < 0.05           → 永不删类型则拉高稳定性留下，否则删
    中间               → 攒进"待判"候选，攒够再给模型看

给模型看的只有最后 10~20%
------------------------
`review_prompt()` 的问法**故意不含任何时间词**：只问「这几条记忆里，哪些她
以后再也不会用到」，让模型回答留/删（`parse_review_reply()`），
它不回答分数——分数是假精确（0.42 和 0.43 有什么区别？）。

metadata 字段约定（接线时建议按这个写，全部可缺省）
--------------------------------------------------
===================  ==========================================  ==================
字段                 含义                                        缺省
===================  ==========================================  ==================
`kind`               promise / birthday / cognition /            按 `observation`
                     observation / emotion（也接受中文别名）     处理
`stability_days`     初始稳定性（天）；显式 `None` = 永不删         按 kind 查表
`use_count`          被召回次数                                   0
`last_used`          最后被召回时间（epoch 秒）                   回退 `create_time`
`create_time`        写入时间（epoch 秒，`store.add()` 已自动写）  缺 → **不删**
`importance`         0~1；≥0.85 视为永不删                        未打分
`never_forget`       显式永不删；显式 `False` 可解除类型保护        未设置
===================  ==========================================  ==================

本模块**只做判断、不落盘**：`plan_prune()` 返回"该删哪些"，真删由调用方
（`MemoryStore.prune` / `memory_bridge`）执行。这样单测可以完全跑在纯函数上。
"""
from __future__ import annotations

import json
import math
import re
import time

__all__ = [
    "DEFAULT_KIND",
    "DEFAULT_STABILITY_DAYS",
    "KIND_ALIASES",
    "KIND_LABELS",
    "MAX_STABILITY_DAYS",
    "NEVER_FORGET_IMPORTANCE",
    "NEVER_FORGET_STABILITY_DAYS",
    "age_days",
    "apply_recall",
    "classify",
    "decayed_stability",
    "never_forget",
    "parse_review_reply",
    "plan_prune",
    "retrievability",
    "review_prompt",
    "stability_for",
    "summary",
    "thresholds",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 按记忆类型给的初始稳定性（天）。**None = 永不删**（承诺 / 生日）。
#: 数值来自 3.7「完整方案」：人物认知 90 天 / 一般观察 30 天 / 情绪 7 天。
DEFAULT_STABILITY_DAYS: dict[str, float | None] = {
    "promise": None,
    "birthday": None,
    "cognition": 90.0,
    "observation": 30.0,
    "emotion": 7.0,
}

#: 认不出的类型一律按"一般观察"处理（宁可给个确定数值，也不要因为缺字段就不算）。
DEFAULT_KIND = "observation"

#: 永不删类型万一 R 掉下去了，把稳定性拉到这个量级（≈100 年）——相当于"重新记牢"。
NEVER_FORGET_STABILITY_DAYS = 36500.0
#: 稳定性上限（≈100 万年），纯防溢出，正常业务碰不到。
MAX_STABILITY_DAYS = 3.65e8
#: importance 到这个值就视为永不删（和"永不删类型"同等保护）。
NEVER_FORGET_IMPORTANCE = 0.85

#: 类型别名：抽取模型顺手打的中文类型、以及英文变体，都归一化到 5 个标准 kind。
KIND_ALIASES: dict[str, str] = {
    "promise": "promise",
    "承诺": "promise",
    "约定": "promise",
    "答应": "promise",
    "birthday": "birthday",
    "生日": "birthday",
    "cognition": "cognition",
    "认知": "cognition",
    "人物认知": "cognition",
    "人物": "cognition",
    "profile": "cognition",
    "observation": "observation",
    "观察": "observation",
    "一般观察": "observation",
    "事实": "observation",
    "emotion": "emotion",
    "情绪": "emotion",
    "心情": "emotion",
    "感受": "emotion",
}

#: 给 `summary()` / 报告看的中文名。
KIND_LABELS: dict[str, str] = {
    "promise": "承诺",
    "birthday": "生日",
    "cognition": "人物认知",
    "observation": "一般观察",
    "emotion": "情绪",
}

_DAY_SECONDS = 86400.0
_MISSING = object()


# --------------------------------------------------------------------------- #
# 内部小工具：全部 fail-safe，绝不因为脏数据抛异常
# --------------------------------------------------------------------------- #
def _as_float(value) -> float | None:
    """把任意值转成有限 float；转不了返回 None（bool 不算数字）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _as_int(value, default: int = 0) -> int:
    """把任意值转成 int；转不了用 default。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        num = _as_float(value)
        return int(num) if num is not None else default


def _as_dict(value) -> dict:
    """metadata 可能是 dict / JSON 字符串 / None，统一成 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truthy(value) -> bool:
    """宽松真值：1 / "true" / "是" / "yes" 都算真。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "是", "永不删"}
    return False


def _normalize_kind(kind) -> str:
    """把类型名归一到 5 个标准 kind；认不出返回空串。"""
    if not isinstance(kind, str):
        return ""
    key = kind.strip().lower()
    if not key:
        return ""
    return KIND_ALIASES.get(key, "")


def _sanitize_thresholds(keep_above, drop_below) -> tuple[float, float]:
    """校验两档门槛，非法就回退默认（0.5 / 0.05），并保证 drop < keep。

    两档**颠倒**（`drop_below >= keep_above`）时整对回退默认：这种配置没有
    可解释的语义（既不是"全留"也不是"全删"），按默认走路比按半个坏配置走安全。
    """
    ka = _as_float(keep_above)
    db = _as_float(drop_below)
    if ka is None or not (0.0 < ka <= 1.0):
        ka = 0.5
    if db is None or not (0.0 <= db < 1.0):
        db = 0.05
    if db >= ka:
        ka, db = 0.5, 0.05
    return ka, db


def _required_stability(age: float, keep_above: float) -> float:
    """要让 R 回到 keep_above，S 至少得多大（解 e^(-t/S) = keep_above）。"""
    age_value = _as_float(age)
    ka = _as_float(keep_above)
    if age_value is None or age_value <= 0.0:
        return NEVER_FORGET_STABILITY_DAYS
    if ka is None or not (0.0 < ka < 1.0):
        return NEVER_FORGET_STABILITY_DAYS
    return age_value / -math.log(ka)


# --------------------------------------------------------------------------- #
# 1. 稳定性初值
# --------------------------------------------------------------------------- #
def stability_for(kind: str, meta: dict | None = None,
                  table: dict | None = None) -> float | None:
    """按记忆类型给**初始稳定性**（天）。返回 None 表示「永不删」。

    优先级：
        1. `meta["stability_days"]` 显式数值（>0）—— 调用方自己算过就用它的；
        2. `meta["stability_days"] is None` —— 显式永不删；
        3. 类型表（`table`，缺省 `DEFAULT_STABILITY_DAYS`）。

    例外：`meta["never_forget"] is False` 时，即使类型是 promise/birthday
    （表里 None）也退化成一般观察的初值——这是给运维留的"我确定要放过期"口子，
    否则该开关在 S=None 上完全无效。

    Args:
        kind: 记忆类型，接受 `promise/birthday/cognition/observation/emotion`
            以及中文别名（承诺/生日/人物认知/一般观察/情绪）。
        meta: 该条记忆的 metadata；脏数据一律忽略，不抛。
        table: 自定义类型表（`thresholds()["default_stability_days"]`）；
            表里非法值按「永不删」处理（宁可留，不可误删）。

    Returns:
        稳定性天数，或 None（永不删）。
    """
    meta_dict = _as_dict(meta)
    if "stability_days" in meta_dict:
        raw = meta_dict.get("stability_days")
        if raw is None:
            return None
        override = _as_float(raw)
        if override is not None and override > 0.0:
            return override

    kind_table = _as_dict(table)
    if not kind_table:
        kind_table = dict(DEFAULT_STABILITY_DAYS)

    key = _normalize_kind(kind) or DEFAULT_KIND
    base = kind_table.get(key, kind_table.get(DEFAULT_KIND, DEFAULT_STABILITY_DAYS[DEFAULT_KIND]))
    if base is None and meta_dict.get("never_forget") is False:
        # 显式 `never_forget=False` 是给运维留的"我确定要放过期"口子：
        # 类型是 promise/birthday（表里 None＝永不删）时，退化成一般观察的初值，
        # 否则这个开关等于没写（S=None 会让 R 永远是 1.0）。
        base = kind_table.get(DEFAULT_KIND, DEFAULT_STABILITY_DAYS[DEFAULT_KIND])
    if base is None:
        return None
    value = _as_float(base)
    if value is None or value <= 0.0:
        # 表写坏了：给不出可信数值时按「永不删」兜底，避免误删
        return None
    return value


# --------------------------------------------------------------------------- #
# 2. 衰减与年龄
# --------------------------------------------------------------------------- #
def retrievability(stability_days: float, t_days: float) -> float:
    """可提取性 `R = e^(-t/S)`（指数衰减，**不是** FSRS 的幂函数）。

    为什么换指数（3.7）：FSRS 目标是「记住」，衰减极慢（S=30 天要 34.7 年
    才掉到 0.1）；我们要的是「让没用的自己消失」，半年没人碰的记忆一年
    多就该淡掉。指数在 t=S 时 R≈0.368、t=3S 时 R≈0.0498，符合直觉。

    fail-safe 约定（**都不抛异常**）：
        * `S is None`   → 1.0（永不删类型，不衰减）；
        * `S <= 0`/NaN/非数字 → 0.0（给不出可信稳定性＝已经记不住）；
        * `t` 非法/负数   → 1.0（时间不可信或"未来"，按刚发生算，宁可留）；
        * 结果恒在 [0, 1]。

    Args:
        stability_days: 稳定性（天）。
        t_days: 距上次想起过了多少天。

    Returns:
        [0, 1] 之间的可提取性。
    """
    if stability_days is None:
        return 1.0
    stability = _as_float(stability_days)
    if stability is None or stability <= 0.0:
        return 0.0
    age = _as_float(t_days)
    if age is None or age <= 0.0:
        return 1.0
    try:
        r = math.exp(-age / stability)
    except (OverflowError, ValueError):
        return 0.0
    if not math.isfinite(r):
        return 0.0
    return min(1.0, max(0.0, r))


def age_days(created_ts: float, now: float | None = None) -> float:
    """算「距今多少天」。

    fail-safe：时间戳缺失/非法/为 0 → 返回 0.0（当作刚发生，结果只会是"留"）；
    时间戳在未来（clamp 到 0）；`now` 缺省用 `time.time()`。

    Args:
        created_ts: 起点时间戳（epoch 秒）。建议传 `last_used`，没有就 `create_time`。
        now: 当前时间戳（epoch 秒），测试里传固定值保证可复现。

    Returns:
        非负天数。
    """
    start = _as_float(created_ts)
    if start is None or start <= 0.0:
        return 0.0
    current = _as_float(now)
    if current is None:
        current = time.time()
    delta = (current - start) / _DAY_SECONDS
    if not math.isfinite(delta) or delta <= 0.0:
        return 0.0
    return delta


def decayed_stability(stability_days: float | None, use_count: int,
                      per_recall_factor: float = 1.5) -> float:
    """把「被召回次数」折算进稳定性：`S_eff = S * factor ** use_count`。

    这就是「越用越熟」（3.7）：初值 7 天，召回 5 次 → 7×1.5^5 ≈ 53.2 天；
    再 5 次 → 403.9 天。参数与类型无关，全部可调（见 `thresholds()`）。

    fail-safe：
        * `stability_days is None` → `NEVER_FORGET_STABILITY_DAYS`（≈100 年）。
          注意这是**纯函数层的兜底值**，别拿它当"永不删"用：`plan_prune()`
          在 S 为 None 时**根本不调本函数**，R 直接取 1.0（永不删类型不衰减）。
        * S 非法/<=0 → 0.0（已经记不住）；
        * `use_count` 非法/负数 → 0；`factor` 非法或 <1 → 1.0（不增长，不罚分）；
        * 溢出 → 封顶 `MAX_STABILITY_DAYS`。

    Args:
        stability_days: 当前稳定性（天）。
        use_count: 被召回次数。
        per_recall_factor: 每次召回的稳定性乘数，默认 1.5。

    Returns:
        折算后的稳定性（天）。
    """
    if stability_days is None:
        return NEVER_FORGET_STABILITY_DAYS
    base = _as_float(stability_days)
    if base is None or base <= 0.0:
        return 0.0
    count = max(0, _as_int(use_count, 0))
    factor = _as_float(per_recall_factor)
    if factor is None or factor < 1.0:
        factor = 1.0
    if count == 0:
        return min(base, MAX_STABILITY_DAYS)
    try:
        grown = base * math.pow(factor, count)
    except (OverflowError, ValueError):
        return MAX_STABILITY_DAYS
    if not math.isfinite(grown) or grown > MAX_STABILITY_DAYS:
        return MAX_STABILITY_DAYS
    return grown


def apply_recall(meta: dict, now: float | None = None,
                 per_recall_factor: float = 1.5) -> dict:
    """「每次召回」的写盘助手：`use_count += 1`、`last_used = now`、`stability *= 1.5`。

    **两种接线方式，二选一，别混用**（混用会重复放大稳定性）：

    * **A 推荐（只存计数）**：召回时只写 `use_count + 1` / `last_used`，
      稳定性在读取时用 `decayed_stability()` 折算。幂等、不写盘也能算。
    * **B 折叠进稳定性**：召回时调本函数，把 `stability_days` 直接乘一次
      factor。此后 `plan_prune()` 必须传 `fold_use_count=False`，否则会再乘
      `use_count` 次。

    永不删类型（stability 为 None 且没有显式数值）保持 None。

    Args:
        meta: 该条记忆的 metadata（不会被原地修改，返回新 dict）。
        now: 召回时间戳，缺省 `time.time()`。
        per_recall_factor: 每次召回的稳定性乘数。

    Returns:
        新的 metadata dict；坏输入返回空 dict。
    """
    meta_dict = dict(_as_dict(meta))
    if not meta_dict:
        return {}

    count = max(0, _as_int(meta_dict.get("use_count"), 0)) + 1
    meta_dict["use_count"] = count
    stamp = _as_float(now)
    meta_dict["last_used"] = stamp if stamp is not None else time.time()

    factor = _as_float(per_recall_factor)
    if factor is None or factor < 1.0:
        factor = 1.5

    current = meta_dict.get("stability_days", _MISSING)
    if current is _MISSING:
        # 没显式写过：用类型初值作为"这一次乘之前"的基线
        base = stability_for(meta_dict.get("kind"), meta_dict)
    elif current is None:
        base = None
    else:
        base = _as_float(current)
        if base is None or base <= 0.0:
            base = stability_for(meta_dict.get("kind"), meta_dict)
    if base is None:
        meta_dict["stability_days"] = None
    else:
        grown = base * factor
        meta_dict["stability_days"] = (
            grown if math.isfinite(grown) and grown < MAX_STABILITY_DAYS else MAX_STABILITY_DAYS
        )
    return meta_dict


# --------------------------------------------------------------------------- #
# 3. 三档判定
# --------------------------------------------------------------------------- #
def classify(stability_days: float, age: float,
             keep_forever: bool = False,
             keep_above: float = 0.5, drop_below: float = 0.05) -> str:
    """每日扫描的三档判定：`"keep"` / `"drop"` / `"review"`。

    边界（**取等归属已钉死，测试锁住**）：
        * `R >= keep_above` → `"keep"`（R 恰好 0.5 算**留**）；
        * `R <= drop_below` → `"drop"`（R 恰好 0.05 算**删**，正好对应 t=3S）；
        * 其余（`drop_below < R < keep_above`）→ `"review"`（攒批给模型）。

    `keep_forever=True`（永不删类型）：R 再低也不 `"drop"`，此时
        * `R >= keep_above`   → `"keep"`；
        * `R <= drop_below`   → `"keep"`（调用方应同时拉高稳定性，见 `plan_prune`）；
        * 中间                → `"review"`（`plan_prune` 会把它并回 `"keep"`，
          永不删类型不必占用模型名额）。

    Args:
        stability_days: 稳定性（天）；None = 永不删（按 R=1.0 算）。
        age: 距上次想起过了多少天。
        keep_forever: 是否永不删类型。
        keep_above: 留的门槛，默认 0.5。
        drop_below: 删的门槛，默认 0.05。

    Returns:
        `"keep"` / `"drop"` / `"review"`。
    """
    high, low = _sanitize_thresholds(keep_above, drop_below)
    r = retrievability(stability_days, age)
    if r >= high:
        return "keep"
    if r <= low:
        return "keep" if keep_forever else "drop"
    return "review"


def never_forget(meta: dict) -> bool:
    """这条记忆是否**永不删**。

    判定（任一命中即 True）：
        1. `meta["never_forget"]` 为真（True / 1 / "true" / "是" / "永不删"…）；
        2. `kind` 是 `promise` / `birthday`（承诺、生日）；
        3. `importance >= 0.85`；
        4. 显式写了 `stability_days = None`（"我就是要它永不删"）。

    显式 `never_forget = False` 是**总开关**：即使 kind 是 promise 也返回 False
    （给运维留一个"我确定要放过期"的口子）。坏输入一律返回 False，不抛。

    Args:
        meta: 该条记忆的 metadata。

    Returns:
        bool。
    """
    meta_dict = _as_dict(meta)
    if not meta_dict:
        return False
    if "never_forget" in meta_dict:
        flag = meta_dict.get("never_forget")
        if isinstance(flag, bool) and flag is False:
            return False
        if _truthy(flag):
            return True
    if _normalize_kind(meta_dict.get("kind")) in ("promise", "birthday"):
        return True
    importance = _as_float(meta_dict.get("importance"))
    if importance is not None and importance >= NEVER_FORGET_IMPORTANCE:
        return True
    if "stability_days" in meta_dict and meta_dict.get("stability_days") is None:
        return True
    return False


# --------------------------------------------------------------------------- #
# 4. 每日扫描：出计划（不落盘）
# --------------------------------------------------------------------------- #
def _doc_id(doc: dict) -> str:
    """从库里的一条 doc 取 doc_id（兼容 doc_id / id / docId）。"""
    for key in ("doc_id", "docId", "id"):
        value = doc.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _timestamp(meta: dict) -> tuple[float | None, str]:
    """取"这条记忆多久没被想起"的起点：优先最后一次召回，回退创建时间。"""
    for key in ("last_used", "last_recall", "last_recalled_at", "last_used_at", "updated_at"):
        value = _as_float(meta.get(key))
        if value is not None and value > 0.0:
            return value, key
    for key in ("create_time", "created_at", "created_ts", "created", "ts"):
        value = _as_float(meta.get(key))
        if value is not None and value > 0.0:
            return value, key
    return None, ""


def plan_prune(docs: list[dict], now: float | None = None, **params) -> dict:
    """每日扫描：给每一条记忆判"留 / 删 / 待判"，**只出计划不落盘**。

    为什么只出计划（3.7 + 数据事故教训）：时间判断必须留在系统层代码里，
    真正的删除动作由调用方（`MemoryStore.prune` / `memory_bridge`）执行，
    方便 dry_run、审计、分批。本函数是纯函数，不碰任何文件/网络。

    返回结构：
        {
          "keep":   [doc_id, ...],   # R 够高，留
          "drop":   [doc_id, ...],   # R 够低且不是永不删类型，删
          "review": [doc_id, ...],   # 中间区间，攒批给模型看内容价值
          "stats":  {...},           # 计数 + 生效参数（boosted 表示"拉高稳定性"）
          "rows":   [ {...}, ... ],  # 逐条判定依据（R / S / age / kind / 档位 / 原因）
        }

    `rows` 每条的字段：
        doc_id / kind / keep_forever / stability_days（类型初值或显式值）/
        effective_stability_days（折算召回次数后；**None = 永不删、不衰减**）/
        use_count / age_days / retrievability / decision / reason /
        boost_to_stability_days / timestamp_source

    可调参数（`**params`，缺省见 `thresholds()`）：
        keep_above / drop_below / per_recall_factor / default_stability_days /
        never_forget_min_stability_days / fold_use_count。

    永不删类型（`never_forget()`）：R 掉到 `drop_below` 以下时**不删**，
    而是给出 `boost_to_stability_days`（拉高稳定性，下次扫描 R 就回到门槛上），
    并在 `stats["boosted"]` 里计数——这就是 3.7 说的"stability 拉高，留"。

    缺时间戳的条目：`decision="keep"`、`stats["no_timestamp"] += 1`
    （fail-safe：拿不到时间就不删，和 `MemoryStore.prune` 的老规矩一致）。

    Args:
        docs: `[{"doc_id", "text", "metadata"}]`，metadata 可能含
            kind / stability_days / use_count / last_used / create_time /
            importance / never_forget。
        now: 当前时间戳（epoch 秒），测试传固定值保证可复现。
        **params: 见上。

    Returns:
        见上；坏输入（None / 字符串 / 缺字段 / 元素不是 dict）一律返回安全结果，不抛。
    """
    cfg = thresholds(params)
    keep_above = cfg["keep_above"]
    drop_below = cfg["drop_below"]
    factor = cfg["per_recall_factor"]
    kind_table = cfg["default_stability_days"]
    fold_use_count = _truthy(params.get("fold_use_count", True))
    never_min = _as_float(params.get("never_forget_min_stability_days"))
    if never_min is None or never_min <= 0.0:
        never_min = NEVER_FORGET_STABILITY_DAYS

    now_ts = _as_float(now)
    if now_ts is None:
        now_ts = time.time()

    items = docs if isinstance(docs, (list, tuple)) else ([] if docs is None else [docs])

    rows: list[dict] = []
    keep: list[str] = []
    drop: list[str] = []
    review: list[str] = []
    stats = {
        "scanned": 0,
        "keep": 0,
        "drop": 0,
        "review": 0,
        "never_forget": 0,
        "boosted": 0,
        "no_timestamp": 0,
        "skipped": 0,
        "params": dict(cfg),
    }

    for doc in items:
        if not isinstance(doc, dict):
            stats["skipped"] += 1
            continue
        stats["scanned"] += 1
        doc_id = _doc_id(doc)
        if not doc_id:
            stats["skipped"] += 1
            continue

        meta = _as_dict(doc.get("metadata"))
        raw_kind = meta.get("kind", doc.get("kind"))
        kind = _normalize_kind(raw_kind) or (
            str(raw_kind).strip()[:24] if raw_kind is not None and str(raw_kind).strip() else DEFAULT_KIND
        )
        keep_forever = never_forget(meta)
        if keep_forever:
            stats["never_forget"] += 1

        initial_stability = stability_for(raw_kind, meta, kind_table)
        use_count = max(0, _as_int(meta.get("use_count"), 0))
        # 永不删类型（S=None）**根本不衰减**：R 恒为 1.0，不参与任何"该淡了"的计算。
        # 注意不要在这里把它折成 `NEVER_FORGET_STABILITY_DAYS` 那个有限值——
        # 那会让一条 60 年前的承诺算出 R=0.55，看起来像"快忘了"，与 3.7 的意图相反。
        effective: float | None = None
        if initial_stability is not None:
            effective = decayed_stability(
                initial_stability, use_count if fold_use_count else 0, factor
            )

        boost_to: float | None = None
        stamp, stamp_source = _timestamp(meta)
        if stamp is None:
            # 拿不到时间 → 不删（fail-safe）。这条永远不可能是待判。
            age = 0.0
            r = 1.0
            decision = "keep"
            reason = "缺时间戳 → 不删（fail-safe：宁可留，不可误删）"
            stats["no_timestamp"] += 1
        else:
            age = age_days(stamp, now_ts)
            r = retrievability(effective, age)
            decision = classify(effective, age, keep_forever, keep_above, drop_below)
            if keep_forever:
                if decision == "review":
                    decision = "keep"
                    reason = (
                        f"永不删类型：不进模型待判（R={r:.4f} 在中间区 "
                        f"({drop_below:g}, {keep_above:g})，暂留）"
                    )
                elif r >= keep_above:
                    reason = f"R={r:.4f} ≥ {keep_above:g} → 留（永不删类型）"
                else:
                    required = _required_stability(age, keep_above)
                    base_for_boost = effective if effective is not None else never_min
                    boost_to = min(
                        max(base_for_boost, never_min, required), MAX_STABILITY_DAYS
                    )
                    stats["boosted"] += 1
                    reason = (
                        f"永不删类型且 R={r:.4f} ≤ {drop_below:g} → "
                        f"拉高稳定性到 {boost_to:g} 天后留"
                    )
            elif decision == "keep":
                reason = f"R={r:.4f} ≥ {keep_above:g} → 留"
            elif decision == "drop":
                reason = f"R={r:.4f} ≤ {drop_below:g} → 删"
            else:
                reason = (
                    f"{drop_below:g} < R={r:.4f} < {keep_above:g} → "
                    f"待判（攒批给模型看内容价值）"
                )

        rows.append({
            "doc_id": doc_id,
            "kind": kind,
            "keep_forever": bool(keep_forever),
            "stability_days": initial_stability,
            # None = 永不删（不衰减）；其余是折算召回次数后的天数
            "effective_stability_days": None if effective is None else float(effective),
            "use_count": use_count,
            "age_days": float(age),
            "retrievability": float(r),
            "decision": decision,
            "reason": reason,
            "boost_to_stability_days": boost_to,
            "timestamp_source": stamp_source,
        })

        if decision == "drop":
            drop.append(doc_id)
        elif decision == "review":
            review.append(doc_id)
        else:
            keep.append(doc_id)
        stats[decision] += 1

    return {"keep": keep, "drop": drop, "review": review, "stats": stats, "rows": rows}


# --------------------------------------------------------------------------- #
# 5. 最后 10~20%：把待判候选给模型看（只问内容，不问时间、不打分）
# --------------------------------------------------------------------------- #
#: 候选内容进 prompt 时的截断长度（省 token，也避免原文里的长引用）。
REVIEW_TEXT_LIMIT = 120

_REVIEW_INSTRUCTION = (
    "下面是一些可能已经不再需要的记忆。\n"
    "\n"
    "请只判断内容：这几条记忆里，哪些她以后再也不会用到？\n"
    "\n"
    "回答要求：\n"
    "1. 只写文档 id，每个 id 后面写「删」或「留」；\n"
    "2. 不要解释，不要编造候选之外的 id；\n"
    "3. 拿不准的写「留」。\n"
)


def review_prompt(candidates: list[dict], max_items: int = 10) -> tuple[str, list[str]]:
    """拼"给模型看"的提示词，返回 `(提示词, 候选 doc_id 顺序)`。

    **问法为什么这么写（3.7 末节，架构事实）**：模型没有真实时间体验，
    所以时间判断必须留在系统层（`retrievability()` 已经算完了）。
    给模型的只能是**内容判断**——「哪些她以后再也不会用到」。

    因此提示词里**绝不出现**任何时间问法（"三个月"/"多久以前"/"天前"/"时间"），
    也**不让模型打分**（分数是假精确，留/删它答得准）。

    候选内容会截断到 `REVIEW_TEXT_LIMIT` 字；doc_id 兼容 `doc_id` / `id`。

    Args:
        candidates: `[{"doc_id", "text", ...}]`，通常就是
            `plan_prune()` 的 `review` 名单对应 docs。
        max_items: 最多塞几条（防止一次问太多、模型答串），缺省 10。

    Returns:
        `(prompt, ids)`；没有有效候选时返回 `("", [])`。
    """
    limit = _as_int(max_items, 10)
    if limit <= 0:
        return "", []
    items = candidates if isinstance(candidates, (list, tuple)) else []

    ids: list[str] = []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        doc_id = _doc_id(item)
        if not doc_id or doc_id in ids:
            continue
        text = str(item.get("text") or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) > REVIEW_TEXT_LIMIT:
            text = text[:REVIEW_TEXT_LIMIT] + "…"
        ids.append(doc_id)
        lines.append(f"[{len(ids)}] id={doc_id} 内容={text}")
        if len(ids) >= limit:
            break

    if not ids:
        return "", []
    prompt = _REVIEW_INSTRUCTION + "\n候选：\n" + "\n".join(lines) + "\n"
    return prompt, ids


#: 解析模型回复时用的关键词（中英都收，模型偶尔会回英文）。
_DROP_WORDS = ("删", "忘", "清掉", "清除", "不要了", "drop", "delete", "remove", "forget")
_KEEP_WORDS = ("留", "保留", "记住", "keep", "hold", "retain", "save")


def _nearest_keyword(segment: str, reverse: bool = False) -> str | None:
    """在片段里找**离 id 最近**的「删/留」关键词，返回 "drop" / "keep" / None。"""
    best_kind: str | None = None
    best_pos: int | None = None
    for words, kind in ((_DROP_WORDS, "drop"), (_KEEP_WORDS, "keep")):
        for word in words:
            idx = segment.rfind(word) if reverse else segment.find(word)
            if idx < 0:
                continue
            pos = -idx if reverse else idx
            if best_pos is None or pos < best_pos:
                best_kind = kind
                best_pos = pos
    return best_kind


def _id_pattern(doc_id: str) -> re.Pattern:
    """候选 id 的"整词"匹配（避免 m1 命中 m10）。"""
    return re.compile(
        r"(?<![0-9A-Za-z_\-])" + re.escape(doc_id) + r"(?![0-9A-Za-z_\-])",
        re.IGNORECASE,
    )


def _parse_json_reply(text: str, ids: list[str]) -> dict | None:
    """回复是 JSON（{"drop": [...], "keep": [...]}）时先按 JSON 解析。"""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, list):
        payload = {"drop": payload}
    if not isinstance(payload, dict):
        return None
    index = {doc_id.lower(): doc_id for doc_id in ids}
    out = {"drop": [], "keep": [], "ok": False, "ignored": []}
    for key in ("drop", "delete", "remove", "forget", "删"):
        for value in _as_list(payload.get(key)):
            real = index.get(str(value).strip().lower())
            if real and real not in out["drop"]:
                out["drop"].append(real)
    for key in ("keep", "hold", "retain", "留"):
        for value in _as_list(payload.get(key)):
            real = index.get(str(value).strip().lower())
            if real and real not in out["keep"] and real not in out["drop"]:
                out["keep"].append(real)
    out["ok"] = bool(out["drop"] or out["keep"])
    return out


def _as_list(value) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [value]


def parse_review_reply(reply: str, candidate_ids: list[str]) -> dict:
    """解析模型对 `review_prompt()` 的回答，得到 `{"drop": [...], "keep": [...]}`。

    安全边界（防幻觉）：**只会返回候选列表里出现过的 id**。模型编出来的 id
    （例如候选是 m1/m2，它答 m99）一律丢弃，记在 `ignored` 里。

    解析顺序：
        1. 回复是 JSON → 按 JSON 解析；
        2. 逐次扫描候选 id 在文本中的位置，取离它最近的关键词（删/留，中英都认）；
        3. 仍没判定的 id，按"整行只有一个关键词"的行级规则补一次
           （兼容 `m1 m2 都留` 这种写法）。

    Returns:
        `{"drop": [...], "keep": [...], "ok": bool, "ignored": [...]}`
        * `ok=False` 表示一个有效判定都没解析出来（乱码/空回复/答非所问）；
        * `ignored` 是"看起来像 id 但不是候选"的幻觉条目（仅用于审计）。
    """
    result: dict = {"drop": [], "keep": [], "ok": False, "ignored": []}
    ids = [str(x).strip() for x in _as_list(candidate_ids) if str(x).strip()]
    if not ids:
        return result
    # 保序去重
    seen: set[str] = set()
    uniq_ids = []
    for doc_id in ids:
        if doc_id not in seen:
            seen.add(doc_id)
            uniq_ids.append(doc_id)
    if not isinstance(reply, str) or not reply.strip():
        return result

    text = reply.strip()
    parsed_json = _parse_json_reply(text, uniq_ids)
    if parsed_json is not None:
        return parsed_json

    decisions: dict[str, str] = {}
    occurrences: list[tuple[int, int, str]] = []
    for doc_id in uniq_ids:
        for match in _id_pattern(doc_id).finditer(text):
            occurrences.append((match.start(), match.end(), doc_id))
    occurrences.sort()

    for index, (start, end, doc_id) in enumerate(occurrences):
        if doc_id in decisions:
            continue
        next_start = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(text)
        prev_end = occurrences[index - 1][1] if index > 0 else 0
        after = _nearest_keyword(text[end:next_start])
        if after is not None:
            decisions[doc_id] = after
            continue
        before = _nearest_keyword(text[prev_end:start], reverse=True)
        if before is not None:
            decisions[doc_id] = before

    # 行级兜底：整行只有一个关键词方向时，把该行里还没判定的候选都按它处理
    for line in text.splitlines():
        line_ids = [d for d in uniq_ids if _id_pattern(d).search(line)]
        pending = [d for d in line_ids if d not in decisions]
        if not pending:
            continue
        direction = _nearest_keyword(line)
        if direction is None:
            continue
        opposite = _nearest_keyword(line, reverse=True)
        if opposite is not None and opposite != direction:
            # 一行里两种关键词都有，方向不唯一，不猜
            continue
        for doc_id in pending:
            decisions[doc_id] = direction

    # 幻觉 id：像 id（含数字）但不在候选里
    for token in re.findall(r"(?<![0-9A-Za-z_\-])([A-Za-z]{0,4}[-_]?[0-9][0-9A-Za-z_\-]*)"
                            r"(?![0-9A-Za-z_\-])", text):
        if token not in uniq_ids and token.lower() not in {d.lower() for d in uniq_ids}:
            if token not in result["ignored"]:
                result["ignored"].append(token)

    for doc_id in uniq_ids:
        direction = decisions.get(doc_id)
        if direction == "drop":
            result["drop"].append(doc_id)
        elif direction == "keep":
            result["keep"].append(doc_id)
    result["ok"] = bool(result["drop"] or result["keep"])
    return result


# --------------------------------------------------------------------------- #
# 6. 参数与体检说明
# --------------------------------------------------------------------------- #
def _pick(section: dict, names: tuple[str, ...]):
    """从配置段里按顺序取第一个"出现过"的键（值原样返回）。"""
    for name in names:
        if name in section:
            return section[name]
    return _MISSING


def thresholds(cfg: dict | None = None) -> dict:
    """读出所有可调参数，缺省用默认值；**永远返回一份合法的完整参数**。

    接受的配置段（`cfg` 为 None 或脏数据时全用默认）：
        * 可嵌套在 `forgetting` / `forget` 子段里；
        * `keep_above`（别名 `forget_keep_above`）：R≥它 → 留，默认 0.5；
        * `drop_below`（别名 `forget_drop_below`）：R≤它 → 删，默认 0.05；
        * `per_recall_factor`（别名 `forget_per_recall_factor`）：默认 1.5；
        * `default_stability_days`（别名 `forget_stability_days`）：按类型
          覆盖初值，**局部合并**到默认表上，值 None 仍表示永不删。

    校验（防住"配错了把记忆全删了"）：`keep_above` 必须在 (0,1]、
    `drop_below` 在 [0,1) 且小于 `keep_above`、`per_recall_factor >= 1`；
    任何一项非法 → 该项回退默认值，不影响其他项。

    Args:
        cfg: 配置段 dict（对应 `config.json` 里 `system` 段或一个子段），可 None。

    Returns:
        `{"keep_above", "drop_below", "per_recall_factor", "default_stability_days"}`
    """
    result = {
        "keep_above": 0.5,
        "drop_below": 0.05,
        "per_recall_factor": 1.5,
        "default_stability_days": dict(DEFAULT_STABILITY_DAYS),
    }
    section = _as_dict(cfg)
    if not section:
        return result
    for key in ("forgetting", "forget"):
        sub = _as_dict(section.get(key))
        if sub:
            section = {**section, **sub}

    raw_keep = _pick(section, ("keep_above", "forget_keep_above"))
    if raw_keep is not _MISSING:
        value = _as_float(raw_keep)
        if value is not None and 0.0 < value <= 1.0:
            result["keep_above"] = value

    raw_drop = _pick(section, ("drop_below", "forget_drop_below"))
    if raw_drop is not _MISSING:
        value = _as_float(raw_drop)
        if value is not None and 0.0 <= value < result["keep_above"]:
            result["drop_below"] = value

    raw_factor = _pick(section, ("per_recall_factor", "forget_per_recall_factor"))
    if raw_factor is not _MISSING:
        value = _as_float(raw_factor)
        if value is not None and 1.0 <= value <= 100.0:
            result["per_recall_factor"] = value

    raw_table = _pick(section, ("default_stability_days", "forget_stability_days"))
    table = _as_dict(raw_table)
    for key, value in table.items():
        normalized = _normalize_kind(key) or str(key).strip().lower()
        if not normalized:
            continue
        if value is None:
            result["default_stability_days"][normalized] = None
            continue
        days = _as_float(value)
        if days is not None and days > 0.0:
            result["default_stability_days"][normalized] = days

    return result


def summary() -> str:
    """一行中文说明当前生效的参数（给体检/文档用，参数变了它跟着变）。"""
    cfg = thresholds()
    table = cfg["default_stability_days"]
    parts: list[str] = []
    for key in ("promise", "birthday", "cognition", "observation", "emotion"):
        value = table.get(key)
        label = KIND_LABELS.get(key, key)
        parts.append(f"{label}{'=永不删' if value is None else f'={value:g}天'}")
    return (
        f"遗忘：R=e^(-t/S)；R≥{cfg['keep_above']:g} 留，"
        f"R≤{cfg['drop_below']:g} 删，中间待判；"
        f"每次召回稳定性×{cfg['per_recall_factor']:g}；"
        f"类型初值 {'、'.join(parts)}"
    )
