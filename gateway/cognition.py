"""认知准则引擎：判断「一句话算不算认知」，并把模型产出的写入动作解析成机械操作。

规格来源：``plan/DMN审核.md``
    * 1.9 认知的录入准则（主人 2026-11-05 定稿）
    * 能力 8 · 记忆库，特别是「主人已定的三条（2026-11-06 09:48）」

设计边界
--------
本模块是**纯函数 + 纯字符串拼装**：不联网、不读写数据库、不 import gateway 里
任何有副作用的模块（system / gateway / memory 全都不碰）。调用方（巩固流程）
负责把 :func:`merge_prompt_block` 的结果送进模型、把 :func:`action_to_effect`
的结果交给向量层执行。

两条判定标准（1.9，两条都要满足才算认知）
----------------------------------------
① **形态**：主谓宾陈述（什么是什么 / 什么怎么样）
② **可验证性**：能通过后续观察判断真假（过了产生它的场景还有用）

关键分界线是「可反复验证」：
    * 「他喜欢在深夜聊天」→ 多看几天就知道真假 → **算认知**
    * 「他嘴硬时在试探」  → 永远无法验证      → **算想法，不落库**

这是一套**启发式**规则，不追求 100% 准确。它只保证一件事：**不会把明显的
单次事件、带推断词的推断句、带时效词的临时状态判成认知**。拿不准时宁可
判「不入库」——漏掉一条认知只是少一条记忆，写错一条认知会长期污染召回。

kind 的三种取值（:func:`classify_text`）
---------------------------------------
* ``"cognition"`` —— 认知，进长期记忆库（``kind="cognition"``），走向量检索。
* ``"event"``     —— 发生了什么 / 一次性动作 / **带时效的临时状态**，进 ``events``
  表（状态机，有时效、要排班）。主人 2026-11-06：「他说要带我去看海（承诺）→ 事件」。
* ``"noise"``     —— 不入库。三类：纯语气/太短/无信息；**推断句（想法）**；
  超长句。推断句落在 ``noise`` 是因为它属于「想法」——想法在 ``inner_os`` 队列里
  用完销毁，不落记忆库（1.9「写入流程」）。需要区分「是噪音还是想法」时用
  :func:`is_thought`。

关于「认知和事实同级」
----------------------
主人：「事实也是从流水账里挑出来的」「认知和事实同级」。所以**调用方不要自己
判断一句话该进认知库还是 events 表**——统一走 :func:`ingest_route`，它一次给出
``memory_type`` / ``sink``。客观可数事实（生日、住址）按主人 2026-11-06 明确指示
**仍归 ``cognition``**（「他生日是 11 月 1 日 → 认知」）。

关于 ``action_to_effect`` 里 ``conflict`` 的 embedding 次数
----------------------------------------------------------
``DMN审核.md`` 能力 8 的四种 action 分工表原文：

    ========  ==============  ====================================  ==============
    action    模型产出        框架执行                              embedding 调用
    ========  ==============  ====================================  ==============
    add       新文字          算向量 → 插入                        1 次
    replace   新文字 + 旧 id  算新向量 → 插入；旧向量删掉            1 次
    merge     合并后最终文字 + 目标 id  删旧向量 → 算新向量 → 插入   1 次
    conflict  新文字          各自已有向量，不动                     0 次
    ========  ==============  ====================================  ==============

表里 ``conflict`` 的 0 次，指的是「**不去动已有那两个副本的向量**」，而不是
「新文字不用入库」——矛盾的两条都要留（「两条都留，旧的降权」），新文字仍然是一条
新记录，必须算一次向量才能进索引。所以本模块的处理是：

    conflict → op="insert_conflict"，delete_vectors=[]（旧条不动，0 次删），
               embedding_calls=1（新文字写入需要 1 次 embedding）

理由：表里的「embedding 调用」统计的是**为处理目标旧条而额外付出的向量操作**；
    conflict 省掉的是「删旧 + 重算」这部分（相对 replace/merge 各 0 次额外删除、
    0 次重算），而新增文字的 embedding 是任何 action 都躲不掉的成本。
    如果按 0 次理解，新认知就没有向量，永远召回不到，等于没写库。

原表数值仍以常量 :data:`DMN_ACTION_TABLE` 原样保留，供审计/对账使用；两者
的差异（0 vs 1）就在 ``embedding_calls`` 这一个字段上，含义见上。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "INFERENCE_WORDS",
    "SINGLE_EVENT_WORDS",
    "TRANSIENT_WORDS",
    "is_cognition",
    "is_thought",
    "classify_text",
    "judge",
    "ingest_route",
    "clean_keywords",
]

# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #

#: 推断词：命中即「不可证伪」→ 想法，不是认知（1.9 标准②）。
#: 至少覆盖：大概/也许/可能/似乎/好像/其实/一定是/估计/感觉/应该/八成/说不定。
INFERENCE_WORDS: tuple[str, ...] = (
    "大概",
    "也许",
    "可能",
    "似乎",
    "好像",
    "其实",
    "一定是",
    "估计",
    "感觉",
    "应该",
    "八成",
    "说不定",
    # 同族补充（都是「不可证伪的解读」）
    "或许",
    "兴许",
    "恐怕",
    "想必",
    "看来",
    "像是",
    "推测",
    "我猜",
    "八成是",
    "在心里",
    "内心",
    "潜台词",
    "动机",
    "试探",
    "在赌",
    "装的",
)

#: 单次事件词：句子锚定在某个具体时刻 / 明确是一次性动作 → 只是流水账，
#: 不是「什么是什么」的持续状态（能力 8 判定规则：看句式）。
SINGLE_EVENT_WORDS: tuple[str, ...] = (
    "今天",
    "昨天",
    "前天",
    "刚才",
    "刚刚",
    "这次",
    "那次",
    "这次说",
    "今天说了",
    "今天聊",
    "昨天说了",
    "这周",
    "上周",
    "上个月",
    "今早",
    "今晚",
    "昨晚",
    "明天",
    "后天",
    "刚才说",
    "说了",
    "讲过",
    "聊了",
    "发了",
    "骂了我",
    "夸了我",
    "带我去",
    "答应",
    "承诺",
)

#: 时效性词：本身不是「单次动作」，但会过时（1.9：「他最近很忙」❌，
#: 「"最近"会过时，不符合标准 ②」）。同样不进认知库，归 ``events``（有时效）。
TRANSIENT_WORDS: tuple[str, ...] = (
    "最近",
    "近来",
    "这几天",
    "这两天",
    "这段时间",
    "目前",
    "现在",
    "暂时",
    "这段时间以来",
    "一阵子",
    "这会儿",
    "最近这段",
)


#: 主谓宾的「主语」提示词。判断标准①时用。
_SUBJECT_WORDS: tuple[str, ...] = (
    "他",
    "她",
    "我",
    "你",
    "您",
    "它",
    "主人",
    "晓",
    "宝贝",
    "大家",
)

#: 主谓宾的「谓语」提示词（系动词 / 常见谓词 / 程度副词）。
_PREDICATE_WORDS: tuple[str, ...] = (
    "是",
    "在",
    "有",
    "叫",
    "做",
    "住",
    "喜欢",
    "讨厌",
    "爱",
    "恨",
    "怕",
    "想",
    "会",
    "能",
    "要",
    "习惯",
    "经常",
    "总是",
    "一直",
    "从不",
    "不",
    "很",
    "挺",
    "太",
    "生日",
    "名字",
    "年龄",
    "岁",
    "上班",
    "工作",
    "家",
    "吃",
    "喝",
    "玩",
    "睡",
    "聊",
    "说",
    "养",
    "买",
    "穿",
    "用",
    "学",
    "教",
    "认识",
    "记得",
    "忘",
    "担心",
    "觉得",
    "带",
    "去",
    "来",
    "回",
    "开",
    "听",
    "看",
    "读",
    "写",
    "唱",
)

#: 纯语气 / 应答，无信息量 → ``noise``。
_INTERJECTIONS: tuple[str, ...] = (
    "嗯",
    "嗯嗯",
    "啊",
    "哦",
    "噢",
    "喔",
    "诶",
    "欸",
    "哎",
    "唉",
    "哈",
    "哈哈",
    "哈哈哈",
    "呵",
    "呵呵",
    "嘿",
    "哎呀",
    "好的",
    "好",
    "行",
    "收到",
    "在吗",
    "你好",
    "早",
    "晚安",
    "么么",
    "抱抱",
    "贴贴",
    "呜呜",
    "嘤",
    "草",
)

#: keywords 清洗用的停用词（单字虚词 + 超短词 + 高频功能词）。
_STOPWORDS: frozenset[str] = frozenset(
    """
    他 她 它 我 你 您 咱 们 的 了 着 过 得 地 是 在 有 就 也 都 和 与 及 而
    但 或 把 被 给 让 对 从 到 向 于 为 以 之 其 这 那 个 些 一 二 三 四 五
    六 七 八 九 十 很 太 挺 更 最 还 又 再 才 只 光 便 却 并 若 如 像 似 吧
    呢 吗 嘛 啊 呀 哦 嗯 哈 唉 诶 欸 么 谁 哪 怎 啥 自己 我们 他们 你们 这个
    那个 这些 那些 什么 怎么 这样 那样 这种 那种 因为 所以 如果 但是 而且
    然后 还是 就是 已经 可以 一直 一样 非常 比较 有点 一些 一点 一下 真的
    事情 东西 时候 时间 感觉 觉得 其实 好像 可能 应该 肯定 知道 需要 起来
    出来 下来 上去 里面 外面 这里 那里 今天 昨天 明天 刚才 现在 目前
    """.split()
)

#: 「与 text 无关的超通用词」——即使出现在 keywords 里也踹掉。
_GENERIC_WORDS: frozenset[str] = frozenset(
    """
    情况 问题 方面 部分 一般 大概 也许 似乎 估计 八成 说不定 意义上 某种
    各种各样 之类 等等 以上 以下 总之 反正 随便 无所谓 所有 全部 任何 其他
    一些 有点 相当 特别 十分 极其 略微 稍微 差不多 基本 主要 明显 确实
    内容 句子 话题 对话 消息 文字 记录 数据
    """.split()
)

#: 认知单条长度上限（``parse_cognition_actions`` 里超长直接丢）。
MAX_COGNITION_CHARS = 120
#: 认知单条长度下限（短于此不可能是完整主谓宾陈述）。
MIN_COGNITION_CHARS = 4
#: keywords 默认上限。
DEFAULT_KEYWORD_LIMIT = 6
#: 巩固 prompt 里最多带多少条新对话（防止把 200 条以后的无限堆进去）。
MAX_NEW_TURNS = 200

_CN_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】…—~·\-]+")
_CJK_OR_ALNUM_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]+"
)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _as_text(value: Any) -> str:
    """把任意输入安全地变成去首尾空白的字符串（None / 非字符串 → 空串）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", "ignore").strip()
        except Exception:  # pragma: no cover - 理论上到不了
            return ""
    return ""


def _hits(text: str, words: Sequence[str]) -> list[str]:
    """返回 ``text`` 里出现的词（保持词表顺序，去重）。"""
    found: list[str] = []
    for word in words:
        if word and word in text and word not in found:
            found.append(word)
    return found


def _is_pure_noise(text: str) -> bool:
    """纯语气 / 太短 / 无信息。"""
    if not text:
        return True
    stripped = _EMOJI_RE.sub("", text)
    stripped = _CN_PUNCT_RE.sub("", stripped)
    if not stripped:
        return True
    if len(stripped) < 3:
        return True
    if stripped in _INTERJECTIONS:
        return True
    # 全是同一个字重复（哈哈哈哈）：剥掉重复字后不足 3 个不同字
    if len(set(stripped)) <= 1:
        return True
    return False


def _has_subject_predicate(text: str) -> tuple[bool, str]:
    """标准①形态：是否像主谓宾陈述（什么是什么 / 什么怎么样）。"""
    body = _CN_PUNCT_RE.sub("", text)
    if not body:
        return False, "去掉标点后没有内容"
    subject = next((w for w in _SUBJECT_WORDS if w in body), "")
    if not subject:
        return False, "找不到主语（他/她/我/你…），不像主谓宾陈述"
    tail = body.split(subject, 1)[1] if subject in body else body
    if len(tail) < 1:  # pragma: no cover - 主谓宾至少要有谓语
        return False, "只有主语，没有谓语"
    predicate = next((w for w in _PREDICATE_WORDS if w in tail), "")
    if not predicate:
        return False, f"主语「{subject}」后面找不到谓语（是/在/有/喜欢/讨厌…）"
    return True, f"主谓宾：主语「{subject}」+ 谓语「{predicate}」"


def _numeric_fact(text: str) -> str:
    """客观可数事实的提示（生日、住址这类仍归认知，主人 2026-11-06 明确）。"""
    marks = ("生日", "住在", "住址", "名字", "年龄", "岁", "月", "日", "号", "点")
    return next((m for m in marks if m in text), "")


# --------------------------------------------------------------------------- #
# A. 认知判定
# --------------------------------------------------------------------------- #


def judge(text: str) -> dict:
    """逐条判定一句话：是不是认知、属于哪种 kind、为什么。

    返回 ``{"text": str, "is_cognition": bool, "kind": "cognition"|"event"|"noise",
    "reasons": [中文说明, ...]}``。``reasons`` 会写清命中了/未命中哪一条标准，
    方便审计时对着 ``DMN审核.md`` 1.9 的对照表逐条核对。

    判定顺序（先排除，再确认）：
        1. 噪音（空/太短/纯语气/超长）→ ``noise``
        2. 推断词（标准②不可能满足）→ ``noise``（想法，不落库）
        3. 单次事件词 → ``event``；时效词 → ``event``
        4. 标准①形态（主谓宾）不满足 → ``noise``
        5. 两条都满足 → ``cognition``
    """
    raw = text
    body = _as_text(text)
    reasons: list[str] = []

    if not body:
        return {
            "text": _as_text(raw),
            "is_cognition": False,
            "kind": "noise",
            "reasons": ["空文本：没有任何内容，不入库"],
        }

    # 1. 噪音
    if len(body) > MAX_COGNITION_CHARS:
        return {
            "text": body,
            "is_cognition": False,
            "kind": "noise",
            "reasons": [
                f"句子过长（{len(body)} 字 > {MAX_COGNITION_CHARS}）："
                "一条认知应该是短的主谓宾陈述，长句多半是场景描述"
            ],
        }
    if _is_pure_noise(body):
        return {
            "text": body,
            "is_cognition": False,
            "kind": "noise",
            "reasons": ["纯语气/太短/无信息：不构成主谓宾陈述，不入库"],
        }

    # 2. 推断词（标准②：不可证伪 → 想法）
    inference = _hits(body, INFERENCE_WORDS)
    if inference:
        reasons.append(
            "✗ 标准②可验证性：命中推断词「"
            + "」「".join(inference)
            + "」——这是推断，后续观察无法判断真假，属于想法不落库"
        )
        reasons.append(
            "（对照 1.9：「他嘴硬时在试探」「他大概是把深夜当自己的时间」都是 ❌）"
        )
        return {
            "text": body,
            "is_cognition": False,
            "kind": "noise",
            "reasons": reasons,
        }

    # 3. 单次事件 / 时效
    events = _hits(body, SINGLE_EVENT_WORDS)
    transient = _hits(body, TRANSIENT_WORDS)
    if events:
        reasons.append(
            "✗ 标准②可验证性：命中单次事件词「"
            + "」「".join(events)
            + "」——这是一次性动作/某个时刻发生的事，进流水账或 events 表"
        )
        if _numeric_fact(body):
            reasons.append(
                f"（检测到可数事实「{_numeric_fact(body)}」，但整句锚在具体时刻，"
                "先按事件处理；纯粹的事实句如「他生日是 11 月 1 日」仍归认知）"
            )
        return {"text": body, "is_cognition": False, "kind": "event", "reasons": reasons}
    if transient:
        reasons.append(
            "✗ 标准②可验证性：命中时效词「"
            + "」「".join(transient)
            + "」——这种状态会过时（1.9：「他最近很忙」❌），不进认知库"
        )
        return {"text": body, "is_cognition": False, "kind": "event", "reasons": reasons}

    # 4. 标准①形态
    ok_form, form_reason = _has_subject_predicate(body)
    if not ok_form:
        reasons.append(f"✗ 标准①形态：{form_reason}")
        return {
            "text": body,
            "is_cognition": False,
            "kind": "noise",
            "reasons": reasons,
        }
    reasons.append(f"✓ 标准①形态：{form_reason}")
    reasons.append(
        "✓ 标准②可验证性：不含推断词/单次事件词/时效词，后续观察可以判断真假"
        "（对照 1.9：「可反复验证」是分界线）"
    )
    fact = _numeric_fact(body)
    if fact:
        reasons.append(
            f"✓ 客观可数事实（「{fact}」）：按主人 2026-11-06 的定稿仍归 cognition"
            "（「他生日是 11 月 1 日 → 认知」；认知与事实同级，都从流水账里挑）"
        )
    return {"text": body, "is_cognition": True, "kind": "cognition", "reasons": reasons}


def is_cognition(text: str) -> bool:
    """两条标准都满足才返回 True（见 :func:`judge`）。"""
    return bool(judge(text)["is_cognition"])


def is_thought(text: str) -> bool:
    """是不是「想法」而不是认知——即命中推断词、不可证伪的那种句子。

    这是 1.9 里「认知 vs 想法」的另一半。想法走 ``inner_os`` 队列用完销毁，
    不进记忆库，所以 :func:`classify_text` 把这类句子归到 ``"noise"``；
    需要审计时用本函数把「想法」和「纯噪音」区分开。
    """
    body = _as_text(text)
    if not body or _is_pure_noise(body):
        return False
    return bool(_hits(body, INFERENCE_WORDS))


def classify_text(text: str) -> str:
    """归类：``"cognition"`` / ``"event"`` / ``"noise"``（判定细节见 :func:`judge`）。"""
    return str(judge(text)["kind"])


def ingest_route(text: str) -> dict:
    """给**记忆入库类型**，让调用方不用自己判（主人：「认知和事实同级」）。

    返回 ``{"text","kind","memory_type","sink","reasons"}``：

    ==============  ======================  ==================================
    kind            memory_type             sink
    ==============  ======================  ==================================
    cognition       ``"cognition"``         ``livingmemory.db``（+ FAISS 向量）
    event           ``"event"``             ``events`` 表（状态机，有时效）
    noise           ``None``                ``None``（不落库：语气/想法/超长）
    ==============  ======================  ==================================

    注意：生日、住址这类客观事实**也是** ``cognition``，不要另开一个 fact 类型——
    主人 2026-11-06 定：「事实也是从流水账里挑出来的」「认知和事实同级」。
    """
    verdict = judge(text)
    kind = verdict["kind"]
    memory_type = kind if kind in ("cognition", "event") else None
    sink = None
    if kind == "cognition":
        sink = "livingmemory.cognition"
    elif kind == "event":
        sink = "events"
    return {
        "text": verdict["text"],
        "kind": kind,
        "memory_type": memory_type,
        "sink": sink,
        "reasons": verdict["reasons"],
    }


# --------------------------------------------------------------------------- #
# C. keywords 清洗
# --------------------------------------------------------------------------- #


def _split_keywords(keywords: Any) -> list[str]:
    """把 keywords 输入摊平成字符串列表（容忍 str / 嵌套 / None / 垃圾）。"""
    out: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 3 or value is None:
            return
        if isinstance(value, str):
            for part in re.split(r"[,，、;；|/\s]+", value):
                part = part.strip()
                if part:
                    out.append(part)
        elif isinstance(value, Mapping):
            walk(value.get("keywords"), depth + 1)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, (int, float)):
            out.append(str(value))
        # 其它类型（dict/list 已覆盖）直接忽略

    walk(keywords)
    return out


def _keep_keyword(word: str) -> bool:
    if len(word) < 2:  # 单字虚词 / 超短词
        return False
    if word in _STOPWORDS:
        return False
    if word in _GENERIC_WORDS:
        return False
    if word.lower() in _STOPWORDS:  # pragma: no cover - 中文为主
        return False
    # 全数字（"39"）不是实词
    if word.isdigit():
        return False
    return True


def _fallback_keywords(text: str, limit: int) -> list[str]:
    """清洗后不足 2 个时的兜底：从 text 里按长度取实词。

    分三层（越靠前越像真正的词）：
        1. 用句子中实际出现的停用词把 text 切开，长度 2~6 的片段本身就是词；
        2. 太长的片段取「头 2~3 字」和「尾 2~3 字」（中文复合词多半贴边）；
        3. 还凑不够就退到底线：剩下内容字按 2-gram 切。

    纯启发式，不追求分词准确，只保证「有东西可用、且都来自 text」。
    """
    body = _as_text(text)
    if not body:
        return []
    # 句子中实际出现的停用词（长的优先，避免「自己」被「己」抢先）
    present = sorted(_STOPWORDS.intersection(set(body)), key=len, reverse=True)
    pattern = "|".join(re.escape(word) for word in present) if present else ""

    tier_a: list[str] = []
    tier_b: list[str] = []

    for run in _CJK_OR_ALNUM_RE.sub(" ", body).split():
        pieces = [p for p in re.split(pattern, run) if p] if pattern else [run]
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            if not _keep_keyword(piece):
                continue
            if len(piece) <= 6:
                tier_a.append(piece)
            else:
                for gram in (piece[:2], piece[-2:], piece[:3], piece[-3:]):
                    if _keep_keyword(gram):
                        tier_b.append(gram)

    tier_c: list[str] = []
    if len(tier_a) + len(tier_b) < 2:
        # 底线：把每个内容片段里的停用词剔掉，剩下的内容字按 2-gram 切
        for run in _CJK_OR_ALNUM_RE.sub(" ", body).split():
            squeezed = _CJK_OR_ALNUM_RE.sub("", run)
            if pattern:
                squeezed = re.sub(pattern, "", squeezed)
            for i in range(0, max(0, len(squeezed) - 1)):
                gram = squeezed[i : i + 2]
                if _keep_keyword(gram):
                    tier_c.append(gram)

    candidates = tier_a + tier_b + tier_c
    # 按长度取实词（长的更像完整实词），长度相同保持出现顺序；去重
    ordered = sorted(enumerate(candidates), key=lambda pair: (-len(pair[1]), pair[0]))
    out: list[str] = []
    for _, word in ordered:
        if word not in out:
            out.append(word)
        if len(out) >= limit:
            break
    return out


def clean_keywords(
    keywords: Any, text: str = "", limit: int = DEFAULT_KEYWORD_LIMIT
) -> list[str]:
    """清洗认知的关键词（认知用于关键词浮现）。

    规则（规格 C）：
        * 3~6 个（``limit`` 默认 6）；
        * 去掉停用词（他/她/的/了/是/在/我/你/不/有/就/也/都 这类单字虚词）和
          长度 < 2 的超短词；
        * 去掉与 text 无关的超通用词（情况/问题/一些/感觉…）；
        * 去重、保持顺序；
        * 清洗后不足 2 个 → 从 ``text`` 里按长度取实词兜底。

    输入容忍 str（按逗号/顿号/空格切）、list、None、嵌套 dict。永不抛异常。
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_KEYWORD_LIMIT
    if cap <= 0:
        return []

    out: list[str] = []
    for raw in _split_keywords(keywords):
        word = raw.strip().strip("“”\"'《》【】()（）")
        if not word:
            continue
        if not _keep_keyword(word):
            continue
        if word in out:
            continue
        out.append(word)
        if len(out) >= cap:
            return out

    if len(out) < 2:
        for word in _fallback_keywords(text, cap):
            if word not in out:
                out.append(word)
            if len(out) >= cap:
                break
    return out[:cap]



# --------------------------------------------------------------------------- #
# B. 认知库的写入动作 —— 2026-11-07 已删除（设计被取代）
# --------------------------------------------------------------------------- #
#
# 原 B 段约 400 行：parse_cognition_actions / merge_prompt_block /
# ction_to_effect / _MERGE_SYSTEM_RULES 等。设计是「让模型吐
# add/replace/merge/conflict 四个标签，框架按标签执行」。
#
# 那套东西**从未接过线**（全仓零调用，119 条单测在测一个没人用的函数）。
# 2026-11-07 主人定了新的合并方案取代它：
#
#     新认知 → 向量检索相关旧认知 → 旧认知转文本
#     → **模型直接写最终文本**（不用学四个标签）→ 新文本向量化入库
#     → 先插新的、成功后删旧的
#
# 实现见 memory_bridge.py：
#     MemoryBridge._merge_into_memory()   合并主流程
#     MemoryBridge._ask_model_to_merge()  调模型合成
#     _MERGE_PROMPT                       规则（六条，含矛盾的写法）
#     _parse_merge_reply()                解析「最终文本 + DROP 列表」
#
# 为什么删而不是留着：留着会让人以为认知四动作是活的，而它恰恰是本项目
# 最典型的写完了但没接线的坑（审计 P0-5）。
# 备份在 cognition.py.bak-before-drop-deadcode-20261107。
