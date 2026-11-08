"""turns 表的来源（channel）列：来源键工具 + 一次性迁移。

背景（架构评审报告 H4）
------------------------
`turns` 表原来只有一个 `path` 列（`chat` / `tool` / `approval` / `approve`），
`recent_turns(..., hide_private=True)` 只滤掉 `tool/approval/approve` 三类。
于是 `path="chat"` 的普通私聊、以及桌宠那条路的原话，照样会进群聊请求的
30 条上下文里 —— 群友问一句「把上面第 3 条念出来」就能突破。来源只做了
「标注」（`[群聊·xx]` / `[私聊]` / `主人:` 前缀），没做「隔离」。

本模块给 `turns` 加一列 `channel`，值域：

    pet                  桌宠（本机 /chat，唯一可信来源）
    qq_private           QQ 私聊
    qq_group:<群名>      QQ 群聊（带群名）
    qq_group              QQ 群聊（没取到群名）
    qq_unknown           QQ 请求但取不到来源标签 —— 不可信，过滤时什么都看不到
    internal             插件注入的「任务说明」（含整段对话转写）—— 任何上下文都不进
    NULL                 迁移时判不出来的零散老行

读取侧的规则是 **fail-closed**：只有 `pet` 能读全部记忆，其余来源一律
`channel = 自己` 等值匹配，**NULL 不放行**。所以判断不出来的行等于不可见，
而不是「可能泄漏」。这一点是 H4 第一版的反面教材：第一版对 NULL 放行，
结果真实库里 30 条群聊上下文有 8 条是 NULL、占了 96.6% 的字符，其中一条
9605 字符是私聊全量转写 —— 隔离形同虚设。

⚠ 顺序约束（**硬规则，别颠倒**）
--------------------------------
**代码先提交，再迁库。** 不允许「先迁库、后提交代码」。

原因：数据库一旦写入新来源键（比如 `internal`），它就只能被**认识这个键**
的代码正确解释。如果代码还停在旧 commit，那份代码会把 `internal` 当成
「认不出来的来源」或者更糟——当成普通 `qq_private` 放行，于是本该被永久
排除的内部转写又回到上下文里。此时库比代码「超前」了一个版本，而任何
已提交的代码都产不出这个状态，`git revert` 也救不回来（revert 只管代码）。

所以：改 `channel_key` / `infer_from_content` / 新增来源键 → 先 commit，
再在真实库上跑 `migrate()`。

⚠ 回滚注意：`channel` 列是「隔离」的唯一依据。如果只回滚代码（回到基线
commit）而不动数据库，基线的 `add_turn` 只写 5 列、新行 channel 恒为 NULL，
隔离会**静默失效**（读的那一侧还是旧代码，本来也不看这一列）。要真正回到
旧行为，代码和数据库都要处理；只回滚代码等于「假装修过」。

`internal` 这个来源
-------------------
插件会往对话里注入「任务说明」类消息（livingmemory 的 `# 任务说明`），
正文是**整段历史对话的转写**，不是聊天内容。它一旦进上下文，等于把被
转写的那部分原样搬进了当前场合——群聊里问一句就能捞到私聊转写。

因此 `internal` 的语义是**任何上下文都不进**，包括：

- 群聊（`qq_group:*`）—— 精确匹配本来就不命中；
- 陌生人的私聊 —— 精确匹配不命中；
- **桌宠 `pet`** —— 桌宠平时看全部，但 `internal` 是唯一例外；
- **`local_pet=True`** —— 同上，同一条无条件排除。

实现上 `recent_turns` 里那一条 `and coalesce(channel,'') <> 'internal'` 是
**无条件**拼进 SQL 的（在来源过滤之前），所以不存在「某条分支漏掉」的可能。
测试 `test_internal行在任何上下文都不可见` 参数化了上面这几种场合
（私聊/群聊 × hide_private 真/假 + 桌宠），逐一断言看不到 `# 任务说明`。

为什么单独一个文件
------------------
迁移要能在**数据库副本**上先跑一遍验证，而 `gateway.py` 一 import 就会读
`secrets.json`（含云端密钥）。所以把纯逻辑放这里：既不重复实现，又允许

    python migrate_channel.py <db 路径>

在副本上离线验证。gateway.py 在 init_db() 里调 `migrate(DB_PATH)`。

幂等性
------
- `PRAGMA table_info(turns)` 检测列是否存在，不存在才 `ALTER TABLE ADD COLUMN`；
- 回填只写 `channel is null` 的行，且只在能从内容反推出来源时才写；
- 连跑两次：第二次 added_column=False、from_prefix=0、inherited=0，不报错。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

# 来源键常量。写库、读库、回填三处共用，避免各处拼字符串拼歪。
CHANNEL_PET = "pet"
CHANNEL_QQ_PRIVATE = "qq_private"
CHANNEL_QQ_GROUP = "qq_group"
# 「QQ 那条路但取不到来源标签」——群名没显示、User ID 也没带。
# 它**不可信**：不能当成桌宠（否则群里的一句会拿到全部私聊上下文）。
# 单独给一个键，过滤时等值匹配，等于什么都看不到 —— 这是有意的 fail-closed。
CHANNEL_QQ_UNKNOWN = "qq_unknown"
# 插件自己注入的「任务说明」——不是聊天，是 livingmemory 的记忆总结请求，
# 正文里带着整段对话的转写（实测三条分别 7303 / 4072 / 9605 字符）。
# 它不该进任何上下文，包括桌宠，所以读取侧无条件排除。
CHANNEL_INTERNAL = "internal"
# 「系统层做过什么」的流水账（2026-11-06 新增，见 `plan/DMN审核.md` 能力 5 第四节）。
#
# 为什么必须新开一个渠道，而不是复用 `internal`：
#   * `internal` 的语义是「任何上下文都不进」（它是插件塞进来的整段对话转写，
#     不是聊天），读取侧无条件排除；
#   * 而 `system_log` 的语义**相反**：系统操作记录必须让模型看到，
#     主人定的核心机制就是「有啥要判断的扔流水账里，跟系统层说一嘴就一起判断了」。
# 两者语义正交，混用会让"系统操作可见"和"内部转写不可见"这两条规则互相打死。
#
# 隔离规则：**全局，不隔离**。因为「系统做了什么」是全局事实，不分场合。
# 它不承载任何人的原话（正文统一 `[系统]` 前缀 + 系统自己拼的摘要），
# 所以跨场合可见不构成隐私泄漏。
CHANNEL_SYSTEM_LOG = "system_log"
CHANNEL_COLUMN = "channel"
TABLE = "turns"

# 展示用标签（gateway._channel_label 的返回值），迁移与写入侧共用。
LABEL_GROUP_PREFIX = "群聊·"
LABEL_GROUP = "群聊"
LABEL_PRIVATE = "私聊"

# 老数据内容里的来源前缀（gateway.handle_chat 写的 `[{channel}] {speaker}: {text}`）
CONTENT_GROUP_PREFIX = "[群聊·"
CONTENT_GROUP = "[群聊]"
CONTENT_PRIVATE = "[私聊]"
CONTENT_OWNER_PREFIXES = ("主人:", "主人：")
# 这两类前缀不是「聊天」，是插件注入的任务说明，没有来源标签。
# 但它们的内容里带着完整对话转写，必须归类，否则回填后是 NULL、
# 一旦 NULL 被放行就会把私聊原文带进群聊（实测：30 条群聊上下文里
# 96.6% 的字符来自这两类行，其中一条 9605 字符是私聊全量转写）。
CONTENT_PROACTIVE = "[系统任务：主动对话]"
CONTENT_SUMMARY = "# 任务说明"
# 群里 @ 她但不带来源标签的老行（QQ 私聊不会有 @）
CONTENT_AT = "@"


def channel_key(label: str, local: bool = False) -> str:
    """把展示用的来源标签转成记忆里的来源键。

    Args:
        label: `gateway._channel_label()` 的返回值，`群聊·示例群` /
            `群聊` / `私聊` / 空串。
        local: 这一轮是不是**本机桌宠**发起的。必须由调用方显式说明，
            不能靠「标签为空」来推断 —— 群聊里群名没取到、User ID 也没带时
            标签同样是空串，把它当桌宠就等于 fail-open（群消息拿到全部私聊
            上下文）。这是 H4 第一版踩过的坑。

    Returns:
        来源键：`qq_group:<群名>` / `qq_group` / `qq_private` / `pet` /
        `qq_unknown`。未知标签退化成 `qq:<标签>`，保证永远有值、
        且一定不会被误判成桌宠或群聊。
    """
    if local:
        return CHANNEL_PET
    label = (label or "").strip()
    if not label:
        # QQ 那条路却拿不到标签：不可信，单独归类，过滤时看不到任何东西
        return CHANNEL_QQ_UNKNOWN
    if label == LABEL_PRIVATE:
        return CHANNEL_QQ_PRIVATE
    if label.startswith(LABEL_GROUP_PREFIX):
        name = label[len(LABEL_GROUP_PREFIX):].strip()
        return f"{CHANNEL_QQ_GROUP}:{name}" if name else CHANNEL_QQ_GROUP
    if label == LABEL_GROUP:
        return CHANNEL_QQ_GROUP
    return f"qq:{label}"


def is_group_channel(key: str | None) -> bool:
    """这个来源键是不是「群聊」场合（广播场合）。"""
    if not key:
        return False
    return key == CHANNEL_QQ_GROUP or key.startswith(CHANNEL_QQ_GROUP + ":")


def is_known_source(key: str | None) -> bool:
    """这个来源键是不是一个**认得出来**的来源。

    认不出来（`qq_unknown`、`qq:<没见过的标签>`、None）时必须走过滤分支 ——
    精确匹配一个不存在的键，结果是「什么都读不到」。这就是 fail-closed：
    以后有人加了新来源却忘了改这里，表现是「那个来源读不到历史」，
    而不是「那个来源能读到全部历史」。
    """
    if not key:
        return False
    if key in (CHANNEL_PET, CHANNEL_QQ_PRIVATE):
        return True
    return is_group_channel(key)


def infer_from_content(content: str) -> str | None:
    """从老数据的内容前缀反推来源键，推不出来返回 None。

    只认 handle_chat 自己写出来的前缀，不做模糊猜测：
        `[群聊·示例群] 主人: …` → `qq_group:示例群`
        `[群聊] …`                  → `qq_group`
        `[私聊] …`                  → `qq_private`
        `主人: …`                   → `pet`
        `[系统任务：主动对话] …`     → `qq_private`（插件写明的「在私聊中发起」）
        `# 任务说明 …`               → `internal`（livingmemory 总结请求，
                                       正文带整段对话转写，不进任何上下文）
        `@…`                        → `qq_group`（群里 @ 她；QQ 私聊不会有 @）
        其它（assistant 回复、探测脚本的零散行）→ None
    """
    text = (content or "").lstrip()
    # 更具体的先判：这两类不是聊天，必须在「[群聊· / [私聊] / 主人:」之前认出来
    if text.startswith(CONTENT_PROACTIVE):
        return CHANNEL_QQ_PRIVATE
    if text.startswith(CONTENT_SUMMARY):
        return CHANNEL_INTERNAL
    if text.startswith(CONTENT_GROUP_PREFIX):
        end = text.find("]")
        if end > len(CONTENT_GROUP_PREFIX):
            name = text[len(CONTENT_GROUP_PREFIX):end].strip()
            if name:
                return f"{CHANNEL_QQ_GROUP}:{name}"
        return CHANNEL_QQ_GROUP
    if text.startswith(CONTENT_GROUP):
        return CHANNEL_QQ_GROUP
    if text.startswith(CONTENT_PRIVATE):
        return CHANNEL_QQ_PRIVATE
    for prefix in CONTENT_OWNER_PREFIXES:
        if text.startswith(prefix):
            return CHANNEL_PET
    if text.startswith(CONTENT_AT):
        return CHANNEL_QQ_GROUP
    return None


def table_columns(conn: sqlite3.Connection, table: str = TABLE) -> set[str]:
    """读表的列名集合（表不存在时返回空集合）。"""
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def ensure_column(conn: sqlite3.Connection) -> bool:
    """确保 turns.channel 存在。返回 True 表示这次真的加了列。"""
    if CHANNEL_COLUMN in table_columns(conn):
        return False
    conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {CHANNEL_COLUMN} TEXT")
    conn.commit()
    return True


def backfill(conn: sqlite3.Connection) -> dict:
    """回填老数据的 channel。幂等：只处理 channel is null 的行。

    两遍：
      1. 内容前缀能反推出来源的直接写；
      2. 仍然为空的 assistant 行，**向前回溯到最近一条有非空 channel 的记录**
         （中间夹着的 NULL 行全部跳过）—— assistant 回复本身没有前缀，
         但它属于刚才那一轮。

    回溯而不是只看紧邻上一条，是因为插件会往同一槽里插任务说明：
    序列可能是「[系统任务：主动对话] → assistant 回复」，紧邻的上一条
    如果是 NULL，只继承紧邻就会让回复也无主。

    推不出来的（探测脚本的零散行）保持 NULL。读取侧对 NULL **不放行**，
    所以这些行等于不可见 —— 丢的是几十条 14~26 字的测试残留，不是对话。
    """
    stats = {"from_prefix": 0, "inherited": 0, "left_null": 0, "total": 0}
    rows = conn.execute(
        f"select id, session, role, content, {CHANNEL_COLUMN} from {TABLE} order by session, id"
    ).fetchall()
    stats["total"] = len(rows)
    # 每个 session 最近一条**非空**来源。NULL 行不更新它，等于自动跳过。
    last_known: dict[str, str | None] = {}
    updates: list[tuple[str, int]] = []
    for row_id, session, role, content, current in rows:
        key = current or None
        if key is None:
            inferred = infer_from_content(content)
            if inferred:
                key = inferred
                stats["from_prefix"] += 1
            elif role == "assistant":
                candidate = last_known.get(session)
                if candidate:
                    key = candidate
                    stats["inherited"] += 1
        if key and not current:
            updates.append((key, row_id))
        if key:
            last_known[session] = key
        else:
            stats["left_null"] += 1
    if updates:
        conn.executemany(
            f"update {TABLE} set {CHANNEL_COLUMN}=? where id=?", updates
        )
        conn.commit()
    return stats


def relabel_internal(conn: sqlite3.Connection) -> int:
    """把已经标错的「任务说明」行改成 internal。幂等。

    为什么需要这一步：v2 曾把 `# 任务说明` 归成 `qq_private`。那些行已经不是
    NULL，`backfill` 不会再碰它们。但它们是 20980 字符的对话转写，留在私聊
    上下文里同样是噪音。这里按内容前缀做一次定向纠正，只动这一类。

    只纠正「内容前缀明确是 internal」的行，不碰写入侧正常打标的数据。

    ⚠ 2026-11-06（E1 清理，`plan/DMN审核.md` 第五节）：
    **这一项没有删，但改成了 SQL 预筛。**

    原写法是 `select id, content, channel from turns` —— 把**整张表**的正文
    拉进 Python 再逐条调 `infer_from_content()`。这在本机启动时跑，成本随
    历史对话线性增长（现在是 604 行 ≈ 30 万字符，一年后就是几百万字符）。

    数据侧已经清干净（2026-11-06 19:44 删掉最后 32 条，剩余 internal = 0），
    源头也已堵（`_is_memory_task` 分支在 `add_turn` 之前 return）。
    但**这个函数不能删**：它守的是"从旧备份恢复 / 从旧版本升级"这条路径 ——
    那些库里仍然会有标错的转写行，而它们一旦被当成私聊上下文，
    就是把整段对话转写塞回 prompt（真实事故：群聊上下文 96.6% 字符来自私聊转写）。

    所以：语义不变，只把「整表拉出来」换成「先让 SQLite 用 LIKE 粗筛」。
    筛条件刻意宽松（`%任务说明%`），因为历史行可能有前导空白/BOM；
    真正的判定仍然交给 `infer_from_content()`（单一真源，不重复实现一套）。
    """
    updated = 0
    rows = conn.execute(
        f"select id, content, {CHANNEL_COLUMN} from {TABLE}"
        " where content like ?",
        (f"%{CONTENT_SUMMARY}%",),
    ).fetchall()
    fixes = [
        (CHANNEL_INTERNAL, row_id)
        for row_id, content, current in rows
        if current != CHANNEL_INTERNAL and infer_from_content(content) == CHANNEL_INTERNAL
    ]
    if fixes:
        conn.executemany(
            f"update {TABLE} set {CHANNEL_COLUMN}=? where id=?", fixes
        )
        conn.commit()
        updated = len(fixes)
    return updated


def migrate(db_path: str) -> dict:
    """对指定数据库跑一次迁移。不存在该库时直接返回（新装由建表语句带列）。

    Args:
        db_path: `gateway.db` 的绝对路径。可以是真实库，也可以是副本。

    Returns:
        统计信息，便于在日志/命令行里核对回填数量是否合理。
    """
    stats: dict = {
        "db": db_path,
        "exists": os.path.exists(db_path),
        "added_column": False,
        "from_prefix": 0,
        "inherited": 0,
        "left_null": 0,
        "relabeled_internal": 0,
        "total": 0,
        "with_channel": 0,
    }
    if not stats["exists"]:
        return stats
    conn = sqlite3.connect(db_path)
    try:
        if not table_columns(conn):
            # 库文件在但还没有 turns 表（半初始化的库）：交给 init_db 建表
            return stats
        stats["added_column"] = ensure_column(conn)
        stats.update(backfill(conn))
        # 修正 v2 把「# 任务说明」错标成 qq_private 的历史行（幂等）
        stats["relabeled_internal"] = relabel_internal(conn)
        stats["with_channel"] = conn.execute(
            f"select count(*) from {TABLE} where {CHANNEL_COLUMN} is not null"
        ).fetchone()[0]
    finally:
        conn.close()
    return stats


def main(argv: list[str]) -> int:
    """命令行入口：python migrate_channel.py <db 路径>。"""
    if len(argv) < 2:
        print("用法: python migrate_channel.py <gateway.db 路径>")
        return 2
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    stats = migrate(argv[1])
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
