"""时间感知的记忆原子：按类型给 TTL、按类型选衰减曲线。

来源与改造说明（2026-11-07）
============================
抄自 `qq/astrbot/data/plugins/astrbot_plugin_livingmemory/core/models/memory_atom.py`
（作者 `lxfight`，该插件已于 2026-11-03 停用）。这是主人明确要求补回来的能力：
**网关自建的记忆层缺了原子化，而插件里本来就有。**

改造（不是原样搬）：
  * 去掉 `persona_id`（网关没有这个概念）
  * `AtomStatus` 精简到网关实际用到的三态（active / expired / forgotten）——
    插件里的 `dormant` / `superseded` 需要状态机推进，网关暂时没有那套驱动
  * 注释改成中文并说明**为什么**这么定（插件的注释只说了是什么）
  * 加 `from_dict` / `to_dict`，因为网关的 `documents.metadata` 是 JSON 列
  * **不引入任何依赖**（只用标准库），插件的 dataclass(slots=True) 需要 3.10+，
    网关要求一致，保留

为什么原子比整句好
------------------
原来的记忆一条就是一句原话，比如「主人：我住在重庆的一个小城镇里」。
这样有两个问题：
  1. **没法按信息类型分别设置遗忘节奏** —— 一句"今天加班"和一句"他生日是11月1号"
     同样重要度、同样衰减，显然不对。
  2. **一条里塞多个事实时无法部分遗忘** —— 「他住重庆、上大三、喜欢灰色」如果
     写成一整条，三个月后想只忘掉"上大三"做不到。

原子化后每一条是「一个类型 + 一句事实 + 一套自己的时间参数」。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AtomType(str, Enum):
    """记忆原子的类型。决定初始 TTL 和衰减曲线。"""

    EPISODIC = "episodic"        # 一次性事件：「今天加班到十点」
    FACTUAL = "factual"          # 长期事实：「他住在重庆」「他生日11月1号」
    RELATIONAL = "relational"    # 人际关系：「他有个室友叫老王」
    PREFERENCE = "preference"    # 喜好：「他喜欢灰色」
    PLANNED = "planned"          # 计划/承诺：「明天要交作业」
    UNKNOWN = "unknown"          # 认不出时兜底


class DecayType(str, Enum):
    """衰减曲线的形状。"""

    LINEAR = "linear"              # 线性掉到 0：适合"一直在但慢慢不重要"的
    EXPONENTIAL = "exponential"    # 指数衰减：适合大部分类型（人类遗忘的主曲线）
    STEP = "step"                  # 阶梯：过了 TTL 断崖 —— 适合计划类（过期就没用）


class AtomStatus(str, Enum):
    """原子的生命周期状态（2026-11-07：只保留网关真正会用的三态）。"""

    ACTIVE = "active"          # 还在用
    EXPIRED = "expired"        # 过了 TTL，但还没清理
    FORGOTTEN = "forgotten"    # 已从检索里摘掉，等物理删除


def compute_decay_score(
    decay_type: DecayType | str,
    ttl_days: float,
    days_since: float,
) -> float:
    """算这个原子现在的"新鲜度"（0~1），供检索排序时降权。

    Args:
        decay_type: 衰减曲线类型。
        ttl_days: 有效期（天）。
        days_since: 距上次被访问过了多少天。

    Returns:
        0~1 的乘数。1 = 完全新鲜，0 = 彻底忘了。

    Note:
        三条曲线各有各的道理，不是随便选的：
          * **EXPONENTIAL** 用半衰期形式（`2^(-t/(TTL/2))`），
            不是 `e^(-t/TTL)` —— 后者在 t=TTL 时还剩 37%，太慢。
            半衰期形式在 t=TTL 时正好剩 25%，更接近"到期就该淡了"。
          * **LINEAR** 是 `1 - t/TTL`，到 TTL 恰好归零。适合关系这类
            "慢慢变淡但不会突然消失"的。
          * **STEP** 是断崖：TTL 内是 1.0，过了就 0.05。计划类过期就是没用了，
            留着一点残余只为让它还能被搜到（比如"他上周说要交作业"）。
    """
    effective_ttl = max(1.0, ttl_days)
    days_since = max(0.0, days_since)
    decay_value = getattr(decay_type, "value", str(decay_type))

    if decay_value == DecayType.LINEAR.value:
        return max(0.0, 1.0 - days_since / effective_ttl)
    if decay_value == DecayType.STEP.value:
        return 1.0 if days_since <= effective_ttl else 0.05

    half_life = effective_ttl / 2.0
    return math.exp(-math.log(2) * days_since / max(0.5, half_life))


#: 各类型的初始 TTL（天）与衰减曲线。
#:
#: 这六个数字是整个原子设计里最值钱的部分 —— 它们回答的是
#: 「不同性质的信息，人的脑子分别留多久」：
#:
#:   * **EPISODIC 7 天**：一次性的琐事，一周后基本没意义
#:   * **PLANNED 2 天**：计划和承诺过期最快 —— 说"明天做"的人第二天不做就没价值了
#:   * **FACTUAL 180 天**：住哪、生日、什么专业，半年前提过也该记得
#:   * **RELATIONAL 90 天**：人际关系的保质期介于事实和情绪之间
#:   * **PREFERENCE 60 天**：喜好会变（换口味、换颜色），但变得不快
#:   * **UNKNOWN 30 天**：认不出来就按中等寿命，宁可留久一点
_ATOM_TTL_CONFIG: dict[AtomType, dict[str, Any]] = {
    AtomType.EPISODIC: {"base_ttl": 7, "decay_type": DecayType.EXPONENTIAL},
    AtomType.PLANNED: {"base_ttl": 2, "decay_type": DecayType.STEP},
    AtomType.FACTUAL: {"base_ttl": 180, "decay_type": DecayType.EXPONENTIAL},
    AtomType.RELATIONAL: {"base_ttl": 90, "decay_type": DecayType.LINEAR},
    AtomType.PREFERENCE: {"base_ttl": 60, "decay_type": DecayType.EXPONENTIAL},
    AtomType.UNKNOWN: {"base_ttl": 30, "decay_type": DecayType.EXPONENTIAL},
}


@dataclass(slots=True)
class MemoryAtom:
    """一条细粒度、带时间参数的记忆。

    Attributes:
        parent_memory_id: 所属 documents 记录的 doc_id（非整数时用 0）。
        atom_type: 类型，决定 TTL 与衰减。
        content: 事实正文（规范化后的一句话）。
        entities: 关联的实体/主题，供将来实体检索用。
        importance: 重要度 0~1，参与 TTL 计算。
        confidence: 分类置信度（规则命中越明确越高）。
        created_at: 创建时间戳。
        last_accessed_at: 上次被召回的时间 —— 衰减从**这里**算，不是从创建算。
            这正是「越用越熟」的实现基础。
        event_time: 这件事**发生**的时间（如果话里带了时间词）。
        ttl_days: 有效期（天）。
        expires_at: 过期时间戳 = created_at + ttl。
        status: 生命周期状态。
        reinforcement_count: 被强化过几次（重复提到同一件事会 +1）。
        decay_type: 衰减曲线。
        session_id: 归属会话（owner / qq_group:<名>），隔离用。
        metadata: 额外字段。
        atom_id: 入库后的内部 id。
    """

    parent_memory_id: int = 0
    atom_type: AtomType = AtomType.UNKNOWN
    content: str = ""
    entities: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.7

    # 时间字段
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    last_reinforced_at: float | None = None
    event_time: float | None = None
    ttl_days: float = 30.0
    expires_at: float = 0.0

    # 生命周期
    status: AtomStatus = AtomStatus.ACTIVE
    reinforcement_count: int = 0
    decay_type: DecayType = DecayType.EXPONENTIAL

    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    atom_id: int = 0

    def compute_temporal_score(self, reference_time: float | None = None) -> float:
        """算这条原子现在的时效权重（0~1）。

        Args:
            reference_time: 参考时间，缺省为现在。

        Returns:
            0~1 的衰减乘数。
        """
        if reference_time is None:
            reference_time = time.time()
        days_since = max(0.0, (reference_time - self.last_accessed_at) / 86400.0)
        return compute_decay_score(self.decay_type, self.ttl_days, days_since)

    def is_expired(self, reference_time: float | None = None) -> bool:
        """这条是否已过 TTL。"""
        if reference_time is None:
            reference_time = time.time()
        return reference_time >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict（存进 `documents.metadata`）。"""
        return {
            "atom_type": self.atom_type.value,
            "content": self.content,
            "entities": list(self.entities),
            "importance": self.importance,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
            "last_reinforced_at": self.last_reinforced_at,
            "event_time": self.event_time,
            "ttl_days": self.ttl_days,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "reinforcement_count": self.reinforcement_count,
            "decay_type": self.decay_type.value,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryAtom:
        """从 dict 还原。脏数据一律退到安全默认值，**不抛**。"""
        d = data or {}

        def _enum(enum_cls, key, default):
            try:
                return enum_cls(d.get(key) or default.value)
            except (ValueError, AttributeError):
                return default

        def _f(key, default):
            try:
                v = d.get(key)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        return cls(
            parent_memory_id=int(_f("parent_memory_id", 0)),
            atom_type=_enum(AtomType, "atom_type", AtomType.UNKNOWN),
            content=str(d.get("content") or ""),
            entities=list(d.get("entities") or []),
            importance=_f("importance", 0.5),
            confidence=_f("confidence", 0.7),
            created_at=_f("created_at", time.time()),
            last_accessed_at=_f("last_accessed_at", time.time()),
            last_reinforced_at=(_f("last_reinforced_at", 0) or None),
            event_time=(_f("event_time", 0) or None),
            ttl_days=_f("ttl_days", 30.0),
            expires_at=_f("expires_at", 0.0),
            status=_enum(AtomStatus, "status", AtomStatus.ACTIVE),
            reinforcement_count=int(_f("reinforcement_count", 0)),
            decay_type=_enum(DecayType, "decay_type", DecayType.EXPONENTIAL),
            session_id=(str(d["session_id"]) if d.get("session_id") else None),
            metadata=dict(d.get("metadata") or {}),
        )


def compute_ttl(
    atom_type: AtomType,
    importance: float = 0.5,
    reinforcement_count: int = 0,
    event_time: float | None = None,
) -> tuple[float, DecayType]:
    """算这条原子的 TTL 和衰减曲线。

    Args:
        atom_type: 类型。
        importance: 重要度 0~1。
        reinforcement_count: 已被强化几次。
        event_time: 事件发生时间（只有 PLANNED 用得到）。

    Returns:
        `(ttl_days, decay_type)`。

    Note:
        三个调整因子叠加：
          * **importance_factor**：0.5~1.5 倍。重要度越高留越久。
          * **reinforcement_factor**：1.0~1.5 倍。反复提到说明它重要
            —— 这就是「越用越熟」在原子层的实现。
          * **PLANNED 特殊处理**：如果事件还在未来（「下周三交作业」），
            TTL 从**事件时间**开始算，而不是从现在算。
            否则一个"下个月"的计划还没发生就过期了。
    """
    config = _ATOM_TTL_CONFIG.get(atom_type, _ATOM_TTL_CONFIG[AtomType.UNKNOWN])
    base_ttl = float(config["base_ttl"])
    decay_type = DecayType(config["decay_type"])

    if atom_type == AtomType.PLANNED and event_time is not None:
        days_until_event = max(0.0, (event_time - time.time()) / 86400.0)
        base_ttl = days_until_event + base_ttl

    importance_factor = 0.5 + max(0.0, min(1.0, importance))
    reinforcement_factor = 1.0 + min(0.5, reinforcement_count * 0.1)
    ttl = base_ttl * importance_factor * reinforcement_factor

    return max(1.0, ttl), decay_type


__all__ = [
    "MemoryAtom",
    "AtomType",
    "DecayType",
    "AtomStatus",
    "compute_decay_score",
    "compute_ttl",
    "_ATOM_TTL_CONFIG",
]
