"""「计划」（原「牵挂」）的纯逻辑层：10 天沉默期、临期触发、成败判定、销毁与留痕。

为什么单独一个文件
------------------
`system.py` 已经有 5000+ 行，而这块逻辑**全是可以用纯函数表达的规则**
（时间判断、措辞拼装、成败判定、销毁后要留什么痕）。放在 `system.py` 里
等于让"机制"和"执行"混在一处，既没法单测，也没法在主人在场时快速调整。

所以：本模块只做**判断和拼字符串**，不碰数据库、不碰模型、不读写文件。
`system.py` 负责把判断结果落到 `events` / `schedules` / `system_log`。

主人定的规则（`plan/DMN审核.md` 能力 5，2026-11-06 定稿）
--------------------------------------------------------
1. 「牵挂」更名为「计划」；语义变成「她打算去做的事」，被动型两侧仍是
   `promise`（承诺）和 `deferred_care`（迟点再说）。
2. **倒计时 10 天**，**从计划创建时算**（不是从承诺时间算）。
   10 天 = 沉默期上限：期间他没提就启动临期程序。
3. 临期时：系统层写成一条想法上浮到内心OS，**提醒送到她面前就算「触发」**，
   她选择不说也算触发。
4. 他回答了「成功 / 失败」之后：**成功失败都销毁计划**，
   但都**留痕**——记进「事件」，不是计划。
5. **系统层判定成功**（它计算着利弊）。→ 见下面 A1 的处理方式。

成败由**模型判**，不由本模块判（2026-11-07 纠正设计偏差）
--------------------------------------------------------
原实现有一套字符串词表（`SUCCESS_WORDS` / `FAILURE_WORDS` / `DENY_WORDS` +
`judge_outcome()`），实测缺口很大：「我做完了」「完工了」「升级好了」全判
`unknown`（`plan/项目审计-2026-11-06.md` H.1 的实测表）。根因不只是词表小：
`judge_outcome(text)` 只看文本、**不知道计划内容是什么**，所以「做完了」指升级
还是指作业它分不出来；而「还没说完」会命中「还没」被误判成 failure。

主人的原始设计（`plan/DMN审核.md` 板块二能力 5）本来就是让模型判：

    计划触发后立刻进入休眠，防止二次触发，触发记录记入流水账，
    然后**系统层会在整理流水账时判断是否执行成功，由系统层决定
    要不要保留、删除或更改计划**。

    有啥要判断的扔流水账里，**跟系统层说一嘴就一起判断了**。

所以词表整套删除（审计 H.1 倾向的方案 C）。现在的分工是：

    system.py   把「待判定计划 + 触发后他说了什么」作为**独立字段**喂给巩固
                那次模型调用（不额外花钱）
    模型        在 `system_consolidate` 的 `plan_verdicts` 里逐条给
                success / failure / unknown
    本模块      只做**纯函数**的两件事：校验/归一裁决值（`normalize_verdict`）、
                决定要不要销毁（`should_close`）+ 拼留痕文本（`outcome_record`）
    system.py   按裁决落库（closed + 留痕事件 + 流水账）

⚠ `unknown` **绝不销毁**（`should_close` 是唯一出口）。一次误判 = 一条真实计划
被静默销毁，以后再也不提。宁可多等 10 天，也不能猜。
"""
from __future__ import annotations

import math
import time

#: 计划的两种被动型。主动型（她打算做的事）不在这里 —— 那是「想法」，归内心OS。
PLAN_KINDS = ("promise", "deferred_care")

#: 沉默期上限（天）。主人：**从计划创建的时候算（10 天）**。
DEFAULT_SILENCE_DAYS = 10.0

#: 计划判定可能落在的三个值。**只有前两个会销毁计划**，`unknown` 永远不。
PLAN_VERDICTS = ("success", "failure", "unknown")

#: 模型偶尔会回中文 / 大写 / 近义词，统一折到上面三个值。
#: 只做**枚举归一**，不做语义判断 —— 语义判断已经交给模型了，这里再猜一遍
#: 就等于把被删掉的词表又请回来（而且是在更糟的位置：模型已经判过了）。
_VERDICT_ALIASES = {
    "success": "success",
    "succeeded": "success",
    "done": "success",
    "ok": "success",
    "完成": "success",
    "成功": "success",
    "兑现": "success",
    "failure": "failure",
    "failed": "failure",
    "fail": "failure",
    "失败": "failure",
    "未兑现": "failure",
    "落空": "failure",
    "unknown": "unknown",
    "uncertain": "unknown",
    "unsure": "unknown",
    "未知": "unknown",
    "不确定": "unknown",
    "判不出": "unknown",
}

_TRAILING_PUNCT = "，。！？、；：,.!?;: \t\n\r"


def silence_days(created_ts: float, now: float | None = None) -> float:
    """计划从创建到现在过了多少天。坏值一律当 0（不催也不丢）。"""
    try:
        created = float(created_ts or 0)
    except (TypeError, ValueError):
        return 0.0
    if created <= 0:
        return 0.0
    cur = time.time() if now is None else float(now)
    return max(0.0, (cur - created) / 86400.0)


def is_due(created_ts: float, now: float | None = None,
           days: float = DEFAULT_SILENCE_DAYS) -> bool:
    """这条计划到临期了吗（沉默期已满）。"""
    return silence_days(created_ts, now) >= max(0.0, float(days))


def next_due_ts(created_ts: float, days: float = DEFAULT_SILENCE_DAYS) -> float:
    """临期时刻 = 创建时刻 + 沉默期。供 `schedules` 排班用。"""
    try:
        created = float(created_ts or 0)
    except (TypeError, ValueError):
        created = 0.0
    return created + max(0.0, float(days)) * 86400.0


def hours_left(created_ts: float, now: float | None = None,
               days: float = DEFAULT_SILENCE_DAYS) -> float:
    """距离临期还有几小时（已过期返回负数）。"""
    cur = time.time() if now is None else float(now)
    return (next_due_ts(created_ts, days) - cur) / 3600.0


# ---------------------------------------------------------------- 措辞（代码拼，不让模型写）
def _clock(now: float) -> str:
    return time.strftime("%H:%M", time.localtime(now))


def _ago(hours: float) -> str:
    """把小时数说成人话。刻意粗粒度：她要的是「多久没聊」，不是精确值。"""
    if hours is None:
        return "有一阵了"
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "有一阵了"
    if h < 1:
        return "刚才"
    if h < 24:
        return f"{int(round(h))} 小时"
    days = h / 24.0
    if days < 2:
        return "一天多"
    if days < 10:
        return f"{int(round(days))} 天"
    return "十来天"


def trigger_line(evidence: str, *, now: float | None = None,
                 last_turn_ts: float | None = None,
                 subject: str = "") -> str:
    """临期提醒的正文 —— 按主人的口吻要求**由代码直接拼**。

    主人的要求（`plan/DMN审核.md` 能力 5「approach_hint 的口吻问题」）：

        不该是「该怎么说话」的执行剧本，而该是「当时发生了什么」，
        让她自己想起来。

    所以这里给的形状是::

        该回头看看了：他说昨晚没睡好，现在几点了
        （现在 21:40，距上次对话 5 小时）

    而不是::

        该回头看看了：先问他昨晚睡够没，再提软件和 skill 的事
    """
    cur = time.time() if now is None else float(now)
    body = str(evidence or "").strip().rstrip(_TRAILING_PUNCT)
    if not body:
        body = str(subject or "他说过的那件事").strip()
    tail = f"（现在 {_clock(cur)}"
    if last_turn_ts:
        try:
            gap = max(0.0, (cur - float(last_turn_ts)) / 3600.0)
            tail += f"，距上次对话 {_ago(gap)}"
        except (TypeError, ValueError):
            pass
    tail += "）"
    return f"该回头看看了：{body}\n{tail}"


def system_log_trigger(evidence: str, subject: str = "") -> str:
    """写进 `system_log` 的那一行（统一 `[系统]` 前缀，让她和模型都能认出来）。

    主人原话：「就搞成这种「[系统] 计划触发：问他升级的事」流水账里呗」。
    """
    body = str(evidence or "").strip().rstrip(_TRAILING_PUNCT)
    if not body:
        body = str(subject or "").strip() or "那件事"
    return f"[系统] 计划触发：{body}"


def system_log_outcome(record: str) -> str:
    """写进 `system_log` 的完成那一行。

    原来这行是在 `system.py` 里内联拼的（`f"[系统] 计划完成：{record}"`）。
    挪到这里的理由：**前缀格式只该有一个来源** —— `system_log_trigger()` 在这，
    它也应该在这，否则以后改前缀（比如 `[系统]` 变成 `[系统层]`）要改两处，
    必漏一处。
    """
    body = str(record or "").strip().rstrip(_TRAILING_PUNCT)
    if not body:
        body = "那件事"
    return f"[系统] 计划完成：{body}"


# ---------------------------------------------------------------- 成败裁决（模型判，这里只校验）
def normalize_verdict(value) -> str:
    """把模型给的裁决**归一成** `success` / `failure` / `unknown` 三者之一。

    这是 2026-11-07 纠正设计偏差后的**唯一入口**：原来这里叫 `judge_outcome(text)`，
    用三套词表猜；现在判定权在模型，本函数只负责两件事：

      1. 把模型偶尔写歪的值（大写 / 中文 / `done` / `failed` / `不确定`…）
         折回三个标准值；
      2. **认不出来的一律返回 `unknown`** —— fail-safe 的方向是「留着」，
         不是「销毁」。模型答了个谁也看不懂的东西，最坏的结果必须是
         "这条计划再等等"，绝不能是"它被删了"。

    ⚠ 这里**不做任何语义判断**。以前 `judge_outcome` 会再猜一遍文本，
    结果和模型打架（模型说 success、词表说 unknown）；现在只有一个来源。

    Args:
        value: 模型给的裁决值（任意类型，脏数据不抛）。

    Returns:
        `"success"` / `"failure"` / `"unknown"`。
    """
    if value is None:
        return "unknown"
    key = str(value).strip().lower()
    if not key:
        return "unknown"
    return _VERDICT_ALIASES.get(key, "unknown")


def parse_verdicts(raw, allowed_ids=None) -> dict[int, dict]:
    """把模型回的 `plan_verdicts` 解析成 `{event_id: {"verdict", "evidence"}}`。

    为什么单独一个纯函数：`system.py` 已经 6000 行，而"模型输出是不可信输入"
    这条规矩应该有可单测的落点 —— 模型编一个不存在的 id、把 verdict 写成
    「大概完成了吧」、`id` 是字符串 `"3"`、整个字段是 `None`，都不该走到 SQL。

    安全边界（三条，全部 fail-safe）：
      1. **只认候选里出现过的 id**（`allowed_ids`）：模型幻觉出来的 id 直接丢，
         不记审计也不删任何东西 —— 幻觉 id 最坏就是"这条计划这轮没判"；
      2. `verdict` 归一（`normalize_verdict`）；归一不出来 = `unknown`；
      3. 同一个 id 出现多次时**取第一个**：后面的很可能是模型自我更正，
         而"第一次说的"和"最后说的"哪个更可信是没法判断的，取谁都一样，
         取第一个至少是确定的。

    Args:
        raw: 模型给的 `plan_verdicts`（list / dict / None / 脏数据）。
        allowed_ids: 本轮真正喂进去的计划 id 集合；`None` 表示不限制
            （仅给单测用，生产必须传）。

    Returns:
        `{int(event_id): {"verdict": str, "evidence": str}}`；
        `evidence` 截断到 120 字（它是**留痕文本**，不是推理过程）。
    """
    allowed: set[int] | None = None
    if allowed_ids is not None:
        allowed = set()
        for item in allowed_ids:
            try:
                allowed.add(int(item))
            except (TypeError, ValueError):
                continue

    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return {}

    out: dict[int, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            eid = int(str(item.get("id")).strip())
        except (TypeError, ValueError):
            continue
        if eid in out:
            continue                       # 同一 id 只认第一次
        if allowed is not None and eid not in allowed:
            continue                       # 幻觉 id：不是本轮候选
        verdict = normalize_verdict(item.get("verdict"))
        evidence = str(item.get("evidence") or "").strip()[:120]
        out[eid] = {"verdict": verdict, "evidence": evidence}
    return out


def outcome_record(ok: str, *, evidence: str = "", subject: str = "") -> str:
    """成败的**留痕**文本 —— 记进「事件」，不是计划。

    主人给的例子：
        成功：「主人给我升级了」
        失败：「主人说要给我升级结果骗了我」
    """
    thing = str(evidence or "").strip().rstrip(_TRAILING_PUNCT)
    if not thing:
        thing = str(subject or "").strip() or "那件事"
    who = str(subject or "主人").strip() or "主人"
    if ok == "success":
        return f"{who}兑现了：{thing}"
    if ok == "failure":
        return f"{who}说要{thing}，结果没有兑现"
    return f"{who}提到过{thing}，结果不明"


def outcome_kind(ok: str) -> str:
    """留痕事件的 `events.kind`。

    复用 `observation`（长期事实）而不是新开一个 kind：`observation` 已经
    在 `EVENT_KINDS` 里、已经参与抽取和人物画像，加一个新 kind 要同步改
    `_stateful_event_kinds` / 体检 / 前端，收益不成比例。
    """
    return "observation" if ok in ("success", "failure") else "observation"


# ---------------------------------------------------------------- 喂给模型的待判清单
#: 一轮最多把几条待判计划塞进巩固 prompt。
#:
#: 为什么封顶：巩固那一次调用的产出上限是 `consolidate_max_tokens`（默认 500），
#: 待判清单一长，模型要么答不全（剩下的一律 unknown → 计划永远收不了尾），
#: 要么把 `insights` 挤没。实测生产上 `attempts>=1` 且还 `open` 的计划是个位数，
#: 10 条够用，超出的**下一轮再判**（不是丢 —— 计划还在库里，条件没变）。
PLAN_VERDICT_MAX = 10

#: 单条待判内容进 prompt 的截断长度。
PLAN_VERDICT_TEXT_LIMIT = 100


def _plan_verdict_item(event_id: int, evidence: str, subject: str,
                       created_ts: float, triggered_ts: float | None,
                       reply_text: str, now: float, channel: str = "") -> dict:
    """拼一条「待判定计划」的 prompt 载荷。纯函数，坏数据不抛。

    ⚠ `channel` 是给 `system.py` 落留痕事件用的**透传字段**，**不进 prompt**：
    句子是 `system._build_consolidate_messages()` 拼的，它在序列化之前会把
    `channel` 摘掉（见那里的注释）。模型不需要知道这是桌宠还是私聊 ——
    那对「成没成」没有信息量，说了反而会诱导它按场合猜。

    ⚠ `triggered_ts` 允许是 `None`，而且**生产上就是 None**：`_fire_due_plans()`
    只把 `events.attempts` 从 0 改成 1，**没有单独存触发时刻**。这带来一个
    必须说清的取舍：

      * 不能拿「创建时间」冒充触发时刻 —— 11 天前创建、刚才才触发的计划
        会被写报成「11 天前触发过」，模型看到"提醒他 11 天了还这么回"
        这种不存在的证据，判得更偏；
      * 所以宁可如实给 `None`（进 prompt 是 `null` = 「不知道」），
        让模型只看 `said_after_trigger` 里的实际发言 —— 那才是判断的原料。

    真正要修的话是给 `events` 加一列存触发时刻，属于 schema 变更，不在这次范围内。
    """
    def _short(text: str, limit: int) -> str:
        body = str(text or "").replace("\r", " ").replace("\n", " ").strip()
        return body[:limit] + "…" if len(body) > limit else body

    triggered_days = None
    if triggered_ts:
        try:
            triggered_days = round(max(0.0, (now - float(triggered_ts)) / 86400.0), 1)
        except (TypeError, ValueError):
            triggered_days = None

    return {
        "id": int(event_id),
        # 计划内容：**这是 key** —— 词表之所以判不准就是因为它不知道计划讲的是什么，
        # 模型拿到内容才能把「做完了」对上「升级」。
        "plan": _short(evidence, PLAN_VERDICT_TEXT_LIMIT),
        "subject": str(subject or "").strip()[:20],
        # 「他开始欠这件事多久了」—— 用天数而不是时间戳：类模型没有真实时钟，
        # 但"11 天前"它是能理解的（时间**换算**由代码做，这是 3.7 的同一条规矩）。
        "created_days_ago": round(silence_days(created_ts, now), 1),
        "triggered_days_ago": triggered_days,
        # 触发之后他说了什么（时间正序）。**由 `system.py` 从流水账里取**，
        # 这里只负责把它收进载荷 —— 纯函数不碰库。
        "said_after_trigger": str(reply_text or "").strip()[-1200:],
        "channel": str(channel or ""),
    }


def plan_verdict_block(items: list[dict]) -> list[dict]:
    """把 `_plan_verdict_item()` 拼出来的条目整理成最终载荷（去重 + 封顶）。

    单独一个纯函数是为了可单测：`id` 重复会让模型把两件事判成一件，
    超过 `PLAN_VERDICT_MAX` 会挤爆 token 预算。
    """
    out: list[dict] = []
    seen: set[int] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            eid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if eid in seen:
            continue
        seen.add(eid)
        out.append(item)
        if len(out) >= PLAN_VERDICT_MAX:
            break
    return out


# ---------------------------------------------------------------- 状态机
def closed_status(action: str) -> str:
    """计划被处理后要落的状态。

    主人：「**成功/失败都销毁「计划」**」。所以无论成败都是 `closed`，
    不是 `done` / `failed` —— 状态机里只有"还欠着 / 已经不欠了"两态，
    成败这个信息在**留痕文本**里，不在状态里。
    """
    return "closed"


def should_close(ok: str) -> bool:
    """这个判定结果要不要销毁计划。**`unknown` 不销毁（继续等）**。

    ⚠ 这是「误判不能静默销毁真实计划」这条铁律的**唯一执行点**
    （项目地图坑 #64 / 审计 H.1）。改判定来源随便改，改这里要三思：
    一次误判 = 一条真实计划永远消失，而且**不会有任何提示**。
    所以这个函数的形状是"白名单"（只认 success / failure），
    而不是"黑名单"（只排除 unknown）—— 以后万一多出第四个裁决值，
    默认行为是**留着**，不是丢掉。
    """
    return ok in ("success", "failure")


def plan_summary(row: dict, now: float | None = None,
                 days: float = DEFAULT_SILENCE_DAYS) -> dict:
    """给体检 / 后台卡片用的一行摘要。坏数据不抛。"""
    created = 0.0
    try:
        created = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        created = 0.0
    sil = silence_days(created, now)
    left = hours_left(created, now, days) if created else 0.0
    return {
        "kind": str(row.get("kind") or ""),
        "status": str(row.get("status") or ""),
        "silence_days": round(sil, 1),
        "hours_left": round(left, 1) if not math.isinf(left) else None,
        "due": bool(created) and is_due(created, now, days) and
               str(row.get("status") or "") in ("open", "deferred"),
    }
