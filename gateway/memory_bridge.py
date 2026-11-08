"""DMN 事实 → 长期记忆库 的桥接。

职责
----
系统层巩固时抽出的结构化事实（`_extract_events`），除了进 `events` 状态机，
还要作为**长期记忆**写进 `gateway/memory/`（`MemoryStore`）。

这是「唯一记忆库」的落地：`turns` 是对话日志，`events` 是状态机，
`MemoryStore` 才是跨天能想起来的长期记忆。

文本形状（关键，别改）
--------------------
`MemoryStore.add(text=...)` 的 `text` 是**给检索用的富文本**，不是对话原文：

    叙述句；关键词1；关键词2；关键词3
    └──┬──┘ └────────┬────────┘
       │             └─ 给 BM25 精确命中（"10月20日"、"生日"）
       └─ 给向量语义（"主人讨厌人群和应酬"）

为什么不能直接塞原文：插件那 13 条 `"对话记录"` 空壳就是教训 ——
原文进向量库会被稀释，关键词也会误命中。

会话键（session_id）
-------------------
* `pet` 与主人私聊**归一成 `owner`** —— 主人在哪说都是同一个人，
  桌宠记的事 QQ 私聊里要能想起来。
* 群聊用 `qq_group:<群名>`，**绝不跨群**。
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time

logger = logging.getLogger("xiaoyu.memory")

#: 保护 `MemoryBridge.store()` 的首次打开（2026-11-04 T-06）。
#: 两个线程同时首次进来会各建一份 faiss 索引对象，各写各的文件 —— 数据错乱。
_STORE_LOCK = threading.Lock()

#: 事实类型 → 写进长期记忆时的语气前缀。
#: 只保留「这个人是什么样 / 发生过什么」的事实；care/deferred 那类是状态机的事，
#: 不进长期记忆（它们有时效性，过期就该忘）。
MEMORY_KINDS = ("observation", "promise")

#: 会话键归一：桌宠和主人私聊是同一个人。
OWNER_ALIASES = ("pet", "qq_private")

#: 合并判断的提示词（主人 2026-11-07 定的方案）。
#:
#: 设计原则：**只给规则和原料，判断交给模型**。不要求它吐 add/replace/merge/
#: conflict 四个标签 —— 那是在替框架省事，不是在帮模型。模型只要写一条
#: 最终该存进库的文本，再加一个「旧的哪几条该删」的清单就行。
_MERGE_PROMPT = """你是记忆整理模块。现在有一条**新事实**要存进长期记忆库，
但库里已经有一些**相关的旧记录**。请你判断该怎么处理。

【已经记住的】
{old}

【这次新得到的】
{new}

按下面的规则处理（这些规则是主人定的，照做就行）：

1. **新的是旧的换个说法**（同一件事，只是措辞不同）
   → 输出一条最完整准确的说法，把旧的几条都列进 `drop`。

2. **新事实取代了旧的**（比如「他住上海」→「他搬去北京了」）
   → 输出新说法（可以把变化写清楚），旧的列进 `drop`。

3. **新的是对旧的补充细节**（「他喜欢蓝色」→「他喜欢深蓝色，尤其藏青色」）
   → 合成一条更完整的，旧的列进 `drop`。

4. **两条都成立、只是不矛盾地并存**（「他喜欢独处」和「他最近总找人聊天」）
   → **都值得留**，那你就输出新的那条，`drop` 留空（旧的保持不动）。

5. **两条确实矛盾**（「他说讨厌人群」vs「他天天泡在群里」）
   → **合成一条，把矛盾写进同一句里**（主人 2026-11-07 定）。
   不要丢一边、也不要分裂成两条——一句话里同时容下两边，用「但」「可」这类
   转折把它们连起来，让这条记忆自己带着那个矛盾。
   例：「他说讨厌人群，但天天泡在群里」/ 「他说不过生日，却记得别人生日」
   这种写法有两个好处：召回时一次就能看到全貌；将来再聊到这件事，
   如果话题里出现更明确的信号，那条更准的说法会通过第 2 条规则把它取代掉。
   旧的列进 `drop`。

6. **旧的和新的其实无关**（只是向量碰巧相似）
   → 输出新的那条，`drop` 留空（**旧的完全不动，原样留着**）。

输出格式（严格照这个，不要多写别的）：

第一行：最终要存的那一条（一句话，陈述事实，不要「我觉得」「可能」这类词）
第二行：`DROP: 1,3`（要删的旧记录编号，用【旧N】里的 N；没有就写 `DROP: 无`）

只输出这两行。
"""

def _parse_merge_reply(reply: str, related: list[dict]) -> tuple[str, list[str]] | None:
    """解析模型的合并回复。

    Args:
        reply: 模型原文。
        related: 喂进去的旧记录（用来把编号映射回 doc_id）。

    Returns:
        `(最终文本, 要删的 doc_id 列表)`；解析不出来返回 None
        （调用方按"纯新增"处理 —— **宁可重复，不可误删**）。
    """
    lines = [ln.strip() for ln in str(reply or "").splitlines() if ln.strip()]
    if not lines:
        return None

    final_text = ""
    drop_nums: list[int] = []
    for ln in lines:
        m = re.match(r"^DROP\s*[:：]\s*(.+)$", ln, re.I)
        if m:
            body = m.group(1).strip()
            if body and not re.match(r"^(无|none|no)$", body, re.I):
                for tok in re.findall(r"\d+", body):
                    drop_nums.append(int(tok))
            continue
        if not final_text:
            final_text = ln

    final_text = final_text.strip().strip("。 ")
    # 太短或看起来像格式噪音 → 判失败，走纯新增。宁可重复不可误删。
    #
    # 门槛取 2 而不是 4：中文里「合并后」「他不去」这种 3~4 字的事实是合法的
    # （「他走了」「没人来」都是完整陈述）。真正的格式噪音是空串、单个字、
    # 或者以 DROP/【 开头的行 —— 那些下面单独挡。
    if len(final_text) < 2:
        return None
    if re.match(r"^(DROP|【)", final_text, re.I):
        return None

    drop_ids: list[str] = []
    seen: set[int] = set()
    for n in drop_nums:
        if n in seen or not (1 <= n <= len(related)):
            continue
        seen.add(n)
        doc_id = str(related[n - 1].get("doc_id") or "")
        if doc_id:
            drop_ids.append(doc_id)
    return final_text, drop_ids



def _atom_for_event(ev: dict, text: str, *, session_id: str, kind: str):
    """把一条事件转成记忆原子（带类型 / TTL / 衰减曲线）。

    这是「原子化」在写入路径上的落点。抄自 livingmemory 插件的 `classify_atoms`，
    但**不是**简单调用它 —— 要做两件适配：

      1. **用 evidence 分类，不是用拼好的富文本**。
         `build_memory_text()` 产出的是「主人：我住在重庆；主人；我住在重庆」
         这种给检索用的富文本，里面塞了关键词、还有主语重复。
         拿它去分类会被关键词干扰（比如 "住" 出现两次可能命中别的规则）。
         真正该分类的是**纯事实正文**（evidence）。

      2. **promise 类强制归 PLANNED**。
         抽取模型已经判定它是"承诺"了，那是比正则更可靠的信号。
         正则可能因为句子里没时间词而把它判成 EPISODIC，
         但承诺该有计划和 STEP 衰减（过期就作废）。

    Args:
        ev: 校验过的事件。
        text: `build_memory_text()` 的产出（富文本，用作兜底正文）。
        session_id: 归属会话。
        kind: 事件类型（observation / promise）。

    Returns:
        `MemoryAtom`。
    """
    from atom_classifier import classify_one
    from memory_atom import AtomType, DecayType, compute_ttl

    evidence = str(ev.get("evidence") or "").strip()
    subject = str(ev.get("subject") or "").strip()
    # 优先用原始 evidence 分类；没有就退回富文本的第一段
    body_for_type = evidence or text.split("；", 1)[0]
    importance = 0.7 if kind == "promise" else 0.6

    atom = classify_one(body_for_type, importance=importance, session_id=session_id)
    if atom is None:
        # 极端兜底：连空串都给了，也造一个 UNKNOWN 原子，别让调用方拿到 None
        atom = classify_one(text.split("；", 1)[0] or "未命名事实",
                            importance=importance, session_id=session_id)

    # promise 覆盖成 PLANNED：模型判定比正则可靠
    if kind == "promise" and atom is not None:
        atom.atom_type = AtomType.PLANNED
        event_time = atom.event_time
        ttl, decay = compute_ttl(AtomType.PLANNED, importance, 0, event_time)
        atom.ttl_days = ttl
        atom.decay_type = decay
        atom.expires_at = atom.created_at + ttl * 86400.0

    return atom

    """解析模型的合并回复。

    Args:
        reply: 模型原文。
        related: 喂进去的旧记录（用来把编号映射回 doc_id）。

    Returns:
        `(最终文本, 要删的 doc_id 列表)`；解析不出来返回 None
        （调用方按"纯新增"处理 —— **宁可重复，不可误删**）。
    """
    lines = [ln.strip() for ln in str(reply or "").splitlines() if ln.strip()]
    if not lines:
        return None

    final_text = ""
    drop_nums: list[int] = []
    for ln in lines:
        m = re.match(r"^DROP\s*[:：]\s*(.+)$", ln, re.I)
        if m:
            body = m.group(1).strip()
            if body and not re.match(r"^(无|none|no)$", body, re.I):
                for tok in re.findall(r"\d+", body):
                    drop_nums.append(int(tok))
            continue
        if not final_text:
            final_text = ln

    final_text = final_text.strip().strip("。 ")
    # 太短或看起来像格式噪音 → 判失败，走纯新增。宁可重复不可误删。
    #
    # 门槛取 2 而不是 4：中文里「合并后」「他不去」这种 3~4 字的事实是合法的
    # （「他走了」「没人来」都是完整陈述）。真正的格式噪音是空串、单个字、
    # 或者以 DROP/【 开头的行 —— 那些下面单独挡。
    if len(final_text) < 2:
        return None
    if re.match(r"^(DROP|【)", final_text, re.I):
        return None

    drop_ids: list[str] = []
    seen: set[int] = set()
    for n in drop_nums:
        if n in seen or not (1 <= n <= len(related)):
            continue
        seen.add(n)
        doc_id = str(related[n - 1].get("doc_id") or "")
        if doc_id:
            drop_ids.append(doc_id)
    return final_text, drop_ids


def _ensure_path() -> None:
    """把 `gateway/memory/` 加进 sys.path（它内部是同目录裸 import）。"""
    mem_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
    if mem_dir not in sys.path:
        sys.path.insert(0, mem_dir)


_ensure_path()


def memory_session_key(channel: str) -> str:
    """turns 的渠道 → 长期记忆的会话键。

    主人（pet / 主人私聊）归一成 `owner`；群聊保留全名，物理隔离。
    """
    ch = str(channel or "").strip()
    if ch in OWNER_ALIASES:
        return "owner"
    return ch or "owner"


def build_memory_text(ev: dict) -> str:
    """把一条事件拼成「叙述句；关键词」形状。

    Args:
        ev: `_validate_events` 校验过的事件（kind / subject / evidence /
            approach_hint）。

    Returns:
        可直接喂给 `MemoryStore.add(text=...)` 的富文本。
        没有可用内容时返回空串（调用方据此跳过）。
    """
    evidence = str(ev.get("evidence") or "").strip()
    if not evidence:
        return ""

    subject = str(ev.get("subject") or "").strip()
    kind = str(ev.get("kind") or "")

    # 叙述句：让向量有语义可抓
    if kind == "promise":
        head = f"{subject or '某人'}答应过：{evidence}" if subject else f"有人答应过：{evidence}"
    else:
        head = f"{subject}：{evidence}" if subject else evidence

    # 关键词段：把 evidence 里的实词再摊一遍，供 BM25 精确命中。
    # 这里不调分词（tokenizer 在记忆层里），只做简单的"去重拆分"——
    # 让同一句话在 FTS 里以两种粒度都留下痕迹。
    keys = [subject] if subject else []
    hint = str(ev.get("approach_hint") or "").strip()
    if hint:
        keys.append(hint)
    keys.append(evidence)

    seen: set[str] = set()
    parts: list[str] = []
    for k in keys:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            parts.append(k)
    return "；".join([head] + parts)


def _memory_kind_for(event_kind: str, ev: dict) -> str:
    """抽取事件类型 → 遗忘算法认的记忆类型（`forgetting.DEFAULT_STABILITY_DAYS`）。

    只做映射，不判语义：`promise` 永不删、`observation` 按一般观察 30 天。
    真正的"认知 vs 事实"判定在 `cognition.py`（那需要读句子），
    这里不重复实现一套，避免两处标准打架。
    """
    kind = str(event_kind or "").strip()
    if kind == "promise":
        return "promise"
    return "observation"


def _event_ts(ev: dict) -> float:
    """这条记忆的**创建时间**：以对话时间为准（主人 2026-11-06 定稿）。

    `_validate_events` 现在会把这一批 turns 里最早/最新的 `ts` 带在事件上
    （`ev["ts"]`）。拿不到才退回现在 —— **退回现在比退回 0 安全**：
    0 会被遗忘算法当成"缺时间戳 → 不删"，等于这批记忆永远不过期。
    """
    raw = ev.get("ts")
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return time.time()
    # 明显不合法的时间（0 / 负数 / 未来太远）一律退回现在
    if stamp <= 0 or stamp > time.time() + 86400:
        return time.time()
    return stamp


class MemoryBridge:
    """把事件批量写进长期记忆库。失败不抛穿（巩固不能被记忆写入拖死）。

    Args:
        db_path: 记忆库路径。默认 `gateway/data/livingmemory.db`。
        enabled: 总开关。关掉时 `write_events` 直接返回 0。
    """

    def __init__(
        self,
        db_path: str | None = None,
        index_path: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "livingmemory.db"
        )
        self.index_path = index_path
        self.enabled = bool(enabled)
        self._store = None
        self._error: str | None = None

    # ------------------------------------------------------------------ 惰性打开
    def store(self):
        """懒加载 `MemoryStore`。失败返回 None 并记下原因（不抛）。

        ⚠ 2026-11-04（T-06）：加了锁。原来是裸的双检，两个线程同时首次进来
        会各自 `open_store()` 一次 —— 而 `open_store` 内部会建 faiss 索引对象。
        **两个索引对象各写各的文件**，正是 `loop_bridge.py` 自己警告的那种
        "真正的数据错乱来源"。锁住之后只会建一份。
        """
        if not self.enabled:
            return None
        if self._store is not None:
            return self._store
        with _STORE_LOCK:
            if self._store is not None:   # 二次检查（可能在等锁期间被建好了）
                return self._store
            try:
                import store as store_mod  # noqa: PLC0415 —— 惰性，避免 import 期拉起 faiss

                self._store = store_mod.open_store(
                    db_path=self.db_path,
                    index_path=self.index_path,
                )
                self._error = None
            except Exception as exc:  # noqa: BLE001 —— 记忆库打不开不该拖垮巩固
                self._error = f"{type(exc).__name__}: {exc}"
                logger.warning("长期记忆库打开失败: %s", self._error)
                return None
            return self._store

    def health(self) -> dict:
        """索引一致性摘要（给体检用）。**绝不抛**，库打不开就返回 ok=False + error。

        为什么不复用 `store()` 的异常路径：体检本身不该因为"库打不开"而失败，
        它需要的恰恰是"库打不开"这个结论。
        """
        store = self.store()
        if store is None:
            return {"ok": False, "error": self._error or "记忆库未启用或打不开"}
        try:
            return store.health()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @property
    def last_error(self) -> str | None:
        return self._error

    def close(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            finally:
                self._store = None

    # ------------------------------------------------------------------ 写
    def write_events(self, events: list[dict], channel: str) -> int:
        """把一批事件写进长期记忆。返回成功写入**原子**条数。

        Args:
            events: `_validate_events` 的结果。
            channel: turns 的原始渠道（pet / qq_private / qq_group:<名>）。

        只写 `MEMORY_KINDS` 里的类型 —— care/deferred 那类有时效性，
        该由 `events` 状态机管，不该沉淀成"长期记忆"。

        ⚠ 2026-11-07 两步改造（都是主人定的）：

        **① 原子化（抄自 livingmemory 插件）**
        每条事件先过 `atom_classifier` 变成**带类型的原子**：
        类型决定初始 TTL 和衰减曲线 —— 「他住在重庆」（factual，180 天）
        和「今天加班到十点」（episodic/planned，2~7 天）该有完全不同的寿命。
        原来是所有记忆一律走同一套 `e^(-t/S)`，那是错的。

        **② 合并写**
        每条原子写之前先向量检索相关旧认知、交给模型合成最终文本，
        **先插新的、成功后再删旧的**。不做的话同一件事每巩固一次就多一条。

        Returns:
            成功写入的原子条数（不是事件条数 —— 一条事件可能拆成多个原子）。
        """
        if not events or not self.enabled:
            return 0
        store = self.store()
        if store is None:
            return 0

        session_id = memory_session_key(channel)
        written = 0
        for ev in events:
            if str(ev.get("kind") or "") not in MEMORY_KINDS:
                continue
            text = build_memory_text(ev)
            if not text:
                continue
            kind = str(ev.get("kind") or "")
            evidence = str(ev.get("evidence") or "").strip()
            try:
                # ② 原子化：一条事件 → 一条原子（带 type/ttl/decay）
                atom = _atom_for_event(ev, text, session_id=session_id, kind=kind)
                meta = {
                    "session_id": session_id,
                    # 保留原始渠道，方便查证（和 events.channel 一个口径）
                    "source_channel": str(channel or ""),
                    "importance": 0.7 if kind == "promise" else 0.6,
                    "topics": [kind],
                    "key_facts": [evidence][:5],
                    "memory_origin": "dmn_extract_events",
                    # 遗忘用的类型标记（`plan/DMN审核.md` 3.7「写入时」）：
                    # 抽取模型顺手打类型，**不额外花钱**。没有它，遗忘算法
                    # 只能给所有记忆同一套衰减参数——承诺和「今天很累」
                    # 一个待遇，那是错的。
                    "kind": _memory_kind_for(kind, ev),
                    # 创建时间**以对话时间为准**（主人 2026-11-06 定稿）：
                    # 用流水账里的 ts，不用提炼时间。攒了 4 天才巩固的那批
                    # 如果按提炼时间算，会显得很新，遗忘就永远删不掉它。
                    "create_time": _event_ts(ev),
                    # ① 原子化的产出：类型 / TTL / 衰减曲线 / 过期时间 /
                    # 分类置信度。遗忘算法按这些字段判寿命，不再一刀切。
                    **atom.to_dict(),
                }
                if self._merge_into_memory(store, text, meta):
                    written += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不拦整批
                logger.warning("写入长期记忆失败: %s", exc)
                self._error = f"{type(exc).__name__}: {exc}"
        return written

    # ------------------------------------------------------- 合并写（P0-5）

    def _find_related(self, store, text: str, top_k: int = 3) -> list[dict]:
        """向量检索：找出和 `text` 相关的旧认知（**不设相似度门槛**）。

        为什么不设门槛：向量分数在不同模型/不同文本长度下不是可比的绝对量。
        设 0.9 只会匹配到几乎逐字相同的那种（换个说法就漏），
        设 0.3 会把「他喜欢蓝色」和「他讨厌人群」凑成一对，逼模型合并
        两件不相干的事。**取 top-k 交给模型判断相关性** —— 模型比阈值聪明。

        这是主人定的原则：框架不做语义判断，只提供原料。
        """
        try:
            hits = store.search(text, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 —— 检索失败就当没有相关的
            logger.warning("合并前检索失败（按无相关处理）: %s", exc)
            return []
        # 只保留同一会话的：owner 的认知不该和群聊的合并
        out = []
        for h in hits or []:
            meta = h.get("metadata") or {}
            if str(meta.get("session_id") or "") != str(
                    (h.get("_session_id") or meta.get("session_id") or "")
            ):
                # 正常情况走下面那段；这里只是防御脏数据
                pass
            out.append(h)
        return out

    def _merge_into_memory(self, store, text: str, meta: dict) -> bool:
        """把一条新事实写进库；如果有相关旧认知，先让模型合成再写。

        主人的方案（2026-11-07）：
            新认知诞生 → 框架用向量库匹配旧认知 → 旧认知转成文本
            → 语言模型读「旧文本 + 新文本」→ 按规则输出最终文本
            → 新文本重新向量化注入向量库

        Returns:
            True = 写入了（不管是新增还是合并后的更新）。

        顺序铁律：**先插新的，成功了再删旧的**。
        反过来的话，embedding 一失败就两条都没了。宁可短暂重复，不可丢失。
        """
        related = self._find_related(store, text)
        # 同会话过滤：owner 的认知不能和群聊的合并（跨场合泄漏）
        same_session = [
            h for h in related
            if str((h.get("metadata") or {}).get("session_id") or "")
            == str(meta.get("session_id") or "")
        ]

        final_text = text
        drop_ids: list[str] = []

        if same_session:
            merged = self._ask_model_to_merge(text, same_session)
            if merged is not None:
                final_text, drop_ids = merged

        # ① 先插（算向量）。失败就直接往上抛，让调用方记失败 —— 此时旧数据完好。
        store.add(text=final_text, metadata=meta)

        # ② 插入成功后才删旧的
        for doc_id in drop_ids:
            try:
                store.delete(doc_id)
            except Exception as exc:  # noqa: BLE001 —— 删不掉只留下重复，不丢数据
                logger.warning("合并后删除旧认知失败 doc_id=%s: %s", doc_id, exc)
        return True

    def _ask_model_to_merge(
        self, new_text: str, related: list[dict]
    ) -> tuple[str, list[str]] | None:
        """让模型读「旧文本 + 新文本」，给出最终该存的那一条。

        Returns:
            `(最终文本, 要删的旧 doc_id 列表)`；模型没答/答乱时返回 None
            （调用方按"纯新增"处理 —— **宁可重复，不可误删**）。

        ⚠ 这里刻意用**一段文本输出**而不是让模型吐 add/replace/merge/conflict
        四个标签：主人 2026-11-07 定的原则是「模型有智慧，让它自己写最终文本，
        框架只负责删旧插新」。让模型额外学四个标签是给框架省事，不是给模型省事。
        """
        import system as _system  # noqa: PLC0415 —— 循环导入，惰性

        if not related:
            return None

        old_block = "\n".join(
            "【旧%d】%s" % (i + 1, str(h.get("text") or "").split("；", 1)[0].strip())
            for i, h in enumerate(related)
        )
        prompt = _MERGE_PROMPT.format(old=old_block, new=new_text.split("；", 1)[0].strip())

        try:
            reply, _meta, _calls = _system.call_system(
                [{"role": "user", "content": prompt}],
                tool_choice="none",
                max_tokens=200,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("合并判断调用失败（按纯新增处理）: %s", exc)
            return None
        if not reply:
            return None

        final = _parse_merge_reply(str(reply), related)
        return final

    def write_thoughts(self, thoughts: list[str], channel: str, *,
                       origin: str = "daily_review", importance: float = 0.55,
                       cognition_only: bool = True) -> int:
        """把每日梳理期产出的**认知**沉淀进长期记忆（P0-5 的 B+C 分流）。

        为什么需要这个（2026-11-03 22:20 定，P0-5）：

        `inner_os` 是**一次性提醒** —— `consume_inner_os()` 读走就标 delivered，
        之后再没有任何一处把它重新注入 prompt，直到 7 天后过期。
        所以如果梳理期只管往 inner_os 扔念头，就等于「每天重新想一遍同样的事」，
        三天后她可能又犯同样的错。

        分流规则：
          * **「他是什么样的人」** → 进 people 画像（每轮都带，不会漏，见 `_apply_profile_hint`）
          * **「该怎么说 / 什么时候别说」** → 进这里（长期记忆，按相关度召回）

        ⚠ 2026-11-06 加 `cognition_only`（`plan/DMN审核.md` 1.9 的明确修正）
        ------------------------------------------------------------------
        主人定的准入标准是「过了产生它的那个场景，还有没有用」，两条都要满足：
        ① 主谓宾形态 ② 可验证（能通过后续观察判断真假）。

        **按新架构，反刍/预演的产出大部分不落库**：

            旧代码里 `sink_texts` 把反刍/预演的**全部产出**都写进记忆库，
            而它们的 prompt 明确要求产出场景推断
            （反刍的 prompt 里示例就写着「他今天话说得少，可能累了」——这是想法，不是认知）。
            按新架构：**这类内容不落库。**

        所以现在默认先用 `cognition.judge()` 过一道：判成认知的才写，
        且带上 `kind="cognition"` + `keywords`（关键词浮现要用）。
        判成「想法」的**不是丢弃** —— 它们已经在 inner_os 队列里，用完即销毁，
        那正是主人要的"想法上浮了就销毁"。

        Args:
            thoughts: 念头文本列表。
            channel: turns 的原始渠道；`memory_session_key()` 会归一（主人跨场合）。
            origin: 写进 metadata 的来源标记，便于查证和将来拆分。
            importance: 重要度。分寸类认知比一般观察更该留住，但不该压过 promise。
            cognition_only: 只写符合认知准入标准的（默认 True）。关掉就退回旧行为，
                把每条念头都写进库——留这个开关是为了出问题时能立刻回滚。

        Returns:
            成功写入条数。
        """
        if not thoughts or not self.enabled:
            return 0
        store = self.store()
        if store is None:
            return 0

        import cognition  # noqa: PLC0415 —— 惰性，避免 import 期多拉一个模块

        session_id = memory_session_key(channel)
        written = 0
        skipped = 0
        for txt in thoughts:
            body = str(txt or "").strip()
            if not body:
                continue
            kind = "observation"
            keywords: list[str] = []
            if cognition_only:
                verdict = cognition.judge(body)
                if not verdict.get("is_cognition"):
                    # 场景推断 / 单次事件 / 噪音：不落库。
                    # **不是丢**：它已经（或即将）进 inner_os，用完即销毁。
                    skipped += 1
                    continue
                keywords = cognition.clean_keywords([], body)
            # 「叙述句；关键词1；关键词2」—— 与 `build_memory_text` 同一形状，
            # 混合检索两条路径都靠它，形状不对会让 FTS 侧召不回来。
            extra = "；" + "；".join(keywords) if keywords else "；梳理期念头；分寸"
            text = f"{body}{extra}"
            try:
                store.add(
                    text=text,
                    metadata={
                        "session_id": session_id,
                        "source_channel": str(channel or ""),
                        "importance": float(importance),
                        "topics": ["daily_review", "分寸"],
                        "key_facts": [body[:120]],
                        "memory_origin": origin,
                        # 遗忘算法认的类型（认知类 90 天，比一般观察更耐忘）
                        "kind": "cognition" if cognition_only else kind,
                        # 上浮关键词：对话里出现这些词时这条认知该浮现
                        "keywords": keywords,
                    },
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不拦整批
                logger.warning("写入梳理期念头失败: %s", exc)
                self._error = f"{type(exc).__name__}: {exc}"
        if skipped:
            logger.info("梳理期念头 %d 条判为「想法」不落库（只进 inner_os）", skipped)
        return written

    # ------------------------------------------------------------------ 读
    def recall(self, query: str, channel: str, top_k: int = 3) -> list[dict]:
        """按会话键召回（本场合 + 主人跨场合）。失败返回空列表。

        2026-11-06：召回命中后**顺手记一次「被用到」**（`use_count += 1` /
        `last_used`）。这是「越用越熟」的前置条件 —— 以前召回一条记忆时，
        除了把内容塞进 prompt 什么都没记，于是"被用过"这件事根本不存在，
        遗忘算法只能拿"写入时间"当唯一依据（`plan/DMN审核.md` 3.7）。

        只对**真的返回给模型的那几条**记账（不是全部命中）：
        没进 prompt 的记忆没有"被用到"，给它加权重就是假的。
        """
        if not self.enabled or not str(query or "").strip():
            return []
        store = self.store()
        if store is None:
            return []
        session_id = memory_session_key(channel)
        try:
            hits = store.search_hybrid(query, top_k=top_k * 2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("长期记忆召回失败: %s", exc)
            return []
        out: list[dict] = []
        for hit in hits:
            # `MemoryStore` 已保证 metadata 是 dict（两条检索路径形状统一）。
            meta = hit.get("metadata") or {}
            # 会话隔离：只留本场合（群聊）或 owner（主人跨场合）
            if str(meta.get("session_id") or "") != session_id:
                continue
            out.append(hit)
            if len(out) >= top_k:
                break
        for hit in out:
            doc_id = str(hit.get("doc_id") or "")
            if not doc_id:
                continue
            try:
                store.touch_recall(doc_id)
            except Exception:  # noqa: BLE001 —— 统计失败不拦召回
                pass
        return out

    # ------------------------------------------------------------------ 遗忘
    def forget_plan(self, dry_run: bool = False) -> dict:
        """每日遗忘扫描（**纯代码、免费、不调模型**），返回计划 + 执行结果。

        判据来自 `forgetting.plan_prune()`（`plan/DMN审核.md` 3.7）：
        指数衰减 `R=e^(-t/S)`、按类型给初始稳定性、召回次数折算进稳定性、
        永不删类型只拉高稳定性不删、中间区间留给模型判。

        为什么**不再用**旧的 `prune()`：旧判据是「够旧 且 importance < 0.3」，
        而 importance 是**写死的常数**（0.6/0.7/0.55），最低的 0.55 也比门槛高
        83% —— 那个门槛永远跨不过去。实测 29 次 prune、扫了 7 条、matched 恒为 0。
        也就是说"遗忘"这一项**在生产上从来没真正工作过**。

        参数从 `config.json` 的 `system` 段读（见 `forgetting.thresholds`），
        没配就用默认：keep_above=0.5 / drop_below=0.05 / per_recall_factor=1.5。

        Args:
            dry_run: 只出计划不真删（**也不拉高稳定性**）。

        Returns:
            `{"scanned","drop","deleted","review","keep","boosted",
            "boost_planned","dry_run"}`；库打不开返回空统计（不抛）。

            `boosted` = **真的写进库**的条数（P0-6 起）；`boost_planned` =
            plan 里算出来的条数。正常情况下两者相等，不等说明有 doc 刚被删、
            或者它当前的 `stability_days` 已经更高（boost 只往上拉）。
        """
        empty = {"scanned": 0, "drop": 0, "deleted": 0, "review": 0,
                 "keep": 0, "boosted": 0, "boost_planned": 0,
                 "dry_run": bool(dry_run)}
        if not self.enabled:
            return empty
        store = self.store()
        if store is None:
            return empty
        import forgetting  # noqa: PLC0415 —— 惰性，避免 import 期多拉一个模块

        try:
            import system as _system  # noqa: PLC0415 —— 循环导入

            cfg = forgetting.thresholds(_system.SYSTEM)
        except Exception:  # noqa: BLE001
            cfg = forgetting.thresholds()
        try:
            docs = store.list_documents()
        except Exception as exc:  # noqa: BLE001
            logger.warning("遗忘扫描读不到文档: %s", exc)
            self._error = f"{type(exc).__name__}: {exc}"
            return empty
        plan = forgetting.plan_prune(docs, **cfg)
        drop_ids = list(plan.get("drop") or [])
        deleted = 0
        if not dry_run:
            for doc_id in drop_ids:
                try:
                    if store.delete(doc_id):
                        deleted += 1
                except Exception as exc:  # noqa: BLE001 —— 单条失败不拦整批
                    logger.warning("遗忘删除失败 doc_id=%s: %s", doc_id, exc)
        # ── P0-6：把「永不删类型拉高稳定性」真的写回库 ────────────────────
        #
        # 审计实测：`plan_prune` 对 keep_forever 且 R 掉到 drop_below 以下的
        # 条目会算 `boost_to_stability_days`，但这里**只读 `stats["boosted"]`
        # 计数、从不写回 metadata**。于是"不删"是生效的、"越久越稳固"是空转的
        # —— 下次扫描 R 还是低，每次都重算一遍，保护只剩半条。
        #
        # 为什么写 `stability_days`：`forgetting.stability_for()` 的优先级①
        # 就是它，写进去下次扫描立刻生效（见 `MemoryStore.bump_stability`）。
        #
        # `dry_run` 时**绝不写**：这个参数的全部意义就是"只出计划不动库"，
        # 而且验收工具/排查都会用它。
        boosted = 0
        if not dry_run:
            for row in plan.get("rows") or []:
                target = row.get("boost_to_stability_days")
                if target is None:
                    continue
                doc_id = str(row.get("doc_id") or "").strip()
                if not doc_id:
                    continue
                try:
                    if store.bump_stability(doc_id, float(target)):
                        boosted += 1
                except Exception as exc:  # noqa: BLE001 —— 单条失败不拦整批
                    logger.warning("拉高稳定性失败 doc_id=%s: %s", doc_id, exc)
        return {
            "scanned": len(docs),
            "drop": len(drop_ids),
            "deleted": deleted,
            "review": len(plan.get("review") or []),
            "keep": len(plan.get("keep") or []),
            # `boosted` 现在是**真的写进去的条数**，不再是计划里算出来的条数。
            # 两者正常情况下相等；不相等（比如 doc 刚被删、或已经更高）时
            # 这个数才是"这次真的发生了什么"，比计划数更有用。
            "boosted": boosted,
            "boost_planned": int((plan.get("stats") or {}).get("boosted") or 0),
            "dry_run": bool(dry_run),
        }

    def review_candidates(self, limit: int = 10) -> tuple[str, list[str]]:
        """中间区间（拿不准的）那批 → 给模型看的提示词 + 候选 id。

        主人的分工（3.7 末节）：

            系统层（有时钟）→ 算 R、筛出"该忘了"的候选
            模型（无时钟）→ 只从候选里挑"内容上确实没价值"的

        所以这里**不把时间给模型**，问法只能是「哪些她以后再也不会用到」。

        ⚠ 2026-11-07：这个函数原来**生产零调用**（审计 G.2 第 1 条 / 板块 J ②）：
        遗忘的三档里，`keep` 和 `drop` 都走 `forget_plan()`，只有中间的 `review`
        档写了 40 行却没人接 —— 等于「问模型」那一档永远不执行，只能等 R 掉到
        `drop_below` 硬删。现在接到巩固里了，见 `system._maybe_forget_review()`。

        Returns:
            `(提示词, 候选 doc_id 列表)`。提示词为空 = 这轮没有中间档候选。
        """
        if not self.enabled:
            return "", []
        store = self.store()
        if store is None:
            return "", []
        import forgetting  # noqa: PLC0415

        try:
            docs = store.list_documents()
        except Exception:  # noqa: BLE001
            return "", []
        plan = forgetting.plan_prune(docs)
        review_ids = set(plan.get("review") or [])
        if not review_ids:
            return "", []
        picked = [d for d in docs if str(d.get("doc_id") or "") in review_ids]
        return forgetting.review_prompt(picked, max_items=limit)

    def forget_review_items(self, limit: int = 10) -> list[dict]:
        """中间档候选的**结构化**版本：`[{"id", "text"}]`，直接进巩固 payload。

        为什么在 `review_candidates()` 之外再开一个（2026-11-07）
        ------------------------------------------------------
        `review_candidates()` 返回的是**一整段拼好的提示词**（
        里面写着「只写文档 id，每个 id 后面写「删」或「留」」），那是给
        **独立一次模型调用**设计的问法。而我们要把它接进巩固那次调用里，
        那里已经有 `system_consolidate` 这一个 tool-call 出口了 —— 再塞一整段
        自由文本问答进去，模型会两套格式打架（实测过的失败模式：它把
        「m1 删」写进了 `insights[0].text`）。

        所以这里只取**数据和问法**，答案通过 `system_consolidate.forget_drop`
        这个 schema 字段回来（和 `plan_verdicts` 同一套机制）。问法口径不变：
        仍然只说「她以后再也不会用到」，**不给任何时间词**（`forgetting.
        review_prompt()` 那一段注释解释了为什么）。

        ⚠ 必须复用 `review_candidates()` 的候选筛选，不要在这里重算一遍 R：
        两处口径一旦漂移，就会出现"问了模型的是这批、删的时候按另一批算"。
        所以直接调它拿 id，再去 store 里补文本。

        Returns:
            `[{"id": doc_id, "text": 截断后的正文}]`；没候选 / 库打不开返回 `[]`。
        """
        _prompt, ids = self.review_candidates(limit=limit)
        if not ids:
            return []
        store = self.store()
        if store is None:
            return []
        import forgetting  # noqa: PLC0415

        try:
            docs = store.list_documents()
        except Exception:  # noqa: BLE001
            return []
        by_id = {str(d.get("doc_id") or ""): d for d in docs}
        out: list[dict] = []
        for doc_id in ids:
            doc = by_id.get(str(doc_id)) or {}
            text = str(doc.get("text") or "").replace("\r", " ").replace("\n", " ").strip()
            # 与 `forgetting.review_prompt()` 同一个截断口径：只给内容，
            # 不给"叙述句；关键词"里那一堆关键词（省 token，也是隐私边界）。
            head = text.split("；", 1)[0].strip() or text
            limit_chars = forgetting.REVIEW_TEXT_LIMIT
            if len(head) > limit_chars:
                head = head[:limit_chars] + "…"
            out.append({"id": str(doc_id), "text": head})
        return out

    def apply_review(self, reply: str, candidate_ids: list[str]) -> dict:
        """模型的「留/删」判定 → 真的删。只认候选里的 id（防幻觉）。"""
        empty = {"dropped": 0, "kept": 0, "ok": False}
        if not candidate_ids:
            return empty
        store = self.store()
        if store is None:
            return empty
        import forgetting  # noqa: PLC0415

        parsed = forgetting.parse_review_reply(reply, candidate_ids)
        if not parsed.get("ok"):
            # 乱码 / 答非所问：**全留**。宁可多记，也不能因为一次解析失败
            # 把一批记忆批量删掉——那是不可逆的。
            return {"dropped": 0, "kept": len(candidate_ids), "ok": False}
        dropped = 0
        for doc_id in parsed.get("drop") or []:
            try:
                if store.delete(doc_id):
                    dropped += 1
            except Exception:  # noqa: BLE001
                pass
        return {"dropped": dropped, "kept": len(parsed.get("keep") or []), "ok": True}

    def prune(self, dry_run: bool = False) -> dict:
        """遗忘：删掉「够旧 **且** 不重要」的长期记忆。失败返回空统计。

        参数从 `config.json` 的 `system` 段读：
        * `long_memory_max_age_days`（默认 90）
        * `long_memory_min_importance`（默认 0.3）

        设 `long_memory_max_age_days <= 0` 就整个关掉遗忘（保留全部）。
        """
        empty = {"scanned": 0, "matched": 0, "deleted": 0, "kept_important": 0}
        if not self.enabled:
            return empty
        import system as _system  # noqa: PLC0415 —— 循环导入，函数内取

        try:
            days = float(_system.SYSTEM.get("long_memory_max_age_days", 90))
        except (TypeError, ValueError):
            days = 90.0
        if days <= 0:
            return empty
        try:
            min_imp = float(_system.SYSTEM.get("long_memory_min_importance", 0.3))
        except (TypeError, ValueError):
            min_imp = 0.3

        store = self.store()
        if store is None:
            return empty
        try:
            return store.prune(
                older_than_days=days, min_importance=min_imp, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 —— 遗忘失败不拦巩固
            logger.warning("长期记忆清理失败: %s", exc)
            self._error = f"{type(exc).__name__}: {exc}"
            return empty


def build_recall_block(hits: list[dict]) -> str:
    """把召回结果拼成一段 system 文本。

    措辞上要让她知道"这是你记得的事"，而不是"这是别人给你的资料"。
    """
    texts = [str(h.get("text") or "").strip() for h in hits]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    lines = ["（这些是你自己记得的事，不是别人刚告诉你的，自然地用，不用复述）"]
    for t in texts:
        # 去掉关键词段，只留叙述句，读起来才像回忆
        head = t.split("；", 1)[0].strip()
        if head:
            lines.append("- " + head)
    return "\n".join(lines)
