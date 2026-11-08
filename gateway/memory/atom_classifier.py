"""把一句话分类成带类型的记忆原子 —— 纯规则，不调模型、不花钱。

来源与改造说明（2026-11-07）
============================
抄自 `qq/astrbot/data/plugins/astrbot_plugin_livingmemory/core/processors/atom_classifier.py`
（作者 `lxfight`，插件已于 2026-11-03 停用）。主人明确要求把原子化搬回网关。

**最大优点：纯正则，零模型调用，零成本。** 这点很关键 ——
如果每次抽事实都要多调一次模型来"分类"，那原子化的成本就不可接受了。

改造：
  * 原样保留五组正则（它们是这套分类器的全部分值所在）
  * 中文化注释 + 补"为什么这么排优先级"
  * `_parse_event_time` 增加对 `\\d{4}年\\d{1,2}月\\d{1,2}日` 的处理
  * **`_STATIVE_PATTERNS` 补了状态动词**（住/在/读/学/来自…）——
    原版只有系动词，导致「他住在重庆」这种最典型的长期事实被判成 UNKNOWN
  * 对外只暴露 `classify_atoms()` / `classify_one()`，内部实现不导出

优先级为什么是这个顺序
----------------------
`_classify_single` 的判断顺序**不能随便调**，每条都在争夺同一种句式：

    1. PLANNED   —— 时间词 + 动作词。两个条件同时满足才算，最严格。
    2. PREFERENCE —— 喜欢/讨厌类，特征词最独特，几乎不会误判。
    3. RELATIONAL —— 关系词（朋友/室友/同事）。
    4. FACTUAL   —— 系动词（是/有/位于）。注意它排在 PREFERENCE 后面：
                    「他喜欢蓝色」也含"是"字句式的可能，但喜欢优先判定更准。
    5. EPISODIC  —— 有动作没时间。
    6. UNKNOWN   —— 兜底。

把 FACTUAL 放到 PREFERENCE 前面会导致「他喜欢蓝色」被判成 factual，
而它给 180 天 TTL（事实），比 preference 的 60 天留得久 —— 喜好变了就错了。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from memory_atom import AtomType, DecayType, MemoryAtom, compute_ttl

# ---------------------------------------------------------------- 词表

#: 时间指示词。命中就说明这句话锚定在某个时间点上。
_TIME_INDICATORS = re.compile(
    r"明天|后天|大后天|昨天|前天|今天|"
    r"(?:上周|本周|下下周|下周)?周[一二三四五六日天]|"
    r"上周|本周|下下周|下周|"
    r"下个?月|上个?月|明年|后年|去年|前年|"
    r"\d{1,2}月\d{1,2}[日号]|\d{4}年\d{1,2}月|"
    r"上午|下午|晚上|凌晨|早上|中午|傍晚|"
    r"\d{1,2}[点时：:]\d{1,2}"
)

#: 动作动词。和 _TIME_INDICATORS 一起命中才算 PLANNED。
_ACTION_VERBS = re.compile(
    r"开会|讨论|参加|组织|安排|举办|进行|执行|完成|提交|发送|发布|"
    r"去|来|到|做|要|准备|计划|打算"
)

#: 状态/系动词。「是什么 / 在哪 / 干什么职业」类句子 → 长期事实。
#:
#: ⚠ 2026-11-07 补：「住 / 在 / 读 / 学 / 老家 / 来自 / 工作」这些**状态动词**
#: 原版词表里没有，导致「他住在重庆」「他在读大三」这种最典型的长期事实
#: 被判成 UNKNOWN（30 天）。实测：抄过来跑第一条就中招。
#: 这类动词的共同点是**描述的是一种持续状态，不是一次性动作**，
#: 所以该和"是/有"同归 FACTUAL（180 天）。
_STATIVE_PATTERNS = re.compile(
    r"是|有|属于|等于|代表|意味|包含|包括|位于|"
    r"住在|家住|来自|老家|"
    r"在读|在念|在上|在干|在工作|在玩|"
    r"就读|毕业|学的是|专业是|"
    r"做的是|干的?是"
)

#: 关系词。人际关系的保质期和事实不同。
_RELATION_PATTERNS = re.compile(
    r"同事|朋友|同学|家人|亲戚|队友|搭档|伙伴|老板|上司|下属|"
    r"合作|合伙|夫妻|情侣|邻居|室友|老乡"
)

#: 喜好词。特征最独特，判得最准。
_PREFERENCE_PATTERNS = re.compile(
    r"喜欢|讨厌|爱|不爱|偏好|最爱|不喜欢|热衷于|沉迷|"
    r"爱吃|爱喝|喜欢喝|喜欢去|讨厌吃|讨厌去"
)

#: 人物指代。目前只用于将来做实体链接，分类本身还没用到。
_PERSON_PATTERNS = re.compile(
    r"(?:我|[你您]|[他她它]们?|[A-Z]\w*|[张李王刘陈杨赵黄周吴徐孙胡朱高何]"
    r"[a-zA-Z一-鿿]{1,3})"
)

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


# ---------------------------------------------------------------- 时间解析

def _parse_weekday_time(text: str, now: float) -> float | None:
    """解析「上周三」「下周一」这类星期表达成具体时间戳。

    Args:
        text: 待解析文本。
        now: 当前时间戳（测试可注入，保证可复现）。

    Returns:
        时间戳；没命中返回 None。

    Note:
        没有前缀的「周三」按**未来最近的那个周三**算（`% 7`），
        不是本週那个 —— 因为人说「周三见」通常指还没到的周三。
    """
    match = re.search(r"(上周|本周|下下周|下周)?周([一二三四五六日天])", text)
    if not match:
        return None

    prefix = match.group(1) or ""
    target_weekday = _WEEKDAY_INDEX[match.group(2)]
    now_dt = datetime.fromtimestamp(now)

    if prefix == "上周":
        days_delta = target_weekday - now_dt.weekday() - 7
    elif prefix == "本周":
        days_delta = target_weekday - now_dt.weekday()
    elif prefix == "下周":
        days_delta = target_weekday - now_dt.weekday() + 7
    elif prefix == "下下周":
        days_delta = target_weekday - now_dt.weekday() + 14
    else:
        days_delta = (target_weekday - now_dt.weekday()) % 7

    return (now_dt + timedelta(days=days_delta)).timestamp()


def _parse_event_time(text: str, now: float | None = None) -> float | None:
    """从中文时间表达里尽力解析出一个绝对时间戳。

    Args:
        text: 待解析文本。
        now: 参考时间，缺省为现在。

    Returns:
        时间戳；解析不出返回 None。

    Note:
        这是**尽力而为**，不追求精确。解析失败不影响原子本身 ——
        `event_time=None` 只是让 PLANNED 类退化成"从现在开始算 TTL"。
    """
    if now is None:
        now = time.time()
    day_sec = 86400.0

    mapping: dict[str, float] = {
        "前天": -2 * day_sec,
        "昨天": -1 * day_sec,
        "今天": 0,
        "明天": 1 * day_sec,
        "后天": 2 * day_sec,
        "大后天": 3 * day_sec,
    }
    for word, offset in mapping.items():
        if word in text:
            return now + offset

    weekday_time = _parse_weekday_time(text, now)
    if weekday_time is not None:
        return weekday_time

    week_mapping: dict[str, float] = {
        "上周": -7 * day_sec,
        "本周": 0,
        "下下周": 14 * day_sec,
        "下周": 7 * day_sec,
    }
    for word, offset in week_mapping.items():
        if word in text:
            return now + offset

    # 「5月30日」这种。今年已过就顺延到明年 —— 人说生日通常指下一个。
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        now_dt = datetime.fromtimestamp(now)
        try:
            target = now_dt.replace(month=month, day=day, hour=0, minute=0,
                                    second=0, microsecond=0)
        except ValueError:
            return None      # 2月30日之类的不存在日期
        if target < now_dt:
            target = target.replace(year=now_dt.year + 1)
        return target.timestamp()

    return None


# ---------------------------------------------------------------- 分类

def _classify_single(text: str) -> tuple[AtomType, float, float | None]:
    """判一条事实的类型，返回 `(类型, 置信度, 事件时间)`。

    Args:
        text: 一条事实（已规范化）。

    Returns:
        `(AtomType, confidence 0~1, event_time 或 None)`。

    Note:
        判断顺序**不能调**，理由见模块 docstring。
    """
    has_time = bool(_TIME_INDICATORS.search(text))
    has_action = bool(_ACTION_VERBS.search(text))
    has_stative = bool(_STATIVE_PATTERNS.search(text))
    has_relation = bool(_RELATION_PATTERNS.search(text))
    has_preference = bool(_PREFERENCE_PATTERNS.search(text))

    event_time = _parse_event_time(text) if has_time else None

    if has_time and has_action:
        return AtomType.PLANNED, 0.85, event_time
    if has_preference:
        return AtomType.PREFERENCE, 0.82, None
    if has_relation:
        return AtomType.RELATIONAL, 0.80, None
    if has_stative:
        return AtomType.FACTUAL, 0.78, None
    if has_action:
        return AtomType.EPISODIC, 0.75, None
    return AtomType.UNKNOWN, 0.60, None


def classify_one(
    fact: str,
    *,
    importance: float = 0.5,
    session_id: str | None = None,
    entities: list[str] | None = None,
) -> MemoryAtom | None:
    """把**一条**事实转成原子。空串返回 None。

    Args:
        fact: 事实正文。
        importance: 重要度 0~1。
        session_id: 归属会话。
        entities: 关联实体。

    Returns:
        `MemoryAtom`；输入为空时 None。
    """
    fact = str(fact or "").strip()
    if not fact:
        return None
    atom_type, confidence, event_time = _classify_single(fact)
    ttl, decay = compute_ttl(atom_type, importance, 0, event_time)
    now = time.time()
    return MemoryAtom(
        atom_type=atom_type,
        content=fact,
        entities=list(entities or []),
        importance=float(importance),
        confidence=confidence,
        event_time=event_time,
        created_at=now,
        last_accessed_at=now,
        ttl_days=ttl,
        decay_type=decay,
        expires_at=now + ttl * 86400.0,
        session_id=session_id,
    )


def classify_atoms(
    key_facts: list[str],
    *,
    importance: float = 0.5,
    session_id: str | None = None,
    topics: list[str] | None = None,
    participants: list[str] | None = None,
) -> list[MemoryAtom]:
    """把一批事实字符串转成原子列表（**一条一个原子**）。

    这就是"原子化"本身：上游给的是一段段文本，这里拆成一条条独立的记忆单元。

    Args:
        key_facts: 事实字符串列表。**每个元素应该已经是一条事实**
            （调用方负责先把长句拆开 —— 见 `memory_bridge` 里的用法）。
        importance: 重要度 0~1，会传给每条原子。
        session_id: 归属会话。
        topics: 主题标签，作为实体。
        participants: 参与者，作为实体。

    Returns:
        `MemoryAtom` 列表（跳过空串）。顺序与入参一致。
    """
    entities: list[str] = []
    if topics:
        entities.extend(topics)
    if participants:
        entities.extend(participants)

    out: list[MemoryAtom] = []
    for fact in key_facts or []:
        atom = classify_one(
            fact, importance=importance, session_id=session_id, entities=entities
        )
        if atom is not None:
            out.append(atom)
    return out


__all__ = [
    "classify_atoms",
    "classify_one",
    "AtomType",
    "DecayType",
    "MemoryAtom",
]
