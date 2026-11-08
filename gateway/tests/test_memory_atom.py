# -*- coding: utf-8 -*-
"""记忆原子化（抄自 livingmemory 插件，2026-11-07）。

主人要的是：不同性质的信息该有**不同的遗忘节奏**。
「他住在重庆」（长期事实）和「明天要交作业」（明天的计划）放同一个 TTL
上显然不对 —— 后者过两天就该没用了，前者半年后还该记得。

这套实现的原版在 `qq/astrbot/data/plugins/astrbot_plugin_livingmemory`，
该插件 2026-11-03 停用后原子化能力一直在网关层缺着。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = GATEWAY_DIR / "memory"
for _p in (str(GATEWAY_DIR), str(MEMORY_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atom_classifier as AC  # noqa: E402
import memory_atom as MA  # noqa: E402
from memory_atom import AtomType, DecayType  # noqa: E402


# ---------------------------------------------------------------- 分类

@pytest.mark.parametrize("text,want", [
    ("主人住在重庆的一个小城镇", AtomType.FACTUAL),
    ("他老家在四川", AtomType.FACTUAL),
    ("他来自东北", AtomType.FACTUAL),
    ("主人是大三学生", AtomType.FACTUAL),
    ("他生日是11月1号", AtomType.FACTUAL),
    ("主人喜欢灰色", AtomType.PREFERENCE),
    ("他讨厌人群和应酬", AtomType.PREFERENCE),
    ("他有个室友叫老王", AtomType.RELATIONAL),
    ("明天要交作业", AtomType.PLANNED),
    ("今天加班到十点", AtomType.PLANNED),
])
def test_分类正确(text, want):
    atom = AC.classify_one(text)
    assert atom is not None
    assert atom.atom_type == want, f"{text} 判成了 {atom.atom_type}"


def test_状态动词归到factual():
    """原版词表只有系动词（是/有/位于），缺「住/在/读/来自」这类状态动词。

    实测它把「主人住在重庆」判成 UNKNOWN（30 天）—— 这是抄过来第一天就中的坑。
    """
    for text in ("主人住在重庆", "他在读大三", "他老家在四川", "他来自东北"):
        atom = AC.classify_one(text)
        assert atom.atom_type == AtomType.FACTUAL, f"{text} -> {atom.atom_type}"


def test_分类优先级_喜欢优先于系动词():
    """「他喜欢蓝色」含"是"字句式的可能是误判，喜欢该优先。

    如果 FACTUAL 排在 PREFERENCE 前面，它会拿到 180 天 TTL 而不是 60 天。
    """
    atom = AC.classify_one("他喜欢蓝色")
    assert atom.atom_type == AtomType.PREFERENCE


def test_空串返回None():
    assert AC.classify_one("") is None
    assert AC.classify_one("   ") is None


def test_批量分类_一条一个原子():
    atoms = AC.classify_atoms(["他住在重庆", "他喜欢灰色", "他是大三学生"])
    assert len(atoms) == 3
    assert [a.content for a in atoms] == ["他住在重庆", "他喜欢灰色", "他是大三学生"]


# ---------------------------------------------------------------- TTL 与衰减

def test_不同类型拿到不同TTL():
    """核心：这是原子化存在的理由。"""
    factual = AC.classify_one("他住在重庆")
    planned = AC.classify_one("明天要交作业")
    assert factual.ttl_days > planned.ttl_days * 10, (
        f"长期事实({factual.ttl_days:.0f}d) 应该远长于计划({planned.ttl_days:.0f}d)")


def test_不同类型拿到不同衰减曲线():
    planned = AC.classify_one("明天要交作业")
    relational = AC.classify_one("他有个室友叫老王")
    assert planned.decay_type == DecayType.STEP
    assert relational.decay_type == DecayType.LINEAR


def test_重要度拉长TTL():
    low = MA.compute_ttl(AtomType.FACTUAL, importance=0.1)[0]
    high = MA.compute_ttl(AtomType.FACTUAL, importance=0.9)[0]
    assert high > low


def test_强化次数拉长TTL():
    """「越用越熟」在原子层的实现。"""
    base = MA.compute_ttl(AtomType.FACTUAL, importance=0.5, reinforcement_count=0)[0]
    boosted = MA.compute_ttl(AtomType.FACTUAL, importance=0.5, reinforcement_count=3)[0]
    assert boosted > base


def test_衰减三条曲线行为正确():
    # 线性：到 TTL 恰好归零
    assert MA.compute_decay_score(DecayType.LINEAR, 10, 10) == pytest.approx(0.0, abs=0.01)
    # 阶梯：TTL 内是 1，过了断崖
    assert MA.compute_decay_score(DecayType.STEP, 10, 9) == 1.0
    assert MA.compute_decay_score(DecayType.STEP, 10, 11) == 0.05
    # 指数：单调递减
    a = MA.compute_decay_score(DecayType.EXPONENTIAL, 30, 0)
    b = MA.compute_decay_score(DecayType.EXPONENTIAL, 30, 30)
    assert a > b


def test_指数的半衰期形式():
    """用 2^(-t/(TTL/2)) 而不是 e^(-t/TTL)。

    t=TTL 时应剩约 25%，不是 37% —— 后者会让"到期"看起来还很新。
    """
    v = MA.compute_decay_score(DecayType.EXPONENTIAL, 30, 30)
    assert 0.2 < v < 0.3, f"t=TTL 时应剩约 25%，实际 {v:.3f}"


# ---------------------------------------------------------------- 序列化

def test_原子能转成dict再还原():
    atom = AC.classify_one("他住在重庆", session_id="owner")
    d = atom.to_dict()
    back = MA.MemoryAtom.from_dict(d)
    assert back.atom_type == atom.atom_type
    assert back.content == atom.content
    assert back.ttl_days == atom.ttl_days
    assert back.session_id == "owner"


def test_还原脏数据不抛():
    """metadata 是 JSON 列，随时可能被写脏。宁可退默认值，不可炸。"""
    atom = MA.MemoryAtom.from_dict({"atom_type": "不存在的类型", "ttl_days": "abc"})
    assert atom.atom_type == AtomType.UNKNOWN
    assert atom.ttl_days == 30.0


def test_过期判定():
    import time

    atom = AC.classify_one("明天要交作业")
    assert not atom.is_expired()
    atom.expires_at = time.time() - 1
    assert atom.is_expired()
