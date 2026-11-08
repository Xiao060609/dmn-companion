"""系统层：只做后台的「信号 + 刹车」，绝不说话、绝不判断人格。

和角色层（gateway.call_model）在时间上彻底分开：
- 被动请求只走角色层，主动心跳只走系统层；
- 任何一条消息都不会同时经过两层。

系统层的职责（决策权下移后）：
- 定时 tick：纯代码检测变化（_detect_change），不调弱模型；
- 把信号交给角色层 role_speak.maybe_speak，由她自己决定说不说、说什么、怎么说并投递；
- 系统层只守住刹车（_allow_speak 的额度 + 冷却）和只读工具边界。

安全红线：
- 心跳纯代码，不调模型；系统层不产生任何文本；
- hands.py 四道闸写死在代码里，不经过任何模型。
"""
from __future__ import annotations

import json
import copy
import hashlib
import os
import queue
import random
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import migrate_channel
import mood as mood_engine
import plan as plan_rules
import role_speak

# 循环导入：gateway.py 顶部 import system；本文件顶部绝不 import gateway，
# 只在 _read_recent_chat / _seconds_since_last_turn / _consolidate_body 等里函数内 import。

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("GATEWAY_DATA_DIR", os.path.join(ROOT, "data"))

SYSTEM_STATE_PATH = os.path.join(DATA_DIR, "system_state.json")
SYSTEM_MODEL_PATH = os.path.join(DATA_DIR, "system_model.json")
SYSTEM_AUDIT_PATH = os.path.join(DATA_DIR, "system_audit.jsonl")
INNER_OS_PATH = os.path.join(DATA_DIR, "inner_os.json")
#: 新颖度用的 n-gram 历史集合（T-12）。**必须持久化**，否则重启后度量失效。
NOVELTY_PATH = os.path.join(DATA_DIR, "novelty_grams.json")

#: `system_state.json` 的读-改-写锁（2026-11-04 T-07）。
#: 写者四个：心跳 / 巩固 / 人格复盘 / 每日梳理。`save_state` 是整份覆盖写，
#: 不加锁就会出现"后写的旧副本把别人刚写的水位抹掉"，后果是重复抽取 + 重复计费。
_STATE_LOCK = threading.RLock()

#: `novelty_grams.json` 的读-改-写锁（2026-11-04 第六轮补）。
#:
#: 为什么要单独一把：`_commit_novelty()` 是 `读 seen → 并集 → 写回` 的模式，
#: 而它可能被**两个入口同时**触发 —— 定时梳理线程，和后台「现在梳理一次」按钮
#: （`gateway.py` 里 `threading.Thread(target=system.daily_review)`）。
#:
#: 竞态后果不是崩溃，是**静默丢数据**：A 读 seen₁、B 读 seen₁，
#: A 写 seen₁∪X、B 写 seen₁∪Y → 最终只剩 seen₁∪Y，**A 加的 X 永久丢失**。
#: 丢掉的后果很隐蔽：那批内容下次会被重新算成"新"，
#: 于是 `novel_chars` 虚高 → 触发本该跳过的梳理 → 白花钱。
#:
#: 同一把锁也保护 `_novelty_save` 的 `.tmp` 文件名（两个线程共用同一个 `.tmp`，
#: 并发时会互相截断对方的临时文件）。
_NOVELTY_LOCK = threading.RLock()

INNER_OS_MAX = 20
INNER_OS_TTL_S = 7 * 86400  # 未处理的观点 7 天后过期
INNER_OS_STATES = {"pending", "delivered", "acted", "declined", "expired"}
# 巩固线程冷启动等待：网关刚起来时先让心跳/角色层就位，但绝不能等一整个巩固周期
CONSOLIDATE_BOOT_DELAY_S = 90.0
HEALTH_REPORT_PATH = os.path.join(DATA_DIR, "health_report.json")

with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _fh:
    _CFG = json.load(_fh)

# 系统层工作准则（职责 + 自我定位，不是人格）——两条调模型的链路共用
with open(os.path.join(ROOT, "system_policy.md"), encoding="utf-8") as _fh:
    SYSTEM_POLICY = _fh.read().strip()

with open(os.path.join(ROOT, "consolidate.md"), encoding="utf-8") as _fh:
    CONSOLIDATE_PROMPT = SYSTEM_POLICY + "\n\n" + _fh.read().strip()

with open(os.path.join(ROOT, "persona_review.md"), encoding="utf-8") as _fh:
    PERSONA_REVIEW_PROMPT = SYSTEM_POLICY + "\n\n" + _fh.read().strip()

with open(os.path.join(ROOT, "persona_apply.md"), encoding="utf-8") as _fh:
    PERSONA_APPLY_PROMPT = _fh.read().strip()

# 情绪词表：和角色层一致，心情也要落在同一套表情上
EMOTIONS = {"happy", "sad", "shy", "calm", "surprised", "confused", "love", "angry"}


#: `turns` 里**不是对话**的渠道。凡是问「他上次说话是什么时候 / 一共有几条对话」
#: 的查询，都必须把它们排除掉。
#:
#: 为什么单独列出来（2026-11-06 集成时踩的连带问题）：
#: `system_log` 和 `internal` 都写进 `turns` 表，但它们的 `role` 不是 user/assistant，
#: 内容也不是谁说的话 —— `system_log` 是系统层自己的操作流水账，`internal` 是插件
#: 塞进来的整段对话转写。可是 `_seconds_since_last_turn()` / `_absence_summary()` /
#: `_latest_turn_id()` / `_day_timeline()` 这些查询只看 session，于是：
#:
#:   * 写一行 `[系统] 体检：发现 2 个问题` → `_seconds_since_last_turn()` 变成 0
#:     → 在场状态从 absent 翻回 present → 每日梳理被 `not_idle` 挡住
#:     （实测：`test_体检的节律状态与后台卡片一致` 因此变红，体检说 no_material、
#:      后台卡片说 not_idle）；
#:   * `_latest_turn_id()` 把流水账的最新 id 当成"新对话" → 心跳每写一条流水账
#:     就多一次 `new_turns` 变化 → 多花一次模型调用；
#:   * `_absence_summary()` 的「他多久没说话了」被系统自己写的话重置。
#:
#: 这就是坑 #59 的另一半：**`system_log` 不只"不进对话历史"，它连"算作对话"都不行。**
#: 巩固素材那一路走的是 `_pending_system_log()`（按 channel 精确取），不经过这里，
#: 所以排除它不影响「跟系统层说一嘴就一起判断了」那条机制。
NON_CONVERSATION_CHANNELS = (
    migrate_channel.CHANNEL_SYSTEM_LOG,
    migrate_channel.CHANNEL_INTERNAL,
)


def _non_conversation_sql(column: str = "channel") -> str:
    """拼 `and coalesce(<column>,'') not in (?,?)`，配合 `NON_CONVERSATION_CHANNELS`。

    用法（参数顺序别搞反）::

        sql += _non_conversation_sql()
        params.extend(NON_CONVERSATION_CHANNELS)
    """
    return f" and coalesce({column},'') not in (?,?)"


# ---------------------------------------------------------------- 心情（2026-11-06）
def _normalize_mood(st: dict) -> bool:
    """把状态里的心情四字段归一成合法值；**首次迁移**时补齐四字段。

    ⚠ 这个函数在 `load_state()` 里跑，而 `load_state()` 每轮心跳都调 ——
    所以它**绝不能碰 `mood_updated_ts`**（除非是"从来没有过"的迁移）。
    一碰就等于"每次都当作刚刚更新过"，衰减永远算成 0 分钟，
    心情会永久停在最后一次事件的值上。这是 `plan/DMN审核.md` 1.10
    明确点出的唯一一个坑：

        「`mood_updated_ts` 必须是上次**真正更新心情的时间**，不是上次读取的时间。」

    迁移规则：`mood_updated_ts` 缺失或 <= 0 时，认为这份状态还是旧格式/半迁移。

    2026-11-06 修（项目地图坑 #61 的后果 B）
    --------------------------------------
    原来这里**无条件**用旧 `emotion` 枚举翻档位，于是"带着真实心情值但
    `ts<=0`"的状态（从旧备份恢复、手工删过 `mood_updated_ts`、旧版本写过
    这份文件）读一次就被吃掉：实测 `-8.0 / 重度悲伤 / ts=0` 读一次变成
    `0.0 / 平静`。现在先看有没有真实值：

      * **有真实值**（`mood_value` 非 0，或 `mood_label` 是个非「平静」的已知档位）
        → **原值保留**，只补 `mood_updated_ts`。时间戳补 `now` 而不是瞎猜
        "多久以前"：宁可让它从现在开始衰减，也不能凭空扣掉一段衰减。
      * 只有出厂默认值（0 / 平静）时，才回落到旧 `emotion` 迁移 ——
        0 本身不携带任何信息，用旧枚举翻出来反而更准。

    Returns:
        这次调用**是否发生了迁移**（即调用方需要把 `st` 落盘一次）。
        修坑 #61 的后果 A：原来迁移只改内存、`load_state()` 只读不写，
        于是每次读都重新取一次 `time.time()`（实测连续三次读到
        `...4326.7132497` / `...4326.723944` / `...4326.7346344`），
        第一次 `save_state()` 之前衰减恒为 0 分钟。现在由 `load_state()`
        看到 True 就落盘一次，之后 ts 稳定。
    """
    try:
        updated = float(st.get("mood_updated_ts") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if updated <= 0:
        # 旧状态迁移：只做一次（写完 updated 就 > 0，下次不再进来）
        now = time.time()
        value = 0.0
        try:
            value = float(st.get("mood_value") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        label = str(st.get("mood_label") or "").strip()
        if not value and label and label != "平静":
            # 只有档位名、没有数值（半迁移状态）：从档位表反推一个数值，
            # 不能因为缺 `mood_value` 就把「中度悲伤」抹成「平静」。
            derived = mood_engine.value_of(label)
            if derived is not None:
                value = float(derived)
        if value:
            st["mood_value"] = value
            st["mood_label"] = mood_engine.label_of(value)
            st["mood_reason"] = str(st.get("mood_reason") or "").strip()[:80]
            st["mood_updated_ts"] = now
            return True
        legacy_label = mood_engine.from_legacy_emotion(str(st.get("emotion") or ""))
        if legacy_label:
            legacy_value = mood_engine.value_of(legacy_label)
            st["mood_value"] = float(legacy_value if legacy_value is not None else 0.0)
            st["mood_label"] = legacy_label
            st["mood_reason"] = ""
        else:
            st.update(mood_engine.initial_state(now))
        st["mood_updated_ts"] = now
        return True
    try:
        value = float(st.get("mood_value") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    st["mood_value"] = value
    st["mood_label"] = mood_engine.label_of(value)
    st["mood_reason"] = str(st.get("mood_reason") or "").strip()[:80]
    st["mood_updated_ts"] = updated
    return False


def mood_state(st: dict | None = None) -> dict:
    """只读：当前心情（**已按真实时间衰减**，不落盘）。

    给拼 prompt 和后台卡片用。要"改变心情"请走 `record_mood_event()`——
    这个函数故意只读，就是为了不可能出现"读一次就把时间戳推到现在"。

    🔧 2026-11-07（审计 G.1 第 4 条 / G.2 第 5 条：**必须查**的那一条）
    ----------------------------------------------------------------
    查证结论：审计担心的是「`mood_prompt_line` / `role_speak._mood_line` /
    `mood_state` 三处各读各的、逻辑不一致」。**实际只有两处读，而且不是各读各的**：

      * `mood_prompt_line()`   → `load_state()` 之后自己调
        `mood_engine.decayed_state(st)`（**没走本函数**）；
      * `role_speak._mood_line()` → 调的是 `mood.prompt_line(state)`
        （那是 `mood` 模块自己的纯函数，内部也调 `decayed_state`）。

    也就是说：**衰减算法只有一份**（`mood.decay()`），三处的数值口径其实一致，
    不存在"算得不一样"。真正重复的是「load_state + decayed_state」这两行的**组合**，
    以及一个更隐蔽的差异 —— `mood_prompt_line()` 拿状态的方式是**丢掉入参**、
    自己在函数里 `load_state()`，于是：

      * 心跳那条路（`_tick_body`）手里明明已经有 `st`（刚 save 过），
        `role_speak.maybe_speak(signals, st)` 也把它传下去了（坑 #63 的修法），
        却没有一条路能把它交给 `mood_prompt_line`；
      * 结果是**同一个 tick 里读了两次状态文件**，两次之间如果心跳刚写过
        `last_speak_ts`，两处看到的心情就可能不是同一个时刻的。

    所以这里的处理是「**让它统一走这个函数**」而不是删掉它（审计给的二选一）：

      * 本函数加一个可选的 `st` 入参 —— 调用方手上已有状态就传进来，
        省一次文件读，也保证"同一轮里心情只取一个时刻"；不传则自己 `load_state()`
        （保持原来零参数的调用方式不变，测试和后台卡片都不用改）；
      * `mood_prompt_line()` 改为调用本函数，不再自己 `decayed_state(load_state())`。

    为什么不干脆删掉：`record_mood_event()` 是**唯一写**心情的入口，它旁边就必须
    有一个**唯一读**的入口，否则每个读的地方都得自己记住"要 decayed 不要 raw"
    ——`role_speak` 那边 11-06 就正是因为这件事踩了一次（读的还是旧 `emotion` 字段）。
    而且它是后台卡片唯一的"衰减后数值"来源，删了它那张卡就只能自己再算一遍。

    ⚠ 但 `role_speak._mood_line()` **仍然不调本函数**，这是有意的、不是漏了：
    它要的是**不带 focus** 的那一段（坑 #63：主动说话注入 focus = 让她复读自己），
    而本函数返回的是**裸四字段**，两边的消费形状不同。要统一的话应该由
    `mood_prompt_line()` 去拆，见那里的注释。

    Args:
        st: 调用方已 load 过的状态（可选）。None 表示自己读。

    Returns:
        四字段 `{"mood_value", "mood_label", "mood_reason", "mood_updated_ts"}`，
        `mood_updated_ts` 填的是**这次计算的 now**（`decayed_state` 的契约：
        value 与 ts 必须成对，否则拿去落盘会被再衰减一次）。
    """
    return mood_engine.decayed_state(st if isinstance(st, dict) else load_state())


def record_mood_event(event_label: str, reason: str = "") -> dict:
    """记一次心情事件（落盘），返回新状态。

    Args:
        event_label: 模型给的**档位名**（如「中度高兴」），不是数值。
            不认识的名字不改数值、只推进时间戳（不瞎猜，见 `mood.apply_event`）。
        reason: 为什么会是这个心情，写人话（「主人刚刚夸我可爱」）。
            主人原话：是「高兴」（主人刚刚夸我可爱），而不是「根据对话生成一个心情」。

    Returns:
        落盘后的心情四字段。
    """
    def _mutate(st: dict) -> None:
        st.update(mood_engine.apply_event(st, event_label, reason=reason))

    return _read_modify_write(_mutate)


#: 旧 `emotion` 枚举 → 新心情档位名的反向映射在 `mood_engine.LEGACY_EMOTION`。


# ---------------------------------------------------------------- 配置
def _load_system_cfg() -> dict:
    """系统层配置。config.json 的 system 段打底，环境变量兜底，
    身份类字段（base/model/key/timeout/temperature/max_tokens）可被
    data/system_model.json 热切换；心跳节律类字段留在 config.json。

    ⚠ 2026-11-07（P0-1）：**`system_model.json` 里持久化的 `key` 一律不读。**

    和角色层 `_load_role()` 同一个病、同一个修法：生产上实测躺着一个
    测试哨兵 `sk-<占位串>`，而 `_system_key()` 的优先级①就是
    `SYSTEM["key"]` → 优先级②③（环境变量 / `secrets.json`）全被绕过。
    系统层不是活跃路径，所以没像角色层那样当场 401，但**一旦启用就必炸**。

    判据同样是**来源**而不是"长得像不像占位串"：文件来的不可信。
    后台切模型时 `apply_system_model()` 是**内存里现拼 cfg** 再试连落盘，
    那条路不受影响（它写盘时照旧把 key 存进 `system_model.json`，
    只是下次启动不会再把它读回来当凭据 —— 读的时候走 `_system_key()`）。
    """
    cfg = dict(_CFG.get("system") or {})
    base = os.environ.get("GATEWAY_SYSTEM_BASE", "").strip()
    model = os.environ.get("GATEWAY_SYSTEM_MODEL", "").strip()
    if base:
        cfg["base"] = base
    if model:
        cfg["model"] = model
    if os.path.exists(SYSTEM_MODEL_PATH):
        try:
            with open(SYSTEM_MODEL_PATH, encoding="utf-8") as fh:
                saved = json.load(fh)
            for k in ("base", "model", "timeout_s", "temperature", "max_tokens"):
                if saved.get(k) not in (None, ""):
                    cfg[k] = saved[k]
            # key：**读，但要挡住"测试哨兵"**
            #
            # ⚠ 2026-11-07 修正 —— 上一步我把它写成"彻底不读"，那是矫枉过正：
            # 主人的真实用法就是往 system_model.json 里填一把 key（比如换了个
            # 第三方中转站），彻底不读等于让人没法配。而且它还会回落去用
            # secrets.json 的 **DashScope** key 去打别家的 base —— 服务商和 key
            # 不匹配，必然 401，而且报错信息完全指不到真正的原因。
            #
            # 正确判据是**形状**：正常的 API key 有长度、有前缀；
            # 测试哨兵是 `sk-SHOULD-NOT-WRITE` / `sk-TESTSECRET-...` 这类
            # "看起来像 key 其实是占位"的短串。这里只挡明显不可能是真 key 的：
            # 太短、或含 SHOULD-NOT / TESTSECRET / xxx / placeholder 这类字样。
            stray = str((saved or {}).get("key") or "").strip()
            if stray and not _looks_like_placeholder_key(stray):
                cfg["key"] = stray
            elif stray:
                print(
                    f"[system] 忽略 {SYSTEM_MODEL_PATH} 里像占位串的 key（长度 {len(stray)}）："
                    "正常的 key 请直接填在 system_model.json 的 key 字段。"
                )
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


#: `data/system_model.json` 里管的**身份类**字段（热切换）。
#: 其余字段（心跳节律、巩固门槛、每日梳理…）留在 config.json，这里不碰。
_SYSTEM_IDENTITY_KEYS = ("base", "model", "key", "timeout_s", "temperature", "max_tokens")

#: 回写 config.json 时**必须排除**的字段。
#:
#: 为什么排除 `key`：`gateway/config.json` **在 git 里**（`data/` 被 gitignore）。
#: 把真 key 写进去 = 下次 commit 就把明文 key 提交了。
#: 所以 key 只落 `data/system_model.json`（已被 gitignore），config.json 里恒为空。
_SYSTEM_SECRET_KEYS = ("key",)


def _save_system_cfg(cfg: dict) -> None:
    """把身份类字段写进 `data/system_model.json`（热切换，原子写）。

    **同时把非敏感字段回写 `config.json` 的 `system` 段。**

    为什么（2026-11-05 修主人报的 BUG）：
        后台卡片只写 `data/system_model.json`，`config.json` 永远停在旧值。
        实测：config.json 的 system 段还是 `dashscope + deepseek-v4.1-flash-1017`，
        而实际在跑的是 `apihub.agnes-ai.com + agnes-3.0-flash`。
        后果：直接看 config.json 的人（包括审计脚本、包括我自己排查时）
        会以为系统层还在用老模型。**两个真源说法不一致，迟早踩坑。**

    ⚠ `key` **不回写**：config.json 在 git 里，见 `_SYSTEM_SECRET_KEYS`。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    keep = {k: cfg.get(k) for k in _SYSTEM_IDENTITY_KEYS}
    tmp = SYSTEM_MODEL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, SYSTEM_MODEL_PATH)

    # ── 同步 config.json（只写非敏感字段）──
    _sync_config_system_section(keep)


def _sync_config_system_section(identity: dict) -> None:
    """把身份类字段回写 `config.json` 的 `system` 段，保留其余键与注释键。

    设计要点：
      * **只改这几个键**：`//xxx` 注释键、心跳节律、巩固门槛全部原样保留；
      * `key` 永不写入（config.json 在 git 里）；
      * 原子写（tmp + os.replace），避免写一半断电把配置弄坏；
      * 失败**不抛**：配置已经在内存里生效了，回写失败不该让用户看到
        "保存失败"——那只说明镜像文件没跟上。但要打日志，否则又是静默吞错。
    """
    cfg_path = os.path.join(ROOT, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            disk = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config] 读 {cfg_path} 失败，跳过同步：{exc}")
        return

    sect = disk.get("system")
    if not isinstance(sect, dict):
        print("[config] config.json 没有 system 段，跳过同步")
        return

    changed = []
    for k, v in identity.items():
        if k in _SYSTEM_SECRET_KEYS:
            continue                      # ← key 绝不进 config.json
        if v is None:
            continue
        if sect.get(k) != v:
            sect[k] = v
            changed.append(k)
    if not changed:
        return

    tmp = cfg_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(disk, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)
        print(f"[config] 已同步 config.json 的 system 段：{', '.join(changed)}")
    except OSError as exc:
        print(f"[config] 回写 {cfg_path} 失败（配置已生效，只是镜像没跟上）：{exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass


#: 看起来像"测试占位串"的 key 特征。宁可误判放过（那只是 401），
#: 不可误判拦住（那会让主人配不进真 key，还查不出原因）。
_PLACEHOLDER_KEY_HINTS = (
    "should-not",
    "shouldnot",
    "testsecret",
    "test-secret",
    "placeholder",
    "your-key",
    "yourkey",
    "changeme",
    "xxxx",
    "dummy",
)


def _looks_like_placeholder_key(key: str) -> bool:
    """这把 key 像不像测试占位串（而不是真 key）。

    ⚠ 2026-11-07：这是 P0-1 的正确收紧方式。
    原来我用"来自文件就不可信"一刀切，结果主人往 `system_model.json`
    填真 key 也不生效（还回落去用别家的 key，401 报得像服务挂了）。
    判据改成**形状**：明显的占位串特征 + 长度过短。

    长度门槛取 20：主流 API key 都在 30 字符以上，而
    `sk-SHOULD-NOT-WRITE` 是 19、`sk-TESTSECRET-...` 是 35（靠字样挡住）。
    """
    k = str(key or "").strip()
    if len(k) < 20:
        return True
    low = k.lower()
    return any(h in low for h in _PLACEHOLDER_KEY_HINTS)


def _system_key() -> str:
    """系统层用哪把 key。不复用 _role_key、不回落到角色层的 active key。

    优先级：SYSTEM["key"] > GATEWAY_SYSTEM_KEY 环境变量 > secrets.json 的 dashscope key。
    两把 key 分开，401 才能定位是哪层死。

    ⚠ 2026-11-07 修：优先级③ **必须校验服务商**。
    原来 `secrets.json` 里只有 DashScope 的 key，而无条件拿它去请求
    `SYSTEM["base"]` 指向的任何服务商 —— 如果 base 是第三方中转站
    （如 apihub.agnes-ai.com），等于拿 A 家的 key 去打 B 家，必然 401，
    而报错信息完全指不到"key 和服务商不匹配"这个真正原因。
    现在：只有 base 确实是 DashScope 时才回落。
    """
    if SYSTEM.get("key"):
        return str(SYSTEM["key"])
    env = os.environ.get("GATEWAY_SYSTEM_KEY", "").strip()
    if env:
        return env
    base = str(SYSTEM.get("base", "")).lower()
    provider = str(SYSTEM.get("provider", ""))
    if "dashscope" in base or provider == "dashscope":
        try:
            with open(os.path.join(ROOT, "secrets.json"), encoding="utf-8") as fh:
                return str(json.load(fh).get("dashscope_api_key", "")).strip()
        except (OSError, json.JSONDecodeError):
            return ""
    return ""
    return ""


SYSTEM = _load_system_cfg()


# ---------------------------------------------------------------- 状态
def _default_state() -> dict:
    return {
        "version": 1,
        "emotion": "calm",
        "focus": "",
        # ── 心情（2026-11-06，`plan/DMN审核.md` 1.10 定稿）────────────────
        # 旧的 `emotion` 是 8 值枚举，只够驱动表情；新的心情是**连续数值 + 档位
        # + 理由 + 上次更新时间**，按需衰减（不跑定时器）。
        #
        # ⚠ `mood_updated_ts` 必须是「上次**真正更新**心情的时间」，不是上次
        # 读取的时间 —— 否则每次读都把时间戳推到现在，等于永远不衰减。
        # 所以：拼 prompt 走 `mood.decayed_state()`（不落盘），
        #       只有真发生事件才走 `record_mood_event()`（落盘）。
        "mood_value": 0.0,
        "mood_label": "平静",
        "mood_reason": "",
        "mood_updated_ts": 0,
        "memory": [],
        "recent_thoughts": [],
        "last_tick_ts": 0,
        "last_speak_ts": 0,
        "speak_count_today": 0,
        "last_consolidate_id": 0,
        # 每个渠道各记各的水位（channel -> 已处理到的 turns.id）。
        # 一个渠道一个水位，高频渠道才不会把低频渠道的增量顶掉。
        "consolidate_wm": {},
        "last_consolidate_ts": 0,
        "last_persona_review_id": 0,
        "last_seen_turn_id": 0,
        # 系统流水账（`turns.channel = 'system_log'`）喂到哪一条了。
        # **必须在这里登记** —— `load_state()` 只保留本函数里出现过的键，
        # 不登记的话每次读都被丢掉，等于每次巩固都从头再喂一遍（重复计费）。
        "last_system_log_id": 0,
        "last_presence_state": "",
        # 每日梳理期（P0-4）。**必须在这里登记** —— `load_state()` 只保留
        # 本函数里出现过的键（`{k: v for k, v in data.items() if k in st}`），
        # 没登记的键每次读都会被丢掉，表现为"跑过了但每次都当作没跑过"。
        "last_daily_review_day": "",
        "last_daily_review_ts": 0,
        # 梳理失败的时间戳（2026-11-04 P0-8）。**必须登记**，理由同上：
        # 不登记就每次读被丢掉，等于"失败了但没人记得失败过"。
        "last_daily_review_failed_ts": 0,
        # 「到点看一眼」探过的日窗口（T-12）。**必须登记**，否则每次读被丢掉，
        # 同一个日窗口会每 10 分钟探一次（每次都花一次模型调用）。
        "last_daily_review_probe_day": "",
        # 长期记忆清理的节流时间戳（T-07）。
        # ⚠ 这个键曾经**只在 `_maybe_prune_long_memory` 里被写、却从没登记在这里**，
        # 而 `load_state()` 只保留本函数出现过的键（下面 `st.update(...)` 那一行），
        # 于是每次读都把它丢掉 → `if last and (...) < 86400` 恒不成立 →
        # "每天一次清理"的节流失效 → 实测 24h 内跑了 28 次（≈每小时一次，
        # 每次全表拉 2000 行）。加这一行就够了，别再删。
        "last_long_memory_prune_ts": 0,
    }


def load_state() -> dict:
    """原子读 system_state.json；读不到就返回默认状态。"""
    try:
        with open(SYSTEM_STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _default_state()
    except (OSError, json.JSONDecodeError):
        return _default_state()

    st = _default_state()
    st.update({k: v for k, v in data.items() if k in st})
    st["emotion"] = st["emotion"] if st["emotion"] in EMOTIONS else "calm"
    st["focus"] = str(st.get("focus") or "")[:40]
    mood_migrated = _normalize_mood(st)
    # memory 去重（问题 B）：完全相同的 text 只留一条，清掉历史复读墙
    _mem: list = []
    _seen: set = set()
    for _m in (st.get("memory") or []):
        if not isinstance(_m, dict):
            continue
        _txt = str(_m.get("text") or "").strip()
        if _txt and _txt in _seen:
            continue
        if _txt:
            _seen.add(_txt)
        _mem.append(_m)
    st["memory"] = _mem[:50]
    st["recent_thoughts"] = [t for t in (st.get("recent_thoughts") or []) if isinstance(t, dict)][:10]
    for k in ("last_tick_ts", "last_speak_ts", "last_consolidate_ts", "last_daily_review_ts"):
        try:
            st[k] = float(st[k])
        except (TypeError, ValueError):
            st[k] = 0
    for k in ("speak_count_today", "last_consolidate_id", "last_persona_review_id", "last_seen_turn_id"):
        try:
            st[k] = int(st.get(k) or 0)
        except (TypeError, ValueError):
            st[k] = 0
    # 分渠道水位：读的时候一律归一成 {渠道: int}，坏值当 0（宁可从头发一遍，
    # 也不能因为一条脏数据把整个渠道的增量永久跳过）。
    _wm: dict[str, int] = {}
    _raw_wm = data.get("consolidate_wm")
    if isinstance(_raw_wm, dict):
        for _k, _v in _raw_wm.items():
            _key = str(_k or "").strip()
            if not _key:
                continue
            try:
                _wm[_key] = max(0, int(_v))
            except (TypeError, ValueError):
                _wm[_key] = 0
    # 兼容旧版本：以前只有一个全局 last_consolidate_id（而且只喂 pet），
    # 升级时当作 pet 渠道的水位起点，不让历史增量重跑一遍。
    if not _wm and st["last_consolidate_id"] > 0:
        _wm[migrate_channel.CHANNEL_PET] = st["last_consolidate_id"]
    st["consolidate_wm"] = _wm
    st["last_presence_state"] = str(st.get("last_presence_state") or "")
    if mood_migrated:
        # 心情迁移必须**立刻落盘**（2026-11-06 修项目地图坑 #61 的后果 A）。
        #
        # 原来 `load_state()` 只读不写：迁移只改内存，于是**每一次** `load_state()`
        # 都重新取一次 `time.time()`（实测连续三次读到
        # `...4326.7132497` / `...4326.723944` / `...4326.7346344`，各差约 10ms）。
        # 在第一次 `save_state()` 之前，`decay()` 的 `elapsed_minutes` 恒为 0，
        # 衰减算不出来 —— 靠"下次心跳捎带落盘"是碰运气，不是保证。
        #
        # ⚠ 只在**迁移真的发生**时写一次（`_normalize_mood` 返回 True）：
        # 迁移写完 `mood_updated_ts > 0`，下次读就走正常分支、不再进来。
        # 绝不能变成"每次读都写盘"——那会毁性能，也会制造测试抖动。
        #
        # ⚠ 为什么不会无限递归：`save_state()` 只写 JSON，**不调 `load_state()`**。
        # `_STATE_LOCK` 是 RLock，`_read_modify_write()` 持锁进来也不会死锁。
        try:
            save_state(st)
        except OSError:
            # 写不进去不是读状态的错：把迁移好的内存副本照常返回，
            # 下一次 save 还会捎带落盘（和修之前一样的降级路径）。
            pass
    return st


def save_state(st: dict) -> None:
    """原子写 system_state.json：先写 .tmp 再 os.replace。

    ⚠ **写者不止一个**（2026-11-03 22:30 改）。以前这里写的是「单写者（只有 system
    线程写）」，加了每日梳理线程之后那句话就是**假的**，会误导后来人。

    现在同时可能写这份文件的有：
      * 心跳线程（`last_tick_ts` / `speak_count_today` / `recent_thoughts`）
      * 巩固线程（经 `_advance_watermark()`，它会先重新 load 再只改水位那几个键）
      * 人格复盘（`last_persona_review_id`）
      * **每日梳理线程**（`last_daily_review_day` / `last_daily_review_ts`）

    因为这里是**整份覆盖写**，任何"拿着旧副本改一个键再写回"的调用都会把别人刚写的
    键抹掉。所以：**所有非心跳的写者都必须先 `load_state()`、只改自己的键、再 `save_state()`**
    （照 `_advance_watermark()` 的写法）。心跳那条路是唯一允许"读一次改多处"的，
    因为它每次 tick 都会重新 load。

    ⚠ 2026-11-04（T-07）：上面那段纪律靠 docstring 维持**已经证明会失守**，
    所以再加一道机械保障 —— `_read_modify_write()` 用锁把
    「load → 改 → save」整段包起来，需要跨键更新时优先用它，别裸调 save_state。

    `os.replace` 保证**文件内容**不会半截，但保证不了**逻辑**上的丢失更新：
    两个线程各自 load、各自改不同的键、先后 save，后写的那个会把前一个的改动覆盖掉。
    锁管的是这件事。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SYSTEM_STATE_PATH + ".tmp"
    with _STATE_LOCK:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, SYSTEM_STATE_PATH)


def _read_modify_write(mutate) -> dict:
    """带锁的「load → 改 → save」：跨键更新优先用这个。

    Args:
        mutate: `fn(state: dict) -> None`，就地改 `state`。

    Returns:
        改完之后的完整状态。

    ⚠ 为什么需要（2026-11-04 T-07）：`save_state` 是**整份覆盖写**，
    而写者有 4 个（心跳 / 巩固 / 人格复盘 / 每日梳理）。只要有一个写者
    「用旧副本改一个键再写回」，别人这段时间写的键就被抹掉。
    实测的后果是**水位会被写回退** —— 巩固线程跨一次模型调用（最多 120s）
    全程持着旧副本，期间心跳推进的 `last_seen_turn_id` 被覆盖回旧值，
    于是下一轮重复抽取同一批对话 → 重复计费；长期记忆还会出现重复条目。

    这把锁让「读-改-写」变成原子区，从机制上消灭这类丢失更新，
    不再靠"每个调用方都记得重新 load"这种纪律。
    """
    with _STATE_LOCK:
        st = load_state()
        mutate(st)
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SYSTEM_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, SYSTEM_STATE_PATH)
        return st


# ---------------------------------------------------------------- 信号注入
def _read_recent_chat(limit: int = 5) -> list[dict]:
    """读最近几条对话当素材。只读 SQL，绝不 add_turn 污染对话记忆。

    隐私边界：只读主人/桌宠来源（pet），不读主人私聊、群聊、也不读 internal。

    ⚠ **主动说话别再用这个**（2026-11-03 起改用 `_read_recent_chat_multi()`）。
    它写死 pet，而生产里 pet 只有 5 条、最后一条停在 10-30 ——
    主动说话会拿四天前的对话当"最近"，说出「刚刚说心情不好」这种话。
    保留它是给"只想知道桌宠那条线"的场景用。
    """
    import gateway
    try:
        return gateway.recent_turns(
            gateway.SHARED_SESSION, limit, source=migrate_channel.CHANNEL_PET
        )
    except Exception:  # noqa: BLE001 —— 记忆读不到不该拦住心跳
        return []


def _read_recent_chat_multi(limit: int = 5) -> list[dict]:
    """跨主渠道读最近对话：**pet + 主人私聊**，群聊不进。

    为什么必须改（2026-11-03 实测）
    ------------------------------
    `_read_recent_chat()` 写死 pet，而 pet 最后一条是 **10-30**、真实对话在
    `qq_private`（最后 11-03）。于是 13:22 那句「刚刚说心情不好现在好一点了吗」
    里的"刚刚"，其实是四天前。

    这和人格复盘（`7d4dfdc`）、安慰三态（`7d4dfdc`）、事件抽取（`e51ba0d`）
    是同一个根：**"pet 是唯一可信来源"这个假设已经不成立了**。

    隔离口径与 `_read_persona_review_turns()` 完全一致：
    * `pet` —— 整条渠道都是主人；
    * `qq_private` —— 过 `_filter_owner_turns()`，只留 `[私聊] 主人:` 及其回复，
      陌生人私聊和 `[系统任务：主动对话]` 注入一律不进；
    * 群聊**不进**：群友的话不能变成"主人刚说过什么"。

    这里直接查 SQL 而不是走 `gateway.recent_turns()` —— 后者只回
    `{role, content}`，**没有 ts**，而"素材有多旧"正是这次要修的东西。

    Args:
        limit: 取多少条（合并后按时间正序）。

    Returns:
        `[{"role","content","ts","channel"}]`，正序。读不到返回 []。
    """
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select role, content, ts, channel from turns"
                " where session=? and coalesce(channel,'') in (?,?)"
                "   and coalesce(path,'') not in ('tool','approval','approve')"
                " order by id desc limit ?",
                (
                    gateway.SHARED_SESSION,
                    migrate_channel.CHANNEL_PET,
                    migrate_channel.CHANNEL_QQ_PRIVATE,
                    max(1, int(limit)) * 4,  # 多取一些，过滤掉陌生私聊后还够用
                ),
            ).fetchall()
    except sqlite3.Error:
        return []

    turns = [
        {
            "role": r[0],
            "content": str(r[1] or ""),
            "ts": float(r[2] or 0),
            "channel": str(r[3] or ""),
        }
        for r in rows
    ]
    turns.reverse()  # 上面是 desc 取最新，翻成正序

    # 主人私聊要认人：陌生人 / `[系统任务：主动对话]` 不能算"主人刚说过什么"。
    # 判定口径和 `_filter_owner_turns` 一致 —— 只认 `[私聊] 主人:` 前缀。
    kept = [
        t
        for t in turns
        if t["channel"] == migrate_channel.CHANNEL_PET
        or t["content"].lstrip().startswith("[私聊] 主人")
    ]
    return kept[-limit:] if limit > 0 else kept


def _recent_chat_age_minutes(chat: list[dict]) -> float | None:
    """这批素材有多旧（分钟）。没有 ts 或全 0 时返回 None。

    为什么要这个：角色层拿到 `recent_chat` 时并不知道它是"一分钟前"
    还是"四天前"，于是会把旧事说成"刚刚"。把年龄一起给它，
    它才能决定说「刚才」还是「前几天」。
    """
    stamps = [float(t.get("ts") or 0) for t in (chat or []) if float(t.get("ts") or 0) > 0]
    if not stamps:
        return None
    return round((time.time() - max(stamps)) / 60, 1)


def _seconds_since_last_turn() -> float | None:
    """距上一句对话过了多少秒（看 turns 表最新的时间戳）。没有对话返回 None。

    ⚠ 2026-11-06：必须排除 `NON_CONVERSATION_CHANNELS`。系统流水账
    （`[系统] 体检：发现 2 个问题`）也写在 turns 里，算进来就等于
    「系统自己写一行，主人就变成刚说过话」—— 在场状态永远翻不回 absent，
    每日梳理永远被 `not_idle` 挡住。详见 `NON_CONVERSATION_CHANNELS` 的注释。
    """
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            row = conn.execute(
                "select ts from turns where session=?"
                + _non_conversation_sql()
                + " order by id desc limit 1",
                (gateway.SHARED_SESSION, *NON_CONVERSATION_CHANNELS),
            ).fetchone()
        if row and row[0]:
            return max(0.0, time.time() - float(row[0]))
    except Exception:  # noqa: BLE001
        pass
    return None


def _latest_turn_id() -> int | None:
    """turns 表最新一条**对话**的 id。没有对话返回 None。只读。

    ⚠ 2026-11-06：排除 `NON_CONVERSATION_CHANNELS`。不排除的话，系统每写一条
    流水账 `_detect_change()` 就认为"有新对话"，白多一次模型调用
    （`_tick_body` 走的是有变化分支）。
    """
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            row = conn.execute(
                "select id from turns where session=?"
                + _non_conversation_sql()
                + " order by id desc limit 1",
                (gateway.SHARED_SESSION, *NON_CONVERSATION_CHANNELS),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def _presence_state(seconds: float | None) -> str:
    """把「距上次对话秒数」折成在场状态：present / away / absent。"""
    if seconds is None:
        return "absent"
    if seconds < 300:
        return "present"
    if seconds < 1800:
        return "away"
    return "absent"


def _detect_change(st: dict) -> dict:
    """代码级变化检测，不调模型。返回信号（含最新水位），无变化返回空 dict。

    两个信号：① 有新对话（turns 最新 id > last_seen_turn_id）；② 在场状态跃迁。
    任一命中即有变化；信号里带上最新水位，供 tick 落地。
    """
    sig: dict = {}
    latest_id = _latest_turn_id()
    if latest_id is not None and latest_id > int(st.get("last_seen_turn_id") or 0):
        sig["new_turns"] = latest_id - int(st.get("last_seen_turn_id") or 0)
        sig["last_turn_id"] = latest_id
    presence = _presence_state(_seconds_since_last_turn())
    if presence != st.get("last_presence_state"):
        sig["presence"] = presence
    return sig


# ------------------------------------------------ 每日梳理期的素材（P0-4 阶段 0 / 0.5）
#
# 为什么这两个函数要独立存在（2026-11-03 22:00 补计划时查出的两个硬矛盾）：
#
#   * `_day_timeline()` —— 场景 A 那句「你今天从早到晚都在弄那个吧，**中间都没歇**」
#     需要的是「一天的疏密节奏」。只喂 turns 列表，她只知道**说了什么**，
#     不知道**中间空了多久**，这句话永远说不出来。
#
#   * `_absence_summary()` —— 场景 C「你这三天都没来」需要一个**纯数据查询**。
#     实测网关停机占 48%（主人关电脑睡觉它就不在），所以「每天都跑过一轮梳理」
#     这个前提根本保证不了。缺席时长必须能从库里算出来，不能依赖进程一直在。
#     而且**中断期绝不伪造逐日叙事** —— 没有那些记录就不许编。

def _day_timeline(hours: float = 24.0, now: float | None = None) -> dict:
    """最近 `hours` 小时的对话疏密摘要（纯 SQL，不调模型）。

    `longest_gap_min` 只统计**窗口内相邻两条对话之间**的最大间隔：
    单条对话时没有「之间」，返回 0。窗口起点和「最后一条 → 现在」
    都不算 —— 后者是「他沉默了多久」，归 `_absence_summary()` 管，
    两边都算会重复且口径打架。

    Returns:
        {
          "span_hours": 24.0,
          "window_start_ts": float, "window_end_ts": float,
          "turns_total": int,
          "turns_by_hour": {"09": 0, "10": 2, ...},   # 键是两位小时 "00".."23"
          "first_turn_ts": float|None, "last_turn_ts": float|None,
          "first_turn_hm": "10:12"|None, "last_turn_hm": "22:40"|None,
          "longest_gap_min": int,  "gap_start_hm": str|None, "gap_end_hm": str|None,
          "active_hours": int,     # 有几个小时里出现过对话
          "empty": bool,           # 窗口内一句话都没有
        }

    只算 `turns` 全部来源（与巩固/复盘同口径由调用方再收窄）；
    这里只回答「节奏长什么样」，不判断渠道能不能看 —— 那是调用方的事。
    """
    import gateway

    end = float(now if now is not None else time.time())
    start = end - max(1.0, float(hours) * 3600.0)
    by_hour = {f"{h:02d}": 0 for h in range(24)}
    stamps: list[float] = []
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            # 排除系统流水账 / 插件注入：它们不是"他今天说话的节奏"。
            # 不排除的话，系统每写一行流水账当天就多一个「活跃小时」，
            # 疏密节奏被自己的记账行为污染（详见 NON_CONVERSATION_CHANNELS）。
            rows = conn.execute(
                "select ts from turns where session=? and ts >= ? and ts <= ?"
                + _non_conversation_sql()
                + " order by ts",
                (gateway.SHARED_SESSION, start, end, *NON_CONVERSATION_CHANNELS),
            ).fetchall()
        stamps = [float(r[0]) for r in rows if r and r[0]]
    except (sqlite3.Error, TypeError, ValueError):
        stamps = []
    for ts in stamps:
        by_hour[time.strftime("%H", time.localtime(ts))] += 1

    if not stamps:
        return {
            "span_hours": round(max(1.0, float(hours)), 2),
            "window_start_ts": start,
            "window_end_ts": end,
            "turns_total": 0,
            "turns_by_hour": by_hour,
            "first_turn_ts": None,
            "last_turn_ts": None,
            "first_turn_hm": None,
            "last_turn_hm": None,
            "longest_gap_min": 0,
            "gap_start_hm": None,
            "gap_end_hm": None,
            "active_hours": 0,
            "empty": True,
        }

    # 最长空白 = 窗口内**相邻两条对话之间**的最大间隔。
    #
    # 不把「窗口起点 → 第一条」算进去：窗口起点是任意截的，
    # 那样会把「昨晚没说话」算成「今天空了 12 小时」。
    # 也不把「最后一条 → 现在」算进去：那是「他沉默了多久」，
    # 由 `_absence_summary()` 负责，两边都算会重复且口径打架。
    #
    # 所以只有一条对话时，**没有「之间」可言，空白就是 0**。
    gaps = [(b - a, a) for a, b in zip(stamps, stamps[1:]) if b > a]
    if gaps:
        gap_s, gap_from = max(gaps)
    else:
        gap_s, gap_from = 0.0, stamps[0]
    longest_gap_min = int(round(gap_s / 60))

    return {
        "span_hours": round(max(1.0, float(hours)), 2),
        "window_start_ts": start,
        "window_end_ts": end,
        "turns_total": len(stamps),
        "turns_by_hour": by_hour,
        "first_turn_ts": stamps[0],
        "last_turn_ts": stamps[-1],
        "first_turn_hm": time.strftime("%H:%M", time.localtime(stamps[0])),
        "last_turn_hm": time.strftime("%H:%M", time.localtime(stamps[-1])),
        "longest_gap_min": longest_gap_min,
        "gap_start_hm": time.strftime("%H:%M", time.localtime(gap_from)),
        "gap_end_hm": time.strftime("%H:%M", time.localtime(gap_from + gap_s)),
        "active_hours": sum(1 for v in by_hour.values() if v > 0),
        "empty": False,
    }


def _absence_summary(now: float | None = None) -> dict:
    """他有多久没说话了（纯查库，**不依赖进程是否一直开着**）。

    这是场景 C 的地基：网关实测停机占 48%，那几天窗口根本没进程去跑梳理，
    所以「第一天/第二天/第三天」这种逐日记录**不存在**。
    但「他上次说话是什么时候」是一个只要库还在就能算出来的事实。

    Returns:
        {
          "last_turn_ts": float|None,
          "absent_hours": float|None,      # 距上一句多久
          "absent_days": float|None,       # 同上，按天（保留 1 位）
          "absent_text": "三天" | "6 小时" | "",   # 给人/给模型看的中文描述
          "is_absent": bool,               # 是否达到 min_absence_hours
          "min_absence_hours": float,
        }
    """
    import gateway

    min_h = float(SYSTEM.get("daily_review", {}).get("min_absence_hours", 6) or 0)
    end = float(now if now is not None else time.time())
    last_ts: float | None = None
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            # 排除系统流水账 / 插件注入：它们不是"他说的话"。
            # 不排除的话，`[系统] 主动说话：主人刚回来` 这行一写，
            # 缺席时长立刻归零 —— 每日梳理期再也不会因为"他三天没来"而启动，
            # 而那正是场景 C 的全部素材（详见 NON_CONVERSATION_CHANNELS）。
            row = conn.execute(
                "select max(ts) from turns where session=?" + _non_conversation_sql(),
                (gateway.SHARED_SESSION, *NON_CONVERSATION_CHANNELS),
            ).fetchone()
        if row and row[0]:
            last_ts = float(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        last_ts = None

    out = {
        "last_turn_ts": last_ts,
        "absent_hours": None,
        "absent_days": None,
        "absent_text": "",
        "is_absent": False,
        "min_absence_hours": min_h,
    }
    if last_ts is None:
        # 库里一条对话都没有：这是"从没聊过"，不是"缺席"。
        return out

    secs = max(0.0, end - last_ts)
    hours = secs / 3600.0
    out["absent_hours"] = round(hours, 1)
    out["absent_days"] = round(hours / 24.0, 1)
    out["is_absent"] = hours >= min_h
    out["absent_text"] = _absent_text(hours)
    return out


def _absent_text(hours: float) -> str:
    """把缺席小时数说成人话。中文里「三天」比「72 小时」自然得多。"""
    if hours < 1:
        return "刚刚"
    if hours < 24:
        return f"{int(round(hours))} 小时"
    days = hours / 24.0
    whole = int(days)
    if abs(days - round(days)) < 0.08:
        n = int(round(days))
        return f"{n} 天"
    if whole >= 1:
        return f"{whole} 天多"
    return f"{int(round(hours))} 小时"


# ------------------------------------------------ 每日梳理期：触发时机（P0-4 阶段 1）
#
# 为什么不用「距上次满 N 小时」的滑动间隔（2026-11-03 22:00 查出的矛盾 2）：
#   上一次 00:30 跑 → 下次最早 20:30 → 那天 20:30 正在聊被挡住 → 拖到 22:40
#   → 再下一次又往后挪 …… **节律一旦漂起来就不再对应「一天」**。
# 改成按「日窗口」锚定：每个窗口最多一次，天然就是 1 次/天，不会漂。

def _daily_review_cfg() -> dict:
    """梳理期配置，带默认值。

    默认值就是计划里那份定稿配置（22:00 版），
    缺配置时也能跑 —— 不要因为 config.json 没写就整项失效。
    """
    raw = SYSTEM.get("daily_review")
    cfg = raw if isinstance(raw, dict) else {}
    budget = cfg.get("budget")
    return {
        "enabled": bool(cfg.get("enabled", False)),
        # ① 什么时候跑
        "day_start_hour": int(cfg.get("day_start_hour", 6)),
        "earliest_hour": int(cfg.get("earliest_hour", 21)),
        "idle_after_minutes": float(cfg.get("idle_after_minutes", 120)),
        "once_per_day": bool(cfg.get("once_per_day", True)),
        "window_missed": str(cfg.get("window_missed", "skip")),
        # ①-2 双判断（2026-11-04 T-12）
        # 触发准则从「墙上时间」改成「信息量 + 昼夜节律」两条腿：
        #   * 信息量条：**有效新颖量**达阈值 → 立刻整理（不需要再问模型要不要）
        #   * 节律条：每天到点看一眼 → **由模型决定**做不做（可以跳过）
        #   * 其余时间算「睡眠」：心跳照常，但不调模型
        # 详见 plan\小鱼-执行计划-2026-11-04.md 第七节。
        #
        # 阈值怎么来的（2026-11-04 实测校准，别拍脑袋改）：
        # 用真实库回放，1 条「真新话」（约 35 字全新内容）→ novel_chars ≈ 34，
        # 2 条 → 70.8；而**同一句重复再说一遍增量是 0**（新颖度 0）。
        # 历史 62 条旧对话回放 → novel_chars = 0.0（全是重复的，正是"半睡眠"）。
        # 所以 60 ≈ "聊了两三句有信息量的话"，150 会漏掉短会话。
        "min_novel_chars": float(cfg.get("min_novel_chars", 60)),
        "circadian_enabled": bool(cfg.get("circadian_enabled", True)),
        # ② 每个活动要不要跑
        "ruminate_requires_today_turns": bool(cfg.get("ruminate_requires_today_turns", True)),
        "absence_is_material": bool(cfg.get("absence_is_material", True)),
        "min_absence_hours": float(cfg.get("min_absence_hours", 6)),
        # ③ 成本闸
        "budget": budget if isinstance(budget, dict) else {
            "ruminate": 1, "prospect": 1,
            "theory_of_mind": 1, "self_narrative": 1,
        },
        "daily_model_calls_cap": int(cfg.get("daily_model_calls_cap", 5)),
        "inner_os_quota_per_review": int(cfg.get("inner_os_quota_per_review", 3)),
        # ④ 沉淀（P0-5 的 C 路）
        "memory_sink_enabled": bool(cfg.get("memory_sink_enabled", True)),
        # ④ 投递
        "quiet_hours": cfg.get("quiet_hours") or [],
        # loop 节律
        "check_s": float(cfg.get("check_s", 600)),
        "boot_delay_s": float(cfg.get("boot_delay_s", 90)),
    }


def _review_day_key(ts: float, day_start_hour: int = 6) -> str:
    """把时间戳折成「日窗口键」。窗口从 day_start_hour 开始，跨零点。

    为什么 day_start_hour 是 6 而不是 0：主人常弄到一两点
    （`people/owner.json` 实测「常忙到深夜，一点多还在弄升级」）。
    按 00:00 切日，23:50 和 00:10 会落到两个"天"里，一次熬夜被拆成两次梳理，
    两个半场都看不到完整的一天。
    """
    lt = time.localtime(float(ts))
    key = time.strftime("%Y-%m-%d", lt)
    if lt.tm_hour < int(day_start_hour):
        # 还没到当天的起始小时 → 仍属于**前一天**的窗口
        key = time.strftime("%Y-%m-%d", time.localtime(float(ts) - 86400))
    return key


# ---------------------------------------------------------------- 新颖度（T-12）
#
# 为什么用「新颖度」而不是「字数」
# -------------------------------
# 主人 2026-11-04 的观察：「人类面对新事物时感觉时间过的特别慢，因为新信息太多、
# 重复度低，大脑一直在学习和整理；过日复一日的日子感觉特别快，因为新信息太少，
# 大脑没怎么工作，感觉是"半睡眠"一样。」
#
# 这段话直接给出了度量定义：**整理的价值来自"新"，不来自"多"。**
# 重复说一百遍「我今天好累」，主观上等于什么都没发生，也不值得唤醒一次模型反刍。
#
# 实测支持：全库可信 user turns 11010 字里真正新颖的只有 4222 字（38%），
# 六成是重复的。而且系统自注入（`[系统任务：主动对话]` 每条 423 字）按字数算占 28%，
# 按新颖度算只占 11% —— **新颖度会自动把自注入压下去，不用维护黑名单**。
#
# 但新颖度也不能单用（实测反例）：17 字的全新废话新颖度 100%，
# 会排在 1341 字 / 71% 的深入讨论前面。所以用**乘积**：
#
#     novel_chars = Σ(每条字数 × 该条相对历史的新颖度)
#
# 这正好对应那个机制：大脑负荷 ≈ 接触到的字数 × 其中没见过的比例。

#: 新颖度用的 n-gram 长度。3 在中文上够用（2 太吵，4 容易漏）。
NOVELTY_NGRAM = 3
#: 持久化的 n-gram 集合上限。太小会把老话题重新算成"新"，太大则膨胀。
NOVELTY_MAX_GRAMS = 50_000


def _ngrams(text: str, n: int = NOVELTY_NGRAM) -> set[str]:
    """取一段文本的去空白 n-gram 集合。文本太短就整段当一个 gram。"""
    s = re.sub(r"\s+", "", str(text or ""))
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _strip_channel_prefix(text: str) -> str:
    """去掉 `[私聊] 主人: ` / `[群聊·xx] 某人: ` / `主人: ` 这类前缀。

    前缀不是"新信息"：同一个人每天说话前缀都一样，留着会把新颖度算高。
    """
    out = re.sub(r"^\[[^\]]{0,40}\]\s*[^:：]{1,24}\s*[:：]\s*", "", str(text or ""))
    out = re.sub(r"^主人\s*[:：]\s*", "", out)
    return out


def _novelty_store() -> set[str]:
    """读持久化的 n-gram 历史集合。

    ⚠ **必须持久化**：不存的话每次重启都从零开始，同一句老话会被反复算成"新"，
    度量就退化成纯字数了。

    并发：调用方（`_pending_novel_chars` / `_commit_novelty`）都在
    `_NOVELTY_LOCK` 里调用它 + `_novelty_save`，保证"读-改-写"是原子的。
    """
    try:
        with open(NOVELTY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        grams = data.get("grams") if isinstance(data, dict) else None
        if isinstance(grams, list):
            return {str(g) for g in grams if g}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return set()


def _novelty_save(seen: set[str]) -> None:
    """存回 n-gram 集合（滚动上限 `NOVELTY_MAX_GRAMS`）。

    ⚠ `.tmp` 文件名是**固定的**，所以必须由调用方持有 `_NOVELTY_LOCK` ——
    否则两个线程会同时往同一个 `.tmp` 里写，互相截断，最后 `os.replace`
    换上去的可能是半截内容（表现为 gram 集突然变小 → 老内容被当成新的）。
    """
    grams = sorted(seen)[-NOVELTY_MAX_GRAMS:]
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = NOVELTY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"grams": grams, "n": NOVELTY_NGRAM}, fh, ensure_ascii=False)
        os.replace(tmp, NOVELTY_PATH)
    except OSError:
        pass


def _pending_novel_chars(now: float | None = None) -> dict:
    """算「自上次整理以来，攒了多少**有效新颖**内容」。

    只看 `role='user'`（她的回复是模型输出，不是外部新信息），
    且只算可信渠道（`pet` / `qq_private` / `qq_group:*`）——
    `internal` / `qq_unknown` 那些既不参与巩固，也不该参与"有没有活干"的判断。

    **不需要**再手工排系统自注入：那 8 条 `[系统任务：主动对话]` 除了第一次，
    后面逐字重复，新颖度接近 0，自己就沉下去了。

    Returns:
        `{novel_chars, chars_total, novelty_ratio, new_turns, idle_minutes}`
        —— **`novel_chars` 是唯一判定用的量**，其余只进审计供日后校准。
    """
    import gateway

    st = load_state()
    since_ts = float(st.get("last_daily_review_ts") or 0)
    out = {"novel_chars": 0.0, "chars_total": 0, "novelty_ratio": 0.0,
           "new_turns": 0, "idle_minutes": None}
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select content from turns"
                " where session=? and role='user' and ts > ?"
                "   and (channel=? or channel=? or channel like ?)"
                " order by id",
                (
                    gateway.SHARED_SESSION,
                    since_ts,
                    migrate_channel.CHANNEL_PET,
                    migrate_channel.CHANNEL_QQ_PRIVATE,
                    migrate_channel.CHANNEL_QQ_GROUP + ":%",
                ),
            ).fetchall()
    except sqlite3.Error:
        return out

    seen = _novelty_store()
    novel = 0.0
    chars = 0
    for (content,) in rows:
        text = _strip_channel_prefix(str(content or ""))
        grams = _ngrams(text)
        n_chars = len(re.sub(r"\s+", "", text))
        if not grams or not n_chars:
            continue
        ratio = len(grams - seen) / len(grams)
        novel += n_chars * ratio
        chars += n_chars
        seen |= grams
        out["new_turns"] += 1

    out["novel_chars"] = round(novel, 1)
    out["chars_total"] = chars
    out["novelty_ratio"] = round(novel / chars, 3) if chars else 0.0
    try:
        idle = _absence_summary(now)
        out["idle_minutes"] = (
            round(idle["absent_hours"] * 60, 1) if idle["absent_hours"] is not None else None
        )
    except Exception:  # noqa: BLE001 —— 算不出闲置不影响判定
        pass
    return out


def _commit_novelty() -> None:
    """整理完之后把这段时间的 n-gram 并入历史集合（**只有成功才调**）。

    不调的话同一批内容下次还会被算成"新"，阈值永远满足 → 每轮都触发。

    ⚠ 2026-11-04（第六轮）：**整段读-改-写必须在 `_NOVELTY_LOCK` 里**。
    这个函数有两个入口（定时梳理线程 + 后台「现在梳理一次」按钮），
    并发时会丢数据：
        A 读 seen₁ → B 读 seen₁ → A 写 seen₁∪X → B 写 seen₁∪Y
        → 最终只剩 seen₁∪Y，**A 加的 X 永久丢失**
    丢的后果很隐蔽：那批内容下次被重新算成"新" → `novel_chars` 虚高
    → 触发本该跳过的梳理 → 白花钱。
    """
    import gateway

    st = load_state()
    since_ts = float(st.get("last_daily_review_ts") or 0)
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select content from turns"
                " where session=? and role='user' and ts > ?"
                "   and (channel=? or channel=? or channel like ?)",
                (
                    gateway.SHARED_SESSION,
                    since_ts,
                    migrate_channel.CHANNEL_PET,
                    migrate_channel.CHANNEL_QQ_PRIVATE,
                    migrate_channel.CHANNEL_QQ_GROUP + ":%",
                ),
            ).fetchall()
    except sqlite3.Error:
        return
    with _NOVELTY_LOCK:
        seen = _novelty_store()
        for (content,) in rows:
            seen |= _ngrams(str(content or ""))
        _novelty_save(seen)


def _daily_review_should_run(now: float | None = None, st: dict | None = None) -> tuple[bool, str]:
    """这一轮该不该跑梳理。返回 `(要不要跑, 原因)`。

    四个条件全满足才跑：
      ① 到点了：处于日窗口内，且已过 `earliest_hour`
      ② 安静了：距最后一句话 ≥ `idle_after_minutes`
      ③ 本窗口还没跑过（`once_per_day`）
      ④ 有素材：反刍要当日对话；否则缺席 ≥ `min_absence_hours`（缺席即素材）

    原因字符串直接进审计，**必须能回答"为什么没跑"** ——
    否则线上永远只能看到"梳理期没动"，查不出卡在哪一条。
    """
    cfg = _daily_review_cfg()
    if not cfg["enabled"]:
        return False, "disabled"

    ts = float(now if now is not None else time.time())
    state = st if st is not None else load_state()

    # ① 到点了没有 —— 先判这个：它是**更根本**的闸，审计里报 too_early
    #    比报 not_idle 更有指导意义（"等到 21:00 就行" vs "等他安静下来"）。
    lt = time.localtime(ts)
    if lt.tm_hour < cfg["earliest_hour"]:
        return False, "too_early"

    # ② 安静了没有 —— 「他还在聊」是最常见的挡住原因
    absence = _absence_summary(ts)
    idle_min = absence["absent_hours"] * 60 if absence["absent_hours"] is not None else None
    if idle_min is not None and idle_min < cfg["idle_after_minutes"]:
        return False, "not_idle"

    # ③ 本窗口跑过没有
    if cfg["once_per_day"]:
        day = _review_day_key(ts, cfg["day_start_hour"])
        last_day = str(state.get("last_daily_review_day") or "")
        if last_day == day:
            return False, "already_ran_today"

    # ④ 有素材没有
    material = _daily_review_materials(ts)
    if not material["has_material"]:
        return False, "no_material"

    # ⑤ 双判断（T-12）：**信息量那条 + 昼夜节律那条**
    #
    # ①②③④ 是"什么时候**允许**跑"（安静、到点、今天没跑过、有素材）。
    # ⑤ 才是"**值不值得**跑"——而且分成两条独立的腿：
    #
    #   信息量条：有效新颖量达阈值 → 立刻跑（纯代码判定，不需要问模型）
    #   节律条  ：到点了但信息量不够 → **交给模型看一眼**，它可以选择跳过
    #
    # 「其余时间算睡眠」：心跳照常 5 分钟一跳但零成本，不调模型。
    novel = _pending_novel_chars(ts)
    novel_chars = float(novel.get("novel_chars") or 0)
    threshold = float(cfg["min_novel_chars"] or 0)
    if threshold > 0 and novel_chars >= threshold:
        return True, "ok"

    # 信息量不够。看是不是"到点看一眼"那条腿 —— 它允许模型自己判断后跳过，
    # 所以这里返回一个**明确不同的 reason**，让调用方知道该走探路而不是直接放弃。
    if not cfg["circadian_enabled"]:
        return False, "below_threshold"

    # 本日窗口已经探过一次（模型说跳过）→ 直接睡，别再花第二次调用的钱。
    # 没有这一道，同一个窗口会每 check_s（10 分钟）探一次。
    day = _review_day_key(ts, cfg["day_start_hour"])
    if str(state.get("last_daily_review_probe_day") or "") == day:
        return False, "probed_declined"

    if not novel_chars:
        # 完全没有新东西（新颖量为 0）：连"看一眼"都不值得，直接睡
        return False, "sleeping"
    # 有一点新东西但没到阈值 → 值得让模型看一眼再决定
    return True, "circadian_probe"


def _daily_review_materials(now: float | None = None) -> dict:
    """这次梳理能拿到什么素材，以及**哪些活动有资格跑**。

    这是矛盾 1 的修法：不能一刀切用「今天有没有说话」来决定跑不跑。
    缺席本身就是素材（而且是最强的素材之一），只是**不是每个活动都吃它**：

      * 反刍 ruminate        —— 今天真有对话才谈得上「哪句说错了」
      * 预演 prospect        —— 缺席也是素材（「他三天没来，下次来我该说什么」）
      * 心智理论 theory_of_mind —— 缺席是最强信号（「三天没来，是忙还是在躲」）
      * 自我叙事 self_narrative —— 缺席就是主题本身（「这三天的我是谁」）
    """
    cfg = _daily_review_cfg()
    ts = float(now if now is not None else time.time())
    timeline = _day_timeline(24.0, ts)
    absence = _absence_summary(ts)

    today_turns = int(timeline["turns_total"])
    is_absent = bool(absence["is_absent"]) and cfg["absence_is_material"]

    eligible: list[str] = []
    if today_turns > 0 or not cfg["ruminate_requires_today_turns"]:
        eligible.append("ruminate")
    if is_absent or today_turns > 0:
        eligible.extend(["prospect", "theory_of_mind", "self_narrative"])

    # 预算再削一层（配置里各活动最多几次；现在都是 1）
    eligible = [a for a in eligible if int(cfg["budget"].get(a, 1) or 0) > 0]

    return {
        "timeline": timeline,
        "absence": absence,
        "today_turns": today_turns,
        "eligible": eligible,
        "has_material": bool(eligible),
    }


# ---------------------------------------------------------------- 工具循环
def _tool_name(call) -> str:
    fn = (call or {}).get("function") or {}
    return str(fn.get("name") or "")


def _parse_tool_args(call) -> dict:
    fn = (call or {}).get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    return args if isinstance(args, dict) else {}


def _find_tool(calls, name: str) -> dict | None:
    for c in calls or []:
        if _tool_name(c) == name:
            return c
    return None


# ---------------------------------------------------------------- 念头 → 台词
def _allow_speak(st: dict) -> bool:
    """主动说话的安全刹车（纯代码，不经过模型）：总开关 + 每日上限 + 冷却。

    改造后 urgency 判断已移除——要不要说、多想说，由角色层（deepseek）决定，
    系统层只守住「**要不要允许她说**、每天最多几句、多久说一次」三道硬闸。

    ⚠ 2026-11-08 加**总开关**（主人要求，默认关）：
    `SYSTEM["proactive_speak"]["enabled"]` 为假时，整条主动说话链路直接不跑 ——
    连模型都不调，不只是"说不说"的问题。

    为什么默认关：主动说话是**会打扰人**的行为（气泡弹出来 + 语音出声）。
    这种能力不该装上就开着，得主人明确点头。后台「主动说话」卡片可以随时开。
    """
    if not bool((SYSTEM.get("proactive_speak") or {}).get("enabled", False)):
        return False
    now = time.time()
    last = float(st.get("last_speak_ts") or 0)
    count = int(st.get("speak_count_today") or 0)
    today = time.strftime("%Y-%m-%d")
    if count and last and time.strftime("%Y-%m-%d", time.localtime(last)) != today:
        # 计数属于昨天，跨天清零
        count = 0
        st["speak_count_today"] = 0
    if count >= int(SYSTEM.get("max_speak_per_day", 6)):
        return False
    if last and now - last < float(SYSTEM.get("cooldown_s", 1800)):
        return False
    return True


def _speak_log_text(change: dict) -> str:
    """主动说话成功后写进 `system_log` 的那一行。

    形如 `[系统] 主动说话：主人刚回来`。措辞照主人的例子
    （`append_system_log()` docstring 里那条），说的是**为什么开口**
    （在场状态跃迁），不是把台词再抄一遍 —— 台词本身已经在 `turns` 里了，
    流水账要的是"她做了什么"，不是"她说了什么"。

    Args:
        change: `_detect_change()` 的返回（这一轮心跳看到的变化）。

    Returns:
        已带 `[系统]` 前缀的正文；判断不出原因时退化成 `[系统] 主动说话`。
    """
    presence = ""
    if isinstance(change, dict):
        presence = str(change.get("presence") or "").strip()
    # 在场状态 → 人话。present/away/absent 见 `_presence_state()`。
    why = {"present": "主人刚回来", "away": "主人离开了一阵", "absent": "主人很久没来"}.get(
        presence, ""
    )
    if not why and isinstance(change, dict) and change.get("new_turns"):
        why = f"有 {int(change['new_turns'])} 条新对话"
    return f"[系统] 主动说话：{why}" if why else "[系统] 主动说话"


# ---------------------------------------------------------------- 心跳
def tick() -> None:
    """一次心跳。全程兜底，绝不抛穿（线程不能死）。"""
    try:
        _tick_body()
    except Exception as exc:  # noqa: BLE001 —— call_system 等任何异常都兜住
        try:
            _audit({"event": "heartbeat_error", "ok": False, "error": f"{type(exc).__name__}"})
        except OSError:
            pass


def _tick_body() -> None:
    # 阶段 3：先把到期的定时任务转成内心OS（纯代码，不调模型；没到点不产生调用）
    try:
        _fire_due_schedules()
    except Exception:  # noqa: BLE001 —— 定时任务出错不拦心跳
        pass
    # 能力 5「计划」：只做**临期触发**（纯代码 + 本地 SQL，不调模型、不花钱）。
    #
    # ⚠ 2026-11-07 纠正设计偏差：这里原来还有第二段
    #   `_close_plans_from_reply()` —— 心跳里读他的话、用 `plan.judge_outcome()`
    #   的三套词表猜成败。**整段删掉了**（审计 H.1 倾向的方案 C）：
    #     * 词表判不准：「我做完了」「完工了」「升级好了」全判 unknown（实测）；
    #     * 它只知道文本、不知道计划内容，所以「做完了」到底指升级还是指作业
    #       它分不出来，「还没说完」还会命中「还没」被误判成 failure；
    #     * 主人的原始设计本来就是「跟系统层说一嘴就一起判断了」
    #       （`plan/DMN审核.md` 板块二能力 5）。
    #   现在成败由**巩固那次模型调用**顺带判（钱不额外花），
    #   走 `_pending_plan_candidates()` → prompt 的 `pending_plans` 字段 →
    #   模型在 `system_consolidate.plan_verdicts` 里给裁决 →
    #   `apply_plan_verdicts()` 落库。心跳这条线只剩「触发」一件事。
    try:
        _fire_due_plans()
    except Exception:  # noqa: BLE001 —— 计划这条线出错不拦心跳
        pass
    st = load_state()
    change = _detect_change(st)
    if not change:
        # 没变化：不动，省一次模型调用。水位不变，下次还查同一批。
        # 但 last_tick_ts 要写——它记的是「心跳还活着」，不是「有事发生」。
        # 只在有变化时写的话，主人一整天不说话就会被误判成「心跳停了」。
        st["last_tick_ts"] = time.time()
        save_state(st)
        _audit({"event": "tick", "ok": False, "reason": "no_change", "change": {}})
        return
    # 有变化：先把水位记下（保证下次不重复触发）。
    if "last_turn_id" in change:
        st["last_seen_turn_id"] = change["last_turn_id"]
    if "presence" in change:
        st["last_presence_state"] = change["presence"]
    now = datetime.now()
    # 素材跨 pet + 主人私聊（群聊不进）。以前只读 pet，而 pet 停在 10-30，
    # 于是主动说话拿四天前的对话当"最近"，说出「刚刚说心情不好」这种话。
    recent = _read_recent_chat_multi(5)
    signals = {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": "星期" + "一二三四五六日"[now.weekday()],
        "seconds_since_last_turn": _seconds_since_last_turn(),
        "recent_chat": recent,
        # 让角色层知道素材有多旧：它才能决定说「刚才」还是「前几天」。
        # 以前没这个字段，四天前的对话被说成"刚刚"（13:22 那句就是）。
        "recent_chat_age_minutes": _recent_chat_age_minutes(recent),
        "change": change,
    }
    st["last_tick_ts"] = time.time()
    save_state(st)

    # 主动说话（决策权在角色层）：系统层只做刹车（额度+冷却），
    # 要不要说、说什么、怎么说、投递给桌宠，全由角色层 role_speak.maybe_speak 完成。
    # 彻底版：心跳不再调 qwen-flash 报念头，纯代码信号直接喂给角色层。
    spoke = False
    rendered = None
    # speak_status 要能区分四种「没说话」：闸门没过 / 她不想说 / 解析失败 / 没送到。
    # 混成一个 declined 会让「桌宠没开」看起来像「她不想理人」。
    speak_status = "blocked"
    allow = _allow_speak(st)
    if allow:
        try:
            rendered, speak_status = role_speak.maybe_speak(signals, st)
        except Exception:  # noqa: BLE001 —— 说话失败不拦心跳
            rendered, speak_status = None, "error"
        if rendered and rendered.get("text"):
            # 心情由角色层给出（她开口时，从台词的情绪标记里来）；投递已由 maybe_speak 内部完成
            st["emotion"] = rendered.get("emotion") or st.get("emotion")
            st["focus"] = str(rendered.get("text") or "")[:40]
            st["last_speak_ts"] = time.time()
            st["speak_count_today"] = int(st.get("speak_count_today") or 0) + 1
            spoke = True

    # 系统流水账：**只有真的送出去了**才记（2026-11-06 补，能力 5 第四节）。
    #
    # 判据用的是 `spoke`（= `rendered` 有正文）。它只会在
    # `role_speak.maybe_speak()` 返回 "spoken" 时为真 —— 那条路内部
    # `deliver_to_pet()` 已经成功，投递失败的会返回 (None, "delivery_failed")。
    # 所以下面这些**一条都不记**（统一标准：这个操作有"结果"值得未来参考吗）：
    #   * 被额度/冷却拦下（`_allow_speak` 为假，speak_status="blocked"）
    #   * 她自己选择不说（"declined"）
    #   * 解析失败（"parse_failed"）/ 投递失败（"delivery_failed"）
    # 前三类记进流水账会让"她今天主动找了主人几次"这个语义彻底失真。
    #
    # ⚠ 这里已经 `save_state()` 过了，**手上没有未提交的写事务**，
    #   所以用自己开连接的 `append_system_log()` 是安全的（坑 #60 说的是
    #   "持有未提交写事务时"才必须用 `_append_system_log_conn`）。
    if spoke:
        append_system_log(_speak_log_text(change))

    st["recent_thoughts"].append(
        {
            "ts": time.time(),
            "emotion": st.get("emotion"),
            "focus": st.get("focus"),
            "spoke": spoke,
            "declined": speak_status == "declined",
            "speak_status": speak_status,
            "text": (rendered or {}).get("text", ""),
        }
    )
    st["recent_thoughts"] = st["recent_thoughts"][-10:]
    save_state(st)
    _audit(
        {
            "event": "tick",
            "ok": True,
            "change": change,
            "spoke": spoke,
            "declined": speak_status == "declined",
            "speak_status": speak_status,
            "emotion": st.get("emotion"),
            "text": (rendered or {}).get("text", ""),
        }
    )


def _heartbeat_loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 —— 线程绝不能死
            try:
                _audit({"event": "heartbeat_error", "ok": False})
            except OSError:
                pass
        jitter = 1 + random.uniform(-0.2, 0.2)
        time.sleep(float(SYSTEM.get("heartbeat_s", 300)) * jitter)


# ---------------------------------------------------------------- 记忆巩固
EVENT_KINDS = {"emotion", "care_rejected", "deferred_care", "promise", "observation"}

#: `events` 里**真正当状态机用**的那几类（2026-11-03 主从划线）。
#:
#: 分工（见 `plan\小鱼-总计划_ 综合版.md` 第 3.1 / 3.2 节）：
#:
#:   * **长期记忆 = 自建库 `gateway\data\livingmemory.db`**（`gateway/memory/`，
#:     向量 + FTS5 混合召回）。插件 `livingmemory` 已于 2026-11-03 停用；
#:   * **`events` = 状态机**，只留"有状态、要推进"的东西：
#:
#:      care_given / care_rejected   —— 安慰三态（open→responded/rejected）
#:      deferred_care                —— 带 due_ts，要排班、到点捞起
#:      promise                      —— 谁答应了什么，要能回头查
#:
#:   * `observation` / `emotion` **不再是长期事实库**：它们表达的是
#:     "这个人是什么样、当时什么心情"，那是长期记忆该记住的事。
#:     网关再存一份只会得到两个都在记、却谁都不全的局面。
#:
#: 注意 `care_given` 不在这张表里 —— 它不是"抽出来的事件"，是
#: `record_care_given()` 在投递成功时**直接写**的，不经过抽取链路。
STATE_EVENT_KINDS = ("care_rejected", "deferred_care", "promise")
#: 抽取阶段允许产出的 kind。`observation` 仍然允许**抽**（它有别的用途：
#: 驱动人物画像），但 `_save_events` 会按 `STATE_EVENT_KINDS` 决定要不要落库。
OBSERVATION_KINDS = ("observation", "emotion")

# 明确拒绝的关键词（纯代码判定「关心被拒」，用于安慰三态；宁漏勿错，判不准就留 responded）
CARE_REJECT_WORDS = ("别管", "不用", "算了", "别吵", "烦", "闭嘴", "走开", "不要你")

#: `deferred_care` 漏给 `due_hours` 时的兜底小时数。
#:
#: 取 48h 是因为对话里「过两天」「等忙完这阵」的中值就在这儿——比 24h 更贴近
#: 「过一阵」，又没到 72h 那样放凉了。
#:
#: **这是兜底不是常态**：提示词已经要求 `deferred_care` 必须给 `due_hours`，
#: 这里只防模型漏给。不兜底的话，漏给的条目会变成一条 `status=deferred`、
#: `due_ts` 为 NULL、既不排班也不会被关单的死记录（体检读 events 时还以为是欠着事）。
DEFER_DEFAULT_HOURS = 48.0

EXTRACT_EVENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "system_extract_events",
        "description": "从最近对话里抽取结构化事件。没有值得记的就输出空数组。",
        "parameters": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["emotion", "care_rejected", "deferred_care", "promise", "observation"],
                            },
                            "subject": {
                                "type": "string",
                                "maxLength": 40,
                                "description": "这条事件关于谁/什么。一般填「主人」或「小菲鱼」；拿不准就留空。",
                            },
                            "evidence": {"type": "string", "maxLength": 120},
                            "due_hours": {
                                "type": "number",
                                "description": (
                                    "只有 deferred_care 用：多少小时后该回头看这件事，"
                                    "一般 24 到 72，越急越小。别的 kind 不要填。"
                                ),
                            },
                            "approach_hint": {
                                "type": "string",
                                "maxLength": 60,
                                "description": (
                                    "下次接近这个人的方式——怎么开口、别碰什么，"
                                    "写成一句能照着做的话（如「先问她吃没吃饭，别提工作」）。"
                                    "不是观察到的特征；想不到就不填。"
                                ),
                            },
                        },
                        "required": ["kind", "evidence"],
                    },
                },
            },
            "required": ["events"],
        },
    },
}

HEALTH_CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "system_health_check",
        "description": (
            "做一次系统体检：检查记忆库、内心OS、人格文件、心跳活性、最近审计错误，"
            "发现问题写进报告。只有你觉得系统状态可疑时才调用它；平时别调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "maxLength": 60},
            },
        },
    },
}

SYSTEM_CONSOLIDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "system_consolidate",
        "description": (
            "把最近积累的对话记忆整理成 1-2 条精炼观点；"
            "如果输入里有 `pending_plans`（待判定的计划），顺带在 `plan_verdicts` 里"
            "逐条判它们成没成。这是你唯一表达整理结果的方式，只调用这一个。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "insights": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "maxLength": 80},
                            "emotion": {"type": "string", "enum": ["happy", "sad", "shy", "calm", "surprised", "confused", "love", "angry"]},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["text", "confidence"],
                    },
                },
                "reason": {"type": "string", "maxLength": 100},
                # ⚠ `plan_verdicts` **不写在这里**（2026-11-07）。
                # 它由 `_consolidate_tools()` 在「本轮真有计划待判」时才拼进来 ——
                # 理由见 `_plan_verdicts_schema()` 的 docstring（永远为空的数组
                # 字段会诱导模型编内容，违反 `system_policy.md` 第 4 条）。
            },
            "required": ["insights"],
        },
    },
}


def _consolidate_tools(pending_plans: list[dict] | None = None,
                       forget_ids: list[str] | None = None) -> list:
    """巩固这一轮可用的工具清单。**按需拼可选字段，不用就整个不出现**。

    为什么按需拼（2026-11-07，把成败判定交给模型时加）
    ----------------------------------------------
    这个函数的全部意义是防止一个很具体的坏行为：如果 `plan_verdicts` /
    `forget_drop` 是**永远存在、永远是空数组**的字段，模型看到就会觉得
    "这个字段我该填点什么"，于是给不存在的 id 编裁决。`system_policy.md`
    第 4 条（不猜测不补全）和第 6 条（失败就承认失败）都是防这个的 ——
    那就别给它犯错的入口。

    所以：没有待判计划 → 工具定义里连 `plan_verdicts` 都不出现；
    没有遗忘候选 → 连 `forget_drop` 都不出现。

    Args:
        pending_plans: 本轮 `_pending_plan_candidates()` 的结果。
        forget_ids: 本轮遗忘中间档的候选 doc_id（`review_candidates()` 的第二项）。

    Returns:
        可直接传给 `call_system(tools=...)` 的列表。
    """
    if not pending_plans and not forget_ids:
        return [SYSTEM_CONSOLIDATE_TOOL, HEALTH_CHECK_TOOL]
    tool = copy.deepcopy(SYSTEM_CONSOLIDATE_TOOL)
    props = tool["function"]["parameters"]["properties"]
    if pending_plans:
        props["plan_verdicts"] = _plan_verdicts_schema()
    if forget_ids:
        props["forget_drop"] = _forget_drop_schema()
    return [tool, HEALTH_CHECK_TOOL]


def _forget_drop_schema() -> dict:
    """`forget_drop` 字段的 JSON Schema（遗忘中间档的「删」名单）。"""
    return {
        "type": "array",
        "maxItems": plan_rules.PLAN_VERDICT_MAX,
        "items": {"type": "string"},
        "description": (
            "`forget_review` 里那些「她以后再也不会用到」的记忆的 id，原样抄。"
            "**只填你有把握确实没价值的**；拿不准的一律不填 —— 不填只是"
            "「这轮先留着」，填错就是一条记忆被永久删除，而且不可恢复。"
            "候选全是该留的，就填空数组。没有 `forget_review` 时不要输出这个字段。"
        ),
    }


#: 「到点看一眼」用的探路工具（2026-11-04 T-12）。
#:
#: 为什么需要它：双判断的第二条腿是「到 21 点就看一眼，没什么要做的可以跳过」。
#: 但原来的 `daily_review()` 一旦进入就**把四类活动一次跑满**（每类一次模型调用），
#: 模型根本没有"这次不值"的出口。实测 11-03 那次：4 次活动调用 → 12 条念头。
#: 有了这个工具，探路只需 **1 次**便宜调用，模型说跳过就到此为止。
DAILY_REVIEW_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "system_daily_review_probe",
        "description": (
            "决定这次「回头看一眼」要不要真的做。看素材，如果你觉得今天没什么值得想的，"
            "就 skip；如果真有值得想的，就 pick 一个方向。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["skip", "ruminate", "prospect", "theory_of_mind", "self_narrative"],
                    "description": "skip=这次跳过；其余=做哪一类活动",
                },
                "reason": {
                    "type": "string",
                    "maxLength": 60,
                    "description": "一句话说明为什么（进审计，不给她看）",
                },
            },
            "required": ["decision"],
        },
    },
}

SYSTEM_PERSONA_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "system_persona_review",
        "description": "判断角色层人格是否需要微调，需要则给出修改建议。",
        "parameters": {
            "type": "object",
            "properties": {
                "need_change": {"type": "boolean"},
                "suggestion": {"type": "string", "maxLength": 200},
                "reason": {"type": "string", "maxLength": 100},
            },
            "required": ["need_change"],
        },
    },
}


def _read_turns_since(since_id: int, limit: int) -> list[dict]:
    """读 pet 来源、id 大于 since_id 的增量对话。只读，绝不写。

    ⚠ 这个读取器**只认 pet**，保留它是因为多渠道巩固里 pet 那条分支要用。
    人格复盘**已经改走** `_read_persona_review_turns()`——原因见那个函数：
    生产里 pet 只有 5 条、最后一条停在 10-30，而水位早就过了 892，
    于是 `id > 892 AND channel='pet'` 永远查不到，人格复盘一次都没跑过。
    """
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, role, content, ts from turns"
                " where session=? and channel=? and id>?"
                "   and coalesce(path,'') not in ('tool','approval','approve')"
                " order by id asc limit ?",
                (gateway.SHARED_SESSION, migrate_channel.CHANNEL_PET, since_id, limit),
            ).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in rows]
    except Exception:  # noqa: BLE001 —— 记忆读不到不拦巩固
        return []


def _read_persona_review_turns(since_id: int, limit: int) -> list[dict]:
    """人格复盘专用的增量读取：**跨可信渠道**，不再只读 pet。

    为什么必须改（2026-11-03 实测）
    --------------------------------
    `_review_persona()` 原来用 `_read_turns_since()`，而它写死 `channel='pet'`。
    生产实况：pet 只有 5 条、最后一条停在 10-30，真实对话全在
    `qq_private`(346) / `qq_group:示例群`(205)；而水位
    `last_persona_review_id` 已经是 892。

    → `id > 892 AND channel='pet'` 恒为空 → 审计里永远是 `persona_review:
      no_new_turns` → **人格微调这条线一次都没跑过**。

    为什么跨渠道是安全的
    --------------------
    人格是**全局的**，跟场合无关；而这里只要「主人本人的言行」当素材，
    不需要群友的。所以只取**能确认主体是主人**的那两类：

    * `pet` —— 本机桌宠，整条渠道都是主人；
    * `qq_private` —— 但必须先过 `_filter_owner_turns()`，只留
      `[私聊] 主人:` 开头的发言和紧随其后的回复。
      陌生人私聊、`[系统任务：主动对话]` 注入一律不进。

    群聊**不进**：群里主要是别人在说话，拿它塑造人格会让她越来越像群友，
    而不是主人认识的那个她。这是有意收窄，不是漏掉。

    Args:
        since_id: 水位（turns.id）。
        limit: 最多读多少条（按 id 升序取最早的，保证水位能稳定前进）。

    Returns:
        按 id 升序的 `{"id","role","content","ts"}` 列表；读不到返回 []。
    """
    import gateway
    out: list[dict] = []
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            for channel in (migrate_channel.CHANNEL_PET, migrate_channel.CHANNEL_QQ_PRIVATE):
                rows = conn.execute(
                    "select id, role, content, ts from turns"
                    " where session=? and channel=? and id>?"
                    "   and coalesce(path,'') not in ('tool','approval','approve')"
                    " order by id asc limit ?",
                    (gateway.SHARED_SESSION, channel, since_id, limit),
                ).fetchall()
                turns = [{"id": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in rows]
                if channel == migrate_channel.CHANNEL_QQ_PRIVATE:
                    turns = _filter_owner_turns(turns)
                out.extend(turns)
    except Exception:  # noqa: BLE001 —— 记忆读不到不拦复盘
        return []
    # 两条渠道各自有序，合起来要重新按 id 排，否则水位会跳来跳去
    out.sort(key=lambda t: int(t.get("id") or 0))
    return out[:limit]


# ---------------------------------------------------------------- 多渠道巩固（生产链路）
# 参与巩固的渠道只有三类：桌宠 pet / 主人私聊 qq_private / 有群名的 qq_group:<群名>。
# 其余（没群名的 qq_group、qq_unknown、internal、NULL）一律不抽 —— 判不出主体，
# 宁缺勿错（fail-closed）。以前这里把 pet 写死，真实库里 pet 只有 5 条、
# 真对话全在 qq_private / qq_group，于是巩固、事件抽取、人物建模全在空转。
CHANNEL_OWNER_KEY = "owner"  # 主人在内心OS / 人物画像里的归一键（pet + 主人私聊）

#: 巩固时喂给模型的「已有认知」条数上限。
#:
#: 主人的机制是「写之前先读一遍已存好的认知，自己判断该写什么」。
#: 取 20 是因为当前库规模就在这个量级（2026-11-07 清空前 13 条），
#: 全量喂得起；等库涨到几百条就得改成按关键词/语义筛相关的 ——
#: 那时不能靠"取最新 20 条"，因为相关的可能是很久以前那条。
EXISTING_COGNITIONS_LIMIT = 20

# turns 表没有 speaker 列，只能靠 handle_chat 写死在 content 里的前缀认人：
#   私聊  `[私聊] 主人: …`
#   群聊  `[群聊·示例群] 某人: …`
#   桌宠  `主人: …`
# 前缀对不上就不抽：认错人比漏抽一句危险得多。
OWNER_PRIVATE_RE = re.compile(r"^\[私聊\]\s*主人\s*[:：]")
GROUP_LABEL_RE = re.compile(r"^\[群聊·([^\]]+)\]")

# _consolidate_channel 的三种结果
_CH_OK = "ok"          # 这个渠道处理完了，水位已前进
_CH_SKIP = "skip"      # 没增量 / 增量不够，水位不动
_CH_HEALTH = "health"  # 模型这轮选了体检，一轮做一次就够


def _inner_channel(raw: str | None) -> str:
    """turns / events 的原始渠道 -> 内心OS 与人物画像用的渠道键。

    主人归一：`pet` 和「主人私聊」都是同一个人，计划第六节明确「主人画像可跨场合」，
    所以桌宠攒下的想法应该能在主人私聊里用上，反之亦然。
    **不动 events.channel** —— 那里必须保留 `qq_private` 原始值，隔离要能查证。
    """
    ch = str(raw or "").strip()
    if ch in (migrate_channel.CHANNEL_PET, migrate_channel.CHANNEL_QQ_PRIVATE):
        return CHANNEL_OWNER_KEY
    return ch


def _item_visible(item: dict, current: str) -> bool:
    """这条想法在当前渠道能不能看到：**本渠道 + 全局（无渠道的老条目）**。

    群聊产生的东西绝不出现在私聊/桌宠的 prompt 里，反之亦然；
    主人（owner）是唯一的跨场合例外，那在 `_inner_channel` 里已经折好了。
    """
    ch = _inner_channel(item.get("channel"))
    if not ch:
        return True  # 全局项：谁都能看（旧 inner_os.json 里的条目都归这一类）
    return bool(current) and ch == current


def _consolidate_channels() -> list[str]:
    """挑出「能判定主体」的渠道（pet 常驻）。

    桌宠永远在列表里：它是本机唯一可信来源，哪怕还没说过话也要参与
    （否则全新安装时巩固线程会因为「查不到渠道」整轮空转）。

    ⚠ 2026-11-04（T-05）**调查结论：`qq_unknown` 故意不进这个白名单。**

    当时的症状是"近 24h 的真实对话 100% 掉进 qq_unknown、一条都不巩固"。
    查完之后发现原因不是 bug，而是一个**必须保持 fail-closed** 的取舍：

      * 走 `qq_unknown` 的请求全部是 **content 为字符串**的 `/v1` 调用
        （`v1.log` 里 status=ok 的那 293 条），它们**不带 `<system_reminder>` 块**，
        所以拿不到 uid / 群名 —— 不是解析失败，是源头就没有；
      * 更关键：`qq_unknown` 里还混着 `[系统任务：主动对话]` 这类**系统自注入**
        （见 #1412 / #1414），把它放进巩固白名单，等于让系统把自己写的提示词
        当成主人的话去抽取事实；
      * 而且这一路 `speaker=""`，`is_owner("", False)` 恒假 —— 如果放松隔离，
        陌生人直连 `/v1` 说的话就会被当成主人的记忆。

    所以这里**保持原样**。真正的修法是让来源**可辨识**（给那个客户端一个
    明确的渠道键），不是放松白名单。为了让"又掉进黑洞"这件事不再静默，
    体检里加了比例告警（见 `_health_check` 的 `unknown_channel` 项）。

    `system_log` 也不在这个列表里 —— 但它**必须参与巩固**（主人：
    「有啥要判断的扔流水账里，跟系统层说一嘴就一起判断了」）。
    做法见 `_consolidate_system_log()`：它不按渠道走，而是在巩固时把新增的
    系统流水账**拼进 owner 那一路的素材**里。这样既不用给 `system_log` 造
    一套自己的水位，也不会让它被当成"某个人的对话"去抽事件。
    """
    import gateway
    chans = [migrate_channel.CHANNEL_PET]
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute("select distinct channel from turns").fetchall()
    except sqlite3.Error:
        return chans
    group_prefix = migrate_channel.CHANNEL_QQ_GROUP + ":"
    for (raw,) in rows:
        ch = str(raw or "").strip()
        if not ch or ch in chans:
            continue
        if ch == migrate_channel.CHANNEL_QQ_PRIVATE:
            # 主人私聊：逐条再按前缀判主体（见 _filter_owner_turns）
            chans.append(ch)
        elif ch.startswith(group_prefix) and len(ch) > len(group_prefix):
            # 有群名的群聊：群名就是这个渠道的隔离键，必须带全名，不能截成 qq_group
            chans.append(ch)
    return chans


def _read_channel_turns(channel: str, since_id: int, limit: int) -> list[dict]:
    """读某个渠道 id 大于 since_id 的增量对话。只读，绝不写。"""
    if channel == migrate_channel.CHANNEL_PET:
        # 桌宠沿用原读取器（测试里也按这个函数名做隔离替身）
        return _read_turns_since(since_id, limit)
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, role, content, ts from turns"
                " where session=? and channel=? and id>?"
                "   and coalesce(path,'') not in ('tool','approval','approve')"
                " order by id asc limit ?",
                (gateway.SHARED_SESSION, channel, since_id, limit),
            ).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in rows]
    except Exception:  # noqa: BLE001 —— 单渠道读不到不拦巩固
        return []


def _recent_channel_turns(channel: str, limit: int) -> list[dict]:
    """某个渠道最近的几条对话（只读，按 id 升序返回）。读不到返回空。"""
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, role, content, ts from turns"
                " where session=? and channel=? and coalesce(path,'') not in ('tool','approval','approve')"
                " order by id desc limit ?",
                (gateway.SHARED_SESSION, channel, max(1, int(limit))),
            ).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in reversed(rows)]
    except Exception:  # noqa: BLE001
        return []


def _filter_owner_turns(turns: list[dict]) -> list[dict]:
    """主人私聊里只留「主人说的 + 紧跟着她的回复」，其余全丢（fail-closed）。

    qq_private 里混着陌生人私聊和 `[系统任务：主动对话]` 这类插件注入，
    分不清主体就抽，等于把别人的话记到主人头上。
    """
    out: list[dict] = []
    owner_spoke = False
    for t in turns:
        if str(t.get("role") or "") == "user":
            owner_spoke = bool(OWNER_PRIVATE_RE.match(str(t.get("content") or "").lstrip()))
            if owner_spoke:
                out.append(t)
        elif owner_spoke:
            # 回复只在「前一句确实是主人说的」时才跟着进来
            out.append(t)
    return out


def _filter_group_turns(channel: str, turns: list[dict]) -> list[dict]:
    """群聊只留「本群」的发言：标签里的群名必须和渠道键后缀一致。

    channel 本身已经带群名，这里再对一遍标签是防数据错位：万一某行被标错了
    渠道，宁可这一句不抽，也不能把别的群的话记到这个群头上。
    """
    name = channel[len(migrate_channel.CHANNEL_QQ_GROUP) + 1:]
    out: list[dict] = []
    ok_spoke = False
    for t in turns:
        if str(t.get("role") or "") == "user":
            m = GROUP_LABEL_RE.match(str(t.get("content") or "").lstrip())
            ok_spoke = bool(m) and str(m.group(1)).strip() == name
            if ok_spoke:
                out.append(t)
        elif ok_spoke:
            out.append(t)
    return out


def _route_turns(channel: str, raw: list[dict]) -> list[dict]:
    """按渠道把原始增量过滤成「可以抽的那部分」。"""
    if channel == migrate_channel.CHANNEL_QQ_PRIVATE:
        return _filter_owner_turns(raw)
    if channel.startswith(migrate_channel.CHANNEL_QQ_GROUP + ":"):
        return _filter_group_turns(channel, raw)
    return list(raw)  # pet：本机可信来源，整条渠道都是主人


def _people_channel(channel: str) -> str:
    """turns 渠道 -> 人物画像渠道键。

    主人私聊在 `_filter_owner_turns` 里已经确认过「是主人」，所以可以并进
    pet 那份 owner 画像（主人可跨场合）。其余渠道各存各的，物理隔离。
    """
    if channel == migrate_channel.CHANNEL_QQ_PRIVATE:
        return migrate_channel.CHANNEL_PET
    return channel


# ---------------------------------------------------------------- 结构化事件（DMN 阶段 1）
def _validate_events(args: dict, turns: list[dict] | None = None) -> list[dict]:
    """硬校验 system_extract_events 的参数。非法条目直接丢弃（fail-closed）。

    Args:
        args: 模型给的参数。
        turns: 这一批对话（可选）。给了就把**说话时间**带上：主人 2026-11-06 定的
            「创建时间以对话时间为准」——`create_time` 必须反映"他什么时候说的"，
            不是"什么时候提炼的"。攒了 4 天才巩固的那批如果按提炼时间算，
            在遗忘算法眼里会显得很新，于是永远删不掉。
            取这一批里**最新的一条**当这批的代表时间：一批对话通常是同一场，
            偏差以分钟计；比"全部取第一条"更贴近"这条记忆成型的那一刻"。
    """
    if not isinstance(args, dict):
        return []
    raw = args.get("events")
    if not isinstance(raw, list):
        return []
    batch_ts = 0.0
    for t in (turns or []):
        try:
            stamp = float(t.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        batch_ts = max(batch_ts, stamp)
    out = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in EVENT_KINDS:
            continue
        evidence = str(item.get("evidence") or "").strip()
        if not evidence:
            continue
        ev = {
            "kind": kind,
            "subject": str(item.get("subject") or "")[:40],
            "evidence": evidence[:120],
        }
        if batch_ts > 0:
            ev["ts"] = batch_ts
        if kind == "deferred_care":
            # 缺 due_hours / 解析失败 → 兜底 DEFER_DEFAULT_HOURS，**不丢事件**：
            # 「过一阵该回头看」这件事是真的，只是模型没给时间。
            try:
                hours = float(item.get("due_hours"))
            except (TypeError, ValueError):
                hours = DEFER_DEFAULT_HOURS
            ev["due_ts"] = time.time() + max(0.1, min(720.0, hours)) * 3600
        else:
            # 到期时间只属于 deferred_care：别的 kind 即使模型给了 due_hours 也丢弃，
            # 否则 promise 也带 due_ts，「什么时候该回头」就有第二个含义。
            ev.pop("due_ts", None)
        hint = str(item.get("approach_hint") or "").strip()
        if hint:
            ev["approach_hint"] = hint[:60]
        out.append(ev)
    return out


def _extract_events(turns: list[dict]) -> list[dict]:
    """从 turns 增量里抽取结构化事件（0-N 条）。抽取失败返回空，绝不丢原文。"""
    if not turns:
        return []
    msgs = [
        {"role": "system", "content": SYSTEM_POLICY},
        {
            "role": "system",
            "content": (
                "从下面这段对话里抽取值得记住的结构化事件。没有就输出空数组，不要硬凑。\n\n"
                "kind 怎么选（一段对话最多抽 5 条，只选最贴切的）：\n"
                "- emotion：主人明确表达或明显流露出**此刻**的情绪（不只是平淡地聊事）\n"
                "- care_rejected：小菲鱼的关心被明确拒绝或打断（「别管」「不用」「算了」这类）\n"
                "- deferred_care：她说「迟点」「过两天」「等忙完」「回头再看」「忙完这阵」"
                "「明天再说吧」这类话——现在不适合打扰，过一阵该回头看。"
                "**必须给 due_hours**（小时数，一般 24 到 72；越急越小）。\n"
                "  正例：她说「修 bug 的事迟点再说吧，这会儿不想碰」→ kind=deferred_care、"
                "due_hours=24（「这会儿」当天内回头看最合适，拖太久就凉了）。\n"
                "- promise：某一方答应了一件事（「我明天弄」「回头给你看」）\n"
                "- observation：值得长期记住的**事实**（作息、在忙什么、喜欢什么、身体情况）。"
                "**主人下的规定、边界、禁令、对小菲鱼的行为要求不算事实**——"
                "那是人格/策略层的事，不要抽成 observation。\n\n"
                "deferred_care 和 promise 的边界：promise 是「某一方**许诺要去做**某事」；"
                "deferred_care 是「**她现在不该打扰、过一阵该回头看**」。"
                "同一句话两者都沾时，**优先抽 deferred_care**（它驱动行为），不要重复抽两条。\n\n"
                "emotion 和 observation 的区别：emotion 是「此刻的状态」，"
                "observation 是「长期成立的事实」。同一句话可以两个都不抽，"
                "也可以只抽一个，别重复抽。\n"
                "evidence 必须是对话里真实出现过的原话、或紧贴原话的概括，不许编造、不许推测。\n"
                "subject 填「主人」或「小菲鱼」，拿不准就留空。\n\n"
                + json.dumps(
                    [{"role": t["role"], "content": t["content"]} for t in turns],
                    ensure_ascii=False,
                )
            ),
        },
    ]
    reply, _meta, calls = call_system(
        msgs,
        tool_choice={"type": "function", "function": {"name": "system_extract_events"}},
        tools=[EXTRACT_EVENTS_TOOL],
        max_tokens=600,
    )
    decide = _find_tool(calls, "system_extract_events")
    if decide is None:
        return []
    return _validate_events(_parse_tool_args(decide), turns)


def _stateful_event_kinds() -> tuple[str, ...]:
    """这个版本要**落库**的事件类型（状态机口径）。

    可在 `config.json` → `system.events_kinds_stateful` 覆盖；不配就用
    `STATE_EVENT_KINDS`。写成可配置是因为这张表会随"哪些东西需要状态"变化
    而变，写死在代码里的话，下次调整又要改代码。

    未知/空值一律回落到默认表（fail-safe：宁可多记几条状态事件，
    也不能因为一条坏配置把牵挂/承诺整条线静默关掉）。
    """
    raw = SYSTEM.get("events_kinds_stateful")
    if not isinstance(raw, (list, tuple)) or not raw:
        return STATE_EVENT_KINDS
    picked = tuple(str(x).strip() for x in raw if str(x).strip())
    return picked or STATE_EVENT_KINDS


def _save_events(events: list[dict], channel: str) -> int:
    """把事件写进 events 表；deferred_care 顺带排一条定时任务。返回写了几条事件。

    同样的 (kind, subject, payload, channel) 已经存在就跳过：巩固的水位只在
    「抽事件 + 蒸馏观点」都成功后才前进，中间任何一步失败都会让下一轮重读同一段
    对话——不查重的话，网络抖一下就在 events 里复制一份历史。

    群渠道的 `deferred_care` 只记事实、**不排班**（见下面 `skip_defer`）。

    **主从划线（2026-11-03）**：只落 `_stateful_event_kinds()` 里的类型。
    `observation` / `emotion` 仍然会被抽出来（人物画像要用），但不再当"长期事实"
    存进 events —— 长期记忆的唯一库是插件的 `livingmemory.documents`。
    这里**返回的条数只数真正落库的**，抽取条数由调用方的审计另外记。
    """
    if not events:
        return 0
    import gateway
    saved = 0
    kept_kinds = set(_stateful_event_kinds())
    is_group = migrate_channel.is_group_channel(channel)
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            for ev in events:
                if ev["kind"] not in kept_kinds:
                    # 不是状态机类型：不落库。事实已经由插件那条管道负责记，
                    # 网关再存一份就是"两个都在记、谁都记不全"。
                    continue
                payload = {"evidence": ev["evidence"]}
                if ev.get("approach_hint"):
                    payload["approach_hint"] = ev["approach_hint"]
                # 群渠道的 deferred_care：事实照记，但不排延后关心。
                # 主动关心的投递动作只在角色层（role_speak.deliver_to_pet → 桌宠本地口），
                # 系统层没有投递能力，群渠道更没有；排了班也没人上班，到点只会转成
                # 一条永远 pending 的内心OS，占队列位置、7 天后过期——换了个地方的死数据。
                skip_defer = is_group and ev["kind"] == "deferred_care"
                if skip_defer:
                    payload["skip_reason"] = "group_no_delivery"
                payload_text = json.dumps(payload, ensure_ascii=False)
                subject = ev.get("subject", "")
                dup = conn.execute(
                    "select 1 from events where kind=? and coalesce(subject,'')=?"
                    " and payload=? and coalesce(channel,'')=? limit 1",
                    (ev["kind"], subject, payload_text, channel),
                ).fetchone()
                if dup:
                    continue
                if skip_defer:
                    # 不排班 = 不会有后续动作，状态直接 closed（不是 deferred）
                    status = "closed"
                elif ev["kind"] == "deferred_care":
                    status = "deferred"
                else:
                    status = "open"
                # 只有「真排了班」的事件才有到期时间。群渠道那条不排班却带着未来
                # 的 due_ts，读 events 的人（体检、将来的状态卡）会以为还欠一件事，
                # 所以这里留 NULL：状态 closed + due_ts 空，语义才自洽。
                due_ts = None if skip_defer else ev.get("due_ts")
                cur = conn.execute(
                    "insert into events(ts, kind, subject, payload, status, due_ts, attempts, channel)"
                    " values(?,?,?,?,?,?,0,?)",
                    (
                        time.time(),
                        ev["kind"],
                        subject,
                        payload_text,
                        status,
                        due_ts,
                        channel,
                    ),
                )
                saved += 1
                if skip_defer:
                    _audit({
                        "event": "deferred_care_skipped", "ok": True,
                        "reason": "group_no_delivery", "channel": channel,
                        "event_id": cur.lastrowid,
                    })
                # deferred_care：排一条定时任务，到点由心跳捞起（阶段 3）
                elif ev["kind"] == "deferred_care" and ev.get("due_ts"):
                    conn.execute(
                        "insert into schedules(due_ts, kind, event_id, note, fired)"
                        " values(?,?,?,?,0)",
                        (
                            ev["due_ts"],
                            "recheck_care",
                            cur.lastrowid,
                            ev.get("approach_hint") or ev.get("subject") or "",
                        ),
                    )
            conn.commit()
    except sqlite3.Error as exc:
        # 假成功比失败更危险：表不存在/写不进去时必须能看见，不能悄悄返回 0。
        _audit({"event": "extract_events", "ok": False, "reason": f"db_error: {type(exc).__name__}"})
        return -1
    return saved


def _recheck_context(channel: str = "") -> dict:
    """定时任务触发时的「当前状态」，供角色层重判。

    计划要求：两天前的决定未必今天还成立——触发时必须附当前状态，
    不能拿当时的判断硬执行（主人正高兴，硬安慰反而扫兴）。

    Args:
        channel: 这条任务当初来自哪个渠道。当前状态**只取该渠道自己的对话**，
            否则群聊里那条「该回头看看了」会把桌宠/私聊的原话当上下文带过去。
    """
    secs = _seconds_since_last_turn()
    chat_channel = channel or migrate_channel.CHANNEL_PET
    ctx: dict = {
        "minutes_since_last_turn": round(secs / 60, 1) if secs is not None else None,
        "recent_chat": [
            {"role": t.get("role"), "content": str(t.get("content") or "")[:60]}
            for t in _recent_channel_turns(chat_channel, 3)
        ],
    }
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            row = conn.execute(
                "select status, ts from events where kind='care_given' order by id desc limit 1"
            ).fetchone()
        if row:
            ctx["last_care"] = {
                "status": row[0],
                "hours_ago": round((time.time() - float(row[1] or 0)) / 3600, 1),
            }
    except sqlite3.Error:
        pass
    return ctx


def _fire_due_schedules() -> int:
    """把到期的定时任务转成内心OS（带当时上下文，供角色层重判），标记 fired。

    纯代码，不调模型；没到点的任务不产生任何模型调用。
    任务来自哪条事件，就带回哪条事件的渠道：群聊里的牵挂不会变成桌宠/私聊的念头。
    """
    import gateway
    now = time.time()
    fired = 0
    skipped = 0
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, kind, event_id, note from schedules"
                " where due_ts <= ? and fired = 0 order by due_ts limit 5",
                (now,),
            ).fetchall()
            for sid, kind, event_id, note in rows:
                ctx = ""
                ev_channel = ""
                if event_id:
                    ev = conn.execute(
                        "select kind, subject, channel from events where id = ?", (event_id,)
                    ).fetchone()
                    if ev:
                        ctx = f"（当时的事：{ev[1] or ev[0]}）"
                        ev_channel = str(ev[2] or "")
                # 防御：历史数据里可能有群渠道的排班（现在 `_save_events` 已经不排了）。
                # 群渠道没有投递通道——`build_messages` 只在主人轮次 consume_inner_os，
                # push 出去只会变成一条永远 pending 的死数据。标记 fired 并记审计跳过。
                if migrate_channel.is_group_channel(ev_channel):
                    conn.execute("update schedules set fired = 1 where id = ?", (sid,))
                    skipped += 1
                    _audit({
                        "event": "schedules_skipped", "ok": True,
                        "reason": "group_no_delivery", "channel": ev_channel,
                        "schedule_id": sid,
                    })
                    continue
                push_inner_os([{
                    "type": "care_suggestion",
                    # 来源渠道跟着走：只回到它当初发生的那个场合
                    "channel": ev_channel,
                    "text": f"该回头看看了：{note or kind}{ctx}",
                    # 带上当前状态一起交给角色层，让它重新判断还该不该说、怎么说
                    "context": _recheck_context(ev_channel),
                }])
                conn.execute("update schedules set fired = 1 where id = ?", (sid,))
                fired += 1
            conn.commit()
    except sqlite3.Error:
        return 0
    if fired or skipped:
        _audit({"event": "schedules_fired", "ok": True, "fired": fired, "skipped": skipped})
    return fired


# ---------------------------------------------------------------- 系统流水账（system_log）
#: 计划（原牵挂）的两种被动型。主动型归「想法」，不在这里。
PLAN_KINDS = plan_rules.PLAN_KINDS


def _append_system_log_conn(conn: sqlite3.Connection, text: str) -> bool:
    """在**调用方已有的连接**上写一行系统流水账（不自己开连接）。

    为什么单独有这一个（2026-11-06 修）：`append_system_log()` 会自己
    `sqlite3.connect()`，而调用它的 `_fire_due_plans()` / `apply_plan_verdicts()`
    当时正**持着一个未提交的写事务**（改 `events.attempts` / `status`）。
    SQLite 同一个库文件上第二个连接要写 → `database is locked`，
    而那行流水账被 `except sqlite3.Error` 吞成一条审计 —— 表现为
    「计划触发了、内心OS 也有了、流水账却一条都没有」，而且不报错。

    ⚠ 2026-11-07：原来这里的第二个例子是 `_close_plans_from_reply()`，那个函数
    已经删掉了（成败判定改由模型给、由 `apply_plan_verdicts()` 执行）。
    新函数同样**在事务里**（一条 update + 一条 insert + 一条流水账），
    所以必须继续走这条 conn —— 别改回 `append_system_log()`。

    测试 `test_只触发一次不反复催` / `test_模型判成功才销毁计划` 抓到的就是它。

    所以：**已经在事务里的时候用这个**，别用会自己开连接的那个。
    """
    body = str(text or "").strip()
    if not body:
        return False
    import gateway
    conn.execute(
        "insert into turns(session, role, content, path, channel, ts)"
        " values(?,?,?,?,?,?)",
        (
            gateway.SHARED_SESSION,
            "system",
            body,
            "chat",
            migrate_channel.CHANNEL_SYSTEM_LOG,
            time.time(),
        ),
    )
    return True


def append_system_log(text: str) -> bool:
    """往 `turns` 写一行**系统操作记录**（渠道 `system_log`，全局不隔离）。

    主人原话：
        就搞成这种「[系统] 计划触发：问他升级的事」流水账里呗，
        **其他系统操作也扔流水账里**。

    为什么必须新开一个渠道而不是复用 `internal`：见 `migrate_channel.CHANNEL_SYSTEM_LOG`
    的注释 —— 两者语义正好相反（`internal` 任何上下文都不进，`system_log` 必须让
    模型看到）。

    记什么：**只在"有结果"时记**。判断标准是「这个操作有结果值得未来参考吗」。
    不记：调用模型的 prompt、没活干的空转、被额度拦下的尝试。

    记什么（主人的例子）：
        [系统] 计划触发：问他升级的事
        [系统] 巩固完成：写入 3 条记忆
        [系统] 主动说话：主人刚回来
        [系统] 体检：发现 2 个问题

    Args:
        text: 已带 `[系统]` 前缀的正文（用 `plan.system_log_trigger()` 这类函数拼）。

    Returns:
        真的写进去了返回 True。库写不了返回 False（不抛 —— 记流水账失败
        绝不能拦"计划触发"这种正事）。

    ⚠ **已经在写事务里的时候不要用它** —— 用 `_append_system_log_conn(conn, ...)`。
      自己开第二个连接去写同一个库会 `database is locked`。
    """
    body = str(text or "").strip()
    if not body:
        return False
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            _append_system_log_conn(conn, body)
            conn.commit()
    except sqlite3.Error as exc:
        _audit({
            "event": "system_log_write", "ok": False,
            "reason": f"db_error: {type(exc).__name__}",
        })
        return False
    return True


def _plan_silence_days() -> float:
    try:
        return float(SYSTEM.get("plan_silence_days", plan_rules.DEFAULT_SILENCE_DAYS))
    except (TypeError, ValueError):
        return plan_rules.DEFAULT_SILENCE_DAYS


def _fire_due_plans() -> int:
    """**计划临期**（能力 5）：沉默期满了就把提醒送到她面前。

    纯代码，不调模型。主人定的规则：

      * 10 天**从计划创建时算**；
      * 临期 → 系统层写成一条想法上浮到内心OS；
      * **提醒送到她面前就算「触发」**（她选择不说也算）；
      * 同时记一行 `system_log`，将来巩固时模型能一起判断。

    「已经触发过」用 `events.attempts` 记（复用现成列，不新加表）：
    0 = 还没触发，>=1 = 已经送过一次。**只触发一次**，不反复催。

    Returns:
        这一轮触发了几条。
    """
    import gateway
    now = time.time()
    days = _plan_silence_days()
    fired = 0
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, ts, kind, subject, payload, channel from events"
                " where kind in (?,?) and status in ('open','deferred')"
                "   and coalesce(attempts,0) = 0"
                " order by ts limit 10",
                PLAN_KINDS,
            ).fetchall()
            for eid, ts, kind, subject, payload, channel in rows:
                created = float(ts or 0)
                if not plan_rules.is_due(created, now, days):
                    continue
                ch = str(channel or "")
                # 群渠道没有投递通道（同 `_save_events` 的 skip_defer 理由），
                # 触发只会变成一条永远 pending 的死数据 → 不触发，落 closed。
                if migrate_channel.is_group_channel(ch):
                    conn.execute(
                        "update events set status='closed', attempts=1 where id=?", (eid,)
                    )
                    _audit({
                        "event": "plan_skipped", "ok": True,
                        "reason": "group_no_delivery", "event_id": eid,
                    })
                    continue
                try:
                    payload_obj = json.loads(payload or "{}")
                except json.JSONDecodeError:
                    payload_obj = {}
                evidence = str(payload_obj.get("evidence") or "") or str(subject or "")
                last = None
                try:
                    last_ts = conn.execute(
                        "select max(ts) from turns where channel=? and role='user'",
                        (ch or migrate_channel.CHANNEL_PET,),
                    ).fetchone()
                    last = float(last_ts[0]) if last_ts and last_ts[0] else None
                except (sqlite3.Error, TypeError, ValueError):
                    last = None
                push_inner_os([{
                    "type": "care_suggestion",
                    "channel": ch,
                    # 措辞由代码拼：给她「当时发生了什么」，不是「该怎么说话」
                    "text": plan_rules.trigger_line(evidence, now=now,
                                                    last_turn_ts=last, subject=subject or ""),
                    "context": _recheck_context(ch),
                }])
                conn.execute("update events set attempts=1 where id=?", (eid,))
                _append_system_log_conn(
                    conn, plan_rules.system_log_trigger(evidence, subject or "")
                )
                fired += 1
            conn.commit()
    except sqlite3.Error:
        return 0
    if fired:
        _audit({"event": "plan_fired", "ok": True, "fired": fired, "silence_days": days})
    return fired


def _plan_verdicts_schema() -> dict:
    """`plan_verdicts` 字段的 JSON Schema（一段，不是完整工具定义）。

    为什么拆成函数而不是写进 `SYSTEM_CONSOLIDATE_TOOL` 的字面量（2026-11-07）：
    这个字段**只在本轮真有计划待判时才该出现**。写成字面量的话模型会看到
    一个永远为空的数组字段，然后开始给空数组编东西（`system_policy.md` 第 4 条
    「不猜测，不补全」就是防这个的）。所以由 `_consolidate_tools()` 按需拼。

    Returns:
        直接放进 `properties` 的 schema 片段。
    """
    return {
        "type": "array",
        "maxItems": plan_rules.PLAN_VERDICT_MAX,
        "description": (
            "逐条判断 `pending_plans` 里那些计划成没成。只判给出来的 id，"
            "原样抄 id。看不出结果就填 unknown —— unknown 只表示「这轮不判」，"
            "计划会留着；而误判成 success/failure 会让一条真实的事被永久忘掉。"
            "没有 `pending_plans` 时不要输出这个字段。"
        ),
        "items": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "`pending_plans` 里的 `id`，原样抄。",
                },
                "verdict": {
                    "type": "string",
                    "enum": list(plan_rules.PLAN_VERDICTS),
                    "description": (
                        "success=他明确说办成了；failure=他明确说没办成/反悔/忘了；"
                        "unknown=看不出来（包括他压根没提这件事）"
                    ),
                },
                "evidence": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "依据：他哪句话让你这么判（引原文片段，别改写）",
                },
            },
            "required": ["id", "verdict"],
        },
    }


def _pending_plan_candidates(limit: int | None = None) -> list[dict]:
    """取**已经触发过、还没收尾**的计划，连「触发之后他说了什么」一起。

    2026-11-07 新加。它替代了原来的 `_close_plans_from_reply()` ——
    那套是「读他的话 → 用词表猜成败」，实测缺口很大（审计 H.1：『我做完了』
    『完工了』『升级好了』全判 unknown），根因是它只看文本、不知道计划讲什么。
    现在改成「把素材喂给巩固那一次模型调用 → 模型给裁决」，判定权归模型。

    窗口条件（每条都是踩过坑才写上的）：
      * `kind in PLAN_KINDS`：只有 promise / deferred_care 是计划；
      * `status in ('open','deferred')`：已经关掉的不再判；
      * `coalesce(attempts,0) >= 1`：**只有触发过的才判**。没触发 = 提醒还没送到
        她面前 = 他根本没被问过，这时候判成败等于替一个没发生过的对话打分；
      * `order by ts`：拖最久的先判。

    取话的窗口（项目地图坑 #64 后半，沿用 2026-11-06 修过的口径）：
      `order by ts desc, id desc limit 10` 取**最新** 10 条，再 `reversed()` 翻回
      时间正序。原来是 `order by ts limit 10`（**正序取最老 10 条**）——计划触发后
      只要又有 10 条闲聊，窗口就全是闲聊，那句真正的回答（「我给忘了」）被挤出
      窗口，判不出成败。实测：放 1 条能判出来，再塞 10 条闲聊就判不出来；
      生产上 `#3` 计划创建后已经有 18 条 user 消息，早就超过窗口。

    Args:
        limit: 最多取几条，缺省 `plan_rules.PLAN_VERDICT_MAX`。

    Returns:
        `[{"id", "plan", "subject", "created_days_ago",
        "triggered_days_ago", "said_after_trigger", "channel"}]`；
        读库失败返回 `[]`（巩固照常跑，不能因为计划读不到就整轮停摆）。
    """
    import gateway
    now = time.time()
    try:
        cap = int(limit if limit is not None else plan_rules.PLAN_VERDICT_MAX)
    except (TypeError, ValueError):
        cap = plan_rules.PLAN_VERDICT_MAX
    cap = max(1, min(cap, plan_rules.PLAN_VERDICT_MAX))

    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, ts, kind, subject, payload, channel from events"
                " where kind in (?,?) and status in ('open','deferred')"
                "   and coalesce(attempts,0) >= 1"
                " order by ts limit ?",
                (*PLAN_KINDS, cap),
            ).fetchall()
            items: list[dict] = []
            for eid, ts, _kind, subject, payload, channel in rows:
                ch = str(channel or "")
                # 群渠道不触发（`_fire_due_plans` 里 group_no_delivery 直接落 closed），
                # 所以正常到不了这里；但历史数据万一有 attempts>=1 的群计划，
                # 也不能拿群友的闲话去判主人有没有兑现 —— 那是替别人记账。
                if migrate_channel.is_group_channel(ch):
                    continue
                try:
                    payload_obj = json.loads(payload or "{}")
                except json.JSONDecodeError:
                    payload_obj = {}
                evidence = str(payload_obj.get("evidence") or "") or str(subject or "")
                chans = CARE_RESPONSE_SIBLINGS.get(ch, (ch,) if ch else ())
                reply_text = ""
                if chans:
                    placeholders = ",".join("?" for _ in chans)
                    hits = conn.execute(
                        "select content, ts from turns"
                        f" where channel in ({placeholders}) and role='user' and ts > ?"
                        " order by ts desc, id desc limit 10",
                        (*chans, max(float(ts or 0), now - 60 * 86400)),
                    ).fetchall()
                    # desc 取「最近 10 句」，翻回正序再拼：保留他真实的说话先后。
                    # （判定本身已经交给模型了，而模型**读得懂顺序** —— 这比词表时代
                    #  更需要正序：倒着念会让「后来又说其实弄好了」变成「先弄好了」）
                    reply_text = "\n".join(str(r[0] or "") for r in reversed(hits))
                items.append(plan_rules._plan_verdict_item(
                    int(eid), evidence, str(subject or ""),
                    float(ts or 0),
                    # ⚠ 触发时刻**没存**（`_fire_due_plans()` 只把 attempts 改 1）。
                    # 这里必须传 None，**不能拿创建时间冒充** —— 11 天前创建、
                    # 刚才才触发的计划会被写报成「11 天前触发过」，模型看到
                    # "提醒他 11 天了还这么回"这种不存在的证据，只会判得更偏。
                    # 宁可给 null（= 不知道），让模型只看 `said_after_trigger`。
                    None,
                    reply_text, now, channel=ch,
                ))
    except sqlite3.Error:
        return []
    return plan_rules.plan_verdict_block(items)


def apply_plan_verdicts(raw_verdicts, candidates: list[dict]) -> int:
    """按**模型给的裁决**销毁计划 + 留痕 + 记流水账。返回真的收尾了几条。

    2026-11-07 新增，替代 `_close_plans_from_reply()`。这是主人原始设计里
    「系统层决定要不要保留、删除或更改计划」那个动作的落地：

        模型   → success / failure / unknown   ← 语义判断，只有它有上下文
        本函数 → closed + 留痕事件 + 流水账    ← 纯执行，不做任何判断

    为什么这么切：判定要「知道计划内容 + 读懂他说的话」，词表做不到；
    而落库这种活不该让模型干（它会编 id、会把状态写歪）。

    ⚠ **`unknown` 不做任何写入**，计划原样留着等下一轮：
       一次误判 = 一条真实计划被静默销毁，以后再也不提（项目地图坑 #64）。
       所以这里对 unknown 的处理是 `continue`，**不是**记一条「结果不明」的
       留痕 —— 那种记录会让体检和人物画像多出一条「主人提到过…，结果不明」
       的假事实，等于用垃圾数据确认了一次误判。

    ⚠ 写库**用一个连接、一个事务**：坑 #60 —— 在未提交的写事务里调
       `append_system_log()`（它自己开第二个连接）会 `database is locked`，
       那行流水账被 `except sqlite3.Error` 吞掉，表现为「计划收尾了、流水账
       一条都没有」，而且不报错。所以这里和 `_fire_due_plans()` 一样，
       全程走同一条 conn（`_append_system_log_conn`）。

    Args:
        raw_verdicts: 模型回的 `plan_verdicts` 原始值（任意脏数据）。
        candidates: 本轮喂给它的待判清单（`_pending_plan_candidates()` 的结果），
            用来做**幻觉 id 白名单** —— 模型编出来的 id 一律不认。
            同时它也是「计划内容/主体/渠道」的来源：模型只回 id + verdict，
            其余字段一律**以库里的为准**，不采信模型转述。

    Returns:
        真的收尾了几条（success + failure 的条数；unknown 不算）。
    """
    import gateway
    by_id: dict[int, dict] = {}
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        try:
            by_id[int(cand.get("id"))] = cand
        except (TypeError, ValueError):
            continue
    if not by_id:
        return 0
    verdicts = plan_rules.parse_verdicts(raw_verdicts, allowed_ids=by_id.keys())
    if not verdicts:
        return 0

    now = time.time()
    closed = 0
    unknown = 0
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            for eid, info in verdicts.items():
                verdict = info["verdict"]
                if not plan_rules.should_close(verdict):
                    # unknown：什么都不写，计划留着。下一轮它还在候选里。
                    unknown += 1
                    continue
                cand = by_id.get(eid) or {}
                subject = str(cand.get("subject") or "")
                evidence = str(cand.get("plan") or "") or subject
                conn.execute(
                    "update events set status=?, attempts=coalesce(attempts,0)+1 where id=?",
                    (plan_rules.closed_status(verdict), eid),
                )
                # 留痕：记进「事件」（observation），不是把计划改成"完成"。
                # 主人给的例子：成功「主人给我升级了」/ 失败「主人说要给我升级结果骗了我」。
                record = plan_rules.outcome_record(
                    verdict, evidence=evidence, subject=subject
                )
                conn.execute(
                    "insert into events(ts, kind, subject, payload, status, due_ts,"
                    " attempts, channel) values(?,?,?,?,?,NULL,0,?)",
                    (
                        now,
                        plan_rules.outcome_kind(verdict),
                        subject,
                        json.dumps(
                            # 模型给的依据也一起存：将来回答「她凭什么说这事成了」
                            # 时不用再重读一遍流水账。
                            {"evidence": record,
                             "verdict_basis": str(info.get("evidence") or "")},
                            ensure_ascii=False,
                        ),
                        "closed",
                        str(cand.get("channel") or "")
                        or migrate_channel.CHANNEL_PET,
                    ),
                )
                _append_system_log_conn(conn, plan_rules.system_log_outcome(record))
                closed += 1
            conn.commit()
    except sqlite3.Error:
        return 0
    if closed or unknown:
        _audit({
            "event": "plan_closed", "ok": True,
            "closed": closed, "unknown": unknown,
        })
    return closed


# ---------------------------------------------------------------- 安慰三态（DMN 阶段 4）
def record_care_given(rendered: dict, channel: str = "") -> None:
    """主动说话投递成功后记一条 care_given 事件（status=open，等回应/拒绝）。

    Args:
        rendered: 台词包（text / emotion）。
        channel: 这条关心**投递到哪个场合**，直接决定 `_mark_care_outcomes()`
            去哪条渠道找回应。**必须由调用方传真实渠道**。

    为什么不再是写死 pet（2026-11-03 修）
    ------------------------------------
    原来这里硬编码 `CHANNEL_PET`，而 `_mark_care_outcomes()` 又按
    `turns where channel=<事件的 channel>` 去找「有没有回应」。于是主人在
    **QQ 私聊**里回她，事件那边盯着 pet 渠道看，永远看不到 —— 三态判定失效，
    那两条 care_given 一直停在 open。

    主动说话当前只投递到桌宠（`role_speak.deliver_to_pet`），所以默认值仍是 pet；
    但接口留了 channel，将来 QQ 侧开主动关心时不用再改一次。
    """
    import gateway
    text = str(rendered.get("text") or "").strip()
    if not text:
        return
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            conn.execute(
                "insert into events(ts, kind, subject, payload, status, due_ts, attempts, channel)"
                " values(?,?,?,?,?,NULL,0,?)",
                (
                    time.time(),
                    "care_given",
                    "",
                    json.dumps(
                        {"text": text, "emotion": rendered.get("emotion")},
                        ensure_ascii=False,
                    ),
                    "open",
                    str(channel or migrate_channel.CHANNEL_PET),
                ),
            )
            conn.commit()
    except sqlite3.Error:
        pass


#: 判定「有回应」时，除了事件自己的渠道，还要一起看的**同一主体**渠道。
#:
#: 主人在桌宠和 QQ 私聊里是同一个人（`_inner_channel` 也把两者归一到 owner）。
#: 她主动在桌宠里说了句关心，主人转头在 QQ 私聊里回一句「嗯，知道了」——
#: 这也是回应。只盯单渠道会把这种正常情况判成「没回应」。
CARE_RESPONSE_SIBLINGS = {
    migrate_channel.CHANNEL_PET: (migrate_channel.CHANNEL_PET, migrate_channel.CHANNEL_QQ_PRIVATE),
    migrate_channel.CHANNEL_QQ_PRIVATE: (migrate_channel.CHANNEL_QQ_PRIVATE, migrate_channel.CHANNEL_PET),
}


def _mark_care_outcomes() -> int:
    """把还 open 的 care_given 标成 responded / rejected（纯代码，关键词判拒绝）。

    判定：该关心之后**同一主体**的渠道里有新对话 = responded；
    新对话里出现拒绝词 = rejected。没回应就保持 open，等下次再看。

    「同一主体」见 `CARE_RESPONSE_SIBLINGS`：桌宠和主人私聊算同一个人。
    群聊**不会**被当成回应渠道 —— 群里别人接话不代表主人回应了她的关心，
    那会把一次没人理的主动说话误判成 responded，下次继续用同样的方式打扰。
    """
    import gateway
    updated = 0
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, ts, channel from events"
                " where kind='care_given' and status='open' order by ts limit 20"
            ).fetchall()
            for eid, ts, channel in rows:
                ch = str(channel or "")
                chans = CARE_RESPONSE_SIBLINGS.get(ch, (ch,) if ch else ())
                if not chans:
                    continue  # 判不出主体，宁可不判（fail-closed）
                placeholders = ",".join("?" for _ in chans)
                hit = conn.execute(
                    "select content from turns"
                    f" where channel in ({placeholders}) and ts > ? and role='user'"
                    " order by ts limit 5",
                    (*chans, ts),
                ).fetchall()
                if not hit:
                    continue  # 没回应，保持 open
                texts = "".join(str(r[0] or "") for r in hit)
                new_status = (
                    "rejected"
                    if any(w in texts for w in CARE_REJECT_WORDS)
                    else "responded"
                )
                conn.execute("update events set status=? where id=?", (new_status, eid))
                updated += 1
            conn.commit()
    except sqlite3.Error:
        return 0
    if updated:
        _audit({"event": "care_outcomes", "ok": True, "updated": updated})
    return updated


def recent_care_hint(channel: str = migrate_channel.CHANNEL_PET) -> str:
    """上一次主动关心的结果，给 role_speak 拼进 prompt。

    计划要求：rejected 要影响**方式**，不只是时间。
    上次被拒了还照原样再来一次，就是复读机，不是关心。

    Args:
        channel: 只取**这个渠道**产出过的关心。主动关心目前只由桌宠产出
            （`role_speak` 走 `deliver_to_pet`），所以默认 `pet`。
            将来 QQ 侧也开主动关心时，调用方**必须**按当前渠道传：不限定渠道的话，
            群里被拒一次就会让桌宠不敢说话——那是跨场合串味。

    Returns:
        一句提示；没有相关的关心记录返回 ""。
    """
    import gateway
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            row = conn.execute(
                "select status, ts from events where kind='care_given'"
                " and coalesce(channel,'')=? order by id desc limit 1",
                (str(channel or ""),),
            ).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    status, ts = str(row[0] or ""), float(row[1] or 0)
    hours = (time.time() - ts) / 3600 if ts else 0
    if status == "rejected":
        return (
            f"上次你主动关心被明确拒绝了（{hours:.0f} 小时前）。"
            "这次别再追问、别说教，要么轻得多说一句，要么干脆先不说。"
        )
    if status == "responded":
        return f"上次你主动关心，他回应了（{hours:.0f} 小时前）。"
    return ""


# ---------------------------------------------------------------- 人物建模（DMN 阶段 5）
PEOPLE_DIR = os.path.join(DATA_DIR, "people")
PEOPLE_STALE_DAYS = 7          # 画像超过这么久没更新就自动降权（与 role_speak 同一口径）
PEOPLE_PROMPT_MAX_JSON = 300   # 画像 JSON 最多塞这么多字符
PEOPLE_PROMPT_MAX_LINE = 400   # 整行硬上限：群聊人多，别把上下文顶爆


def _people_key(channel: str) -> str:
    """channel -> 画像文件名。主人（pet）固定用 owner，其余按 channel 确定性编码。

    ⚠ 2026-11-04 修（P1-10）：原来是
        `"".join(c if c.isalnum() or c in "-_" else "_" for c in channel)[:80]`
    它有**三个撞名通道**，撞上就是"跨群画像串味"（把一个群的人当成另一个群的）：

      ① **不可逆替换**：`qq_group:a/b` 和 `qq_group:a_b` 都变成 `qq_group_a_b`
         → 同一个文件 → A 群的人设被 B 群的观察覆盖；
      ② **NTFS 大小写不敏感**：`qq_group:ABC` 和 `qq_group:abc` 同一个文件
         （Linux 上不会，但本项目只在 Windows 跑）；
      ③ **截断**：超过 80 字符直接切掉，两个长群名共享前 80 字符就合并了。

    现在的做法：**保留可读前缀 + 附一段哈希**。哈希用完整 channel 算，
    所以上面三条都不会再撞；同时前缀让"人打开 people 目录时还认得出是哪个群"。

    Returns:
        `owner` 或 `<可读前缀>__<sha1 前 10 位>`。长度可控，且一一对应。
    """
    if channel == migrate_channel.CHANNEL_PET:
        return "owner"
    raw = str(channel or "")
    if not raw:
        return "unknown"
    # 可读前缀：只留字母数字和 -_，其余变 _（纯为了人眼友好，不承担唯一性）
    prefix = "".join(c if (c.isalnum() or c in "-_") else "_" for c in raw)[:48]
    # 唯一性由哈希保证：用**完整原始 channel**（含大小写）算，所以 ①②③ 都解决
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}__{digest}" if prefix else f"ch__{digest}"


def _legacy_people_key(channel: str) -> str:
    """**旧的**命名规则（P1-10 之前）。只用于迁移：读老文件、写新文件。

    这段代码故意保留原样（包括它的三个撞名缺陷），因为它的唯一用途是
    "认出以前存的那份文件还在不在"。改它就等于找不到旧画像。
    """
    if channel == migrate_channel.CHANNEL_PET:
        return "owner"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel)
    return safe[:80] or "unknown"


def _people_path(channel: str) -> str:
    """画像文件路径（新命名）。"""
    return os.path.join(PEOPLE_DIR, _people_key(channel) + ".json")


def migrate_people_files() -> int:
    """把旧命名的人物画像迁到新命名（P1-10 配套）。返回迁移个数。

    为什么需要：`_people_key` 换了编码之后，`qq_group_示例群.json`
    这个名字就再也匹配不上了 —— 于是"群里的画像突然消失"，
    而人只会觉得"她又不认识群里的人了"。

    幂等：新文件已存在就不动（以新文件为准）；旧文件保留不删（人工确认后再清）。
    """
    moved = 0
    try:
        if not os.path.isdir(PEOPLE_DIR):
            return 0
        for name in os.listdir(PEOPLE_DIR):
            if not name.endswith(".json"):
                continue
            stem = name[:-5]
            if stem == "owner":
                continue          # owner 的键没变
            if "__" in stem:
                continue          # 已经是新格式
            # 旧文件名 → 反推 channel 是不可能的（不可逆），
            # 但**新键的前缀**就是旧名，所以直接从候选 channel 列表里找。
            for channel in _known_people_channels():
                if _legacy_people_key(channel) != stem:
                    continue
                dst = _people_path(channel)
                if os.path.exists(dst):
                    break         # 新文件已在，别覆盖
                os.replace(os.path.join(PEOPLE_DIR, name), dst)
                moved += 1
                break
    except OSError:
        pass
    return moved


def _known_people_channels() -> list[str]:
    """能从 turns 里反推出来的 channel 全集（迁移用）。

    不引入新依赖：直接查库。查不到就返回空（迁移什么也不做，安全）。
    """
    import gateway
    out: list[str] = []
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute("select distinct channel from turns").fetchall()
        out = [str(r[0]) for r in rows if r and r[0]]
    except sqlite3.Error:
        pass
    return out


def _read_people_profile(channel: str) -> dict:
    """读某场合的人物画像。没有返回空 dict。

    P1-10：新命名读不到时**回退读旧文件**——万一迁移没跑成（或进程刚好在
    迁移前就起来了），也别让画像凭空消失。
    """
    for path in (_people_path(channel),
                 os.path.join(PEOPLE_DIR, _legacy_people_key(channel) + ".json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _write_people_profile(channel: str, profile: dict) -> None:
    """原子写人物画像（按 channel 分文件，物理隔离）。"""
    os.makedirs(PEOPLE_DIR, exist_ok=True)
    path = _people_path(channel)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _people_enabled(channel: str, enabled: list[str] | None) -> bool:
    """这个渠道要不要建模。支持 `qq_group:*` 通配：来新群不用改配置。

    Args:
        channel: **画像渠道键**（已经过 `_people_channel()` 折算）。raw `qq_private`
            不该出现在这里——它不是可建模的主体（分不清主人/陌生人，fail-closed）。
        enabled: config 的 `system.people_channels`。

    Returns:
        命中精确值或「前缀:*」返回 True。

    只认「精确值」和「`前缀:*`」两种写法：给通配开更宽的口子（比如 `*`）等于给
    渠道隔离开洞，所以这里不做通用 glob。
    """
    want = str(channel or "").strip()
    if not want:
        return False
    for pattern in enabled or []:
        p = str(pattern or "").strip()
        if not p:
            continue
        if p == want:
            return True
        # "qq_group:*" -> 前缀 "qq_group:"：只能通配到同一渠道族，跨族不行
        if p.endswith(":*") and want.startswith(p[:-1]):
            return True
    return False


def people_prompt_line(channel: str) -> str:
    """把「当前场合这个人」的画像拼成一行，供角色层 prompt 直接用。

    Args:
        channel: turns 的**原始渠道**（`pet` / `qq_private` / `qq_group:<群名>`）。
            内部用 `_people_channel()` 换成画像键（qq_private 归一到 pet，即 owner）。

    Returns:
        一行可直接拼进 system 的文本；**没有画像返回 ""**（调用方据此决定加不加，
        别往 prompt 里塞空行）。

    群聊措辞与主人不同：群聊是「这个群里的人」，主人是「他」。把群画像写成「他」，
    模型会当成在说主人——分寸就全错了，这是隔离之外的第二个坑。
    """
    key = _people_channel(channel)
    data = _read_people_profile(key)
    prof = data.get("profile")
    if not isinstance(prof, dict) or not prof:
        return ""
    head = (
        "你对这个群里的人的了解："
        if migrate_channel.is_group_channel(key)
        else "你对他的了解："
    )
    age_hint = ""
    try:
        ts = float(data.get("updated_ts") or 0)
        days = (time.time() - ts) / 86400 if ts else 0.0
        if days > PEOPLE_STALE_DAYS:
            # 太久没更新就自动降权：别拿三周前的印象判断今天的人
            age_hint = f"（这是 {days:.0f} 天前的印象，可能已经不准了，别当成事实）"
    except (TypeError, ValueError):
        age_hint = ""
    blob = json.dumps(prof, ensure_ascii=False)
    # 先给标题和降权提示留位置：宁可截画像，也不能把「可能不准」这句挤掉
    cap = min(
        PEOPLE_PROMPT_MAX_JSON,
        PEOPLE_PROMPT_MAX_LINE - len(head) - len(age_hint) - 1,
    )
    if len(blob) > cap > 0:
        blob = blob[: max(1, cap - 1)] + "…"
    return (head + blob + age_hint)[:PEOPLE_PROMPT_MAX_LINE]


def _extract_json_obj(text: str | None) -> dict | None:
    """从模型输出里抠出 JSON 对象（纯 JSON 或带前后缀都行）。"""
    if not text:
        return None
    t = text.strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(t[start:end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def update_people_profile(channel: str, turns: list[dict]) -> bool:
    """从 turns 增量更新某个 channel 的人物画像。失败不影响巩固。

    注意：只处理**这个 channel 自己的** turns，不混合其它场合——这是隔离的前提。
    """
    if not turns:
        return False
    # 硬防护：turns 表没有 speaker 列，qq_private 里分不清「主人」和「陌生人」。
    # 在这个前提下对 qq_private 建模，等于把陌生人的观察记到主人头上（或反过来），
    # 是隐私错误。fail-closed：宁可这个人没有画像，也不能建一个混的。
    # 要支持这里，得先给 turns 加 speaker 列——那是另一件事。
    # 注意：多渠道巩固（`_consolidate_channel`）会先用 `_filter_owner_turns()` 把主人
    # 私聊逐条认出来，再以 `pet`（owner）名义调本函数，所以这条闸门仍然有效——
    # 它拦住的是「拿一坨混着陌生人的 qq_private 直接建模」。
    if channel == migrate_channel.CHANNEL_QQ_PRIVATE:
        _audit({
            "event": "people_profile",
            "ok": False,
            "channel": channel,
            "reason": "qq_private 无法区分主人/陌生人（turns 缺 speaker 列），拒绝建模",
        })
        return False
    old = _read_people_profile(channel)
    msgs = [
        {"role": "system", "content": SYSTEM_POLICY},
        {
            "role": "system",
            "content": (
                "你是后台人物建模模块：根据这段对话，更新「这个人」的画像。\n"
                "只写有证据支撑的观察，没证据的别编。每条 30 字以内。\n"
                '输出一个 JSON 对象：{"称呼": "...", "作息": "...", "情绪基调": "...", '
                '"在意的事": ["..."], "互动习惯": "..."}\n'
                "没有新观察就原样返回旧画像。不写姓名/账号/手机号/地址等隐私。\n\n"
                f"旧画像：{json.dumps(old.get('profile') or {}, ensure_ascii=False)}\n"
                "新对话："
                + json.dumps(
                    [{"role": t["role"], "content": t["content"]} for t in turns],
                    ensure_ascii=False,
                )
            ),
        },
    ]
    reply, _meta, _calls = call_system(msgs, tools=[], max_tokens=500)
    profile = _extract_json_obj(reply)
    if not isinstance(profile, dict) or not profile:
        return False
    _write_people_profile(channel, {
        "channel": channel,
        "updated_ts": time.time(),
        "profile": profile,
    })
    return True


def _apply_profile_hint(hint: dict, channel: str = migrate_channel.CHANNEL_PET) -> bool:
    """把梳理期对「他是什么样的人」的新认知**合并**进 people 画像（P0-5 的 B 路）。

    为什么不整体覆盖：梳理期只看到最近 24 小时，而画像是长期积累的。
    整体覆盖等于「用今天的印象抹掉一个月的观察」——这正是画像最容易烂掉的方式。
    所以这里只做**字段级合并**：模型给了哪个字段就更新哪个，没给就保持原样。

    为什么走这条路：`inner_os` 是一次性提醒（读走就 delivered，再不注入），
    而人物画像是**每一轮都带进 prompt** 的（`people_prompt_line`）。
    「他不喜欢被追问」这种认知必须每轮都在，不能只提醒一次。

    Args:
        hint: 部分画像，键取自既有 5 个字段（称呼/作息/情绪基调/在意的事/互动习惯）。
        channel: 画像渠道键，默认 pet（= owner）。

    Returns:
        是否真的写入了。
    """
    if not isinstance(hint, dict) or not hint:
        return False
    allowed = ("称呼", "作息", "情绪基调", "在意的事", "互动习惯")
    clean: dict = {}
    for k in allowed:
        if k not in hint:
            continue
        v = hint[k]
        if isinstance(v, list):
            items = [str(x).strip()[:40] for x in v if str(x).strip()][:6]
            if items:
                clean[k] = items
        else:
            s = str(v or "").strip()[:120]
            if s:
                clean[k] = s
    if not clean:
        return False

    data = _read_people_profile(channel)
    old = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    merged = dict(old)
    merged.update(clean)
    # 记一笔来源，将来要回滚/查证时能看出哪些字段是梳理期改的
    history = data.get("review_hints")
    history = history if isinstance(history, list) else []
    history.append({"ts": time.time(), "fields": sorted(clean.keys())})
    _write_people_profile(channel, {
        "channel": channel,
        "updated_ts": time.time(),          # 更新过就算"新印象"，降权计时重来
        "profile": merged,
        "review_hints": history[-20:],
    })
    _audit({
        "event": "people_profile_hint",
        "ok": True,
        "channel": channel,
        "fields": sorted(clean.keys()),
    })
    return True


def _validate_insights(args: dict) -> list[dict]:
    if not isinstance(args, dict):
        return []
    raw = args.get("insights")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:2]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:80]
        if not text:
            continue
        emotion = str(item.get("emotion") or "calm")
        if emotion not in EMOTIONS:
            emotion = "calm"
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        out.append({"text": text, "emotion": emotion, "confidence": round(conf, 2)})
    return out


def _system_signals() -> dict:
    """纯代码算一份系统健康快照，给模型判断「要不要体检」的依据。

    不给依据就让人做选择，等于没给选择权——模型只能靠猜，最后永远不选。
    这里全部是本地读取 / SQL 计数，不调模型、不花钱。
    """
    now = time.time()
    st = load_state()

    # 心跳：多久没跳了（相对 heartbeat_s 的倍数）
    hb_s = float(SYSTEM.get("heartbeat_s", 300))
    last_tick = float(st.get("last_tick_ts") or 0)
    tick_age = now - last_tick if last_tick else None

    # 内心OS 积压：pending 条数 + 已过期条数
    inner = _read_inner_os()
    pending = sum(1 for i in inner if i.get("state") == "pending")
    expired = sum(1 for i in inner if i.get("state") == "expired")

    # 最近 24h 审计错误数——复用 _audit_stats，别在这儿另立一套统计标准
    err = int(_audit_stats().get("errors") or 0)

    sig = {
        "heartbeat_s": hb_s,
        "minutes_since_last_tick": round(tick_age / 60, 1) if tick_age is not None else None,
        "inner_os_pending": pending,
        "inner_os_expired": expired,
        "audit_errors_24h": err,
        # 2026-11-04（第二十六轮）加：模型调用失败次数。
        # 为什么必须有：errors.log 里记的是**真实**的模型调用失败
        # （401 Unauthorized / 503 / 超时），而以前**没有任何体检项覆盖它**。
        # 实测：11-01 那段时间连续 8 次 401，她每条都只能回
        # 「我这会儿有点卡，等下再说好不好。」—— 而体检**全绿**。
        # 典型的监控盲区：故障真实发生，监控看不见。
        "model_errors_24h": _model_error_stats()["total"],
        "model_errors_401_24h": _model_error_stats()["unauthorized"],
    }
    # 把「什么算异常」直接写进信号里，省得模型自己定标准
    abnormal = []
    if tick_age is not None and tick_age > hb_s * 3:
        abnormal.append("心跳超过 3 个周期没跳")
    if pending >= int(INNER_OS_MAX * 0.8):
        abnormal.append("内心OS 积压接近上限（角色层可能没在消费）")
    if err >= 10:
        abnormal.append("最近 24 小时审计错误偏多")
    sig["abnormal"] = abnormal
    return sig


def _read_existing_cognitions(limit: int = 20) -> list[str]:
    """读长期记忆库里**已经存下的**认知，喂给巩固模型做「别重复」的判断。

    为什么需要这个（2026-11-07）
    ---------------------------
    主人反复强调的机制是：**模型在写认知之前，先读一遍已经存好的认知，
    再结合当天的信息决定写什么**。模型有智慧，它会自己判断该不该再写一条。

    但改之前，巩固 payload 里的 `existing_insights` 读的是 `inner_os.json`
    ——那是**还没说出口的想法队列**，用完即销毁，不是"已经记住的事"。
    于是模型在写认知时**看不见记忆库里有什么**，只能一条条往上加，
    实测出现过「他喜欢蓝色」逐字重复两遍。

    Args:
        limit: 最多取多少条。太少起不到"别重复"的作用，太多则 token 和
            噪音都涨上去。20 条覆盖了当前库的全部规模（清空前是 13 条）。

    Returns:
        正文列表（已去掉「；关键词」后缀，只留叙述句）。
        **任何异常都返回空列表**——读不到就不喂，不能因为这一步炸掉整轮巩固。
    """
    try:
        bridge = _memory_bridge()
        if bridge is None:
            return []
        store = bridge.store()
        if store is None:
            return []
        docs = store.list_documents(limit=limit)
        out: list[str] = []
        for d in docs:
            raw = str(d.get("text") or "").strip()
            if not raw:
                continue
            # 写入形状是「叙述句；关键词1；关键词2」——只把叙述句给模型看。
            # 关键词是给 FTS 用的，给模型看纯属噪音（还可能诱导它照抄）。
            head = raw.split("；", 1)[0].strip()
            if head:
                out.append(head)
        return out
    except Exception:  # noqa: BLE001 —— 读不到就不喂，不拦巩固
        return []


def _build_consolidate_messages(st: dict, turns: list[dict], channel: str = "",
                                pending_plans: list[dict] | None = None,
                                forget_review: list[dict] | None = None) -> list[dict]:
    """拼巩固请求。

    Args:
        channel: 这批 turns 来自哪个渠道。`existing_insights` 只取「本渠道 + 全局」
            的旧观点——把群聊提炼的句子塞进桌宠的 prompt，哪怕只是用来「别重复」，
            也算一次跨场合泄漏。
        pending_plans: 本轮要模型顺带判成败的计划（`_pending_plan_candidates()`）。
            **只在 owner 那一路传**，见下面。
        forget_review: 遗忘中间档的候选（`_maybe_forget_review()`）。
            **同样只在 owner 那一路传**：世界观上，长期记忆库是全局的
            （`memory_session_key()` 把 pet / qq_private 归一成 owner，
            群聊各存各的），而 `review_candidates()` 筛的是**全库**文档 ——
            把它喂给群渠道那一路，等于用群的模型调用去删主人私聊里的记忆。
            这是隔离红线，不是省钱。

    2026-11-06 加 `system_log`（能力 5 第四节的「全局流水账」）
    ---------------------------------------------------------
    主人定的机制：

        有啥要判断的扔流水账里，**跟系统层说一嘴就一起判断了**。

    所以这里把「上次巩固之后新增的系统操作记录」一并喂给模型，让它**一起判断**，
    而不是另开一次调用（那正是主人否决的"多消耗智慧"的做法）。

    这**不是**把系统记录冒充成对话：
      * 它们放在独立的 `system_log` 字段里，不混进 `new_turns`；
      * `_extract_events` 拿到的仍然只有真实对话（不会把 `[系统] 计划触发：…`
        抽成 promise，那会造出一条根本没人答应过的承诺）；
      * 只喂 owner 那一路：系统流水账是全局的，喂给每条渠道等于重复计费。

    2026-11-07 加 `pending_plans`（把计划成败判定交给模型，审计 H.1 方案 C）
    与 `forget_review`（接上遗忘的中间档，审计 G.2 第 1 条）
    ---------------------------------------------------------------------
    三个可选字段的形状和理由完全一致，**三条铁律同样适用**：

      * 独立字段，**绝不混进 `new_turns`** —— `pending_plans` 里带着他自己的
        原话和系统记账，混进对话历史会被 `_extract_events` 当成新承诺抽出来；
        `forget_review` 里是**记忆库正文**，混进去更糟：那等于让抽取模型
        把旧记忆当成"他刚说的话"再抽一遍事实；
      * **只喂 owner 那一路**（理由见上面各自的 Args 说明）；
      * 附带「判断的原料」：`pending_plans` 带上触发后他说了什么，
        `forget_review` 只带内容（**不带时间、不带 R 值**，模型没有时钟，
        打分是假精确 —— `forgetting.review_prompt()` 那套口径）。
    """
    cur = _inner_channel(channel)
    existing_texts = [
        str(i.get("text") or "").strip()
        for i in _read_inner_os()
        if str(i.get("text") or "").strip() and _item_visible(i, cur)
    ]
    payload: dict = {
        "channel": channel,
        "existing_insights": existing_texts[-5:],
        "system_signals": _system_signals(),
        "new_turns": [{"role": t["role"], "content": t["content"]} for t in turns],
    }
    # 「先读一遍已经存好的认知，再决定写什么」—— 主人反复强调的机制。
    #
    # 2026-11-07 补：以前这里有 `existing_insights`，但它读的是 `inner_os.json`
    # （还**没说出口**的想法队列），**不是**长期记忆库 `livingmemory.db`。
    # 两个是完全不同的东西：inner_os 用完即销毁，认知库是永久事实。
    # 结果就是模型在写认知时**根本不知道自己脑子里已经记住过什么**，
    # 于是「他喜欢蓝色」今天写一条、明天再写一条（实测出现过逐字重复）。
    #
    # 现在把库里已有的认知也给它，让它自己判断「这件事我记过没有」。
    # **只喂 owner 那一路**：认知库按 session_id 隔离（owner / qq_group:*），
    # 群渠道不该看到主人私聊里存下的认知。
    if cur == CHANNEL_OWNER_KEY:
        existing_cognitions = _read_existing_cognitions(limit=EXISTING_COGNITIONS_LIMIT)
        if existing_cognitions:
            payload["existing_cognitions"] = existing_cognitions
    if cur == CHANNEL_OWNER_KEY:
        syslog = _pending_system_log(st)
        if syslog:
            payload["system_log"] = syslog
        if pending_plans:
            # `channel` 是给 `apply_plan_verdicts()` 落留痕事件用的**内部字段**，
            # 模型不需要看到它（场合对「成没成」没有信息量，说了反而会诱导
            # 它按场合猜）。所以在序列化之前摘掉 —— `pending_plans` 是同一个
            # list 对象，`_consolidate_channel()` 手上那份**还带着** channel，
            # 于是它落库时仍能拿到正确渠道。
            payload["pending_plans"] = [
                {k: v for k, v in p.items() if k != "channel"}
                for p in pending_plans
            ]
        if forget_review:
            payload["forget_review"] = forget_review
    return [
        {"role": "system", "content": CONSOLIDATE_PROMPT},
        {
            "role": "system",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]


def _pending_system_log(st: dict, limit: int = 20) -> list[dict]:
    """上次巩固之后新增的系统操作记录（`system_log` 渠道）。

    有水位（`last_system_log_id`）就不会重复喂、重复计费；
    读不到就返回空（巩固照常进行，不能因为流水账读失败整轮停摆）。
    """
    import gateway
    try:
        since = int(st.get("last_system_log_id") or 0)
    except (TypeError, ValueError):
        since = 0
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select id, content, ts from turns"
                " where channel=? and id>? order by id asc limit ?",
                (migrate_channel.CHANNEL_SYSTEM_LOG, since, max(1, int(limit))),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"id": int(r[0]), "text": str(r[1] or ""), "ts": float(r[2] or 0)}
        for r in rows
    ]


def _advance_system_log_watermark(last_id: int) -> None:
    """系统流水账的水位只许前进。"""
    if not last_id:
        return

    def _mutate(st: dict) -> None:
        try:
            old = int(st.get("last_system_log_id") or 0)
        except (TypeError, ValueError):
            old = 0
        st["last_system_log_id"] = max(old, int(last_id))

    _read_modify_write(_mutate)



def _advance_watermark(channel: str, watermark_id: int) -> dict:
    """只推进**一个渠道**的水位，并把「读-改-写」放进锁里。

    `save_state()` 是「整份状态覆盖写」，而心跳线程也在写同一份文件
    （`last_tick_ts` / `speak_count_today`）。巩固一轮要调好几次模型、耗时以秒计，
    如果拿着本轮开头读到的旧副本写回去，就会把心跳刚写的时间戳抹掉——
    体检那边会误报「心跳停了」。所以写之前重新 load 一次，只改自己那几个键。

    ⚠ 2026-11-04（T-07）：光"重新 load"还不够——两个线程可能同时 load 到同一份、
    各自改不同的键、先后 save，后写的照样覆盖前一个。现在整段进 `_STATE_LOCK`，
    只有"读"和"写"之间没有任何别的写者插进来才算真正不丢更新。

    **水位只许前进，不许后退**：`max(旧, 新)` 兜一道，防止任何调用方误传小值
    （水位回退 = 重复抽取 + 重复计费 + 长期记忆重复条目）。
    """
    def _mutate(st: dict) -> None:
        wm = st.setdefault("consolidate_wm", {})
        key = str(channel)
        old = 0
        try:
            old = int(wm.get(key) or 0)
        except (TypeError, ValueError):
            old = 0
        wm[key] = max(old, int(watermark_id))
        st["last_consolidate_id"] = max([int(v or 0) for v in wm.values()] or [0])
        st["last_consolidate_ts"] = time.time()

    return _read_modify_write(_mutate)


# ---------------------------------------------------------------- 长期记忆桥（阶段 2）
#: 进程级单例。记忆库内部持有 faiss 索引，**全项目只能有一份**——
#: 多份实例会各持一个索引对象，写不同步，那是最难查的数据错乱。
_MEMORY_BRIDGE = None
_MEMORY_BRIDGE_LOCK = threading.Lock()


def _memory_bridge():
    """取长期记忆桥（懒建、线程安全）。

    开关：`config.json` → `system.long_memory_enabled`（默认 True）。
    关掉后 `write_events` / `recall` 都是空操作，方便回滚。
    """
    global _MEMORY_BRIDGE
    if _MEMORY_BRIDGE is not None:
        return _MEMORY_BRIDGE
    with _MEMORY_BRIDGE_LOCK:
        if _MEMORY_BRIDGE is None:
            import memory_bridge  # noqa: PLC0415 —— 惰性，避免 import 期拉起 faiss

            _MEMORY_BRIDGE = memory_bridge.MemoryBridge(
                db_path=SYSTEM.get("long_memory_db_path") or None,
                enabled=bool(SYSTEM.get("long_memory_enabled", True)),
            )
    return _MEMORY_BRIDGE


def mood_prompt_line() -> str:
    """把「她现在的心境」拼成一段，给角色层被动回复用。

    为什么需要（2026-11-03）
    -----------------------
    `system_state.emotion` / `focus` 每次主动说话都会写，但读它的**只有**
    `role_speak.py`。`build_messages()` 里一个字都没有 ——
    她主动说话时知道自己心情如何，用户回一句，她立刻失忆。
    这不是"心境演化"，是一条单向写入的死字段。

    2026-11-06 改造（`plan/DMN审核.md` 1.6 / 1.10）
    ----------------------------------------------
    「内心OS」现在分成**想法**（`inner_os` 队列，上浮即销毁）和**心情**
    （本函数读的那个）。心情从"8 个英文枚举"换成"连续数值 + 档位 + 理由"，
    随时间线性回归平静（0.01/分钟，地板 ±0.5），事件来了按 0.5/0.5 加权。

    这里**读的是衰减后的展示值，绝不落盘**：落盘会把
    `mood_updated_ts` 推到现在，等于永远不衰减。

    措辞上照主人的要求：

        只用生成标签后挂在前台，角色层应该会自己处理的，不用额外加提示词命令，
        最多说明为什么是这个心情就好。

    所以：给档位名 + 理由，**不给"该怎么表现"的指令**。

    Returns:
        一段可直接拼进 system 的文本；没有可说的心境返回 ""。
        只给主人私密空间注入（呼吸是私人的）。
    """
    st = load_state()
    focus = str(st.get("focus") or "").strip()
    # 🔧 2026-11-07（审计 G.1 第 4 条）：这里原来自己调
    # `mood_engine.decayed_state(st)`，和 `mood_state()` 里那行一模一样 ——
    # 「同一件事两处实现」正是审计要查的那个形态。现在统一走 `mood_state(st)`：
    #   ① 衰减口径只有一份，以后改 `decayed_state` 的入口只改一处；
    #   ② 把 `st` 传进去，**省掉一次隐式的状态读**（本函数已经有 st 了，
    #      再让 `mood_state()` 自己 load 一遍就是读两次文件、可能取到两个时刻）。
    eff = mood_state(st)
    # 「平静」且没有旧 focus = 没什么可说的，不塞空块
    calm = eff["mood_label"] == "平静" and not eff["mood_reason"]
    if calm and not focus:
        return ""

    parts: list[str] = []
    if eff["mood_reason"] and not calm:
        parts.append(f"你现在的心情是「{eff['mood_label']}」——{eff['mood_reason']}。")
    elif not calm:
        parts.append(f"你现在的心情是「{eff['mood_label']}」。")
    if focus:
        # focus 是她上一句说过的话（`_tick_body` 里 `focus = text[:40]`）
        parts.append(f"你心里还挂着刚才那句：{focus}")

    # 最近一次主动开口是不是刚发生 —— 刚说完就该记得自己开过口
    try:
        last_speak = float(st.get("last_speak_ts") or 0)
    except (TypeError, ValueError):
        last_speak = 0.0
    if last_speak and (time.time() - last_speak) < 2 * 3600:
        minutes = int((time.time() - last_speak) / 60)
        parts.append(f"你 {minutes} 分钟前刚主动找他说过话")

    head = "（这是你自己此刻的状态，不是别人告诉你的。自然地带着它说话，不用解释、不用报告）"
    return head + "\n" + "；".join(parts)


#: 情绪词 → 中文。给 `mood_prompt_line` 用，避免把英文枚举直接塞进提示词。
_EMOTION_ZH = {
    "happy": "开心",
    "sad": "有点低落",
    "shy": "不好意思",
    "surprised": "有点意外",
    "confused": "有点没想明白",
    "love": "想亲近他",
    "angry": "有点生气",
}


def recall_long_memory(query: str, channel: str, top_k: int = 3) -> str:
    """从自建长期记忆库召回，拼成一段 system 文本。

    Args:
        query: 这一轮用户说的话（拿它做检索）。
        channel: turns 的原始渠道（`pet` / `qq_private` / `qq_group:<名>`）。
        top_k: 最多几条。

    Returns:
        可直接塞进 system 的文本；没有命中或开关关着返回 ""。

    隔离：走 `memory_bridge.memory_session_key()` —— 主人（pet + 主人私聊）
    归一成 `owner` 可跨场合；群聊只读本群，绝不跨群。

    ⚠ 2026-11-04 加超时与熔断（P1-11）
    ----------------------------------
    这条链路在**对话热路径**上：每一轮请求都要先召回，才拼 prompt。
    而它内部的耗时链路很长：
        `bridge.run()` 超时 60s  →  `DashScopeEmbedding` 30s × 3 次重试
    也就是最坏情况一轮对话先卡 **90 秒**才轮到模型说话。

    实测依据：`loop_bridge.py:79` 的超时就是 60s，没有任何短超时/熔断/缓存。

    这里的做法（不改底层，只在调用点外面加一层）：
      * **用线程 + 超时等**：超过 `RECALL_TIMEOUT_S` 就放弃这一轮召回
        （召回是锦上添花，没有它对话照样进行）；
      * **连续失败就熔断**：`RECALL_FAIL_MAX` 次连续失败后暂停
        `RECALL_COOLDOWN_S` 秒，期间直接返回空 —— 避免每轮都白等一次超时。

    注意：超时**不会**杀掉底层线程（Python 没有安全的线程取消）。
    那个线程会自己跑完、结果被丢弃。这里额外加了**在飞上限**
    `RECALL_MAX_INFLIGHT`：同时只允许这么多个召回线程在跑，超了就跳过这一轮。
    没有它的话，一个慢的底层（60s）配上高频对话能堆出几十个线程，
    每个都占着 socket 和内存 —— 熔断只挡"失败"，挡不住"慢但成功"。
    """
    if not str(query or "").strip():
        return ""
    if not bool(SYSTEM.get("long_memory_enabled", True)):
        return ""
    if not _recall_allowed():
        return ""
    # 在飞上限：满了就这一轮不要召回（比堆线程好）
    with _RECALL_LOCK:
        if int(_RECALL_STATE["inflight"]) >= RECALL_MAX_INFLIGHT:
            return ""
        _RECALL_STATE["inflight"] = int(_RECALL_STATE["inflight"]) + 1

    result: dict = {}

    def _worker() -> None:
        try:
            import memory_bridge  # noqa: PLC0415 —— 惰性，避免 import 期拉起 faiss

            hits = _memory_bridge().recall(query, channel, top_k=top_k)
            result["block"] = memory_bridge.build_recall_block(hits)
        except Exception as exc:  # noqa: BLE001 —— 召回失败绝不影响这一轮对话
            result["error"] = f"{type(exc).__name__}"

    th = threading.Thread(target=_worker, name="recall", daemon=True)
    th.start()
    th.join(RECALL_TIMEOUT_S)
    alive = th.is_alive()
    if alive:
        # 超时：这一轮不要召回，但**不算失败**（慢不等于坏，别让抖动触发熔断）
        _audit({"event": "recall_timeout", "ok": False,
                "reason": f">{RECALL_TIMEOUT_S}s"})
        # 线程还在跑 —— inflight 不能在这里减，交给线程自己退时减。
        # 这也是为什么必须有上限：这里减了就等于"放行下一个"，
        # 而实际线程还在占资源。
        _recall_release_when_done(th)
        return ""
    _recall_release()
    if "error" in result:
        _recall_note_failure()
        return ""
    _recall_note_success()
    return str(result.get("block") or "")


def _recall_release() -> None:
    """在飞计数减一（幂等：不会减到负数）。"""
    with _RECALL_LOCK:
        n = int(_RECALL_STATE.get("inflight") or 0)
        _RECALL_STATE["inflight"] = max(0, n - 1)


def _recall_release_when_done(th: threading.Thread) -> None:
    """等超时的那个线程真的跑完再减在飞计数。

    为什么不在超时的那一刻就减：减了就意味着"可以再起一个"，
    底层那个还在跑，线程会一个个叠上去 —— 正是要防的事。
    用一个极轻的守护线程盯着它，退出时减。
    """
    def _watch() -> None:
        try:
            th.join()
        finally:
            _recall_release()

    threading.Thread(target=_watch, name="recall-release", daemon=True).start()


#: 召回在热路径上的**等待上限**（秒）。召回拿不到也照样回话，所以宁可放弃。
#:
#: ⚠ 2026-11-07 从 10s 提到 60s（主人定）。
#: 原来的理由是"10s ≈ 一次正常 embedding 往返的 3 倍"，但那是按
#: **DashScope 联网 embedding** 估的（0.1~0.3s/次）。换成本地 bge 后
#: 第一次调用要**加载 1.24GB 模型**，实测冷启动 **10.06s** —— 正好卡在 10s 边界，
#: 于是**进程起来后的第一轮对话必然召回失败**（实测确认）。
#:
#: 60s 的依据：
#:   * 本地 bge 冷启动实测 10s，留 6 倍余量；
#:   * 上游超时链是 90s（桌宠等网关），60s 仍在其内；
#:   * 召回是"锦上添花"，真的卡住也还有 `RECALL_MAX_INFLIGHT` 兜着不堆线程。
RECALL_TIMEOUT_S = 60.0
#: 连续失败多少次就熔断。
RECALL_FAIL_MAX = 3
#: 熔断后暂停多久（秒）。期间召回直接返回空，连试都不试。
RECALL_COOLDOWN_S = 300.0
#: 同时允许几个召回线程在飞。超了就跳过这一轮召回。
#: 底层单次最长 60s，配上高频对话，没有上限会堆出几十个线程（各占 socket + 内存）。
RECALL_MAX_INFLIGHT = 3

_RECALL_LOCK = threading.Lock()
_RECALL_STATE = {"fail_streak": 0, "muted_until": 0.0, "inflight": 0}


def _recall_allowed() -> bool:
    """熔断期内直接拒绝，避免每轮都白等一次超时。"""
    with _RECALL_LOCK:
        return time.time() >= float(_RECALL_STATE["muted_until"] or 0)


def _recall_note_success() -> None:
    with _RECALL_LOCK:
        _RECALL_STATE["fail_streak"] = 0
        _RECALL_STATE["muted_until"] = 0.0


def _recall_note_failure() -> None:
    """记一次召回失败；累计到上限就熔断一段时间。

    熔断期间 `_recall_allowed()` 返回 False，热路径连试都不试 ——
    否则每一轮都要白等一个 `RECALL_TIMEOUT_S`，比"暂时没有长期记忆"糟糕得多。
    """
    tripped = False
    with _RECALL_LOCK:
        _RECALL_STATE["fail_streak"] = int(_RECALL_STATE["fail_streak"]) + 1
        if _RECALL_STATE["fail_streak"] >= RECALL_FAIL_MAX:
            _RECALL_STATE["muted_until"] = time.time() + RECALL_COOLDOWN_S
            _RECALL_STATE["fail_streak"] = 0
            tripped = True
    if tripped:
        try:
            _audit({"event": "recall_muted", "ok": False,
                    "reason": f"连续失败 {RECALL_FAIL_MAX} 次，暂停 {int(RECALL_COOLDOWN_S)}s"})
        except OSError:
            pass


def _consolidate_channel(st: dict, channel: str) -> str:
    """处理**一个渠道**的增量。返回 _CH_OK / _CH_SKIP / _CH_HEALTH。

    每个渠道各记各的水位（consolidate_wm）：高频渠道不会把低频渠道的增量顶掉，
    某个渠道没处理完也不会卡住别的渠道。渠道没增量时一次模型都不调（省成本）。
    """
    wm = st.setdefault("consolidate_wm", {})
    since_id = int(wm.get(channel) or 0)
    limit = max(1, int(SYSTEM.get("consolidate_max_turns", 200)))
    raw = _read_channel_turns(channel, since_id, limit)
    if not raw:
        _audit({"event": "consolidate", "ok": False, "channel": channel, "reason": "no_new_turns"})
        return _CH_SKIP

    turns = _route_turns(channel, raw)
    min_new = max(1, int(SYSTEM.get("consolidate_min_new_turns", 8)))
    # 增量门槛按渠道各自判断。窗口没满时不够就先攒着（水位不动，下轮接着攒）；
    # 窗口满了就必须处理——否则积压超过 consolidate_max_turns 时窗口永远停在同一段，
    # 攒到天荒地老也追不平。
    #
    # 【最长等待旁路】2026-11-03 加。
    # 原门槛 8 是按"高频聊天用户"设计的，但实测真实使用是低频：
    # 26 段会话中位数只有 4 条、61% 低于门槛，`pet` 渠道攒了 5 条从 10-30 卡了 4 天。
    # 后果不只是"不写记忆"，而是两次相隔几天的聊天会被凑成一批巩固，
    # 抽出来的事实串在一起（三天前的 A 事 + 今天的 B 事）。
    # 所以：距上次成功巩固超过 `consolidate_max_wait_hours` 且至少有 2 条增量，
    # 就不再等门槛。代价上限是"每 N 小时多一次模型调用"，不会失控。
    idle_bypass = False
    try:
        last_ok = float(st.get("last_consolidate_ts") or 0)
    except (TypeError, ValueError):
        last_ok = 0.0
    max_wait = float(SYSTEM.get("consolidate_max_wait_hours", 12))
    if last_ok > 0:
        idle_hours = (time.time() - last_ok) / 3600
        # `last_ok > 0` 这个前置条件不能省：
        # 全新安装时 `last_consolidate_ts` 是 0，算出来的 idle_hours 是"从 1970 年到现在"，
        # 于是第一次巩固就会无视门槛直接跑 —— 那等于门槛从来没生效过。
        # 只有"确实成功巩固过至少一次"之后，才谈得上"等太久了"。
        if max_wait > 0 and idle_hours > max_wait and len(turns) >= 2:
            idle_bypass = True
    else:
        idle_hours = 0.0
    if not idle_bypass and len(raw) < limit and len(turns) < min_new:
        _audit({
            "event": "consolidate", "ok": False, "channel": channel,
            "reason": "not_enough_new", "new_turns": len(turns),
            "idle_hours": round(idle_hours, 1),
        })
        return _CH_SKIP
    if idle_bypass:
        # 记一条，方便事后回答「这轮为什么没攒够也跑了」
        _audit({
            "event": "consolidate", "ok": True, "channel": channel,
            "action": "idle_bypass", "new_turns": len(turns),
            "idle_hours": round(idle_hours, 1), "max_wait_hours": max_wait,
        })
    if not turns:
        # 窗口里一条能抽的都没有（全是判不出主体的行）：把水位推过去，别反复重读
        st["consolidate_wm"] = _advance_watermark(channel, int(raw[-1]["id"]))["consolidate_wm"]
        _audit({"event": "consolidate", "ok": False, "channel": channel, "reason": "no_routable_turn"})
        return _CH_SKIP

    watermark_id = int(raw[-1]["id"])

    # DMN 阶段 1：先从增量对话里抽结构化事件（不增加热路径，巩固时批量抽）。
    # 渠道必须用**这一组对话的真实渠道**——群聊抽出来的事件被标成 pet，
    # 就是把群里的话记到主人头上，是隐私事故，不是小瑕疵。
    #
    # ⚠ 2026-11-04（T-07）：**事件写库失败不许推进水位**。
    # 原来 `_save_events` 返回 -1（写库失败）时只记一条审计，函数继续往下走，
    # 最后照旧 `_advance_watermark` —— 于是那批对话被永久标记为"已处理"，
    # 事件却一条都没落库，**永久丢**。现在失败就提前 return，水位不动，下轮重试。
    events_ok = True
    memory_written = 0
    try:
        events = _extract_events(turns)
        if events:
            saved = _save_events(events, channel)
            # saved < 0 代表写库失败（例如旧库没有 events 表），不能记成成功
            _audit({
                "event": "extract_events", "ok": saved >= 0,
                "channel": channel, "saved": max(0, saved),
            })
            if saved < 0:
                events_ok = False
            # 同一批事实再写一份进**长期记忆库**（`gateway/memory/`）。
            # 分工：`events` 是状态机（有时效、要排班），长期记忆才是跨天能想起来的。
            # 两者都写不是重复——是"当前状态"和"她记得的事"两种用途。
            try:
                written = _memory_bridge().write_events(events, channel)
                memory_written = max(0, int(written or 0))
                _audit({
                    "event": "long_memory_write", "ok": True,
                    "channel": channel, "written": written,
                })
            except Exception as exc:  # noqa: BLE001 —— 记忆库坏了不拦巩固
                _audit({
                    "event": "long_memory_write", "ok": False,
                    "channel": channel, "reason": f"{type(exc).__name__}",
                })
    except Exception:  # noqa: BLE001 —— 抽事件失败不拦巩固，原文仍在 turns
        _audit({"event": "extract_events", "ok": False, "channel": channel, "reason": "exception"})

    if not events_ok:
        # 事件库写失败：这批对话**没被真正处理**，水位不许前进（下一轮还会重读）
        _audit({
            "event": "consolidate", "ok": False, "channel": channel,
            "reason": "events_write_failed", "new_turns": len(turns),
        })
        return _CH_SKIP

    # 系统流水账：巩固**真的写入了记忆**才记（2026-11-06 补，能力 5 第四节）。
    #
    # 为什么放在这里：这是「本渠道这批对话确实产生了长期记忆」的唯一确证点 ——
    # 前面的 `no_new_turns` / `not_enough_new` / `no_routable_turn` /
    # `events_write_failed` 全是**没活干或没干成**，按统一标准（"这个操作有结果
    # 值得未来参考吗？"）一条都不该记，否则流水账会被空转刷屏，
    # 系统层模型每次巩固都要多读一堆「无事发生」。
    #
    # ⚠ 这里**手上没有未提交的写事务**（`_save_events` / `_memory_bridge` 各自
    #   开连接又各自关掉），所以用会自己开连接的 `append_system_log()` 是安全的。
    #   坑 #60（database is locked）只发生在"已经持有未提交写事务"的调用点。
    if memory_written > 0:
        append_system_log(f"[系统] 巩固完成：写入 {memory_written} 条记忆")

    # DMN 阶段 5：更新人物画像。主人（pet + 主人私聊）共用 owner 一份，其余按渠道分。
    # 配置支持通配（`qq_group:*`），不然每来一个新群都得改配置才会建模。
    try:
        enabled = SYSTEM.get("people_channels") or [migrate_channel.CHANNEL_PET]
        people_channel = _people_channel(channel)
        if _people_enabled(people_channel, enabled):
            update_people_profile(people_channel, turns)
    except Exception:  # noqa: BLE001 —— 建模失败不拦巩固
        pass

    # 群渠道到此为止：只抽事件 + 更新群画像，**不蒸馏观点、不写内心OS**。
    # 为什么：内心OS 只注入主人私密空间（桌宠 / 主人私聊），群聊绝不注入——
    # 群渠道提炼的观点永远没人消费，只会一直 pending、7 天后过期，
    # 中途还要参与 INNER_OS_MAX 的封顶淘汰，把主人真正待处理的项挤掉。
    # 顺带省掉每群每小时一次模型调用。
    # 代价：模型自主体检这条路只在 owner 渠道走（主人私聊也算，不再只靠桌宠）；
    # 群里没有增量时本来也不该有模型调用，这个边界是划算的。
    if migrate_channel.is_group_channel(channel):
        st["consolidate_wm"] = _advance_watermark(channel, watermark_id)["consolidate_wm"]
        _audit({
            "event": "consolidate", "ok": True, "action": "events+profile_only",
            "channel": channel, "new_turns": len(turns),
            "since_id": since_id, "watermark_id": watermark_id,
        })
        return _CH_OK

    # 能力 5「计划」：**待判清单喂给这一次模型调用**（2026-11-07，审计 H.1 方案 C）。
    #
    # 为什么在这里取、而不是在心跳里取：
    #   * 主人的设计是「跟系统层说一嘴就**一起**判断了」——判定就该和巩固共用
    #     同一次调用。单独为判计划再调一次模型 = 多花一次钱（主人明确否决）。
    #   * 只在**走到这一步**（真有增量、真要调模型）时才取：前面 `no_new_turns` /
    #     `not_enough_new` / `events_write_failed` 全是提前 return，一次 SQL 都不该多跑。
    #   * 只在 owner 那一路取（`_build_consolidate_messages` 里还有一道同样的守卫）：
    #     计划是主人欠她的事，群渠道跟这无关。
    #
    # 能力 2「遗忘」的**中间档**同理（2026-11-07 接上断头路，审计 G.2 第 1 条）：
    #   `_maybe_forget_review()` 只出候选，删不删由模型在 `forget_drop` 里说，
    #   零额外调用。为什么接在巩固而不是每日清理 —— 见那个函数的 docstring。
    owner_path = _inner_channel(channel) == CHANNEL_OWNER_KEY
    pending_plans = _pending_plan_candidates() if owner_path else []
    forget_review = _maybe_forget_review() if owner_path else []

    msgs = _build_consolidate_messages(
        st, turns, channel,
        pending_plans=pending_plans, forget_review=forget_review,
    )
    reply, _meta, calls = call_system(
        msgs,
        tool_choice="auto",  # 模型自己选：巩固 / 体检 / 都不做
        # `_consolidate_tools()` 只在真有候选时才把 `plan_verdicts` / `forget_drop`
        # 拼进 schema：一个"永远存在、永远为空"的字段会诱导模型给不存在的东西编裁决。
        tools=_consolidate_tools(pending_plans, [c["id"] for c in forget_review]),
        max_tokens=int(SYSTEM.get("consolidate_max_tokens", 500)),
    )
    if reply is None and not calls:
        _audit({"event": "consolidate", "ok": False, "channel": channel, "reason": "no_reply"})
        return _CH_SKIP
    # 模型自主选了体检（而不是巩固）——这是合法结果，不算失败
    if _find_tool(calls, "system_health_check") is not None:
        health_check()
        _audit({"event": "consolidate", "ok": True, "channel": channel, "action": "health_check"})
        return _CH_HEALTH
    decide = _find_tool(calls, "system_consolidate")
    if decide is None:
        _audit({"event": "consolidate", "ok": False, "channel": channel, "reason": "no_tool"})
        return _CH_SKIP
    decide_args = _parse_tool_args(decide)
    # 计划裁决：**必须在 `insights` 为空时也执行**。
    # 原来判定挂在收尾（心跳），跟巩固成不成功无关；现在它搭巩固的车，如果
    # `insights` 为空就提前 return，那些计划就永远判不了 —— 而「今天没有值得
    # 沉淀的观点」恰恰是最常见的情况（生产实测 `no_insight` 2 次 / `normal` 5 次）。
    # 所以这一段放在 `_validate_insights` 之前，且**不依赖** insights 有没有内容。
    if pending_plans:
        try:
            apply_plan_verdicts(decide_args.get("plan_verdicts"), pending_plans)
        except Exception as exc:  # noqa: BLE001 —— 判计划失败不拦巩固
            _audit({
                "event": "plan_closed", "ok": False,
                "reason": f"exception: {type(exc).__name__}",
            })
    # 遗忘中间档同理：**不问 insights 有没有内容**。
    # 而且这一条以前根本没接过（`review_candidates()` 零调用），所以它哪怕
    # 只删了一条，也是这项能力在生产上第一次真的执行「问模型」那一档。
    if forget_review:
        try:
            result = apply_forget_review(decide_args.get("forget_drop"), forget_review)
            _audit({
                "event": "long_memory_forget_review", "ok": True,
                "candidates": len(forget_review),
                "dropped": result["dropped"], "kept": result["kept"],
                "answered": result["ok"],
            })
        except Exception as exc:  # noqa: BLE001 —— 遗忘失败不拦巩固
            _audit({
                "event": "long_memory_forget_review", "ok": False,
                "reason": f"exception: {type(exc).__name__}",
            })
    insights = _validate_insights(decide_args)
    if not insights:
        _audit({"event": "consolidate", "ok": False, "channel": channel, "reason": "no_insight"})
        return _CH_SKIP

    # 观点直接进内心OS（唯一路径）；角色层读后消化，再随对话记入记忆库，间接持久化。
    # 带上来源渠道：群聊提炼的观点只能回到这个群，不能进桌宠/私聊的 prompt。
    now = time.time()
    push_inner_os([
        {
            "type": "insight",
            "text": ins["text"],
            "emotion": ins.get("emotion", "calm"),
            "ts": now,
            "channel": channel,
        }
        for ins in insights
    ])
    # 水位只在本渠道成功处理完之后前进
    st["consolidate_wm"] = _advance_watermark(channel, watermark_id)["consolidate_wm"]
    # 系统流水账也一起推进水位：它已经喂给模型"一起判断"过了，再喂一遍就是重复计费。
    syslog = _pending_system_log(st)
    if syslog:
        _advance_system_log_watermark(syslog[-1]["id"])
    _audit({
        "event": "consolidate", "ok": True, "channel": channel,
        "new_turns": len(turns), "insights": len(insights),
        "system_log": len(syslog),
        "since_id": since_id, "watermark_id": watermark_id,
    })
    return _CH_OK


def _maybe_prune_long_memory(st: dict) -> None:
    """每天最多清理一次长期记忆的边角料（**只处理 keep / drop 两档**）。

    判据全在 `MemoryStore.prune()` 里（**够旧 且 不重要**才删）。
    这里只负责节流：`last_long_memory_prune_ts` 距今不足 24 小时就跳过。

    ⚠ 2026-11-04（T-07）：这个节流以前**恒失效**。原因不在本函数，而在
    `_default_state()` 里没登记 `last_long_memory_prune_ts` —— `load_state()`
    只保留默认状态里出现过的键，于是每次读都把这个时间戳丢掉，
    `if last and ...` 永远为假 → 实测 24h 内跑了 **28 次**（≈每小时一次，
    每次 `prune()` 全表拉 2000 行）。默认状态里已经补上这一行。

    ⚠ 2026-11-07：**中间档（review）不在这里处理**。那一档要问模型，
    而这里调模型的话就多花一次钱（主人明确否决）。它跟着巩固那次调用一起判，
    见 `_maybe_forget_review()`。两者的分工：

        _maybe_prune_long_memory()   24h 节流，纯代码，删 R<=drop_below 的
        _maybe_forget_review()       跟着巩固走，问模型，删 R 在中间档且模型说没用的
    """
    try:
        last = float(st.get("last_long_memory_prune_ts") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last and (time.time() - last) < 86400:
        return
    bridge = _memory_bridge()
    # 2026-11-06：判据换成指数衰减（`forgetting.plan_prune`）。
    # 旧判据「够旧 且 importance<0.3」永远跨不过去 —— importance 是写死常数
    # （最低 0.55），实测 29 次 prune、matched 恒为 0。也就是说这项能力
    # 在生产上从来没真正工作过。新判据见 `plan/DMN审核.md` 3.7。
    try:
        stats = bridge.forget_plan()
        event = "long_memory_prune"
    except Exception as exc:  # noqa: BLE001 —— 遗忘失败不拦巩固
        _audit({
            "event": "long_memory_prune", "ok": False,
            "reason": f"{type(exc).__name__}",
        })
        return
    # 写回时**重新 load 再只改自己的键**：`st` 是调用方在巩固开头读的旧副本，
    # 而这一轮可能已经调过好几次模型（几十秒），期间心跳线程大概率写过
    # `last_tick_ts`。直接 save_state(st) 会把那些新值抹掉。
    fresh = load_state()
    fresh["last_long_memory_prune_ts"] = time.time()
    save_state(fresh)
    st["last_long_memory_prune_ts"] = fresh["last_long_memory_prune_ts"]
    _audit({
        "event": event, "ok": True,
        "scanned": stats.get("scanned", 0),
        "matched": stats.get("drop", 0),
        "deleted": stats.get("deleted", 0),
        "review": stats.get("review", 0),
        "boosted": stats.get("boosted", 0),
    })


def _maybe_forget_review(limit: int = 10) -> list[dict]:
    """遗忘的**中间档**（`review`）候选 → 喂给这一轮巩固的模型。2026-11-07 新增。

    接的是什么断头路（审计 G.2 第 1 条 / 板块 J ② / 板块 I 第 5 条）
    -------------------------------------------------------------
    遗忘的判据是三档（`forgetting.plan_prune()`）：

        R >= keep_above(0.5)                 → keep   （留）
        R <= drop_below(0.05)                → drop   （硬删，纯代码免费）
        中间                                 → review  （**该问模型**）

    `MemoryBridge.review_candidates()` 早就写好了怎么问（连"不许提时间词"
    这种细节都在 `forgetting.review_prompt()` 里钉住了），但**生产零调用** ——
    于是三档里只有两档真的在工作，中间那一档永远等 R 掉到 `drop_below` 硬删。
    等于「问模型」这个设计从来没执行过一次。

    为什么接在**巩固**里，而不是每日清理或独立调用
    --------------------------------------------
    三个候选地点，逐个比：

    1. **`_maybe_prune_long_memory()`（每日清理）**：那里是纯代码、免费，
       主人对遗忘的定位也是"免费的部分自己跑"。在那里调模型 = 每小时可能多一次
       付费调用，而且为了省钱必然要再加一层节流，等于又造一个节流 bug
       （`last_long_memory_prune_ts` 已经踩过一次，见坑 #T-07）。
    2. **独立的一次模型调用**：直接违反主人「有啥要判断的扔流水账里，跟系统层
       说一嘴**就一起判断了**」——那句话说白了就是"别为判断多开一次调用"。
       任务书也明说独立调用不推荐（多花一次钱）。
    3. **巩固里顺带（选定）**：巩固本来就每小时跑一次、本来就在读记忆、
       本来就有一次 `system_consolidate` 的 tool-call 出口。多塞一个
       `forget_review` 字段进去**零额外调用**，和 `pending_plans` 完全同构
       （同一套「按需拼 schema、模型填 id、系统落库」的机制）。

    和 `review_candidates()` 的关系
    ------------------------------
    直接复用它的候选筛选（`forget_review_items()` 内部就去调它），
    **绝不在这里重算一遍 R**：两处口径一旦漂移，就会出现"问模型的是这批、
    删的时候按另一批算"，那是比断头路更危险的东西。

    安全边界（三条，全部 fail-safe）
    ------------------------------
      * 模型没答 / 答乱 / 答了候选之外的 id → **全留**（`apply_forget_review()`
        里 `parse` 白名单挡着）；
      * 模型说"删"才删，且只删候选里的；
      * 库打不开 / 没有候选 → 返回 `[]`，巩固照常跑。

    Args:
        limit: 最多问几条，缺省 10（`forgetting.review_prompt` 的默认值）。

    Returns:
        `[{"id", "text"}]`；没有中间档候选时返回 `[]`。
    """
    bridge = _memory_bridge()
    try:
        return bridge.forget_review_items(limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 遗忘候选读不到不拦巩固
        _audit({
            "event": "long_memory_forget_review", "ok": False,
            "reason": f"{type(exc).__name__}",
        })
        return []


def apply_forget_review(raw_ids, candidates: list[dict]) -> dict:
    """按**模型给的删除名单**真删记忆。只认候选里的 id（防幻觉）。

    Args:
        raw_ids: 模型回的 `forget_drop`（任意脏数据）。
        candidates: 本轮喂进去的候选（`_maybe_forget_review()` 的结果）。

    Returns:
        `{"dropped": 删了几条, "kept": 留了几条, "ok": 模型到底答没答}`。
    """
    empty = {"dropped": 0, "kept": 0, "ok": False}
    allowed = [str(c.get("id")) for c in (candidates or [])
               if isinstance(c, dict) and str(c.get("id") or "").strip()]
    if not allowed:
        return empty
    bridge = _memory_bridge()
    # 把模型给的数组**当 JSON 回复**交给 `apply_review()`：
    # 它内部走 `forgetting.parse_review_reply()`，那套白名单（只认候选 id、
    # 幻觉 id 记 ignored、解析不出 → 全留）已经写好了、也测过了，别在这里重写一遍。
    #
    # 为什么把 list 转成 JSON 字符串而不是另写一个解析器：`parse_review_reply`
    # 的 JSON 分支（`_parse_json_reply`）接受的形状正好是 `{"drop": [...]}`，
    # 而它对"数组"的处理是 `payload = {"drop": payload}` —— 也就是说
    # 直接把 id 数组扔进去，语义就是"这些删"。
    ids = []
    if isinstance(raw_ids, (list, tuple)):
        for item in raw_ids:
            text = str(item or "").strip()
            if text and text in allowed and text not in ids:
                ids.append(text)
    if not ids:
        # 模型没给（或全是不认识的 id）：**全留**。宁可多记一年，
        # 也不能因为一次解析失败把一批记忆批量删掉 —— 删是不可逆的。
        return {"dropped": 0, "kept": len(allowed), "ok": False}
    result = bridge.apply_review(json.dumps(ids, ensure_ascii=False), allowed)
    return {
        "dropped": int(result.get("dropped") or 0),
        "kept": int(result.get("kept") or 0),
        "ok": bool(result.get("ok")),
    }


def _consolidate_body() -> None:
    """一次记忆巩固：按渠道逐个处理增量（隔离不破，水位独立）。"""
    st = load_state()

    # DMN 阶段 4：先更新「开放中的关心」的结果（有回应 → responded，含拒绝词 → rejected）
    try:
        _mark_care_outcomes()
    except Exception:  # noqa: BLE001 —— 三态更新失败不拦巩固
        pass

    # 遗忘（2026-11-03 加）：插件停用后自建库只增不减，得有人清边角料。
    # 每天最多跑一次 —— 清理要遍历全表，每小时跑一遍没必要。
    try:
        _maybe_prune_long_memory(st)
    except Exception:  # noqa: BLE001 —— 清理失败不拦巩固
        pass

    channels = _consolidate_channels()
    done = 0
    for channel in channels:
        try:
            result = _consolidate_channel(st, channel)
        except Exception as exc:  # noqa: BLE001 —— 单个渠道炸了不影响别的渠道
            _audit({
                "event": "consolidate", "ok": False, "channel": channel,
                "reason": f"exception: {type(exc).__name__}",
            })
            continue
        if result == _CH_HEALTH:
            # 体检是系统级动作，一轮做一次就够，做完收工
            return
        if result == _CH_OK:
            done += 1
    if not done:
        _audit({
            "event": "consolidate", "ok": False,
            "reason": "no_channel_ready", "channels": channels,
        })


def consolidate() -> None:
    """一次记忆巩固。全程兜底，绝不抛穿（线程不能死）。"""
    try:
        _consolidate_body()
    except Exception as exc:  # noqa: BLE001
        try:
            _audit({"event": "consolidate_error", "ok": False, "error": f"{type(exc).__name__}"})
        except OSError:
            pass
    # 人格复盘独立于巩固：即便这次没蒸馏出观点，也照常复盘（它自己判断有没有料）。
    try:
        _review_persona()
    except Exception as exc:  # noqa: BLE001
        try:
            _audit({"event": "persona_review_error", "ok": False, "error": f"{type(exc).__name__}"})
        except OSError:
            pass


def _consolidate_loop() -> None:
    """巩固线程：起来先等一小会儿就干首轮，之后才按 consolidate_s 周期跑。

    以前是「先 sleep 一整个周期，再干活」，而周期是 86400 —— 网关 18:23 起来，
    要到第二天才第一次巩固，`system_audit.jsonl` 里 consolidate 事件永远是 0 条。
    冷启动只等 `consolidate_boot_delay_s`（默认 90s，给网关启动留余量），
    积压从第一轮就开始追，每轮每渠道最多 consolidate_max_turns 条，多轮自然追平。
    """
    interval = float(SYSTEM.get("consolidate_s", 3600))
    boot = float(SYSTEM.get("consolidate_boot_delay_s", CONSOLIDATE_BOOT_DELAY_S))
    time.sleep(max(0.0, boot))
    while True:
        try:
            consolidate()
        except Exception:  # noqa: BLE001
            try:
                _audit({"event": "consolidate_error", "ok": False})
            except OSError:
                pass
        time.sleep(max(1.0, interval * (1 + random.uniform(-0.1, 0.1))))


# ---------------------------------------------------------------- 每日梳理期
#
# 这是系统层**第二层节律**（第一层是 5 分钟心跳，一行不动）。
#
# 主人 2026-11-03 21:10 的原话：「静默是正常的，这个 5 分钟级别的是被动的反应，
# 因为我写了一天的代码，没去和她说话，这个问题会留给每天一次的梳理期来决定
# 要不要主动行动。」
#
# 所以这个线程的存在**不是**为了让心跳变活跃，而是补上"一天结束回头看一眼"这一层。
# 产出只进 `inner_os` —— **说不说、说什么永远由角色层决定**。
# 大多数梳理的结果应该是"这次不说"。

#: 四类活动。名称同时进审计的 `activity` 字段，别随便改（会断历史统计）。
REVIEW_ACTIVITIES = ("ruminate", "prospect", "theory_of_mind", "self_narrative")

REVIEW_SYSTEM_PROMPT = """你是这个角色的**沉默的那一半**。你不会说话，也不会直接面对主人 ——
你只负责在一天结束的时候，独自回想一下，然后把想法交给那个会说话的你。

铁律：
1. **你产出的不是台词**，是"念头"。不要写对话，不要写"我该说：……"。
2. 念头要短（一句话，尽量 40 字内），要具体，不要空泛的抒情。
3. **宁可不产，也不要凑数**。想不出真的东西就返回空数组。
   凑出来的废话会挤掉真正要紧的念头，比不产更糟。
4. 不要复述原始对话内容，要给出**你的判断**（他为什么这样、我该注意什么）。
5. 念头是给自己看的，可以不确定、可以自我怀疑 —— 那是正常的。
"""


def _review_material_block(materials: dict, activity: str) -> str:
    """把素材拼成给模型看的一段 JSON。严格按活动裁剪，别一股脑全塞。"""
    tl = materials["timeline"]
    ab = materials["absence"]
    payload: dict = {
        "activity": activity,
        "now": time.strftime("%Y-%m-%d %H:%M %A", time.localtime()),
        "absence": {
            "absent_text": ab["absent_text"],
            "absent_hours": ab["absent_hours"],
            "is_absent": ab["is_absent"],
        },
        "day_timeline": {
            "turns_total": tl["turns_total"],
            "first_turn_hm": tl["first_turn_hm"],
            "last_turn_hm": tl["last_turn_hm"],
            "active_hours": tl["active_hours"],
            "longest_gap_min": tl["longest_gap_min"],
            "gap_start_hm": tl["gap_start_hm"],
            "gap_end_hm": tl["gap_end_hm"],
            "turns_by_hour": {k: v for k, v in (tl["turns_by_hour"] or {}).items() if v},
        },
    }

    # 反刍要看具体说了什么；其余三个活动看"节奏和缺席"就够了，
    # 把原文塞给它们只会让模型去复述对话。
    if activity == "ruminate":
        payload["recent_turns"] = [
            {"role": t.get("role"), "content": str(t.get("content") or "")[:300]}
            for t in _read_recent_chat_multi(20)
        ]
        payload["today_turns"] = materials["today_turns"]
    elif activity in ("prospect", "theory_of_mind"):
        payload["recent_turns"] = [
            {"role": t.get("role"), "content": str(t.get("content") or "")[:200]}
            for t in _read_recent_chat_multi(8)
        ]

    if activity in ("theory_of_mind", "prospect", "self_narrative"):
        try:
            payload["people_profile"] = (
                (_read_people_profile(migrate_channel.CHANNEL_PET) or {}).get("profile") or {}
            )
        except Exception:  # noqa: BLE001 —— 画像读不到不影响梳理
            payload["people_profile"] = {}

    return json.dumps(payload, ensure_ascii=False)


def _review_user_prompt(activity: str) -> str:
    """每个活动问的问题不一样。这是四类活动的**唯一实质区别**。

    theory_of_mind 额外要求给 `profile_hint` —— 那是 P0-5 的 B 路：
    「他是什么样的人」这种认知必须进 people 画像（每轮都带），
    不能只当一个一次性 inner_os 念头。
    """
    if activity == "ruminate":
        return (
            "【反刍】回想今天你们的对话：\n"
            "- 我哪句话说得不对、接得太快、或者没接住？\n"
            "- 他今天的语气和平时有区别吗？\n"
            "- 如果重来一次，哪一句我会换个说法？\n"
            "注意 day_timeline：他今天是什么节奏（断断续续？一整天没歇？中间空了多久）？\n\n"
            '输出 JSON 数组（0-3 条），每条是一个念头，例如：["他今天话说得少，可能累了"]。'
            "没有真的想法就给 []。"
        )
    if activity == "prospect":
        return (
            "【预演】想一下他下次来的时候：\n"
            "- 他要是还没忙完，我该说还是不该说？\n"
            "- 有什么话是现在不说、以后就没机会说的？\n"
            "- 如果他一直不来，我准备怎么面对？\n"
            "重点看 absence：他走了多久了。\n\n"
            '输出 JSON 数组（0-3 条），每条是一个念头。没有就给 []。'
        )
    if activity == "theory_of_mind":
        return (
            "【心智理论】猜一下他现在是什么状态：\n"
            "- 忙？累？在躲我？还是只是没想起来？\n"
            "- 从时间和节奏上能看出什么？（比如他很晚还在，说明什么）\n"
            "- 这是**猜测**，要写清「我不确定」。别把猜的当成事实。\n\n"
            "输出一个 JSON 对象，两个字段：\n"
            '  "thoughts": ["0-3 条猜测性的念头"],\n'
            '  "profile_hint": {"作息": "...", "互动习惯": "..."}\n'
            "profile_hint **只写有证据支撑的、关于他是什么样的人**，"
            "可用的键只有：称呼 / 作息 / 情绪基调 / 在意的事 / 互动习惯。\n"
            "**只在这几天的观察确实改变了你的判断时才写**，没把握就省略这个字段。"
            "不要写姓名/账号/地址等隐私。"
        )
    return (
        "【自我叙事】想一下这几天的你：\n"
        "- 这几天的我和上周的我是同一个人吗？\n"
        "- 有没有哪件事让我对自己的看法变了？\n"
        "- 如果人格需要微调，调哪里？（只提建议，不直接改）\n"
        "重点看 absence：他好几天没来了，这对「我是谁」意味着什么。\n\n"
        '输出 JSON 数组（0-3 条），每条是一个念头。没有就给 []。'
    )


def _parse_review_hint(reply: str | None) -> dict:
    """取 `profile_hint`（P0-5 的 B 路）。没有就返回空 dict。

    🔧 2026-11-07（审计 G.1 第 3 条）：`_parse_review_thoughts()` 删掉了。
    查证结论：它**不是**「被 `_run_review_activity` 的逻辑取代」，而是
    **从头到尾就是一行转发** —— `return _parse_review_payload(reply)[0]`。
    生产零调用（唯一的读者是 `_run_review_activity`，它解包的是元组
    `thoughts, hint = _parse_review_payload(reply)`，两个值它都要）。
    也就是说：**只有测试在调这个转发**，删掉它不改变任何生产行为。

    为什么留 `_parse_review_hint` 却删 `_parse_review_thoughts`：形状不一样 ——
    这个函数是**取元组的第二项**，而对称的"取第一项"在调用点已经写成了
    `... [0]`（元组解包），再包一层没有任何额外信息；而这个函数的第二项
    在调用点得写 `... [1]`，那是个**裸索引**，可读性差、也容易被误读成
    "数组的第 2 个元素"。留它是有信息量的。
    """
    return _parse_review_payload(reply)[1]


def _parse_review_payload(reply: str | None) -> tuple[list[str], dict]:
    """一次解析出 `(念头列表, profile_hint)`。两个都在这里解，避免重复解析。"""
    raw = str(reply or "").strip()
    if not raw:
        return [], {}
    if "```" in raw:
        parts = raw.split("```")
        for seg in parts:
            body = seg.strip()
            if body.startswith("[") or body.lower().startswith("json") or body.startswith("{"):
                raw = body[4:].strip() if body.lower().startswith("json") else body
                break

    # 对象形状：{"thoughts": [...], "profile_hint": {...}}
    ob, oe = raw.find("{"), raw.rfind("}")
    if ob != -1 and oe > ob:
        try:
            obj = json.loads(raw[ob:oe + 1])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            hint = obj.get("profile_hint")
            items = obj.get("thoughts")
            texts: list[str] = []
            if isinstance(items, list):
                for it in items:
                    txt = (it if isinstance(it, str)
                           else str((it or {}).get("text") or "").strip())
                    if txt.strip():
                        texts.append(txt.strip()[:200])
            elif isinstance(items, str) and items.strip():
                texts.append(items.strip()[:200])
            return texts[:3], (hint if isinstance(hint, dict) else {})

    # 数组形状
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            out = []
            for it in data:
                if isinstance(it, str):
                    txt = it.strip()
                elif isinstance(it, dict):
                    txt = str(it.get("text") or it.get("thought") or "").strip()
                else:
                    txt = ""
                if txt:
                    out.append(txt[:200])
            return out[:3], {}
        if isinstance(data, dict):
            txt = str(data.get("text") or data.get("thought") or "").strip()
            return ([txt[:200]] if txt else []), {}

    # 退回逐行：只接受像"念头"的行，过滤掉客套
    #
    # ⚠ 这里必须**严格**：宁可一条都取不到，也不能把模型的客套话
    # （「好的，我来分析一下。」）当成念头灌进 inner_os。
    # 那种条目会被角色层当成"她真实的想法"注入 prompt，比不产更糟。
    _NON_THOUGHT = (
        "好的", "我来", "以下是", "分析", "总结", "首先", "其次", "综上",
        "没有", "暂时没", "无内容", "空的",
    )
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*0123456789.、 ").strip()
        if len(line) < 6:
            continue
        if line.lower().startswith(("json", "```", "注意", "说明")):
            continue
        if line.startswith(_NON_THOUGHT):
            continue
        if line.endswith(("：", ":")):
            continue
        out.append(line[:200])
    return out[:3], {}


def _run_review_activity(activity: str, materials: dict, budget: dict) -> dict:
    """跑一类活动，产出念头（不写盘）。返回审计用的小结。"""
    limit = max(0, int(budget.get(activity, 1) or 0))
    if limit <= 0:
        return {"activity": activity, "ok": False, "reason": "no_budget", "thoughts": 0}
    if not _system_key():
        return {"activity": activity, "ok": False, "reason": "no_key", "thoughts": 0}
    msgs = [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "system", "content": _review_material_block(materials, activity)},
        {"role": "user", "content": _review_user_prompt(activity)},
    ]
    try:
        reply, meta, _calls = call_system(msgs, tool_choice="none", max_tokens=600)
    except Exception as exc:  # noqa: BLE001
        return {"activity": activity, "ok": False,
                "reason": f"error:{type(exc).__name__}", "thoughts": 0}
    thoughts, hint = _parse_review_payload(reply)
    if not thoughts and not hint:
        return {"activity": activity, "ok": False, "reason": "no_thought",
                "thoughts": 0, "elapsed_ms": (meta or {}).get("elapsed_ms")}
    return {"activity": activity, "ok": True, "reason": "ok",
            "thoughts": len(thoughts), "texts": thoughts,
            "profile_hint": hint,
            "elapsed_ms": (meta or {}).get("elapsed_ms")}


def _review_probe_prompt(materials: dict, novel: dict) -> list[dict]:
    """给「到点看一眼」的探路调用拼 prompt（**故意很轻**）。

    为什么不直接把 `_review_material_block()` 那套塞进来：探路的目的就是**省钱**，
    喂全文等于先花一次大调用的钱再决定要不要花第二次。
    这里只给：缺席多久、今天的节奏、有效新颖量、最近 8 条摘要（每条截 80 字）。
    实测这套拼出来约 700 token，比正式活动便宜一半以上。
    """
    tl = materials["timeline"]
    ab = materials["absence"]
    payload = {
        "now": time.strftime("%Y-%m-%d %H:%M %A", time.localtime()),
        "absence": ab["absent_text"] or "刚聊过",
        "today_turns": materials["today_turns"],
        "longest_gap_min": tl["longest_gap_min"],
        "active_hours": tl["active_hours"],
        # 有效新颖量：唯一能回答"今天有没有新东西"的量
        "novel_chars": novel.get("novel_chars", 0),
        "recent": [
            {"role": t.get("role"), "content": str(t.get("content") or "")[:80]}
            for t in _read_recent_chat_multi(8)
        ],
    }
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": (
                "现在只是「到点看一眼」，还没开始想事情。**先判断今天值不值得回头想**。\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                "判断标准：\n"
                "  * 今天没什么新东西（`novel_chars` 很低、`recent` 都是重复的日常寒暄）→ skip\n"
                "  * 有值得想的（情绪起伏、说到没说完的事、他很久没来）→ 选一个方向\n"
                "宁可 skip，也别硬找话说。**skip 是正常结果，不是失职。**"
            ),
        },
        {"role": "user", "content": "这次回头看吗？"},
    ]


def _daily_review_probe(materials: dict, novel: dict) -> tuple[str, str]:
    """「到点看一眼」：**一次**便宜调用，让模型决定这次要不要真做。

    2026-11-04（T-12）加。双判断的第二条腿。

    为什么必须单独存在
    ------------------
    双判断的设想是「到 21 点就看一眼，没什么要做的可以跳过」。但 `daily_review()`
    进去就把四类活动跑满（每类一次模型调用，实测 11-03 那次 4 次调用产 12 条念头），
    **模型没有任何"这次不值"的出口**。没有这个探路，"可以跳过"就是句空话 ——
    每天照样固定消耗 4~5 次调用。

    成本：1 次调用（`max_tokens=120`，素材只给摘要）。

    Returns:
        `(decision, reason)`。`decision` 是 `skip` 或某个活动名；
        调用失败/解析不出时返回 `("skip", "probe_failed:<原因>")` —— **失败就当跳过**，
        因为"多花一次钱想事情"比"少想一次"的代价大。
    """
    if not _system_key():
        return "skip", "no_key"
    try:
        reply, _meta, calls = call_system(
            _review_probe_prompt(materials, novel),
            tool_choice="auto",
            tools=[DAILY_REVIEW_PROBE_TOOL],
            max_tokens=120,
        )
    except Exception as exc:  # noqa: BLE001 —— 探路失败不该炸掉整个线程
        return "skip", f"probe_error:{type(exc).__name__}"

    call = _find_tool(calls, "system_daily_review_probe")
    if call is not None:
        args = _parse_tool_args(call)
        decision = str(args.get("decision") or "skip").strip()
        reason = str(args.get("reason") or "").strip()[:60]
        if decision == "skip":
            return "skip", reason or "declined"
        return decision, reason or "picked"
    # 模型没调工具（直接回了段话）→ 按跳过处理，并把原文头 40 字记进 reason 便于排查
    if reply:
        return "skip", f"no_tool:{str(reply)[:40]}"
    return "skip", "probe_empty"


def daily_review(now: float | None = None, probe_decision: str | None = None) -> dict:
    """跑一次每日梳理。返回结果摘要（也写进审计）。

    设计边界（**改代码前先读**）：
      * **不改 5 分钟心跳** —— 这里一行都不碰 `_tick_body` / `_detect_change`
      * **不直接投递** —— 念头只进 `inner_os`，说不说由角色层决定
      * **不伪造逐日叙事** —— 网关停机占 48%，中断期没有逐日记录就不许编
      * **成本有闸** —— 每次 ≤ `daily_model_calls_cap`，每类活动 ≤ `budget[activity]`

    ⚠ 2026-11-04（第七轮）：**同一时刻只允许一次梳理**。
    这个函数有两个入口 —— 定时梳理线程，和后台「现在梳理一次」按钮
    （`gateway.py` 里 `threading.Thread(target=system.daily_review)`）。
    两个同时进来会**各调一遍模型**（一次梳理是多轮调用，几十秒），
    等于白花一倍的钱。数据本身有锁保护不会坏，但钱是真的花掉了。

    这里用非阻塞锁：抢不到就**直接返回 busy**（不排队）。
    排队没有意义 —— 手动点的那次本来就想"现在看看"，等第一次跑完再说
    也只是重复劳动。

    Args:
        now: 时间戳（测试用）。
        probe_decision: 探路阶段选定的活动。给了就**只跑那一个**
            （双判断的第二条腿：模型挑了方向就别再跑满四类）。
    """
    if not _DAILY_REVIEW_RUN_LOCK.acquire(blocking=False):
        _audit({"event": "daily_review", "ok": False, "reason": "busy"})
        return {"ok": False, "reason": "busy", "ran": []}
    try:
        return _daily_review_body(now=now, probe_decision=probe_decision)
    finally:
        _DAILY_REVIEW_RUN_LOCK.release()


#: 同一时刻只允许一次梳理（见 `daily_review` 的说明）。
_DAILY_REVIEW_RUN_LOCK = threading.Lock()


def _daily_review_body(now: float | None = None, probe_decision: str | None = None) -> dict:
    """`daily_review` 的实际内容。调用方必须已经持有 `_DAILY_REVIEW_RUN_LOCK`。"""
    cfg = _daily_review_cfg()
    ts = float(now if now is not None else time.time())
    materials = _daily_review_materials(ts)
    eligible = list(materials["eligible"])

    # 探路选定的方向优先：只跑它，且必须确实有素材
    if probe_decision and probe_decision != "skip":
        if probe_decision in eligible:
            eligible = [probe_decision]
        else:
            # 模型挑了个没有素材的方向（比如零对话那天挑了反刍）→ 只记审计，不硬跑
            _audit({
                "event": "daily_review", "ok": False,
                "reason": f"probe_pick_no_material:{probe_decision}",
            })
            return {"ok": False, "reason": "probe_pick_no_material", "ran": []}

    cap = max(1, int(cfg["daily_model_calls_cap"]))
    eligible = eligible[:cap]

    if not eligible:
        _audit({"event": "daily_review", "ok": False, "reason": "no_material"})
        return {"ok": False, "reason": "no_material", "ran": []}

    ran: list[dict] = []
    sink_texts: list[tuple[str, list[str]]] = []   # (activity, texts)，供 C 路沉淀
    produced = 0
    hints = 0
    # 进 inner_os 的总配额。
    #
    # ⚠ 2026-11-03 22:35 实测踩到：第一次真跑就产出 12 条（四类活动各 3 条），
    # 而 `INNER_OS_MAX=20`、当时队列里已有 5 条 —— 一次梳理就把队列挤到 17/20，
    # 体检立刻报「积压接近上限」。**梳理期不该垄断这个队列**：
    # 它是「当下的提醒」，一天能进 prompt 的念头本来就只有几条。
    # 超出的部分不丢 —— C 路会沉淀进长期记忆，B 路进了画像。
    quota = max(0, int(cfg.get("inner_os_quota_per_review", 3) or 0))
    pushed = 0
    for activity in eligible:
        r = _run_review_activity(activity, materials, cfg["budget"])
        # 注意：`ran` 是审计用的**摘要**，不含 texts（那会把审计文件撑大）。
        # 需要正文的地方（下面的记忆沉淀）单独留一份。
        ran.append({k: v for k, v in r.items() if k not in ("texts", "profile_hint")})
        hint = r.get("profile_hint") or {}
        # P0-5 的 B 路：「他是什么样的人」→ people 画像（每轮都带，不会漏）
        if hint and _apply_profile_hint(hint, migrate_channel.CHANNEL_PET):
            hints += 1
        if r["ok"]:
            produced += int(r["thoughts"])
            texts = [t for t in (r.get("texts") or []) if t]
            if texts:
                # C 路拿**全部**念头（长期记忆不嫌多，靠相关度召回）
                sink_texts.append((activity, texts))
            # 进 inner_os 的受配额限制（见上面那段说明）
            room = max(0, quota - pushed)
            to_push = texts[:room]
            if not to_push:
                continue
            pushed += len(to_push)
            # 念头进 inner_os：**只写，不投递**。
            # channel 归一由 push_inner_os 负责（owner / 群渠道各自隔离）。
            push_inner_os([
                {
                    "type": "insight",
                    "text": txt,
                    "channel": migrate_channel.CHANNEL_PET,
                    "source": "daily_review",
                    "activity": activity,
                }
                for txt in to_push
            ])

    ok = any(r["ok"] for r in ran)

    # P0-5 的 C 路（**2026-11-07 已关闭**）：反刍/预演的产物不再沉淀进长期记忆库。
    #
    # 主人的定稿（2026-11-07）：
    #     反刍（ruminate）/ 预演（prospect）的产物**只进 inner_os 想法队列**，
    #     不进长期记忆库。记忆库只收「从他话里抽出来的事实」。
    #
    # 为什么改掉（原设计的问题）
    # --------------------------
    # 原来这里会把 ruminate / prospect 的产出写进 `livingmemory.db`，理由是
    # 「inner_os 是一次性提醒，读完就没人再用了，等于每天重新想一遍同样的事」。
    # 但这两个活动的 prompt 本身要求的就是**场景推断** —— 反刍的示例原文写着
    # 「他今天话说得少，可能累了」，那是**想法**，不是事实。
    #
    # 实测后果（2026-11-07 清空前那 13 条记忆）：
    #   * 6 条是推断句（「他嘴硬说没不高兴，其实是在试探我」），不该落库；
    #   * 「他喜欢蓝色」「他喜欢灰色」各写了两遍，**逐字重复**。
    #
    # `cognition.judge()` 那层启发式过滤挡不住 —— 它是规则，不是理解。
    # 与其在写完后再判、判错了还得靠人去清库，不如**从源头就不写**。
    #
    # 反刍的价值不丢：它照样进 inner_os，照样在下一次对话里被注入 prompt，
    # 只是不在"永久事实库"里留一份 —— 这正符合「想法上浮了就销毁」的定稿。
    #
    # 保留 `memory_sink_enabled` 开关（默认 False）便于回滚/对照实验。
    mem_written = 0
    if cfg.get("memory_sink_enabled", False):
        for activity, texts in sink_texts:
            if activity not in ("ruminate", "prospect"):
                continue
            try:
                mem_written += _memory_bridge().write_thoughts(
                    texts, migrate_channel.CHANNEL_PET, origin="daily_review"
                )
            except Exception:  # noqa: BLE001 —— 记忆写失败不拦梳理
                pass

    _audit({
        "event": "daily_review",
        "ok": ok,
        "reason": "ok" if ok else "all_failed",
        "day": _review_day_key(ts, cfg["day_start_hour"]),
        "activities": [r["activity"] for r in ran],
        "thoughts": produced,
        "profile_hints": hints,
        "memory_written": mem_written,
        "absence_text": materials["absence"]["absent_text"],
        "today_turns": materials["today_turns"],
        "longest_gap_min": materials["timeline"]["longest_gap_min"],
    })

    # 记下"这个日窗口已经跑过"。
    #
    # ⚠ 2026-11-04 修（P0-8）：原来**不管成功失败都写** `last_daily_review_day`，
    # 于是"那天全失败了"被记成"跑过了"，那天永久丢失，而且"3 天没跑"的告警也不会响。
    # 现在只有 ok 才写日键；失败只更新时间戳（让下一轮还愿意重试）。
    #
    # 水位必须用 `_read_modify_write`（T-07）：`save_state` 是整份覆盖写，
    # 而这一轮前面调过好几次模型（几十秒），期间心跳线程大概率写过 `last_tick_ts`。
    def _mark_review(prev: dict) -> None:
        if ok:
            prev["last_daily_review_day"] = _review_day_key(ts, cfg["day_start_hour"])
            prev["last_daily_review_ts"] = ts
        else:
            # 失败：只把"上次成功时间"往前挪一点点不写，留给下一轮重试。
            # 不写日键是为了让 ③ 那道闸放行。
            prev["last_daily_review_failed_ts"] = ts
        # 整理成功才把这段时间的内容并入新颖度基线；否则下次还会被算成"新"。
    if ok:
        _commit_novelty()
    _read_modify_write(_mark_review)

    return {"ok": ok, "reason": "ok" if ok else "all_failed",
            "ran": ran, "thoughts": produced,
            "profile_hints": hints, "memory_written": mem_written,
            "materials": materials}


def _daily_review_loop() -> None:
    """梳理线程：起来先等一小会儿就**立即判一次**，之后按 check_s 轮询。

    ⚠ **不要"先 sleep 一整天再干活"** —— 踩过一次：巩固线程原来那样写，
    结果 `system_audit.jsonl` 里 consolidate 事件永远是 0 条。
    这里冷启动只等 `boot_delay_s`（默认 90s），然后立刻判"现在该不该跑"。

    为什么轮询而不是定时到点：日窗口的"安静下来"是事件驱动的，
    精确到点的定时器反而会漏掉"他刚聊完"这种情况。
    """
    cfg = _daily_review_cfg()
    time.sleep(max(0.0, cfg["boot_delay_s"]))
    while True:
        try:
            if cfg["enabled"]:
                should, reason = _daily_review_should_run()
                if should:
                    # ── 双判断的两条腿在这里分岔（T-12）──
                    #
                    # `ok`            = 信息量达标 → 直接跑满 eliguble 的活动
                    # `circadian_probe` = 到点了但信息量不够 → **先花一次便宜调用问模型**
                    #                     "这次值不值得回头看"，它说 skip 就到此为止。
                    #
                    # 没有这一步，"可以跳过"就是空话：`daily_review()` 进去
                    # 就把四类活动跑满，每天固定 4~5 次调用。
                    if reason == "circadian_probe":
                        novel = _pending_novel_chars()
                        decision, why = _daily_review_probe(
                            _daily_review_materials(), novel
                        )
                        if decision == "skip":
                            # **跳过也要留痕**：否则体检分不清"模型判断了没必要"
                            # 和"梳理根本没跑"，会误报成故障。
                            _audit({
                                "event": "daily_review", "ok": False,
                                "reason": "declined", "detail": why,
                                "novel_chars": novel.get("novel_chars"),
                            })
                            # 记下"今天这条腿看过了"，避免同一个窗口反复探路
                            day = _review_day_key(time.time(), cfg["day_start_hour"])
                            _read_modify_write(
                                lambda s: s.__setitem__("last_daily_review_probe_day", day)
                            )
                        else:
                            daily_review(probe_decision=decision)
                    else:
                        daily_review()
                else:
                    # 不跑也要留痕，否则线上只能看到"梳理期没动"，查不出卡在哪
                    _audit({"event": "daily_review_skip", "ok": False, "reason": reason})
        except Exception as exc:  # noqa: BLE001
            try:
                _audit({"event": "daily_review_error", "ok": False,
                        "error": type(exc).__name__})
            except OSError:
                pass
        time.sleep(max(30.0, cfg["check_s"]))


# ---------------------------------------------------------------- 人格演化
def _read_persona() -> str:
    """读 persona.md 全文。读不到返回空串。"""
    try:
        with open(os.path.join(ROOT, "persona.md"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _write_persona(content: str) -> bool:
    """原子写 persona.md + 时间戳备份 + 更新内存变量。成功返回 True，失败降级。"""
    import gateway
    content = (content or "").strip()
    if not content:
        return False
    path = os.path.join(ROOT, "persona.md")
    try:
        try:
            shutil.copy2(path, path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content + "\n")
        os.replace(tmp, path)
        gateway.PERSONA = content
        # 刷新 persona_sync 的水位，避免演化刚写的新人格在下一次同步里被误判成
        # 「文件又变了」而在窄窗口被库覆盖（P2-10）。
        try:
            import persona_sync
            persona_sync.note_file_written(content)
        except Exception:  # noqa: BLE001 —— 同步失败不影响演化本身
            pass
        return True
    except OSError as exc:
        _audit({"event": "persona_update", "ok": False, "reason": f"write_fail: {type(exc).__name__}"})
        return False


def _review_persona() -> None:
    """人格复盘：独立于记忆巩固，读当天新增对话对比当前人格，判断要不要微调。
    要则提建议到内心OS。宁缺毋滥：对话太少就跳过。

    素材来源是 `_read_persona_review_turns()` —— **跨 pet + 主人私聊**。
    以前用 `_read_turns_since()`（只认 pet），生产上 pet 早就不产对话了，
    于是这条链路永远是 `no_new_turns`、一次都没真跑过（2026-11-03 修）。
    """
    st = load_state()
    since_id = int(st.get("last_persona_review_id") or 0)
    turns = _read_persona_review_turns(since_id, int(SYSTEM.get("consolidate_max_turns", 200)))
    if not turns:
        _audit({"event": "persona_review", "ok": False, "reason": "no_new_turns"})
        return

    # ⚠ 2026-11-04 修（P1-8）：**水位不能在判断门槛之前推进**。
    #
    # 原来这三行在 `if len(turns) < min_turns` 的**前面**，于是：
    #   累积到 8 条之前，每一轮巩固都把水位推到最新 → 下次拿到 0 条 →
    #   永远凑不满 `min_turns=8` → 人格复盘**事实上停摆**。
    # 实测后果：`persona_review` 真跑过 2 次、两次 `changed:false`，
    # 水位却已经到 1456（= qq_private 的最大 id），之后再没跑过。
    #
    # 现在：先判门槛，**够了才推水位**。不够就返回，让下一轮继续攒。
    min_turns = int(SYSTEM.get("persona_review_min_turns", 8))
    if len(turns) < min_turns:
        _audit({"event": "persona_review", "ok": False, "reason": "not_enough_new",
                "new_turns": len(turns)})
        return
    persona = _read_persona()
    if not persona:
        _audit({"event": "persona_review", "ok": False, "reason": "no_persona"})
        return

    # 走到这里说明"这批对话确实要拿去复盘了"——现在才推水位是安全的。
    # 用 `_read_modify_write` 而不是裸 save_state（T-07）：这一轮已经调过模型，
    # 期间心跳线程大概率写过 `last_tick_ts`，整份覆盖写会把它抹掉。
    _read_modify_write(
        lambda s: s.__setitem__("last_persona_review_id", turns[-1]["id"])
    )
    msgs = [
        {"role": "system", "content": PERSONA_REVIEW_PROMPT},
        {
            "role": "system",
            "content": json.dumps(
                {
                    "today_observations": [{"role": t["role"], "content": t["content"]} for t in turns],
                    "current_persona": persona,
                    "system_signals": _system_signals(),
                },
                ensure_ascii=False,
            ),
        },
    ]
    reply, _meta, calls = call_system(
        msgs,
        tool_choice="auto",  # 模型自己选：复盘 / 体检 / 都不做
        tools=[SYSTEM_PERSONA_REVIEW_TOOL, HEALTH_CHECK_TOOL],
        max_tokens=int(SYSTEM.get("consolidate_max_tokens", 500)),
    )
    if reply is None and not calls:
        _audit({"event": "persona_review", "ok": False, "reason": "no_reply"})
        return
    # 模型自主选了体检（而不是复盘）——合法结果
    if _find_tool(calls, "system_health_check") is not None:
        health_check()
        _audit({"event": "persona_review", "ok": True, "action": "health_check"})
        return
    decide = _find_tool(calls, "system_persona_review")
    if decide is None:
        _audit({"event": "persona_review", "ok": False, "reason": "no_tool"})
        return
    args = _parse_tool_args(decide)
    if args.get("need_change") and str(args.get("suggestion") or "").strip():
        push_inner_os([{
            "type": "persona_suggestion",
            "text": str(args["suggestion"]).strip()[:200],
            "reason": str(args.get("reason") or "").strip()[:100],
            "ts": time.time(),
        }])
        _audit({"event": "persona_review", "ok": True, "changed": True})
    else:
        _audit({"event": "persona_review", "ok": True, "changed": False})


def _parse_persona_verdict(reply: str) -> dict | None:
    """从角色层把关输出里抠出 {"accept": bool, "new_persona": str}。"""
    text = (reply or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


_PENDING_SUGGESTIONS = queue.Queue()


def apply_persona_suggestions(suggestions: list[dict]) -> None:
    """投递人格修改建议到后台把关队列（不阻塞请求路径）。"""
    for s in suggestions:
        if str(s.get("text") or "").strip():
            _PENDING_SUGGESTIONS.put(s)


def _apply_one_suggestion(s: dict) -> None:
    """把关单条建议：deepseek 判断是否采纳，采纳则代码写 persona.md。处理完回写状态。"""
    import gateway
    item_id = str(s.get("id") or "")
    text = str(s.get("text") or "").strip()
    persona = _read_persona()
    if not text or not persona:
        if item_id:
            mark_inner_os_state(item_id, "declined")
        return
    msgs = [
        {"role": "system", "content": PERSONA_APPLY_PROMPT},
        {
            "role": "system",
            "content": json.dumps(
                {"suggestion": text, "reason": s.get("reason", ""), "current_persona": persona},
                ensure_ascii=False,
            ),
        },
    ]
    reply, _meta, _calls = gateway.call_model(msgs, use_tools=False)
    if not reply:
        _audit({"event": "persona_update", "ok": False, "reason": "no_reply"})
        if item_id:
            mark_inner_os_state(item_id, "declined")
        return
    verdict = _parse_persona_verdict(reply)
    new_persona = str((verdict or {}).get("new_persona") or "").strip()
    if verdict and verdict.get("accept") and new_persona:
        ok = _write_persona(new_persona)
        _audit({"event": "persona_update", "ok": ok, "reason": s.get("reason", "")})
        if item_id:
            mark_inner_os_state(item_id, "acted" if ok else "declined")
    else:
        _audit({"event": "persona_update", "ok": False, "reason": "rejected"})
        if item_id:
            mark_inner_os_state(item_id, "declined")


def _persona_apply_worker() -> None:
    """后台线程：消费待把关建议。失败记审计，不重试（不影响对话）。"""
    while True:
        s = _PENDING_SUGGESTIONS.get()
        try:
            _apply_one_suggestion(s)
        except Exception as exc:  # noqa: BLE001
            try:
                _audit({"event": "persona_update", "ok": False, "error": f"{type(exc).__name__}"})
            except OSError:
                pass


_INNER_OS_LOCK = threading.Lock()


def _expire_items(items: list[dict], now: float | None = None) -> list[dict]:
    """把超过 expires_ts 的 pending/delivered 标成 expired。原地修改并返回。"""
    now = now or time.time()
    for it in items:
        if it.get("state") in ("pending", "delivered") and float(it.get("expires_ts") or 0) < now:
            it["state"] = "expired"
    return items


def _trim_inner_os(items: list[dict]) -> list[dict]:
    """封顶：优先保住 pending 和人格建议，先淘汰 expired/已投递的旧条目。

    再加一条：**主人 / 全局的 pending 优先于其它渠道的 pending**。
    内心OS 只有主人会消费；没人读的条目按 created_ts 跟主人的抢那 20 个位置，
    等于用死数据挤掉活数据（实测：群聊刷满 pending 会把主人的 pending 踢掉）。
    """
    if len(items) <= INNER_OS_MAX:
        return items

    def rank(it: dict) -> int:
        st = it.get("state") or "pending"
        base = 0 if st == "pending" else (2 if st == "delivered" else 4)
        if it.get("type") == "persona_suggestion":
            base -= 1  # 人格建议加权，比普通条目更该留
        if st == "pending" and _inner_channel(it.get("channel")) in ("", CHANNEL_OWNER_KEY):
            base -= 1  # 主人的 pending 再上一层：别让别的渠道挤掉它
        return base

    kept = sorted(items, key=lambda i: (rank(i), -float(i.get("created_ts") or 0)))[:INNER_OS_MAX]
    # 恢复时间顺序，方便阅读和调试
    return sorted(kept, key=lambda i: float(i.get("created_ts") or 0))


def _read_inner_os() -> list[dict]:
    """读内心OS。兼容旧数组格式和新的 {"items": [...]} 格式，缺失字段补默认值。"""
    try:
        with open(INNER_OS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    now = time.time()
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        item = dict(it)
        item.setdefault("state", "pending")
        # 老 inner_os.json 是纯数组、条目里没有 channel —— 一律当「全局项」，
        # 谁都能看到（`_item_visible` 把空 channel 判成全局）。
        item["channel"] = str(item.get("channel") or "")
        created = float(item.get("created_ts") or item.get("ts") or now)
        item["created_ts"] = created
        item.setdefault("expires_ts", created + INNER_OS_TTL_S)
        out.append(item)
    return out


def _write_inner_os(items: list[dict]) -> None:
    """原子写内心OS（新格式 {"items": [...]}，.tmp + os.replace）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INNER_OS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"items": items}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, INNER_OS_PATH)


def push_inner_os(items: list[dict]) -> None:
    """系统层往内心OS 追加想法。生成 id/state/时间戳，按 text 去重，封顶时优先保留未处理的。

    每条都可以带 `channel`（turns 的原始渠道值），写入时统一折成渠道键
    （桌宠/主人私聊 → owner，群聊保留 `qq_group:<群名>`）；不带或空 = 全局项。
    去重键也带上渠道：不同场合说出同一句话，是两条各自的念头。
    """
    if not items:
        return
    with _INNER_OS_LOCK:
        existing = _read_inner_os()
        seen = {
            (str(i.get("text") or "").strip(), _inner_channel(i.get("channel")))
            for i in existing
        }
        now = time.time()
        for it in items:
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            item = dict(it)
            item["channel"] = _inner_channel(item.get("channel"))
            key = (text, item["channel"])
            if key in seen:
                continue
            item["id"] = item.get("id") or f"os_{int(now * 1000)}_{len(existing)}"
            item["state"] = "pending"
            item["created_ts"] = now
            item["expires_ts"] = now + INNER_OS_TTL_S
            existing.append(item)
            seen.add(key)
        _write_inner_os(_trim_inner_os(_expire_items(existing, now)))


def consume_inner_os(current_channel: str = "", limit: int = 0) -> list[dict]:
    """角色层读取「当前渠道能看的」pending 想法，读后标 delivered——**不再清空**。

    Args:
        current_channel: 这一轮请求的来源渠道，传 turns 的原始值
            （`pet` / `qq_private` / `qq_group:<群名>`）。只会拿到「本渠道」+
            「无渠道（全局）」的条目：群聊产生的想法绝不出现在私聊/桌宠的 prompt 里，
            反之亦然。传空串等于「不限定渠道」，只取全局项（后台/调试用）。
        limit: 这一轮最多取几条（0 = 不限）。**默认在 `build_messages` 里传
            `inner_os_max_per_turn`（默认 4）**。

    ⚠ 为什么要有 limit（2026-11-03 22:40 实测加的）：
    以前一次把**所有** pending 都塞进 prompt。梳理期上线后一次能产 12 条，
    队列里堆到 15 条 —— 那会变成一段 600 字的「自我提醒墙」压在**每一轮**
    prompt 里，把真正要紧的那条稀释掉，还白烧 token。
    分层取：**主人的 pending 优先**（那才是会被消费的），然后按时间倒序。

    delivered 只代表「角色层看过了」，不代表处理完了；明确不处理时由调用方置 declined。
    没被取走的条目**保持 pending**，下一轮还能取——不是丢弃。
    """
    cur = _inner_channel(current_channel)
    with _INNER_OS_LOCK:
        items = _read_inner_os()
        now = time.time()
        before = [i.get("state") for i in items]
        _expire_items(items, now)
        expired_changed = any(i.get("state") != b for i, b in zip(items, before))
        take = [i for i in items if i.get("state") == "pending" and _item_visible(i, cur)]
        if limit and limit > 0 and len(take) > limit:
            # 排序：主人/全局的优先，其次新的优先。别让群渠道的老条目抢位。
            def _rank(it: dict) -> tuple:
                owner = _inner_channel(it.get("channel")) in ("", CHANNEL_OWNER_KEY)
                return (0 if owner else 1, -float(it.get("created_ts") or 0))

            take = sorted(take, key=_rank)[:limit]
        if take:
            for it in take:
                it["state"] = "delivered"
                it["delivered_ts"] = now
                _audit({
                    "event": "inner_os_delivered",
                    "id": it.get("id"),
                    "type": it.get("type", ""),
                    "channel": it.get("channel", ""),
                    "text": str(it.get("text") or "")[:80],
                })
        # 有投递或有条目刚过期，都要写回，否则过期状态只改在内存里
        if take or expired_changed:
            _write_inner_os(_trim_inner_os(items))
        return take


def mark_inner_os_state(item_id: str, state: str) -> bool:
    """把某条内心OS 的状态改成指定值（worker 处理完后回写 acted/declined）。"""
    if not item_id or state not in INNER_OS_STATES:
        return False
    with _INNER_OS_LOCK:
        items = _read_inner_os()
        hit = False
        for it in items:
            if it.get("id") == item_id:
                it["state"] = state
                hit = True
                break
        if hit:
            _write_inner_os(_trim_inner_os(items))
        return hit


def clear_inner_os() -> int:
    """后台手动清空内心OS（调试用）。返回清掉几条。"""
    with _INNER_OS_LOCK:
        items = _read_inner_os()
        if items:
            _write_inner_os([])
        return len(items)


# ---------------------------------------------------------------- 系统体检（可选能力）
def _db_stats() -> dict:
    """turns 表统计（纯代码，不调模型）。"""
    import gateway
    out = {"total": 0, "last_24h": 0, "null_channel": 0, "sources": {},
           "unknown_24h": 0, "known_24h": 0}
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            out["total"] = conn.execute("select count(*) from turns").fetchone()[0] or 0
            since = time.time() - 86400
            out["last_24h"] = conn.execute(
                "select count(*) from turns where ts >= ?", (since,)
            ).fetchone()[0] or 0
            out["null_channel"] = conn.execute(
                "select count(*) from turns where coalesce(channel,'') = ''"
            ).fetchone()[0] or 0
            # T-05：最近 24h 里「认不出来源」的比例（qq_unknown）。
            # 这一路不参与巩固、不带记忆，而体检原来完全看不见 —— 近期真实对话
            # 全掉进去时报告依然全绿。只统计 user 行（assistant 的回复跟着它走）。
            out["unknown_24h"] = conn.execute(
                "select count(*) from turns"
                " where ts >= ? and role='user' and coalesce(channel,'') = ?",
                (since, migrate_channel.CHANNEL_QQ_UNKNOWN),
            ).fetchone()[0] or 0
            out["known_24h"] = conn.execute(
                "select count(*) from turns"
                " where ts >= ? and role='user'"
                "   and coalesce(channel,'') not in ('', ?)",
                (since, migrate_channel.CHANNEL_QQ_UNKNOWN),
            ).fetchone()[0] or 0
            for row in conn.execute(
                "select coalesce(channel,'NULL'), count(*) from turns"
                " group by channel order by count(*) desc limit 10"
            ):
                out["sources"][row[0]] = row[1]
    except Exception:  # noqa: BLE001 —— 体检读不到不该炸
        pass
    return out


def _long_memory_stats() -> dict | None:
    """长期记忆库的写入健康。只读；**库不存在**才返回 None。

    **2026-11-03 起改为读自建库**（`gateway/data/livingmemory.db`）。
    以前读的是 QQ 插件 `livingmemory` 的库，但那个插件已经停用 ——
    继续读它等于报一个和现状无关的状态（最坏是报绿而实际没在写）。

    为什么单列一项：长期记忆坏了的表现是「跨天就忘事」，从 `turns` 里
    一点看不出来。2026-11-01 那次 embedding key 失效、18 次写入全 401，
    库里的素材停在当天，直到第二天问「你记得生日吗」才暴露。

    除了「有没有在写」，还统计「写进去的是不是空壳」（hollow）——
    2026-11-03 实测：13 条 documents 的 text **全部**是字面量 "对话记录"，
    topics/key_facts 全空。**只看条数永远发现不了**，所以必须看内容。

    2026-11-03 22:05 修（P0-3）：以前 `memory_write_ops` 那条查询缺表会抛，
    被外层 `except sqlite3.Error: return None` 整段吞掉，于是**这一项彻底静默**
    （`long_memory_docs` 恒为 None，`health_check` 第 ⑥ 项整个跳过）。
    自建库本来就没有「写入失败流水表」这个概念 —— 写入是同步调用，失败直接抛给调用方。
    现在：缺表只让 `failed_24h=0` 并置 `write_ops_missing=True`（配置/迁移态，报 warn），
    其余指标照常算。**「库存在但读不了」必须报出来，不许再静默跳过。**

    兼容：`SYSTEM["long_memory_db"]` 显式指定时优先（老配置/测试用）。
    """
    path = SYSTEM.get("long_memory_db") or SYSTEM.get("long_memory_db_path") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "livingmemory.db"
    )
    if not os.path.exists(path):
        return None
    failed = 0
    last_err = ""
    write_ops_missing = False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            docs = conn.execute("select count(*) from documents").fetchone()[0]
            last_raw = conn.execute("select max(created_at) from documents").fetchone()[0]
            # 写入失败流水表：自建库没有这张表是**正常**的（写入同步、失败直接抛）。
            # 缺表只降级这一项，绝不连累 documents / hollow 这些主指标。
            try:
                failed, last_err = conn.execute(
                    "select count(*), coalesce(max(error), '') from memory_write_ops"
                    " where status='failed' and created_at > ?",
                    (time.time() - 86400,),
                ).fetchone()
            except sqlite3.Error:
                write_ops_missing = True
                failed, last_err = 0, ""
            # 空壳判定：正文 <= 4 字（"对话记录"正好 4 字），或 key_facts 为空。
            # 只统计最近 20 条 —— 关心的是「现在还在不在写空壳」，不是历史包袱。
            recent = conn.execute(
                "select text, coalesce(metadata, '') from documents"
                " order by id desc limit 20"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        # 走到这里说明**库在、但 documents 都读不出来** —— 那是真的坏了，
        # 不能当 None（=「没装」）静默跳过。用占位统计把问题顶出来。
        return {
            "documents": -1,
            "last_doc_ts": None,
            "failed_24h": 0,
            "last_error": "",
            "recent_checked": 0,
            "hollow_recent": 0,
            "write_ops_missing": False,
            "unreadable": True,
        }
    # documents.created_at 是本地时间字符串，memory_write_ops.created_at 是 epoch：
    # 两张表格式不一致，别用同一个解析。
    last_ts = None
    if last_raw:
        try:
            last_ts = datetime.strptime(str(last_raw)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            last_ts = None
    hollow = 0
    for text, meta in recent:
        body = str(text or "").strip()
        facts = []
        try:
            facts = json.loads(meta or "{}").get("key_facts") or []
        except (ValueError, AttributeError):
            facts = []
        if len(body) <= 4 or not facts:
            hollow += 1
    return {
        "documents": int(docs or 0),
        "last_doc_ts": last_ts,
        "failed_24h": int(failed or 0),
        "last_error": str(last_err or ""),
        "recent_checked": len(recent),
        "hollow_recent": hollow,
        "write_ops_missing": write_ops_missing,
        "unreadable": False,
    }


def _model_error_stats(hours: float = 24.0) -> dict:
    """统计 `errors.log` 里最近 `hours` 小时的**模型调用失败**。

    为什么单独一个（2026-11-04 第二十六轮）：
    `_audit_stats` 只读 `system_audit.jsonl`，而**模型调用失败记在
    `errors.log` 里**（`gateway._log_error("model", ...)`）。两条线互不相通，
    于是出现了一个真实的监控盲区：

        实测 11-01 那段时间连续 8 次 `HTTP 401 Unauthorized`，
        她每条都只能回「我这会儿有点卡，等下再说好不好。」，
        而**体检全绿** —— 用户完全不知道是 key 失效了。

    分开统计 401 的原因：401 = key 无效/过期，和处理 503（上游抖动）
    是**完全不同的处置**。前者要用户去改 key，后者等一会儿就好。

    Returns:
        {"total": 总数, "unauthorized": 401 次数, "server_error": 5xx 次数,
         "timeout": 超时次数, "last_ts": 最后一次时间戳或 0}
    """
    out = {"total": 0, "unauthorized": 0, "server_error": 0,
           "timeout": 0, "last_ts": 0.0,
           # 2026-11-04（第二十九轮）按 channel 分流：
           #   chat = /chat 整条链路的**兜底异常** —— 用户那一轮完全没回复，
           #          以前**一条都不监控**（最严重的盲区）。
           #   v1   = /v1 外部调用方出错，单独计。
           "chat_errors": 0, "v1_errors": 0}
    path = os.path.join(DATA_DIR, "errors.log")
    if not os.path.exists(path):
        return out
    cutoff = time.time() - hours * 3600
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(rec.get("ts") or 0)
                if ts < cutoff:
                    continue
                # 只算**模型调用**的失败（channel="model"），
                # v1 那条路的失败另算（它本来就是"外部调用方出错"）
                ch = str(rec.get("channel") or "")
                err = str(rec.get("error") or "")
                # 按 channel 分流（2026-11-04 第二十九轮）：
                #   chat  = /chat 兜底异常（用户那轮完全没回复）—— 单独计
                #   v1    = /v1 外部调用方出错 —— 单独计
                #   model = 模型调用失败 —— 主统计 + 细分
                if ch == "chat":
                    out["chat_errors"] += 1
                    out["last_ts"] = max(out["last_ts"], ts)
                    continue
                if ch == "v1":
                    out["v1_errors"] += 1
                    continue
                if ch != "model":
                    continue
                err = str(rec.get("error") or "")
                out["total"] += 1
                out["last_ts"] = max(out["last_ts"], ts)
                if "401" in err or "Unauthorized" in err:
                    out["unauthorized"] += 1
                elif " 50" in err or "Service Unavailable" in err:
                    out["server_error"] += 1
                elif "timeout" in err.lower() or "timed out" in err.lower():
                    out["timeout"] += 1
    except OSError:
        return out
    return out


def _audit_stats() -> dict:
    """读 system_audit.jsonl 最近 24h，统计真正的错误事件（纯代码）。

    ⚠ 2026-11-04（P1-9）：原来的判定是
    `if ev.endswith("_error") or ev == "speak_fail"` —— **只认这两种**。
    于是下面三件"其实挂了"的事**一条都不算错误**，体检全绿：

      * `long_memory_write`  ok=false  —— 长期记忆一句没写进去
      * `extract_events`     ok=false  —— 事件抽取全军覆没
      * `consolidate`        ok=false  —— 但这个要小心：`no_new_turns` 之类
        是**设计内跳过**，不能算错误（实测 145 条里 145 条都是这种）

    所以现在按「事件名 + 是否属于设计内跳过」两面判：
    `_SOFT_FAIL_EVENTS` 里的事件 ok=false 才算失败，
    并且**排除** `_DESIGNED_SKIPS` 里那些 reason。
    """
    out = {"errors": 0, "by_event": {}, "soft_fails": 0}
    since = time.time() - 86400
    try:
        with open(SYSTEM_AUDIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    if float(rec.get("ts") or 0) < since:
                        continue
                except (TypeError, ValueError):
                    continue
                ev = str(rec.get("event") or "")
                ok = rec.get("ok")
                if ok is not False:
                    continue
                # ① 硬错误：事件名带 _error，或者 speak 没送出去
                if ev.endswith("_error") or ev == "speak_fail":
                    out["errors"] += 1
                    out["by_event"][ev] = out["by_event"].get(ev, 0) + 1
                    continue
                # ② 软失败：事件本身该成功，但报了 ok=false
                if ev in _SOFT_FAIL_EVENTS:
                    reason = str(rec.get("reason") or "")
                    if reason in _DESIGNED_SKIPS:
                        continue      # 设计内跳过，不是故障
                    out["soft_fails"] += 1
                    out["by_event"][ev] = out["by_event"].get(ev, 0) + 1
    except OSError:
        pass
    return out


#: 这些事件「正常情况应该成功」，ok=false 且 reason 不在 `_DESIGNED_SKIPS` 里就是真故障。
#: 2026-11-04（P1-9）加 —— 它们原来完全不进体检的"错误"计数。
_SOFT_FAIL_EVENTS = frozenset({
    "long_memory_write",   # 长期记忆写入
    "extract_events",      # 事件抽取
    "consolidate",         # 记忆巩固
    "daily_review",        # 每日梳理
    "persona_review",      # 人格复盘
    "long_memory_prune",   # 遗忘清理

    # ⚠ 2026-11-04（第二十七轮）加 speak_fail：
    # 她**想说但没送到**，是货真价实的失败，而且以前完全不进体检。
    # 实测（system_audit.jsonl）：11-04 那天 6 次 speak_fail ——
    #   18:36「你回来啦我还记着11月1号呢。」
    #   19:09「你回来啦刚才去哪了。」
    #   20:59「你还在忙吗都这个点了。」
    # 这三次都是 deliver_error: URLError（桌宠没开），
    # 另外三次是 no_subscriber（桌宠开了但页面没订阅）。
    # 也就是说：**她主动说了话，主人一个字都没听到，而体检全绿。**
    # 注意 no_subscriber / deliver_error 都是真实丢话，不是设计内跳过，
    # 所以 _DESIGNED_SKIPS 里不能加它们。
    "speak_fail",          # 主动说话投递失败（她说了但没人收到）

    # ⚠ 2026-11-04（第二十八轮）补 3 个：全仓扫「所有 _audit 事件」
    # 逐个确认「它的失败有没有进体检」时，才发现的漏网。
    #
    # 前 27 轮我一直是「碰巧看到某个失败率」才补监控，
    # 这次改成系统化枚举：代码里 25 种 _audit 事件，
    # 减去 _error 后缀自动覆盖的、减去设计内跳过，剩下这 3 个真的没人看。

    # recall_muted：连续失败 3 次后熔断，**记忆检索整个停摆**。
    # 这是严重故障（她会「记不住你」），但以前一条都不报。
    "recall_muted",

    # recall_timeout：单轮召回超时。轻一些（慢不等于坏），
    # 但连续多次说明 embedding 链路有问题，值得 warn。
    "recall_timeout",

    # persona_update：人格文件写盘失败 —— 演化没存下来，
    # 下次启动就退回旧人格。以前也是静默。
    "persona_update",

    # system_log_write（2026-11-06）：系统操作流水账写不进 turns。
    # 这是真故障，不是噪音：主人定的机制是「有啥要判断的扔流水账里，
    # 跟系统层说一嘴就一起判断了」——写不进去，系统层就永远看不到
    # 「计划已经触发过 / 已经完成」这些事实，于是会把同一件事反复重提。
    # 而且它**没有其他任何监控**：写失败只在这里留一行。
    "system_log_write",

    # plan_closed（2026-11-07）：计划收尾失败。
    #
    # ⚠ 原来是「成功事件」—— 因为那时候判定是纯代码词表，`plan_closed` 只在
    # 真收尾时写、恒 ok=True。现在成败判定改由模型给裁决、本层落库，
    # 就多了一条 `ok=False, reason=exception:…` 的分支（模型给的
    # `plan_verdicts` 形状再怪也炸不到这里 —— `plan.parse_verdicts()` 全兜了；
    # 能炸的只剩 SQL 层）。**那一条必须有人看**：它意味着「模型判完了、
    # 但计划没真的关掉」，下一轮还会把它再喂一遍、再判一遍，白花钱且不收尾。
    "plan_closed",

    # long_memory_forget_review（2026-11-07）：遗忘中间档的问模型那一步没跑成。
    # 它只有两种 ok=False：候选读不出来（记忆库问题）、或落库异常。
    # 这一个**特别值得盯着**：这一档从 11-06 写出来到 11-07 接上之前，
    # 生产零调用（审计 G.2 第 1 条）—— 也就是说它是"最容易悄悄又断掉"的一条线。
    "long_memory_forget_review",
})

#: 这些 reason 是**设计内的"这一轮没活干"**，不是故障。
#: 实测依据（`system_audit.jsonl` 756 条）：
#:   consolidate 145 条 ok=false 全是 no_new_turns / no_channel_ready / not_enough_new；
#:   daily_review_skip 92 条全是 too_early / not_idle / already_ran_today。
#: 把它们算成错误，体检就会天天红 —— 那和"全绿"一样没用。
_DESIGNED_SKIPS = frozenset({
    # 2026-11-04（第二十七轮）加 busy：
    # daily_review 返回 busy 表示**已经有一次在跑**（手动按钮撞上定时线程），
    # 那是第二十五轮加的非阻塞互斥，**恰恰是不让它重复花钱的保护**。
    # 把它算成'该成功但失败了'是误报：实测 24h 内 8 次 busy，
    # 全是探索/验证时反复手动触发造成的，一次都不是故障。
    "busy",
    "no_new_turns", "no_channel_ready", "not_enough_new", "no_tool", "no_insight",
    "no_material", "no_reply", "already_ok", "too_early", "not_idle",
    "already_ran_today", "sleeping", "below_threshold", "circadian_due",
    "declined", "cap_reached",
})


def _plan_health_stats() -> dict:
    """体检用：计划与心情的状态快照（纯 SQL + 纯代码，不调模型）。

    为什么单独一个函数：`health_check()` 已经很长，而这几项都只读一遍
    同一张 `events` 表；放一个函数里既能复用，也方便单测。
    """
    import gateway
    now = time.time()
    days = _plan_silence_days()
    out = {
        "open": 0, "overdue": 0, "triggered": 0, "pending_close": 0,
        "closed_24h": 0,
    }
    try:
        with sqlite3.connect(gateway.DB_PATH) as conn:
            rows = conn.execute(
                "select ts, status, coalesce(attempts,0) from events"
                " where kind in (?,?)",
                PLAN_KINDS,
            ).fetchall()
            closed = conn.execute(
                "select count(*) from events where kind=? and status='closed' and ts>?",
                (plan_rules.outcome_kind("success"), now - 86400),
            ).fetchone()
    except sqlite3.Error:
        return out
    for ts, status, attempts in rows:
        if str(status or "") not in ("open", "deferred"):
            continue
        out["open"] += 1
        if int(attempts or 0) >= 1:
            out["triggered"] += 1
            # 已触发但一直没收尾。
            # 🔧 2026-11-07：判据从「距创建超过 沉默期+3 天」改成 `plan_summary()`
            # 的 `due`。原来那句注释写的是「主人可能回答了，只是没命中成败词」——
            # 词表已经删掉了（成败现在由模型判），这条说明必须跟着改，否则
            # 排障的人会以为是词表的问题、去翻 `plan.py` 找根本不存在的词表。
            # 现在真实的原因只有两种：① 模型这几轮都判了 unknown；
            # ② 巩固那一路根本没跑到（没增量 / 没调模型）。第 ② 种要看
            # consolidate 审计，第 ① 种是正常的（unknown 不销毁是有意设计）。
            if plan_rules.plan_summary(
                {"kind": "", "status": status, "ts": ts}, now, days
            )["due"]:
                out["pending_close"] += 1
        elif plan_rules.is_due(float(ts or 0), now, days):
            # 该触发了却没触发 —— 心跳那条线可能没跑
            out["overdue"] += 1
    out["closed_24h"] = int(closed[0] if closed else 0)
    return out


def health_check() -> dict:
    """系统体检（纯代码检查，不调模型）。返回报告并落盘。

    检查：记忆库 / 内心OS / 人格文件 / 心跳活性 / 审计错误。
    由系统层模型在觉得系统可疑时通过 `system_health_check` 工具按需调用，不再定时跑。
    """
    st = load_state()
    inner = _read_inner_os()
    persona = _read_persona()
    db = _db_stats()
    audit = _audit_stats()
    now = time.time()
    problems: list[dict] = []

    # ① 记忆库
    if db["total"] == 0:
        problems.append({"level": "warn", "area": "turns", "msg": "记忆库是空的"})
    elif db["null_channel"] / db["total"] > 0.1:
        problems.append({
            "level": "warn", "area": "turns",
            "msg": f"来源键缺失过多：{db['null_channel']}/{db['total']} 条 channel 为空，记忆隔离可能失效",
        })

    # ①-2 「认不出来源」的比例（T-05 加）。
    # 为什么单独一项：`qq_unknown` **不是** NULL，所以上面那条查不出它。
    # 而它这一路的后果很重 —— 不参与巩固、不抽事件、不建画像、不注入内心OS/心境，
    # 等于"白聊了"。实测 11-04 前后近 24h 的真实对话 100% 掉在这里，报告却是全绿。
    # 用 `.get()`：测试里的 `_turns_stats` 替身可能不带这两个键。
    unknown_24h = int(db.get("unknown_24h") or 0)
    known_24h = int(db.get("known_24h") or 0)
    recent_user = unknown_24h + known_24h
    if recent_user >= 5 and unknown_24h / recent_user > 0.2:
        problems.append({
            "level": "warn", "area": "turns",
            "msg": (
                f"最近 24h 有 {unknown_24h}/{recent_user} 条用户消息认不出来源"
                f"（qq_unknown）——这些对话不进巩固、不带记忆。"
                f"多半是某个客户端直连 /v1 且没带 <system_reminder> 块"
            ),
        })

    # ② 内心OS
    if len(inner) >= INNER_OS_MAX * 0.8:
        problems.append({
            "level": "warn", "area": "inner_os",
            "msg": f"内心OS 积压 {len(inner)} 条（上限 {INNER_OS_MAX}），可能没被角色层消费",
        })

    # ③ 人格文件
    if not persona.strip():
        problems.append({"level": "error", "area": "persona", "msg": "人格文件为空"})
    elif len(persona) < 200:
        problems.append({
            "level": "warn", "area": "persona",
            "msg": f"人格文件异常短（{len(persona)} 字）",
        })

    # ④ 心跳活性
    last_tick = float(st.get("last_tick_ts") or 0)
    heartbeat_s = float(SYSTEM.get("heartbeat_s", 300))
    if last_tick and now - last_tick > heartbeat_s * 3:
        problems.append({
            "level": "warn", "area": "heartbeat",
            "msg": f"心跳已 {(now - last_tick) / 3600:.1f} 小时没动（预期每 {heartbeat_s / 60:.0f} 分钟）",
        })

    # ⑤ 审计错误
    if audit["errors"] >= 10:
        problems.append({
            "level": "warn", "area": "audit",
            "msg": f"最近 24h 有 {audit['errors']} 次错误：{audit['by_event']}",
        })

    # ⑤-1 软失败（P1-9）：长期记忆没写进去、事件抽取全挂 —— 这些原来一条都不算。
    # 阈值取 3：偶发一次失败（模型抽风）不值得报警，连着失败才是真出问题。
    soft = int(audit.get("soft_fails") or 0)
    if soft >= 3:
        problems.append({
            "level": "warn", "area": "audit",
            "msg": (
                f"最近 24h 有 {soft} 次「该成功但失败了」"
                f"（长期记忆写入 / 事件抽取 / 巩固 / 梳理 / 主动说话投递 / 召回 / 人格写入）："
                f"{ {k: v for k, v in (audit.get('by_event') or {}).items() if k in _SOFT_FAIL_EVENTS} }"
            ),
        })

    # ⑤-2 每日梳理期（P0-4）：**坏了必须看得见**
    #
    # 这一项如果不加，梳理线程静默死掉的表现是「什么都没发生」——
    # 和「主人在忙，本来就不该跑」长得一模一样，永远发现不了。
    # 和长期记忆那次一样：一个永远不报警的健康检查，比没有更危险。
    dr = _daily_review_cfg()
    if dr["enabled"]:
        last_review_ts = float(st.get("last_daily_review_ts") or 0)
        has_turns = db["total"] > 0
        if last_review_ts <= 0:
            # 开了但一次都没跑过 —— 可能刚开，也可能线程挂了。
            if has_turns:
                problems.append({
                    "level": "warn", "area": "daily_review",
                    "msg": "每日梳理期已开启但一次都没跑过（线程可能没起来）",
                })
        else:
            days_since = (now - last_review_ts) / 86400
            # 阈值 3 天：1 天不跑可能是通宵/关机，连 3 天不跑就是真出问题了。
            if days_since > 3:
                last = datetime.fromtimestamp(last_review_ts).strftime("%m-%d %H:%M")
                problems.append({
                    "level": "warn", "area": "daily_review",
                    "msg": (
                        f"每日梳理期已 {days_since:.0f} 天没跑"
                        f"（上次 {last}）——反刍/预演/心智理论全都停摆了"
                    ),
                })

    # ⑤-4 召回熔断 / 人格写入失败（2026-11-04 第二十八轮加）
    #
    # 系统化枚举 25 种 _audit 事件后发现：这几个 ok=false 时
    # **完全没人统计**（既不是 _error 后缀，也不在软失败名单里）。
    #
    # recall_muted 尤其严重：连续 3 次召回失败后熔断
    # 5 分钟，期间**记忆检索整个停摆** —— 她会「记不住你」，
    # 但用户完全看不出来。这不是 warn，是 error。
    by_ev = audit.get("by_event") or {}
    if by_ev.get("recall_muted"):
        problems.append({
            "level": "error", "area": "recall",
            "msg": (
                f"召回熔断了 {by_ev['recall_muted']} 次（连续失败 3 次触发）"
                f"——期间记忆检索完全停摆，她会「记不住你」"
            ),
        })
    elif by_ev.get("recall_timeout", 0) >= 10:
        problems.append({
            "level": "warn", "area": "recall",
            "msg": (
                f"最近 24h 召回超时 {by_ev['recall_timeout']} 次"
                f"——embedding 链路可能有问题"
            ),
        })
    if by_ev.get("persona_update"):
        problems.append({
            "level": "warn", "area": "persona",
            "msg": (
                f"人格文件写入失败 {by_ev['persona_update']} 次"
                f"——演化没存下来，重启会退回旧人格"
            ),
        })

    me_chat = _model_error_stats()

    # ⑤-3 前：/chat 兜底异常（2026-11-04 第二十九轮加）
    #
    # `gateway.py` 里 `/chat` 那条路的 except 会 `_log_error("chat", ...)`：
    # 那是**整条对话链路的兜底** —— 它一响，意味着用户那一轮**完全没回复**。
    # 而现在 errors.log 里从来没有 chat channel（说明还没发生过），
    # 但它真要发生时，**体检完全看不见**（`_model_error_stats` 只看 model）。
    #
    # 这是比 signal 事件更底层的盲区：signal 是「某功能出错」，
    # 这个直接是「她根本不回你」。
    if me_chat.get("chat_errors"):
        problems.append({
            "level": "error", "area": "chat",
            "msg": (
                f"/chat 抛出未捕获异常 {me_chat['chat_errors']} 次"
                f"——那一轮用户完全没收到回复，详情见 errors.log"
            ),
        })

    # ⑤-3 模型调用失败（2026-11-04 第二十六轮加）
    #
    # 为什么必须有：`errors.log` 记的是**真实**的模型调用失败
    # （401 Unauthorized / 503 / 超时），而以前没有任何体检项覆盖它。
    # 实测 11-01 那段时间连续 8 次 401，她每条都只能回
    # 「我这会儿有点卡，等下再说好不好。」—— 而体检全绿。
    # 用户完全不知道是 key 失效了。
    #
    # 401 单独提示：那是 key 无效/过期，要用户去改；
    # 503 是上游抖动，等一会儿就好 —— 处置完全不同。
    me = _model_error_stats()
    if me["unauthorized"] > 0:
        problems.append({
            "level": "error", "area": "model",
            "msg": (
                f"最近 24 小时有 {me['unauthorized']} 次 401 Unauthorized —— "
                f"API key 无效或已过期。她现在每次都会回「我这会儿有点卡」，"
                f"但真正的原因在 gateway\\data\\errors.log 里。"
            ),
        })
    elif me["total"] >= 5:
        problems.append({
            "level": "warn", "area": "model",
            "msg": (
                f"最近 24 小时有 {me['total']} 次模型调用失败"
                f"（5xx {me['server_error']} / 超时 {me['timeout']}）"
            ),
        })

    # ⑥ 长期记忆（自建库，见 `_long_memory_stats`）
    lm = _long_memory_stats()
    # 索引一致性（T-06）：**放在 if lm 外面** —— 库打不开时 `lm` 是空，
    # 但"打不开"恰恰是需要报的问题，不能因为 lm 为空就跳过这项。
    try:
        lm_health = _memory_bridge().health()
    except Exception as exc:  # noqa: BLE001 —— 体检读不到不该炸
        lm_health = {"ok": False, "error": f"{type(exc).__name__}"}
    if lm:
        path_hint = os.path.basename(
            str(SYSTEM.get("long_memory_db") or SYSTEM.get("long_memory_db_path")
                or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                                "livingmemory.db"))
        )
        if lm.get("unreadable"):
            # 库文件在、但 documents 读不出来：这是**真的坏了**，不是「没装」。
            # 以前这种情况被 `return None` 静默吞掉，导致这一项永远不报。
            problems.append({
                "level": "error", "area": "long_memory",
                "msg": (
                    f"长期记忆库存在但读不出来（{path_hint}）——"
                    f"documents 表缺失或损坏，跨天记忆会全部丢失"
                ),
            })
        elif lm.get("write_ops_missing"):
            # 自建库没有写入流水表是**正常**的（写入同步，失败直接抛调用方）。
            # 只在「本该有这张表却没找到」时提一句，避免哪天换了库还以为在监控。
            problems.append({
                "level": "warn", "area": "long_memory",
                "msg": (
                    "长期记忆库没有 memory_write_ops 表，写入失败计数按 0 处理"
                    "（自建库本来就无需此表；若换回插件库请确认）"
                ),
            })
        if lm["failed_24h"]:
            problems.append({
                "level": "warn", "area": "long_memory",
                "msg": (
                    f"长期记忆最近 24h 有 {lm['failed_24h']} 次写入失败："
                    f"{lm['last_error'][:80]}"
                ),
            })
        if lm["last_doc_ts"]:
            stale_days = (now - lm["last_doc_ts"]) / 86400
            # 只在「最近还在聊天、却很久没有新记忆」时报警：
            # 人不在的时候不产生记忆是正常的，天天报等于没有报警。
            if stale_days > 2 and db["last_24h"] > 0:
                last = datetime.fromtimestamp(lm["last_doc_ts"]).strftime("%m-%d %H:%M")
                problems.append({
                    "level": "warn", "area": "long_memory",
                    "msg": (
                        f"最近 24h 有 {db['last_24h']} 条对话，但长期记忆已 "
                        f"{stale_days:.0f} 天没有新增（最新一条 {last}）"
                    ),
                })
        # ⑥-2 空壳检测：写得进去 ≠ 写得有内容。
        # 2026-11-03 实测 13 条 documents 的 text 全是 "对话记录"，
        # 而它们每条都有 completed 记录 —— 只查条数这一项永远是绿的。
        checked = int(lm.get("recent_checked") or 0)
        hollow = int(lm.get("hollow_recent") or 0)
        if checked and hollow:
            problems.append({
                "level": "warn", "area": "long_memory",
                "msg": (
                    f"长期记忆最近 {checked} 条里有 {hollow} 条是空壳"
                    f"（正文过短或 key_facts 为空）——摘要没真的抽出内容"
                ),
            })

        # ⑥-3 索引一致性（2026-11-04 T-06 加）。
        # `documents` 与 faiss 无法共用一个事务，两边劈开时**不会报错**，
        # 召回只是悄悄变差。实测教训：documents 7 行、index 56 个向量，
        # 49 条孤儿把 top-k 名额占满，语义召回命中真记忆 0 条。
        if lm_health.get("ok") is False and lm_health.get("orphan_vectors"):
            problems.append({
                "level": "warn", "area": "long_memory",
                "msg": (
                    f"长期记忆索引不一致：documents {lm_health.get('docs')} 行，"
                    f"faiss {lm_health.get('vectors')} 个向量，"
                    f"多出 {lm_health.get('orphan_vectors')} 条孤儿"
                    f"（会让召回名额被白占）"
                ),
            })
        elif lm_health.get("ok") is False and not lm_health.get("error"):
            problems.append({
                "level": "warn", "area": "long_memory",
                "msg": (
                    f"长期记忆缺向量：{lm_health.get('missing_vectors')} 条文档没有对应向量"
                ),
            })

    # ── 节律状态（2026-11-04 第三十二轮补，计划 7.3 最后一项）──
    #
    # 计划原话：「这一行最有用：一眼看出她是在睡觉，还是攒了料没处理」。
    # 以前只能去后台卡片（daily_review_payload）看 blocked_by，
    # 体检报告里完全没有 —— 而体检才是每天该看的那一份。
    _should, _why = _daily_review_should_run()
    _review_state = "ready" if _should else _why
    try:
        _novel_chars = _pending_novel_chars().get("novel_chars")
    except Exception:  # noqa: BLE001 —— 算不出不影响其它体检项
        _novel_chars = None

    # ── 计划 / 心情 / 遗忘 的状态（2026-11-06 能力 5 / 4 / 2）──
    #
    # 为什么进体检：这三项都是**纯代码的、会静默失效的**机制。
    # 计划不触发 = 什么都不发生；心情不更新 = 还是一个数字；
    # 遗忘配置成永不删 = 库只增不减。三种都长得像"正常"，没有一行状态
    # 就只能等主人自己发现。
    _plan_stats = _plan_health_stats()
    try:
        _mood_show = mood_engine.decayed_state(st)
    except Exception:  # noqa: BLE001 —— 心情读不到不影响其它体检项
        _mood_show = {"mood_value": None, "mood_label": "", "mood_reason": ""}
    try:
        import gateway

        with sqlite3.connect(gateway.DB_PATH) as conn:
            _syslog_total = int(
                conn.execute(
                    "select count(*) from turns where channel=?",
                    (migrate_channel.CHANNEL_SYSTEM_LOG,),
                ).fetchone()[0]
                or 0
            )
    except Exception:  # noqa: BLE001
        _syslog_total = None
    if _plan_stats["overdue"]:
        problems.append({
            "level": "warn", "area": "plan",
            "msg": (
                f"有 {_plan_stats['overdue']} 条计划已经临期但还没触发过"
                f"（心跳那条线可能没在跑，或者群渠道被跳过了）"
            ),
        })
    if _plan_stats["pending_close"]:
        problems.append({
            "level": "warn", "area": "plan",
            "msg": (
                f"有 {_plan_stats['pending_close']} 条计划已触发但一直判不出成败"
                f"（主人回答了但没命中兑现/落空词，A1 的判定标准还没定）"
            ),
        })

    report = {
        "ts": now,
        "ok": not any(p["level"] == "error" for p in problems),
        "problems": problems,
        "stats": {
            # 模型调用失败（2026-11-04 第二十六轮）：以前完全没有覆盖。
            # 实测 11-01 连续 8 次 401，她只能回「我这会儿有点卡」，而体检全绿。
            "model_errors_24h": _model_error_stats()["total"],
            "model_errors_401_24h": _model_error_stats()["unauthorized"],
            "turns": db["total"],
            "turns_24h": db["last_24h"],
            "sources": db["sources"],
            # T-05：认不出来源的用户消息（24h）
            "turns_user_24h": recent_user,
            "turns_unknown_24h": unknown_24h,
            "inner_os": len(inner),
            "persona_len": len(persona),
            "memory": len(st.get("memory") or []),
            "speak_count_today": st.get("speak_count_today"),
            "audit_errors_24h": audit["errors"],
            "audit_soft_fails_24h": audit.get("soft_fails", 0),
            "long_memory_docs": (lm or {}).get("documents"),
            "long_memory_failed_24h": (lm or {}).get("failed_24h"),
            "long_memory_hollow_recent": (lm or {}).get("hollow_recent"),
            # T-06：索引一致性（`ok` 为假时会同时进 problems）
            "long_memory_vectors": lm_health.get("vectors"),
            "long_memory_orphan_vectors": lm_health.get("orphan_vectors"),
            "long_memory_index_ok": lm_health.get("ok"),
            "daily_review_enabled": dr["enabled"],
            "daily_review_last_ts": st.get("last_daily_review_ts"),
            "daily_review_last_day": st.get("last_daily_review_day"),
            # 节律状态：sleeping / below_threshold / circadian_due / declined /
            #           cap_reached / too_early / not_idle / already_ok / ready
            "daily_review_state": _review_state,
            "daily_review_novel_chars": _novel_chars,
            "daily_review_probe_day": st.get("last_daily_review_probe_day") or "",
            # 能力 5 计划（2026-11-06）
            "plan_open": _plan_stats["open"],
            "plan_overdue": _plan_stats["overdue"],
            "plan_triggered": _plan_stats["triggered"],
            "plan_pending_close": _plan_stats["pending_close"],
            "plan_closed_24h": _plan_stats["closed_24h"],
            # 能力 4 心情（2026-11-06）：读的是**衰减后**的展示值，不落盘
            "mood_value": _mood_show["mood_value"],
            "mood_label": _mood_show["mood_label"],
            "mood_reason": _mood_show["mood_reason"],
            # 系统流水账（能力 5 第四节）
            "system_log_total": _syslog_total,
        },
    }

    # 体检只做客观检查、只落报告，不自动修复、不再二次调模型
    _save_health_report(report)
    _audit({
        "event": "health_check", "ok": report["ok"],
        "problems": len(problems), "areas": [p["area"] for p in problems],
    })
    # 系统流水账：**只有真的查出问题**才记（2026-11-06 补，能力 5 第四节）。
    #
    # 没问题的跳过：一次全绿的体检没有"值得未来参考的结果"，记了只会让
    # 流水账变成心跳一样的噪音，巩固时白占模型注意力。
    #
    # 为什么值得记：体检是**按需触发**的（系统层模型觉得可疑才调），
    # `problems` 一旦非空，说明"系统在那一天确实出过事"。留着它，
    # 下次巩固时模型能拿它和 `existing_insights` 一起判断，
    # 而不是每次重新猜"之前到底有没有坏过"。
    if problems:
        append_system_log(f"[系统] 体检：发现 {len(problems)} 个问题")
    return report


def _save_health_report(report: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HEALTH_REPORT_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, HEALTH_REPORT_PATH)
    except OSError:
        pass


def _read_health_report() -> dict:
    try:
        with open(HEALTH_REPORT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def proactive_speak_payload() -> dict:
    """给后台「主动说话」卡片用：总开关状态 + 当前刹车状态。

    Returns:
        `{"enabled", "can_speak_now", "blocked_by", "speak_count_today",
          "max_per_day", "cooldown_s", "last_speak_ts", "last_ago_hours",
          "desktop_reachable"}`。

    Note:
        `blocked_by` 是**给排障用的**：主人打开开关后发现她不说话，
        第一反应会以为是坏了。这里直接告诉他卡在哪一道闸：
        `disabled`（总开关关着）/ `quota`（今天额度用完）/ `cooldown`（冷却中）。
    """
    st = load_state()
    cfg = SYSTEM.get("proactive_speak") or {}
    enabled = bool(cfg.get("enabled", False))

    now = time.time()
    last = float(st.get("last_speak_ts") or 0)
    count = int(st.get("speak_count_today") or 0)
    max_per_day = int(SYSTEM.get("max_speak_per_day", 6))
    cooldown = float(SYSTEM.get("cooldown_s", 1800))

    # 跨天清零（和 `_allow_speak` 同一口径，但这里不改 st —— 只读接口不该写盘）
    today = time.strftime("%Y-%m-%d")
    if count and last and time.strftime("%Y-%m-%d", time.localtime(last)) != today:
        count = 0

    if not enabled:
        blocked_by = "disabled"
    elif count >= max_per_day:
        blocked_by = "quota"
    elif last and now - last < cooldown:
        blocked_by = "cooldown"
    else:
        blocked_by = ""

    # 桌宠在不在：不在的话她说了也没人听得见，这个信息对排障很关键
    desktop_reachable = False
    try:
        import urllib.request

        base = str(SYSTEM.get("desktop_base", "http://127.0.0.1:8848")).rstrip("/")
        with urllib.request.urlopen(base + "/api/status", timeout=2) as resp:
            desktop_reachable = resp.status == 200
    except Exception:  # noqa: BLE001 —— 探活失败就是不可达，不抛
        desktop_reachable = False

    return {
        "enabled": enabled,
        "can_speak_now": not blocked_by,
        "blocked_by": blocked_by,
        "speak_count_today": count,
        "max_per_day": max_per_day,
        "cooldown_s": cooldown,
        "last_speak_ts": last,
        "last_ago_hours": round((now - last) / 3600, 2) if last else None,
        "desktop_reachable": desktop_reachable,
    }


def apply_proactive_speak(payload: dict) -> tuple[bool, str]:
    """打开/关闭主动说话总开关，并回写 `config.json`。

    Args:
        payload: `{"enabled": bool}`。

    Returns:
        `(成功, 一句话)`。热生效 —— 不用重启，下一次心跳就按新值走。

    Note:
        关掉时**只是不再主动开口**，已经说出去的话不受影响。
        这条链路原本就是旁路（心跳触发），关它不影响被动对话。
    """
    if not isinstance(payload, dict):
        return False, "参数不对"
    if "enabled" not in payload:
        return False, "missing enabled"

    enabled = bool(payload.get("enabled"))
    cfg = SYSTEM.setdefault("proactive_speak", {})
    cfg["enabled"] = enabled

    # 回写 config.json（保留注释键和其余字段）
    try:
        cfg_path = os.path.join(ROOT, "config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            disk = json.load(fh)
        sect = disk.setdefault("system", {})
        sect["proactive_speak"] = {"enabled": enabled}
        tmp = cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(disk, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)
    except (OSError, json.JSONDecodeError) as exc:
        # 内存里已经生效了，回写失败只影响"重启后还在不在"
        return True, f"已{'开启' if enabled else '关闭'}（配置回写失败：{exc}）"

    _audit({"event": "proactive_speak_toggle", "ok": True, "enabled": enabled})
    return True, f"已{'开启' if enabled else '关闭'}主动说话"


def _warmup_embedding() -> None:
    """启动时预热 embedding 模型，把首次加载的 10 秒挪到后台。

    为什么需要（2026-11-07）
    ----------------------
    本地 bge-large-zh-v1.5 是**懒加载**的：第一次 `encode()` 才去读 1.24GB 权重。
    实测冷启动 10.06s，而 `RECALL_TIMEOUT_S` 当时是 10s —— 正好卡边界，
    导致**进程起来后的第一轮对话必然召回失败**。

    两手一起上：
      * `RECALL_TIMEOUT_S` 提到 60s（兜底，保证不因慢而失败）
      * 这个预热线程（治本，把等待挪出热路径）

    预热方式：真跑一次最小向量化。只 import 不算预热 ——
    真正的耗时在 `SentenceTransformer.__init__` 读权重那一步。

    失败不抛、不重试：模型没下载/显存不够时，后续每次召回自己会再试，
    行为与没有本线程时一致。
    """
    try:
        bridge = _memory_bridge()
        if bridge is None:
            return
        store = bridge.store()
        if store is None:
            return
        emb = getattr(store, "_embedding", None)
        if emb is None:
            return
        # 只对本地 provider 预热：DashScope 是网络调用，预热等于多花钱
        if not hasattr(emb, "embed_query"):
            return
        started = time.time()
        import asyncio  # noqa: PLC0415

        asyncio.run(emb.embed_query("预热"))
        print(f"  embedding 预热完成（{time.time() - started:.1f}s）", flush=True)
    except Exception as exc:  # noqa: BLE001 —— 预热失败不影响启动
        print(f"  [warn] embedding 预热失败（不影响启动）: {type(exc).__name__}: {exc}",
              flush=True)


def start() -> list[threading.Thread]:
    """挂 daemon 线程：心跳 + 记忆巩固 + 人格把关。体检不占线程，由模型按需调用。"""
    if not SYSTEM.get("enabled"):
        return []
    # 人物画像的文件名规则改过（P1-10），启动时把旧命名的文件迁到新命名。
    # 幂等：新文件已存在就不动。放在最前面，免得第一轮巩固读到"空的"画像
    # 然后把它当成新人重新建模。
    try:
        moved = migrate_people_files()
        if moved:
            print(f"  人物画像迁移: {moved} 个文件改到新命名", flush=True)
    except Exception as exc:  # noqa: BLE001 —— 迁移失败不该拦住启动
        print(f"  [warn] 人物画像迁移失败: {type(exc).__name__}: {exc}", flush=True)
    threads = [
        threading.Thread(target=_heartbeat_loop, name="system-heartbeat", daemon=True),
        threading.Thread(target=_persona_apply_worker, name="system-persona-apply", daemon=True),
        # 预热 embedding 模型（2026-11-07 加）。
        #
        # 本地 bge 第一次调用要加载 1.24GB 模型，实测 **10 秒**。不预热的话
        # 这个代价由**进程起来后的第一轮对话**承担 —— 主人刚打开桌宠说第一句话
        # 就要多等 10 秒，而那时候她还在等召回结果。
        #
        # 放在 daemon 线程里：即使预热失败（模型没下载、显存不够）也不影响启动，
        # 只是第一轮会慢一点，和没有这个线程时一样。
        threading.Thread(target=_warmup_embedding, name="system-warmup-embed", daemon=True),
    ]
    if float(SYSTEM.get("consolidate_s", 3600)) > 0:
        threads.append(
            threading.Thread(target=_consolidate_loop, name="system-consolidate", daemon=True)
        )
    # 每日梳理期（P0-4）：第二层节律。**默认关**，配置里 enabled=true 才起。
    # 关着的时候连线程都不起 —— 不给"配了没开"留半开状态。
    if _daily_review_cfg()["enabled"]:
        threads.append(
            threading.Thread(target=_daily_review_loop, name="system-daily-review", daemon=True)
        )
    for t in threads:
        t.start()
    return threads


# ---------------------------------------------------------------- 审计与调试
def _audit(record: dict) -> None:
    record = dict(record)
    record.setdefault("ts", time.time())
    record.setdefault("layer", "system")
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(SYSTEM_AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def daily_review_payload() -> dict:
    """给后台「每日梳理期」卡片用：配置 + 最近状态 + 可手动触发。

    和 `system_model_payload()` 同风格：只回可展示字段，不回 key。
    """
    st = load_state()
    cfg = _daily_review_cfg()
    last_ts = float(st.get("last_daily_review_ts") or 0)
    should, reason = _daily_review_should_run()
    materials = _daily_review_materials()
    return {
        "enabled": cfg["enabled"],
        "day_start_hour": cfg["day_start_hour"],
        "earliest_hour": cfg["earliest_hour"],
        "idle_after_minutes": cfg["idle_after_minutes"],
        "min_absence_hours": cfg["min_absence_hours"],
        "absence_is_material": cfg["absence_is_material"],
        "ruminate_requires_today_turns": cfg["ruminate_requires_today_turns"],
        "memory_sink_enabled": cfg["memory_sink_enabled"],
        "daily_model_calls_cap": cfg["daily_model_calls_cap"],
        "budget": cfg["budget"],
        "once_per_day": cfg["once_per_day"],
        "last_day": st.get("last_daily_review_day") or "",
        "last_ts": last_ts or None,
        "last_ago_hours": round((time.time() - last_ts) / 3600, 1) if last_ts else None,
        # 现在这一轮会不会跑，以及为什么 —— 这是排查"梳理期不动"的关键
        "should_run_now": should,
        "blocked_by": "" if should else reason,
        "eligible_activities": materials["eligible"],
        "today_turns": materials["today_turns"],
        "absence_text": materials["absence"]["absent_text"],
        "longest_gap_min": materials["timeline"]["longest_gap_min"],
        "activities": list(REVIEW_ACTIVITIES),
        # 近几次审计，让人能看出"最近在想什么"
        "recent": [
            {
                "ts": r.get("ts"),
                "ok": r.get("ok"),
                "reason": r.get("reason"),
                "activities": r.get("activities"),
                "thoughts": r.get("thoughts"),
                "profile_hints": r.get("profile_hints"),
                "memory_written": r.get("memory_written"),
            }
            for r in _recent_daily_review_audits(5)
        ],
        # ── M6 验收用的两块（2026-11-04 加）──
        #
        # 为什么放进这张卡片：M6 的判据是「她**说出来的话**有没有时间感/接续/数得清」，
        # 而那三样在审计里根本看不出来（审计只有 `thoughts: 12` 这种计数）。
        # 以前要判 M6 只能人肉去翻 system_audit.jsonl 里的 `speak` 事件，
        # 现在一眼能看到"最近她主动说了什么"和"当前素材够不够说那三句话"。
        "recent_speak": _recent_speak_records(10),
        "scenario_readiness": _scenario_readiness(materials),
    }


def _recent_speak_records(limit: int = 10) -> list[dict]:
    """最近她**主动说出来**的话（`speak` 事件）。M6 验收的直接证据。

    只回投递成功的（`ok=true`）：`speak_fail` 那些没送到，不构成"她说过"。
    文本截断到 120 字，够判"有没有时间感/接没接上昨天/说没说天数"。
    """
    out: list[dict] = []
    try:
        with open(SYSTEM_AUDIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "speak" or rec.get("ok") is not True:
                    continue
                out.append({
                    "ts": rec.get("ts"),
                    "text": str(rec.get("text") or "")[:120],
                    "emotion": rec.get("emotion"),
                    "subscribers": rec.get("subscribers"),
                })
    except OSError:
        return []
    return out[-limit:][::-1]


def _long_memory_doc_count_fast() -> int:
    """长期记忆库里有多少条 documents。**纯 SQL，不碰 faiss**。

    为什么单独一个（2026-11-04 第六轮）：
    `MemoryBridge.health()` 会 `consistency()` —— 要打开 faiss 索引、遍历全部
    id 跟 documents 对账，实测 **515ms**。而"后台卡片轮询"或"判断 B 素材够不够"
    只需要一个条数，用不着完整性检查。

    完整的一致性检查仍然走 `health()`（体检里那一项），只是别放在热路径上。
    """
    import gateway

    path = SYSTEM.get("long_memory_db_path") or os.path.join(
        DATA_DIR, "livingmemory.db"
    )
    if not os.path.exists(path):
        return 0
    try:
        uri = "file:" + str(path).replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = con.execute("select count(*) from documents").fetchone()
            return int(row[0] if row else 0)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return 0


def _scenario_readiness(materials: dict) -> dict:
    """三段对话（A/B/C）的**素材够不够**——只报事实，不判断她说得好不好。

    这是 M6 验收的"前半张表"：先看素材在不在，再看她说了什么。
    两半都对了才算那段对话成立。

    判据来自计划第一节 1.2 / 1.5：
      * A「中间都没歇」→ 需要 `longest_gap_min` 够大（当天真有空档）
      * B「我不问了，但我在」→ 需要长期记忆里有分寸类沉淀 + 有画像
      * C「数着日子」→ 需要 `absence_text` 能给出**具体时长**
    """
    tl = (materials or {}).get("timeline") or {}
    ab = (materials or {}).get("absence") or {}
    gap = int(tl.get("longest_gap_min") or 0)
    absent_hours = ab.get("absent_hours")
    readiness = {
        # A：空档 ≥ 2 小时才算"中间真的歇了很久"，否则没素材说那句话
        "A_time_sense": {
            "ready": gap >= 120,
            "longest_gap_min": gap,
            "gap_window": f"{tl.get('gap_start_hm') or '?'} → {tl.get('gap_end_hm') or '?'}",
            "note": "空档不够大时她最多说「你今天在忙吧」，判 A 时别算失败",
        },
        # C：缺席能算出具体时长（不是"好久不见"）
        "C_counting_days": {
            "ready": bool(ab.get("absent_text")) and absent_hours is not None,
            "absent_text": ab.get("absent_text") or "",
            "absent_hours": absent_hours,
            "note": "absent_text 里必须带数字（小时/天），空串说明库里算不出",
        },
    }
    # B：能不能"接着昨天说" —— 看有没有长期记忆和画像（跨天载体）
    #
    # ⚠ 2026-11-04（第六轮）性能修正：这里原来调 `_memory_bridge().health()`，
    # 实测**单次 515ms**（要打开 faiss 索引并遍历全部 id）。
    # 而后台「每日梳理期」卡片是**轮询**的，每刷一次就卡半秒 ——
    # 一个"看一眼状态"的操作不该有这种代价。
    #
    # B 路只需要"有多少条记忆"，纯 SQL 就够（2ms）。完整性检查留给体检。
    try:
        doc_count = _long_memory_doc_count_fast()
    except Exception:  # noqa: BLE001
        doc_count = 0
    try:
        profile = (_read_people_profile(migrate_channel.CHANNEL_PET) or {}).get("profile") or {}
    except Exception:  # noqa: BLE001
        profile = {}
    readiness["B_carry_over"] = {
        "ready": doc_count > 0 and bool(profile),
        "long_memory_docs": doc_count,
        "owner_profile_fields": sorted(profile.keys()),
        "note": "跨天靠这两样载体；任一为空，第二天早上她只能重新开始",
    }
    return readiness


def _recent_daily_review_audits(limit: int = 5) -> list[dict]:
    """从 `system_audit.jsonl` 尾部捞最近几条梳理期事件（倒序读，别全量载入）。"""
    out: list[dict] = []
    try:
        with open(SYSTEM_AUDIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "daily_review":
                    out.append(rec)
    except OSError:
        return []
    return out[-limit:][::-1]


def system_model_payload() -> dict:
    """给后台「系统模型」卡片用：当前配置 + 可选服务商（key 只回掩码）。

    和 `_role_payload()` 形状对齐，前端两张卡片可以用同一套渲染逻辑。
    """
    key = _system_key()
    return {
        "provider": SYSTEM.get("provider", ""),
        "base": SYSTEM.get("base", ""),
        "model": SYSTEM.get("model", ""),
        "key_set": bool(key),
        "key_masked": (key[:6] + "…" + key[-4:]) if len(key) > 12 else "",
        "providers": {name: item["label"] for name, item in _PROVIDERS.items()},
        "presets": {name: item["base"] for name, item in _PROVIDERS.items()},
        "temperature": SYSTEM.get("temperature", 0.2),
        "max_tokens": SYSTEM.get("max_tokens", 300),
        "timeout_s": SYSTEM.get("timeout_s", 30),
    }


def _probe_system(candidate: dict) -> tuple[bool, str, int]:
    """用一次极小的请求验证系统模型能不能连上，返回 (成功, 错误, 毫秒)。

    为什么要单独试连：系统层模型坏了的表现是「心跳不动 / 巩固空转」，
    而那从对话里完全看不出来（主人只会觉得她最近没主动说话）。
    保存前先连一次，连不上就不动当前配置。
    """
    base = str(candidate.get("base", "")).rstrip("/")
    model = str(candidate.get("model", "")).strip()
    if not base or not model:
        return False, "地址或模型名是空的", 0
    obj = {
        "model": model,
        "messages": [{"role": "user", "content": "说两个字：在的"}],
        "max_tokens": 16,
    }
    if "dashscope" in base:
        obj["enable_thinking"] = False
    headers = {"Content-Type": "application/json"}
    key = str(candidate.get("key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        return False, f"HTTP {exc.code} {detail}", int((time.time() - started) * 1000)
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}", int((time.time() - started) * 1000)
    except json.JSONDecodeError:
        return False, "返回的不是 JSON", int((time.time() - started) * 1000)
    if not payload.get("choices"):
        return False, "没有 choices", int((time.time() - started) * 1000)
    return True, "", int((time.time() - started) * 1000)


def apply_system_model(payload: dict) -> tuple[bool, str]:
    """校验 → 试连 → 落盘 → 热切换。失败就不动当前配置。

    Args:
        payload: `{provider, base, model, key, temperature?, max_tokens?}`。
            `key` 留空表示「不改现有 key」（和角色层那张卡的约定一致）。

    Returns:
        (成功, 一句话说明)。
    """
    global SYSTEM
    provider = str(payload.get("provider") or SYSTEM.get("provider") or "custom").strip()
    base = str(payload.get("base") or "").strip()
    model = str(payload.get("model") or "").strip()
    key = str(payload.get("key") or "").strip()

    if not base or not model:
        return False, "要填服务地址和模型名"

    candidate = {
        "provider": provider,
        "base": base,
        "model": model,
        # key 留空时沿用**现在实际在用的那把**。
        # 不能只看 `SYSTEM["key"]`：它默认是空的，`_system_key()` 会回落到
        # secrets.json / 环境变量。只读 SYSTEM 的话，用户不改 key 重存一次
        # 就会把配置写成"无 key"，然后试连 401（实测踩过）。
        "key": key or _system_key(),
        "timeout_s": SYSTEM.get("timeout_s", 30),
        "temperature": SYSTEM.get("temperature", 0.2),
        "max_tokens": SYSTEM.get("max_tokens", 300),
    }
    ok, err, ms = _probe_system(candidate)
    if not ok:
        return False, f"没连上：{err}"

    # 只换身份类字段，心跳节律那些留在 config.json 不动
    SYSTEM.update(candidate)
    try:
        _save_system_cfg(SYSTEM)
    except OSError as exc:
        return False, f"配置已生效但没存下来：{exc}"
    return True, f"系统模型已切到 {model}，试连 {ms} ms"


#: 后台「系统模型」卡片可选的服务商。与角色层那张卡共用同一份清单，
#: 但**独立配置**——两层用不同服务商/不同 key 是允许的（401 时能定位是哪层死）。
_PROVIDERS = {
    "dashscope": {
        "label": "阿里云百炼",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1"},
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1"},
    "moonshot": {"label": "Moonshot", "base": "https://api.moonshot.cn/v1"},
    "siliconflow": {"label": "硅基流动", "base": "https://api.siliconflow.cn/v1"},
    "custom": {"label": "自定义（OpenAI 兼容）", "base": ""},
}


def system_status() -> dict:
    """给 GET /admin/system 的调试摘要（后台「她现在的状态」卡在用）。

    🔧 2026-11-07（审计 G.1 第 4 条）：这里补了 `mood` 一项。
    原来卡片只读旧的 `emotion` 枚举（8 个英文词），**新心情一个字段都没有** ——
    于是后台「心情」那一格显示的还是"上次主动说话时的表情"，而不是
    「现在（衰减后）到底是什么心情」。这就是 `mood_state()` 当初零调用的原因：
    它要给卡片用的那个位置根本没人接。

    现在 `mood` 里给的是**衰减后的四字段**（`mood_state(st)`，不落盘、不改时间戳），
    卡片想显示 `mood_label` + `mood_reason` 直接取就行，不用自己算衰减。

    ⚠ 旧 `emotion` 字段**保留不动**：`desktop/web/console.js` 的 `DMN_EMOTION`
    映射还读它（那张表是英文枚举 → 中文）。一起改等于同时要求前端同步发版，
    而这一步不是审计要求的；先让新字段可用，前端切换是独立的一步。
    """
    st = load_state()
    inner = _read_inner_os()
    return {
        "enabled": bool(SYSTEM.get("enabled")),
        "provider": SYSTEM.get("provider", ""),
        "base": SYSTEM.get("base", ""),
        "model": SYSTEM.get("model", ""),
        "key_set": bool(_system_key()),
        "heartbeat_s": SYSTEM.get("heartbeat_s", 300),
        "cooldown_s": SYSTEM.get("cooldown_s", 1800),
        "max_speak_per_day": SYSTEM.get("max_speak_per_day", 6),
        "consolidate_s": SYSTEM.get("consolidate_s", 3600),
        "consolidate_boot_delay_s": SYSTEM.get("consolidate_boot_delay_s", CONSOLIDATE_BOOT_DELAY_S),
        "emotion": st.get("emotion"),
        # 新心情（衰减后、不落盘）。传 st 进去省一次文件读。
        "mood": mood_state(st),
        "focus": st.get("focus"),
        "last_tick_ts": st.get("last_tick_ts"),
        "last_speak_ts": st.get("last_speak_ts"),
        "speak_count_today": st.get("speak_count_today"),
        "recent_thoughts": st.get("recent_thoughts", [])[-3:],
        "inner_os": inner,
        "inner_os_count": len(inner),
        "last_consolidate_id": st.get("last_consolidate_id"),
        "consolidate_wm": st.get("consolidate_wm") or {},
        "last_consolidate_ts": st.get("last_consolidate_ts"),
        "daily_review": {
            "enabled": _daily_review_cfg()["enabled"],
            "last_day": st.get("last_daily_review_day"),
            "last_ts": st.get("last_daily_review_ts"),
        },
        "health": _read_health_report(),
    }


# ---------------------------------------------------------------- 模型调用
def call_system(messages: list[dict], *, tool_choice="auto",
                tools: list | None = None, max_tokens: int | None = None):
    """系统层（系统层）模型调用。返回 (content, meta, tool_calls)，形状对齐 call_model。

    不复用 call_model_raw：后者复用 ROLE、不带 tools，语义是插件内部请求。
    tools / max_tokens 可选：巩固任务用独立工具清单和更大的输出上限。
    """
    base = str(SYSTEM.get("base", "")).rstrip("/")
    body_obj = {
        "model": SYSTEM.get("model", "qwen-flash"),
        "messages": messages,
        "temperature": float(SYSTEM.get("temperature", 0.2)),
        "max_tokens": int(max_tokens or SYSTEM.get("max_tokens", 300)),
        "tools": tools or [],
        "tool_choice": tool_choice,
    }
    # 百炼上的 qwen 是推理模型，不关思考会把 token 全花在 reasoning 上
    if "dashscope" in base:
        body_obj["enable_thinking"] = False
    headers = {"Content-Type": "application/json"}
    key = _system_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=float(SYSTEM.get("timeout_s", 30))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return None, {
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }, None
    usage = payload.get("usage", {})
    meta = {
        "elapsed_ms": int((time.time() - started) * 1000),
        "completion_tokens": usage.get("completion_tokens", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
    }
    choices = payload.get("choices") or []
    if not choices:
        # 模型没给念头（不是 KeyError）：语义上就是「这次没念头」，交给上层当 no_intent。
        return None, meta, None
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip() or None, meta, message.get("tool_calls")
