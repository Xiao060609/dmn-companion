"""`gateway/forgetting.py` 的单测（遗忘算法，能力 2 / 板块三 3.7）。

这些用例钉住的是**判断**，不是实现细节：
1. 指数衰减 `R=e^(-t/S)` 的数值（t=S → 0.368，t=3S → 0.0498）；
2. 「照抄 FSRS 会坏掉」——同样 S=30 天，FSRS 要 34.7 年才掉到 0.1，
   我们要 69 天。这条如果不钉住，将来"优化"回幂函数没人拦；
3. 「越用越熟」的算式（7 天 → 53.2 天 → 403.7 天）；
4. 三档判定的**取等归属**（R=0.5 算留、R=0.05 算删）；
5. 永不删类型：R 再低也不 drop，只"拉高稳定性"；
6. 给模型的问法里**没有时间词**（模型没有真实时间体验，3.7 末节）；
7. 模型回复解析**只认候选里的 id**（防幻觉）；
8. 参数缺省/覆盖/非法回退；
9. 所有函数拿到坏数据都不抛异常。

纯函数、无网络、不写 `data/`：本文件不导入 `gateway` / `system`，
只导入被测模块本身（`conftest.py` 仍会做数据目录隔离，属于兜底）。
"""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import forgetting  # noqa: E402

DAY = 86400.0
#: 固定"现在"，让所有按时间推导的断言可复现。
NOW = 1_800_000_000.0


def _ts(days_ago: float) -> float:
    return NOW - days_ago * DAY


def _doc(doc_id: str, text: str = "他今天说很累", **meta) -> dict:
    return {"doc_id": doc_id, "text": text, "metadata": meta}


# --------------------------------------------------------------------------- #
# 1. 指数衰减 R = e^(-t/S)
# --------------------------------------------------------------------------- #
def test_t等于S时R约等于0_368():
    """t=S → R≈0.368。这是指数衰减的形状，也是"稳定性"在这里的定义。"""
    assert forgetting.retrievability(30.0, 30.0) == pytest.approx(0.367879, abs=1e-6)
    assert forgetting.retrievability(1.0, 1.0) == pytest.approx(math.exp(-1.0))


def test_t等于3S时R约等于0_0498():
    """t=3S → R≈0.0498，正好掉在 drop 门槛（0.05）下面。"""
    assert forgetting.retrievability(30.0, 90.0) == pytest.approx(0.049787, abs=1e-6)


def test_刚写入时R为1():
    """t=0（刚写入/刚被召回）→ R=1.0，一条都不会删。"""
    assert forgetting.retrievability(30.0, 0.0) == 1.0
    assert forgetting.retrievability(7.0, 0.0) == 1.0


def test_稳定性非法时R兜底为0而不是抛异常():
    """S<=0 / NaN → 0.0（给不出可信稳定性＝已经记不住），但**不抛**。"""
    assert forgetting.retrievability(0.0, 5.0) == 0.0
    assert forgetting.retrievability(-7.0, 5.0) == 0.0
    assert forgetting.retrievability(float("nan"), 5.0) == 0.0
    # None = 永不删类型，不衰减
    assert forgetting.retrievability(None, 99999.0) == 1.0


def test_时间非法或为负时按刚发生算():
    """t<0（时间戳在未来）或 t 非法 → 1.0：宁可留，不可误删。"""
    assert forgetting.retrievability(30.0, -5.0) == 1.0
    assert forgetting.retrievability(30.0, "昨天") == 1.0
    assert forgetting.retrievability(30.0, None) == 1.0


def test_结果恒在0到1之间():
    for s, t in ((30.0, 1e6), (0.001, 1e6), (1e9, 1.0), (30.0, 1e-9)):
        r = forgetting.retrievability(s, t)
        assert 0.0 <= r <= 1.0


# --------------------------------------------------------------------------- #
# 2. 「照抄 FSRS 会坏掉」——这条判断必须被测试固定住
# --------------------------------------------------------------------------- #
def test_照抄FSRS会几十年不淡_指数衰减180天就该删():
    """S=30 天、t=180 天：我们是 0.0025（早就该删），FSRS 是 ~0.64（还"记得"）。

    FSRS：R = (1 + F·t/S)^C，F=19/81、C=-0.5。它的目标是"让人记住"，
    所以衰减极慢；我们要"让没用的自己消失"。**公式能借，函数形式不能照抄。**
    """
    r_ours = forgetting.retrievability(30.0, 180.0)
    assert r_ours == pytest.approx(0.00247875, abs=1e-7)
    assert r_ours < 0.05
    assert forgetting.classify(30.0, 180.0) == "drop"

    # 同一个 t/S 下 FSRS 的参考值（算出来给"方向相反"留证据）
    t_over_s = 180.0 / 30.0
    r_fsrs = (1.0 + (19.0 / 81.0) * t_over_s) ** -0.5
    assert r_fsrs == pytest.approx(0.6445, abs=1e-3)
    assert r_fsrs > r_ours * 100  # 差两个数量级


def test_指数衰减掉到0_1只要69天_不是34_7年():
    """S=30 天时 R 掉到 0.1 需要 `S·ln(10)`≈69 天（≈0.19 年），不是 34.7 年。"""
    s = 30.0
    t_at_r01 = -s * math.log(0.1)
    assert t_at_r01 == pytest.approx(69.077, abs=1e-3)
    assert forgetting.retrievability(s, t_at_r01) == pytest.approx(0.1, abs=1e-9)
    assert t_at_r01 / 365.0 < 0.2  # 半年内淡掉，符合直觉
    # 反证：真到"几十年不淡"就是照抄 FSRS 了
    assert 34.7 * 365.0 > 10000.0


# --------------------------------------------------------------------------- #
# 3. 越用越熟：S_eff = S · 1.5^use_count
# --------------------------------------------------------------------------- #
def test_召回5次稳定性从7天涨到53_2天():
    """7 × 1.5**5 = 53.15625 天（3.7 的算式）。"""
    effective = forgetting.decayed_stability(7.0, 5)
    assert effective == pytest.approx(7.0 * 1.5 ** 5)
    assert effective == pytest.approx(53.15625, abs=1e-6)
    assert effective == pytest.approx(53.2, abs=0.05)


def test_再召回5次稳定性到404天():
    """7 × 1.5**10 = 403.655 天（3.7 写的"→ 400 天"）。"""
    effective = forgetting.decayed_stability(7.0, 10)
    assert effective == pytest.approx(7.0 * 1.5 ** 10)
    assert effective == pytest.approx(403.6553, abs=1e-3)
    assert effective == pytest.approx(404.0, abs=0.5)


def test_召回次数为0时稳定性不变():
    assert forgetting.decayed_stability(30.0, 0) == 30.0


def test_越用越熟能把一条记忆从该删救回该留():
    """同一份年龄 100 天、初值 30 天：没被召回 R≈0.036（删），召回 5 次后 R≈0.645（留）。"""
    age = 100.0
    cold = forgetting.decayed_stability(30.0, 0)
    warm = forgetting.decayed_stability(30.0, 5)
    assert cold == pytest.approx(30.0)
    assert warm == pytest.approx(227.8125, abs=1e-6)  # 30 × 1.5^5
    assert forgetting.retrievability(cold, age) == pytest.approx(0.035674, abs=1e-5)
    assert forgetting.retrievability(warm, age) == pytest.approx(0.644708, abs=1e-6)
    assert forgetting.classify(cold, age) == "drop"
    assert forgetting.classify(warm, age) == "keep"


def test_召回时写盘助手把use_count和last_used一起更新():
    meta = {"kind": "emotion"}
    out = forgetting.apply_recall(meta, now=NOW)
    assert out["use_count"] == 1
    assert out["last_used"] == NOW
    assert out["stability_days"] == pytest.approx(7.0 * 1.5)
    # 不原地改
    assert meta == {"kind": "emotion"}
    # 连续召回继续累加
    again = forgetting.apply_recall(out, now=NOW + DAY)
    assert again["use_count"] == 2
    assert again["stability_days"] == pytest.approx(7.0 * 1.5 ** 2)


def test_召回时永不删类型仍然是永不删():
    out = forgetting.apply_recall({"kind": "promise"}, now=NOW)
    assert out["stability_days"] is None
    assert out["use_count"] == 1


def test_per_recall_factor可调():
    assert forgetting.decayed_stability(7.0, 2, per_recall_factor=2.0) == pytest.approx(28.0)
    # 非法 factor（<1）不罚分，按 1.0 处理
    assert forgetting.decayed_stability(7.0, 2, per_recall_factor=0.5) == pytest.approx(7.0)


# --------------------------------------------------------------------------- #
# 4. 三档判定的边界（取等归属必须明确）
# --------------------------------------------------------------------------- #
def test_R恰好等于留门槛时算留():
    """`R >= keep_above` → keep。取等归"留"，偏一点到下面才进 review。"""
    s, age = 30.0, 12.0
    r = forgetting.retrievability(s, age)
    assert forgetting.classify(s, age, keep_above=r, drop_below=0.01) == "keep"


def test_R略低于留门槛时进待判():
    """R=0.4966（S=1, t=0.70）→ 中间区 → review。"""
    r = forgetting.retrievability(1.0, 0.70)
    assert r < 0.5
    assert forgetting.classify(1.0, 0.70) == "review"


def test_R恰好等于删门槛时算删():
    """`R <= drop_below` → drop（永不删类型除外）。取等归"删"——t=3S 正是这个点。"""
    s, age = 7.0, 21.0
    r = forgetting.retrievability(s, age)
    assert forgetting.classify(s, age, drop_below=r, keep_above=0.9) == "drop"


def test_t等于3S时按默认门槛正好判删():
    assert forgetting.retrievability(7.0, 21.0) == pytest.approx(0.049787, abs=1e-6)
    assert forgetting.classify(7.0, 21.0) == "drop"
    # 差一点点还在中间区
    assert forgetting.classify(7.0, 20.0) == "review"


def test_中间区两端都不算待判():
    # R≈0.06 → review；R≈0.04 → drop
    age_review = -math.log(0.06)
    age_drop = -math.log(0.04)
    assert forgetting.classify(1.0, age_review) == "review"
    assert forgetting.classify(1.0, age_drop) == "drop"


def test_永不删类型在任何R下都不判删():
    assert forgetting.classify(1.0, 1000.0, keep_forever=True) == "keep"
    assert forgetting.classify(0.001, 1e6, keep_forever=True) == "keep"


def test_门槛参数非法时回退默认而不是跑飞():
    # 两个门槛颠倒 → 回退成 0.5 / 0.05
    assert forgetting.classify(1.0, 0.70, keep_above=0.1, drop_below=0.9) == "review"
    assert forgetting.classify(1.0, 3.0, keep_above=7, drop_below=-3) == "drop"


# --------------------------------------------------------------------------- #
# 5. plan_prune：每日扫描（永不删 / 删 / 待判）
# --------------------------------------------------------------------------- #
def test_plan_prune三档分流():
    docs = [
        _doc("fresh", kind="observation", create_time=_ts(2)),     # R≈0.935 留
        _doc("stale", kind="observation", create_time=_ts(200)),   # R≈0.0013 删
        _doc("middle", kind="observation", create_time=_ts(40)),   # R≈0.264 待判
    ]
    plan = forgetting.plan_prune(docs, now=NOW)
    assert plan["keep"] == ["fresh"]
    assert plan["drop"] == ["stale"]
    assert plan["review"] == ["middle"]
    assert plan["stats"]["scanned"] == 3
    assert plan["stats"]["keep"] == 1
    assert plan["stats"]["drop"] == 1
    assert plan["stats"]["review"] == 1


def test_plan_prune_永不删类型R很低也不drop而是拉高稳定性():
    """3.7：R<0.05 时"是永不删类型 → stability 拉高，留"。"""
    docs = [_doc("vow", kind="promise", stability_days=7.0, create_time=_ts(365 * 3))]
    plan = forgetting.plan_prune(docs, now=NOW)
    assert plan["drop"] == []
    assert plan["keep"] == ["vow"]
    row = plan["rows"][0]
    assert row["keep_forever"] is True
    assert row["retrievability"] < 0.05
    assert row["boost_to_stability_days"] is not None
    assert row["boost_to_stability_days"] > 0
    # stats 里能一眼看出这是"拉高稳定性"保下来的
    assert plan["stats"]["boosted"] == 1
    assert plan["stats"]["never_forget"] == 1


def test_plan_prune_承诺生日默认永不删():
    docs = [
        _doc("p1", kind="promise", create_time=_ts(2000)),
        _doc("b1", kind="生日", create_time=_ts(2000)),
    ]
    plan = forgetting.plan_prune(docs, now=NOW)
    assert plan["drop"] == []
    assert sorted(plan["keep"]) == ["b1", "p1"]
    assert plan["stats"]["never_forget"] == 2


def test_plan_prune_永不删类型根本不衰减():
    """S=None 的永不删条目 R 恒为 1.0：**不能**把它折成一个有限的大 Stability 去算。

    回归钉住（曾经写错过）：如果拿 `NEVER_FORGET_STABILITY_DAYS`(≈36500 天)
    去套 e^(-t/S)，一条 30 年前的承诺会算出 R≈0.74——看起来像"快忘了"，
    和 3.7「永不删」的意图正好相反。这里断言：不衰减、不需要拉高稳定性。
    """
    docs = [_doc("ancient", kind="promise", create_time=_ts(365 * 30))]
    row = forgetting.plan_prune(docs, now=NOW)["rows"][0]
    assert row["stability_days"] is None
    assert row["effective_stability_days"] is None
    assert row["retrievability"] == 1.0
    assert row["boost_to_stability_days"] is None
    assert "R=1.0000" in row["reason"]


def test_plan_prune_高重要度也算永不删():
    """importance ≥ 0.85 与"永不删类型"同等保护（3.7 的补充口径）。"""
    docs = [
        _doc("hi", kind="observation", importance=0.9, create_time=_ts(500)),
        _doc("lo", kind="observation", importance=0.6, create_time=_ts(500)),
    ]
    plan = forgetting.plan_prune(docs, now=NOW)
    assert "lo" in plan["drop"]
    assert "hi" not in plan["drop"]


def test_plan_prune_缺时间戳不删():
    """fail-safe：拿不到时间就不删（和 MemoryStore.prune 的老规矩一致）。"""
    docs = [_doc("no_time", kind="emotion"), _doc("bad_time", kind="emotion", create_time="不知道")]
    plan = forgetting.plan_prune(docs, now=NOW)
    assert plan["drop"] == []
    assert sorted(plan["keep"]) == ["bad_time", "no_time"]
    assert plan["stats"]["no_timestamp"] == 2


def test_plan_prune_优先用最后一次召回时间():
    docs = [_doc("recent_use", kind="observation", create_time=_ts(300), last_used=_ts(1))]
    plan = forgetting.plan_prune(docs, now=NOW)
    row = plan["rows"][0]
    assert row["timestamp_source"] == "last_used"
    assert row["age_days"] == pytest.approx(1.0, abs=1e-9)
    assert plan["keep"] == ["recent_use"]


def test_plan_prune_use_count参与稳定性折算():
    """100 天前、初值 30 天的 doc：不折算就该删，折算召回次数后回到待判/留。"""
    docs = [_doc("warm", kind="observation", create_time=_ts(100), use_count=3)]
    folded = forgetting.plan_prune(docs, now=NOW)
    raw = forgetting.plan_prune(docs, now=NOW, fold_use_count=False)
    assert folded["rows"][0]["effective_stability_days"] == pytest.approx(30.0 * 1.5 ** 3)
    assert raw["rows"][0]["effective_stability_days"] == 30.0
    assert folded["rows"][0]["decision"] == "review"   # R≈0.373
    assert raw["rows"][0]["decision"] == "drop"        # R≈0.036
    # 召回 5 次还能更进一步被"救活"（R≈0.645 → 留）
    warm5 = forgetting.plan_prune(
        [_doc("warm5", kind="observation", create_time=_ts(100), use_count=5)], now=NOW
    )
    assert warm5["keep"] == ["warm5"]


def test_plan_prune_rows能回答为什么这么判():
    docs = [_doc("d1", kind="cognition", create_time=_ts(100), use_count=2)]
    plan = forgetting.plan_prune(docs, now=NOW)
    row = plan["rows"][0]
    for key in (
        "doc_id", "kind", "keep_forever", "stability_days", "effective_stability_days",
        "use_count", "age_days", "retrievability", "decision", "reason",
        "boost_to_stability_days", "timestamp_source",
    ):
        assert key in row
    assert row["kind"] == "cognition"
    assert row["stability_days"] == 90.0
    assert row["use_count"] == 2
    assert row["age_days"] == pytest.approx(100.0, abs=1e-9)
    assert row["reason"]  # 人话解释，不能是空串
    assert plan["stats"]["params"]["keep_above"] == 0.5


def test_plan_prune不修改入参():
    docs = [_doc("d1", kind="observation", create_time=_ts(40))]
    snapshot = copy.deepcopy(docs)
    forgetting.plan_prune(docs, now=NOW)
    assert docs == snapshot


def test_plan_prune参数可覆盖():
    docs = [_doc("d1", kind="observation", create_time=_ts(40))]  # R≈0.264
    plan = forgetting.plan_prune(docs, now=NOW, drop_below=0.4)
    assert plan["drop"] == ["d1"]
    plan2 = forgetting.plan_prune(docs, now=NOW, keep_above=0.2)
    assert plan2["keep"] == ["d1"]


def test_plan_prune返回结构与空输入():
    plan = forgetting.plan_prune([], now=NOW)
    assert plan == {
        "keep": [],
        "drop": [],
        "review": [],
        "stats": plan["stats"],
        "rows": [],
    }
    assert plan["stats"]["scanned"] == 0


# --------------------------------------------------------------------------- #
# 6. 给模型的问法：不许出现时间词
# --------------------------------------------------------------------------- #
def test_review_prompt不含时间词且含候选id():
    docs = [
        _doc("m1", text="他说最近在忙一个项目"),
        _doc("m2", text="那天下雨，她提了一句想看海"),
        _doc("m3", text="他喜欢喝冰美式"),
    ]
    prompt, ids = forgetting.review_prompt(docs)
    assert ids == ["m1", "m2", "m3"]
    for doc_id in ids:
        assert doc_id in prompt
    for word in ("天前", "个月", "多久", "时间"):
        assert word not in prompt, f"问法里出现了时间词 {word!r}：模型没有真实时间体验"
    assert "以后再也不会用到" in prompt
    assert "删" in prompt and "留" in prompt
    assert "分" not in prompt  # 不让模型打分


def test_review_prompt受max_items限制():
    docs = [_doc(f"m{i}", text="一些旧观察") for i in range(1, 13)]
    prompt, ids = forgetting.review_prompt(docs, max_items=10)
    assert len(ids) == 10
    assert ids[0] == "m1" and ids[-1] == "m10"
    assert prompt.count("id=") == 10


def test_review_prompt空候选返回空():
    assert forgetting.review_prompt([]) == ("", [])
    assert forgetting.review_prompt([{"doc_id": "", "text": "无名"}]) == ("", [])
    assert forgetting.review_prompt([_doc("a")], max_items=0) == ("", [])


def test_review_prompt截断过长内容():
    long_text = "很长的记忆" * 100
    prompt, _ = forgetting.review_prompt([_doc("m1", text=long_text)])
    assert long_text not in prompt
    assert forgetting.REVIEW_TEXT_LIMIT < len(long_text)


# --------------------------------------------------------------------------- #
# 7. 解析模型回复：只认候选 id（防幻觉）
# --------------------------------------------------------------------------- #
def test_parse_review_reply正常解析():
    reply = "m1 删\nm2 留\nm3 删"
    out = forgetting.parse_review_reply(reply, ["m1", "m2", "m3"])
    assert out["drop"] == ["m1", "m3"]
    assert out["keep"] == ["m2"]
    assert out["ok"] is True


def test_parse_review_reply过滤幻觉id():
    reply = "m1 删\nm2 留\nm99 删\nm10 删"
    out = forgetting.parse_review_reply(reply, ["m1", "m2"])
    assert out["drop"] == ["m1"]
    assert out["keep"] == ["m2"]
    assert set(out["drop"]) | set(out["keep"]) <= {"m1", "m2"}
    assert "m99" in out["ignored"]
    assert "m99" not in out["drop"]


def test_parse_review_reply整词匹配不会串号():
    """候选有 m1 和 m10 时，"m10 删" 不能把 m1 也判成删。"""
    out = forgetting.parse_review_reply("m10 删", ["m1", "m10"])
    assert out["drop"] == ["m10"]
    assert out["keep"] == []


def test_parse_review_reply整行只有一个方向时按行处理():
    out = forgetting.parse_review_reply("m1 m2 都留\nm3 删", ["m1", "m2", "m3"])
    assert out["keep"] == ["m1", "m2"]
    assert out["drop"] == ["m3"]


def test_parse_review_reply支持JSON回复():
    out = forgetting.parse_review_reply('{"drop": ["m1"], "keep": ["m2"]}', ["m1", "m2"])
    assert out["drop"] == ["m1"]
    assert out["keep"] == ["m2"]
    assert out["ok"] is True


def test_parse_review_reply乱码返回ok为假():
    for reply in ("完全看不懂", "", "……", "好的没问题"):
        out = forgetting.parse_review_reply(reply, ["m1", "m2"])
        assert out["ok"] is False
        assert out["drop"] == []
        assert out["keep"] == []
        assert out["ignored"] == []


def test_parse_review_reply没有候选时安全():
    out = forgetting.parse_review_reply("m1 删", [])
    assert out == {"drop": [], "keep": [], "ok": False, "ignored": []}


# --------------------------------------------------------------------------- #
# 8. 类型稳定性初值 / 永不删判定 / 参数
# --------------------------------------------------------------------------- #
def test_稳定性按类型给初值():
    assert forgetting.stability_for("promise") is None
    assert forgetting.stability_for("birthday") is None
    assert forgetting.stability_for("cognition") == 90.0
    assert forgetting.stability_for("observation") == 30.0
    assert forgetting.stability_for("emotion") == 7.0


def test_中文类型别名能认():
    assert forgetting.stability_for("承诺") is None
    assert forgetting.stability_for("生日") is None
    assert forgetting.stability_for("人物认知") == 90.0
    assert forgetting.stability_for("情绪") == 7.0


def test_认不出的类型按一般观察处理():
    """宁可给个确定数值，也不要因为缺字段就整条不算——那等于变相永不删。"""
    assert forgetting.stability_for("天气") == 30.0
    assert forgetting.stability_for("") == 30.0
    assert forgetting.stability_for(None) == 30.0


def test_metadata显式稳定性优先于类型表():
    assert forgetting.stability_for("emotion", {"stability_days": 12}) == 12.0
    assert forgetting.stability_for("promise", {"stability_days": 5}) == 5.0
    assert forgetting.stability_for("observation", {"stability_days": None}) is None


def test_never_forget判定():
    assert forgetting.never_forget({"kind": "promise"}) is True
    assert forgetting.never_forget({"kind": "承诺"}) is True
    assert forgetting.never_forget({"kind": "birthday"}) is True
    assert forgetting.never_forget({"importance": 0.85}) is True   # ≥0.85 取等
    assert forgetting.never_forget({"importance": 0.84}) is False
    assert forgetting.never_forget({"never_forget": True}) is True
    assert forgetting.never_forget({"never_forget": "是"}) is True
    assert forgetting.never_forget({"never_forget": False, "kind": "promise"}) is False
    assert forgetting.never_forget({"kind": "observation", "importance": 0.6}) is False


def test_显式never_forget_False能解除类型保护():
    """运维口子：`never_forget=False` 让 promise 也能按一般观察的初值淡掉。"""
    meta = {"kind": "promise", "never_forget": False}
    assert forgetting.stability_for("promise", meta) == 30.0
    plan = forgetting.plan_prune(
        [_doc("p", kind="promise", never_forget=False, create_time=_ts(200))], now=NOW
    )
    assert plan["drop"] == ["p"]
    # 不写这个开关时，同样的 doc 永不删
    plan2 = forgetting.plan_prune([_doc("p", kind="promise", create_time=_ts(200))], now=NOW)
    assert plan2["drop"] == []


def test_thresholds缺省值():
    cfg = forgetting.thresholds()
    assert cfg == {
        "keep_above": 0.5,
        "drop_below": 0.05,
        "per_recall_factor": 1.5,
        "default_stability_days": {
            "promise": None, "birthday": None, "cognition": 90.0,
            "observation": 30.0, "emotion": 7.0,
        },
    }
    assert forgetting.thresholds(None) == cfg


def test_thresholds覆盖值():
    cfg = forgetting.thresholds({
        "keep_above": 0.6,
        "drop_below": 0.02,
        "per_recall_factor": 2.0,
        "default_stability_days": {"emotion": 3, "cognition": None},
    })
    assert cfg["keep_above"] == 0.6
    assert cfg["drop_below"] == 0.02
    assert cfg["per_recall_factor"] == 2.0
    assert cfg["default_stability_days"]["emotion"] == 3.0
    assert cfg["default_stability_days"]["cognition"] is None  # 显式永不删
    assert cfg["default_stability_days"]["observation"] == 30.0  # 局部合并不丢默认


def test_thresholds支持嵌套forgetting子段():
    cfg = forgetting.thresholds({"forgetting": {"keep_above": 0.7, "drop_below": 0.1}})
    assert cfg["keep_above"] == 0.7
    assert cfg["drop_below"] == 0.1


def test_thresholds非法配置回退默认():
    """防住"配错了把记忆全删了"：非法项回退默认，不影响别项。"""
    cfg = forgetting.thresholds({"keep_above": 2.0, "drop_below": -1})
    assert cfg["keep_above"] == 0.5
    assert cfg["drop_below"] == 0.05
    cfg2 = forgetting.thresholds({"keep_above": 0.4, "drop_below": 0.6})  # drop≥keep
    assert cfg2["keep_above"] == 0.4
    assert cfg2["drop_below"] == 0.05
    cfg3 = forgetting.thresholds({"per_recall_factor": 0.9})
    assert cfg3["per_recall_factor"] == 1.5


def test_summary是一行中文且带当前参数():
    text = forgetting.summary()
    assert "\n" not in text
    assert "遗忘" in text and "e^(-t/S)" in text
    assert "0.5" in text and "0.05" in text and "1.5" in text
    assert "永不删" in text and "90天" in text and "30天" in text and "7天" in text


# --------------------------------------------------------------------------- #
# 9. 坏数据一律不抛
# --------------------------------------------------------------------------- #
def test_所有函数拿到坏数据都不抛异常():
    bad_values = [None, "", "abc", [], {}, float("nan"), float("inf"), -1, 0]
    for bad in bad_values:
        forgetting.retrievability(bad, bad)
        forgetting.age_days(bad, bad)
        forgetting.decayed_stability(bad, bad, bad)  # type: ignore[arg-type]
        forgetting.classify(bad, bad, keep_forever=None)  # type: ignore[arg-type]
        forgetting.never_forget(bad)  # type: ignore[arg-type]
        forgetting.stability_for(bad, bad)  # type: ignore[arg-type]
        forgetting.thresholds(bad)  # type: ignore[arg-type]
        forgetting.plan_prune(bad)  # type: ignore[arg-type]
        forgetting.review_prompt(bad)  # type: ignore[arg-type]
        forgetting.parse_review_reply(bad, bad)  # type: ignore[arg-type]
    forgetting.apply_recall(None)  # type: ignore[arg-type]
    forgetting.summary()


def test_plan_prune跳过不是dict的元素和没有id的文档():
    docs = [None, 1, "字符串", {}, {"text": "没有 id"}, _doc("ok", create_time=_ts(1))]
    plan = forgetting.plan_prune(docs, now=NOW)  # type: ignore[list-item]
    assert plan["stats"]["skipped"] == 5
    assert plan["keep"] == ["ok"]


def test_metadata是JSON字符串或脏数据都不抛():
    docs = [
        {"doc_id": "a", "text": "x", "metadata": '{"kind": "emotion", "create_time": 1}'},
        {"doc_id": "b", "text": "x", "metadata": "不是 JSON"},
        {"doc_id": "c", "text": "x", "metadata": None},
        {"doc_id": "d", "text": "x"},  # 没有 metadata 键
    ]
    plan = forgetting.plan_prune(docs, now=NOW)
    assert plan["stats"]["scanned"] == 4
    assert set(plan["keep"]) | set(plan["drop"]) | set(plan["review"]) == {"a", "b", "c", "d"}


def test_age_days不会因为未来时间戳变负():
    assert forgetting.age_days(NOW + 5 * DAY, NOW) == 0.0
    assert forgetting.age_days(NOW - 2 * DAY, NOW) == pytest.approx(2.0)
    assert forgetting.age_days(0, NOW) == 0.0
    assert forgetting.age_days(None) == 0.0  # type: ignore[arg-type]
