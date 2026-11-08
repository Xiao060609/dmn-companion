"""`gateway/plan.py`（计划的状态机与措辞）的单元测试。

对应 `plan/DMN审核.md` 能力 5 的每一条定稿：
  * 10 天从**计划创建时**算；
  * 临期提醒的**措辞**要像"她自己想起来"，不是执行剧本；
  * 成败都销毁，但都留痕；
  * 判定不出时**不销毁**（unknown 继续等）。
"""
from __future__ import annotations

import time

import plan


DAY = 86400.0


# --------------------------------------------------------------------------- #
# 沉默期与临期
# --------------------------------------------------------------------------- #
def test_沉默期从创建时刻算():
    now = 1_800_000_000.0
    assert plan.is_due(now - 10 * DAY, now) is True, "正好 10 天就该临期"
    assert plan.is_due(now - 9.9 * DAY, now) is False
    assert plan.is_due(now - 30 * DAY, now) is True


def test_不是从承诺时间算而是从创建算():
    """主人原话：「10 天从计划创建时算（不是从承诺时间算）」。

    `next_due_ts()` 只收一个 created_ts，签名本身就堵死了"从别处算"的可能。
    """
    created = 1_700_000_000.0
    assert plan.next_due_ts(created) == created + 10 * DAY
    assert plan.next_due_ts(created, days=3) == created + 3 * DAY


def test_沉默期坏值不抛也不催():
    assert plan.silence_days(None) == 0.0
    assert plan.silence_days("不是数字") == 0.0
    assert plan.silence_days(0) == 0.0
    assert plan.is_due(0, time.time()) is False, "拿不到创建时间就不该催"


def test_剩余小时数():
    now = 1_800_000_000.0
    assert plan.hours_left(now - 5 * DAY, now) == 5 * 24.0
    assert plan.hours_left(now - 12 * DAY, now) < 0


# --------------------------------------------------------------------------- #
# 措辞：给她「当时发生了什么」，不给她执行剧本
# --------------------------------------------------------------------------- #
def test_临期提醒给的是当时的事而不是指令():
    now = time.mktime((2026, 11, 6, 21, 40, 0, 0, 0, -1))
    line = plan.trigger_line("他说昨晚没睡好", now=now, last_turn_ts=now - 5 * 3600)
    assert line.startswith("该回头看看了：")
    assert "他说昨晚没睡好" in line
    assert "现在 21:40" in line
    assert "距上次对话 5 小时" in line
    # 关键：不该出现"先问他…再提…"这种剧本式祈使
    for bad in ("先问", "再提", "别一上来", "你应该", "记得要"):
        assert bad not in line, f"不该出现执行剧本式的词：{bad}"


def test_临期提醒没有上次对话时间时省略后半句():
    now = time.mktime((2026, 11, 6, 9, 5, 0, 0, 0, -1))
    line = plan.trigger_line("他答应给我加升级", now=now)
    assert "09:05" in line
    assert "距上次对话" not in line


def test_临期提醒空正文有兜底():
    line = plan.trigger_line("", subject="升级的事", now=time.time())
    assert "升级的事" in line


def test_系统流水账那一行带统一前缀():
    assert plan.system_log_trigger("问他升级的事") == "[系统] 计划触发：问他升级的事"
    assert plan.system_log_trigger("") .startswith("[系统] 计划触发：")


def test_多久没聊要说人话():
    assert plan._ago(0.5) == "刚才"
    assert plan._ago(5) == "5 小时"
    assert plan._ago(30) == "一天多"
    assert plan._ago(24 * 4) == "4 天"
    assert plan._ago(24 * 15) == "十来天"
    assert plan._ago(None) == "有一阵了"


# --------------------------------------------------------------------------- #
# 成败裁决：**模型判，本模块只校验**（2026-11-07 纠正设计偏差）
#
# 🔧 这里原来测的是 `plan.judge_outcome(text)` 的三套词表。词表整套删掉了
# （审计 H.1 倾向的方案 C，也是 `DMN审核.md` 能力 5 的原始设计），
# 但**测的东西一条都不能少** —— 换成测「模型给的裁决怎么被归一/怎么落库」：
#
#   旧断言                                 新断言
#   judge_outcome("弄好了") == "success"   normalize_verdict("success") == "success"
#                                          + system 层真的销毁（见 test_dmn_20261106.py）
#   judge_outcome("嗯嗯") == "unknown"     normalize_verdict("嗯嗯") == "unknown"（认不出一律 unknown）
#   judge_outcome("") == "unknown"         normalize_verdict(None/"") == "unknown"
#   ——                                     parse_verdicts 的白名单 / 去重 / 截断（新增）
#
# 也就是说：**"判不出来必须落到 unknown、unknown 绝不销毁"这条铁律
# 在新的实现里被守得更死了** —— 旧词表至少还会猜，现在除了模型给的三档
# 之外一切都是 unknown。
# --------------------------------------------------------------------------- #
def test_标准裁决原样通过():
    assert plan.normalize_verdict("success") == "success"
    assert plan.normalize_verdict("failure") == "failure"
    assert plan.normalize_verdict("unknown") == "unknown"


def test_模型写歪的裁决能归一():
    """模型偶尔会回大写 / 英文变体 / 中文 —— 折回三个标准值，别当成脏数据丢掉。"""
    assert plan.normalize_verdict("SUCCESS") == "success"
    assert plan.normalize_verdict(" Done ") == "success"
    assert plan.normalize_verdict("成功") == "success"
    assert plan.normalize_verdict("兑现") == "success"
    assert plan.normalize_verdict("failed") == "failure"
    assert plan.normalize_verdict("失败") == "failure"
    assert plan.normalize_verdict("不确定") == "unknown"
    assert plan.normalize_verdict("unknown") == "unknown"


def test_认不出来的裁决一定是unknown():
    """最危险的方向：认不出来**绝不能**落到 success/failure（那会销毁计划）。"""
    for value in ("嗯嗯", "大概完成了吧", "50%", "", None, 0, [], {}, True, "maybe"):
        got = plan.normalize_verdict(value)
        assert got == "unknown", f"{value!r} 归成了 {got!r} —— 认不出必须 unknown"
        assert plan.should_close(got) is False


def test_裁决只有三种取值():
    """`PLAN_VERDICTS` 是 schema 的 enum 来源，也是 `parse_verdicts` 的取值域。"""
    assert plan.PLAN_VERDICTS == ("success", "failure", "unknown")
    for value in plan.PLAN_VERDICTS:
        assert plan.normalize_verdict(value) == value


# --------------------------------------------------------------------------- #
# 解析模型回的 `plan_verdicts`（不可信输入 → 安全的数据）
# --------------------------------------------------------------------------- #
def test_解析正常裁决():
    got = plan.parse_verdicts(
        [{"id": 3, "verdict": "success", "evidence": "早就给你了"},
         {"id": "5", "verdict": "failure"}],
        allowed_ids=[3, 5],
    )
    assert got[3]["verdict"] == "success"
    assert got[3]["evidence"] == "早就给你了"
    assert got[5]["verdict"] == "failure", "字符串 id 也要能认"
    assert got[5]["evidence"] == ""


def test_幻觉id一律丢掉():
    """模型编一个不存在的 id —— 绝不能因此去改一条别的计划。"""
    got = plan.parse_verdicts(
        [{"id": 99, "verdict": "success"}, {"id": 3, "verdict": "failure"}],
        allowed_ids=[3],
    )
    assert set(got) == {3}, f"幻觉 id 没被拦住：{got}"


def test_没给白名单时不限制():
    """`allowed_ids=None` 只给单测用（生产必须传，见 `apply_plan_verdicts`）。"""
    got = plan.parse_verdicts([{"id": 7, "verdict": "success"}])
    assert set(got) == {7}


def test_同一id只认第一次():
    """模型自我更正时取第一个：两次说法都"可能对"，取第一个至少是确定的。"""
    got = plan.parse_verdicts(
        [{"id": 3, "verdict": "unknown"}, {"id": 3, "verdict": "success"}],
        allowed_ids=[3],
    )
    assert got[3]["verdict"] == "unknown"


def test_坏输入不抛也不产生裁决():
    for raw in (None, "", "不是数组", 42, [None, 1, "x"], [{"verdict": "success"}]):
        assert plan.parse_verdicts(raw, allowed_ids=[1, 2]) == {}, repr(raw)


def test_依据文本会截断():
    """`evidence` 是留痕文本，不是推理过程 —— 120 字封顶。"""
    got = plan.parse_verdicts(
        [{"id": 1, "verdict": "success", "evidence": "很长" * 200}],
        allowed_ids=[1],
    )
    assert len(got[1]["evidence"]) == 120


def test_语义判定用的词表已经删了():
    """三套词表 + 判定函数必须**真的**不在了（不是"还在但没人调"）。

    留着它们最危险的地方是：以后有人看见 `SUCCESS_WORDS` 会以为判定还在用词表，
    于是去补词表 —— 而补词表对现在的链路**完全没用**（判定在模型那侧）。
    """
    for name in ("SUCCESS_WORDS", "FAILURE_WORDS", "DENY_WORDS",
                 "judge_outcome", "_has_negated", "_NEG_PREFIX"):
        assert not hasattr(plan, name), (
            f"{name} 被加回来了？成败判定已经交给模型（审计 H.1 方案 C），"
            f"再留一套字符串启发式只会和模型打架"
        )


def test_unknown不销毁计划():
    assert plan.should_close("unknown") is False
    assert plan.should_close("success") is True
    assert plan.should_close("failure") is True
    # 白名单语义：以后多出第四个值，默认也是"留着"而不是"丢掉"
    assert plan.should_close("以后新加的裁决值") is False


def test_成败都落closed():
    """主人：「成功/失败都销毁「计划」」。状态只有"还欠着/不欠了"两态。"""
    assert plan.closed_status("success") == "closed"
    assert plan.closed_status("failure") == "closed"


def test_留痕文本():
    assert "兑现" in plan.outcome_record("success", evidence="给我升级")
    assert "没有兑现" in plan.outcome_record("failure", evidence="给我升级")
    assert "不明" in plan.outcome_record("unknown", evidence="给我升级")
    # 空 evidence 也要能拼出完整句子
    assert plan.outcome_record("success", subject="主人")


# --------------------------------------------------------------------------- #
# 待判清单（喂给模型的那份载荷）
# --------------------------------------------------------------------------- #
def test_待判条目带计划内容和他说的话():
    """**这是判定能准的前提**：旧词表失败的根本原因就是它不知道计划讲的是什么。

    所以载荷里必须有 `plan`（计划内容）和 `said_after_trigger`（触发后他的话）。
    """
    now = 1_800_000_000.0
    item = plan._plan_verdict_item(
        7, "给我加升级", "主人", now - 11 * DAY, now - 2 * DAY,
        "主人: 哎我给忘了", now,
    )
    assert item["id"] == 7
    assert item["plan"] == "给我加升级"
    assert item["subject"] == "主人"
    assert item["created_days_ago"] == 11.0
    assert item["triggered_days_ago"] == 2.0
    assert "我给忘了" in item["said_after_trigger"]
    # 时间**换算成天数**由代码做，不给模型原始时间戳（它没有时钟）
    assert "created_ts" not in item and "triggered_ts" not in item


def test_待判条目截断和空值兜底():
    now = 1_800_000_000.0
    item = plan._plan_verdict_item(
        1, "长" * 500, "主" * 50, 0, 0, "话" * 5000, now,
    )
    assert len(item["plan"]) <= plan.PLAN_VERDICT_TEXT_LIMIT + 1
    assert len(item["subject"]) <= 20
    assert len(item["said_after_trigger"]) <= 1200
    assert item["created_days_ago"] == 0.0, "时间戳 0 不能算出个天文数字"
    assert item["triggered_days_ago"] is None


def test_待判清单封顶且id不重复():
    items = [{"id": i, "plan": f"第{i}件"} for i in range(50)]
    items.append({"id": 3, "plan": "重复的 3 号"})
    block = plan.plan_verdict_block(items)
    assert len(block) == plan.PLAN_VERDICT_MAX
    assert [b["id"] for b in block] == list(range(plan.PLAN_VERDICT_MAX))
    # 重复 id 只留第一个（后出现的那个是"重复的 3 号"，它不该顶掉原来的）
    assert block[3]["plan"] == "第3件"


def test_待判清单坏数据不抛():
    assert plan.plan_verdict_block(None) == []
    assert plan.plan_verdict_block([None, "x", {}, {"id": "坏"}]) == []


def test_系统流水账完成那行带统一前缀():
    """前缀格式只该有一个来源：和 `system_log_trigger()` 成对。"""
    assert plan.system_log_outcome("主人兑现了：给我升级") == "[系统] 计划完成：主人兑现了：给我升级"
    assert plan.system_log_outcome("") == "[系统] 计划完成：那件事"
    # 句末标点不重复（`outcome_record()` 不加句号，这里只防有人传进来）
    assert plan.system_log_outcome("完成。") == "[系统] 计划完成：完成"


# --------------------------------------------------------------------------- #
# 摘要（体检 / 后台卡片）
# --------------------------------------------------------------------------- #
def test_摘要不抛且字段齐全():
    now = 1_800_000_000.0
    row = {"kind": "promise", "status": "open", "ts": now - 11 * DAY}
    s = plan.plan_summary(row, now)
    assert s["kind"] == "promise"
    assert s["status"] == "open"
    assert s["silence_days"] == 11.0
    assert s["due"] is True
    # 坏数据
    assert plan.plan_summary({}, now)["due"] is False
    assert plan.plan_summary({"ts": "坏"}, now)["silence_days"] == 0.0


def test_已经关闭的计划不再算临期():
    now = 1_800_000_000.0
    row = {"kind": "promise", "status": "closed", "ts": now - 30 * DAY}
    assert plan.plan_summary(row, now)["due"] is False
