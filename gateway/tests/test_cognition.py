"""``gateway/cognition.py`` 的单元测试：认知准则引擎。

规格来源：``plan/DMN审核.md`` 1.9（认知的录入准则）+ 能力 8·记忆库
（含「主人已定的三条」2026-11-06 09:48）。

全部是**纯函数测试**：不联网、不碰数据库、不 import system/gateway。
断言的是「启发式规则不把明显该判的东西判反」，不是 100% 语言学准确率。
"""
from __future__ import annotations

import json

import pytest

from cognition import (
    INFERENCE_WORDS,
    MAX_COGNITION_CHARS,
    SINGLE_EVENT_WORDS,
    TRANSIENT_WORDS,
    classify_text,
    clean_keywords,
    ingest_route,
    is_cognition,
    is_thought,
    judge,
)


# =========================================================================== #
# 1. 八条边界案例（DMN审核.md 1.9「边界案例对照表」逐条断言）
# =========================================================================== #

#: (句子, 是否认知, 期望 kind)
BOUNDARY_CASES = [
    # 1.9 对照表里 ✅ 的四条
    ("他生日是 11 月 1 日", True, "cognition"),
    ("他住在上海", True, "cognition"),
    ("他讨厌人群", True, "cognition"),
    ("他喜欢在深夜聊天", True, "cognition"),
    # 1.9 对照表里 ❌ 的四条
    ("他最近很忙", False, "event"),        # "最近"会过时，不符标准 ②
    ("他嘴硬时在试探", False, "noise"),     # 推断、不可证伪 → 想法
    ("他大概是把深夜当自己的时间", False, "noise"),  # "大概" = 推断
    ("他今天说了 39 条", False, "event"),   # 单次事件 → 流水账
]


@pytest.mark.parametrize("text,expected_is_cognition,expected_kind", BOUNDARY_CASES)
def test_boundary_cases_is_cognition(text, expected_is_cognition, expected_kind):
    """8 条边界案例逐条断言 ``is_cognition``。"""
    assert is_cognition(text) is expected_is_cognition, (
        f"{text!r} 的 is_cognition 应为 {expected_is_cognition}；judge={judge(text)}"
    )


@pytest.mark.parametrize("text,expected_is_cognition,expected_kind", BOUNDARY_CASES)
def test_boundary_cases_classify_text(text, expected_is_cognition, expected_kind):
    """8 条边界案例逐条断言 ``classify_text`` 的 kind。"""
    assert classify_text(text) == expected_kind, (
        f"{text!r} 的 kind 应为 {expected_kind}；judge={judge(text)}"
    )


@pytest.mark.parametrize("text,expected_is_cognition,expected_kind", BOUNDARY_CASES)
def test_boundary_cases_judge_shape(text, expected_is_cognition, expected_kind):
    """``judge`` 的返回形状 + reasons 是中文说明（方便审计）。"""
    verdict = judge(text)
    assert set(verdict) == {"text", "is_cognition", "kind", "reasons"}
    assert verdict["is_cognition"] is expected_is_cognition
    assert verdict["kind"] == expected_kind
    assert isinstance(verdict["reasons"], list) and verdict["reasons"]
    assert all(isinstance(r, str) and r.strip() for r in verdict["reasons"])
    # reasons 里必须提到它命中了哪条标准（①形态 / ②可验证性）
    joined = " ".join(verdict["reasons"])
    assert ("标准①" in joined) or ("标准②" in joined) or ("噪音" in joined)


def test_judge_reasons_explain_the_missed_standard():
    """未判成认知时，reasons 要说清是哪条标准没过（审计用）。"""
    assert any("标准②" in r for r in judge("他最近很忙")["reasons"])
    assert any("标准②" in r for r in judge("他今天说了 39 条")["reasons"])
    assert any("标准①" in r or "标准②" in r for r in judge("随便写点什么吧")["reasons"])

    # 判成认知时两条标准都要写明「✓」
    reasons = judge("他讨厌人群")["reasons"]
    assert any(r.startswith("✓") and "标准①" in r for r in reasons)
    assert any(r.startswith("✓") and "标准②" in r for r in reasons)


def test_numeric_fact_is_judged_as_cognition_not_new_kind():
    """主人 2026-11-06：生日/住址这类客观事实**仍归 cognition**，不另开 fact 类型。"""
    verdict = judge("他生日是 11 月 1 日")
    assert verdict["kind"] == "cognition"
    assert any("客观可数事实" in r for r in verdict["reasons"])


# =========================================================================== #
# 2. 推断词 / 单次事件词：各自至少 3 个例子
# =========================================================================== #

@pytest.mark.parametrize(
    "text",
    [
        "他大概是累了",          # 大概
        "他也许不喜欢这个",       # 也许
        "他可能只是客气",         # 可能
        "他似乎不想说话",         # 似乎
        "他好像是故意的",         # 好像
        "他其实很在意",           # 其实
        "他一定是想我了",         # 一定是
        "估计他又忘了",           # 估计
        "他应该是不好意思",       # 应该
        "他八成是不高兴",         # 八成
        "说不定他在等我",         # 说不定
        "感觉他挺孤单的",         # 感觉
    ],
)
def test_inference_words_block_cognition(text):
    """命中推断词 → 不是认知（标准②不可证伪 → 想法）。"""
    assert is_cognition(text) is False
    assert classify_text(text) == "noise"
    assert is_thought(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "他今天说了很久",         # 今天
        "他刚才发了一条长语音",    # 刚才
        "他昨天讲了个笑话",       # 昨天
        "他这次说得很认真",       # 这次
        "他说要带我去看海",       # 承诺/一次动作
        "他刚刚夸了我",           # 刚刚 + 夸了我
    ],
)
def test_single_event_words_block_cognition(text):
    """命中单次事件词 → 不是认知，归 event（流水账/events 表）。"""
    assert is_cognition(text) is False
    assert classify_text(text) == "event"


def test_word_lists_cover_the_required_words():
    """规格要求的推断词表必须覆盖到位（防止后续被人改窄）。"""
    required_inference = [
        "大概", "也许", "可能", "似乎", "好像", "其实",
        "一定是", "估计", "感觉", "应该", "八成", "说不定",
    ]
    missing = [w for w in required_inference if w not in INFERENCE_WORDS]
    assert not missing, f"推断词表缺少：{missing}"

    required_event = ["今天", "昨天", "刚才", "这次", "刚刚"]
    missing_event = [w for w in required_event if w not in SINGLE_EVENT_WORDS]
    assert not missing_event, f"单次事件词表缺少：{missing_event}"

    # 时效词单列（「最近很忙」要能被识别出来）
    assert "最近" in TRANSIENT_WORDS


# =========================================================================== #
# 3. classify_text / ingest_route：认知、事件、噪音三分 + 记忆入库类型
# =========================================================================== #

@pytest.mark.parametrize(
    "text,expected_kind",
    [
        ("他喜欢蓝色", "cognition"),
        ("他生日是 11 月 1 日", "cognition"),
        ("他养了一只猫", "cognition"),
        ("我不知道该怎么办", "cognition"),  # 主谓宾：「我 + 不知道」
        ("他说要带我去看海", "event"),      # 承诺/一次动作 → 事件
        ("他今天骂了我", "event"),
        ("他最近很忙", "event"),            # 时效状态 → 事件（有时效）
        ("哈哈哈", "noise"),
        ("嗯", "noise"),
        ("", "noise"),
        ("他嘴硬时在试探", "noise"),        # 想法 → 不落库
        ("随便", "noise"),
    ],
)
def test_classify_text_three_way(text, expected_kind):
    assert classify_text(text) == expected_kind


def test_ingest_route_gives_memory_type_for_both_cognition_and_event():
    """「认知和事实同级」：调用方不该自己判，统一走 ingest_route 拿入库类型。"""
    cog = ingest_route("他讨厌人群")
    assert cog["kind"] == "cognition"
    assert cog["memory_type"] == "cognition"
    assert cog["sink"] == "livingmemory.cognition"

    fact = ingest_route("他生日是 11 月 1 日")
    assert fact["memory_type"] == "cognition", "客观事实仍归认知，不另开 fact 类型"

    ev = ingest_route("他说要带我去看海")
    assert ev["kind"] == "event"
    assert ev["memory_type"] == "event"
    assert ev["sink"] == "events"

    noise = ingest_route("哈哈哈")
    assert noise["kind"] == "noise"
    assert noise["memory_type"] is None
    assert noise["sink"] is None


def test_ingest_route_never_raises_on_garbage():
    for bad in [None, "", 0, [], {}, "   ", object()]:
        route = ingest_route(bad)  # type: ignore[arg-type]
        assert route["kind"] in ("cognition", "event", "noise")
        assert route["memory_type"] in (None, "cognition", "event")


# =========================================================================== #
# 5. clean_keywords：停用词/单字过滤、兜底、去重保序、limit
# =========================================================================== #

def test_clean_keywords_filters_stopwords_and_single_chars():
    """他/她/的/了/是/在/我/你/不/有/就/也/都 这类单字虚词 + <2 字符必须被过滤。"""
    raw = ["他", "的", "了", "是", "在", "我", "你", "不", "有", "就", "也", "都",
           "a", "喜欢", "咖啡"]
    got = clean_keywords(raw, "他喜欢喝咖啡")
    for bad in ["他", "的", "了", "是", "在", "我", "你", "不", "有", "就", "也", "都", "a"]:
        assert bad not in got, f"停用词/单字 {bad!r} 不该留下：{got}"
    assert "喜欢" in got
    assert "咖啡" in got


def test_clean_keywords_filters_generic_words():
    """与 text 无关的超通用词要踹掉。"""
    got = clean_keywords(["情况", "问题", "感觉", "咖啡", "深夜"], "他喜欢在深夜喝咖啡")
    assert "情况" not in got
    assert "问题" not in got
    assert "感觉" not in got
    assert "咖啡" in got
    assert "深夜" in got


def test_clean_keywords_fallback_from_text_when_too_few():
    """清洗后不足 2 个 → 从 text 里按长度取实词兜底。"""
    got = clean_keywords(["他", "的"], "他喜欢在深夜喝咖啡")
    assert len(got) >= 2, f"兜底后应至少 2 个关键词：{got}"
    # 兜底词必须真的来自 text
    assert all(word in "他喜欢在深夜喝咖啡" or len(word) >= 2 for word in got)
    assert any(word in "他喜欢在深夜喝咖啡" for word in got)

    assert clean_keywords([], "他讨厌人群")  # 空输入也能从 text 兜底


def test_clean_keywords_dedupes_and_keeps_order():
    got = clean_keywords(["咖啡", "深夜", "咖啡", "奶茶", "深夜"], "他喜欢在深夜喝咖啡")
    assert got == ["咖啡", "深夜", "奶茶"], got


def test_clean_keywords_limit_is_respected():
    raw = ["咖啡", "深夜", "奶茶", "加班", "键盘", "窗台", "雨声", "耳机"]
    assert len(clean_keywords(raw, limit=3)) <= 3
    assert len(clean_keywords(raw, limit=6)) <= 6
    assert clean_keywords(raw, limit=2) == ["咖啡", "深夜"]
    assert clean_keywords(raw, limit=0) == []
    # 默认上限 6
    assert len(clean_keywords(raw, text="")) <= 6


def test_clean_keywords_accepts_str_and_garbage():
    """输入可以是逗号分隔字符串；坏数据不抛异常。"""
    assert clean_keywords("咖啡, 深夜、奶茶", "") == ["咖啡", "深夜", "奶茶"]
    for bad in [None, "", 42, {}, [], [None], {"keywords": ["咖啡", "深夜"]}]:
        got = clean_keywords(bad, "他喜欢在深夜喝咖啡")
        assert isinstance(got, list)


# =========================================================================== #

# --------------------------------------------------------------------------- #
# 已删除：第 4 / 6 / 7 节（2026-11-07）
# --------------------------------------------------------------------------- #
# 那三节共约 270 行，测的是 parse_cognition_actions / merge_prompt_block /
# ction_to_effect —— 全仓**从未接过线**的死代码，已在同日从 cognition.py 删除。
#
# 它们测的东西现在由 memory_bridge.py 的合并方案承担，
# 对应用例见 	ests/test_memory_merge.py（10 条）。
# 原文件备份：	est_cognition.py.bak-before-drop-deadcode-20261107。
