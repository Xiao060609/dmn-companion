# 03 · gateway 系统层 + 记忆层审计

> 审计对象：`gateway/system.py`（6563 行）+ 四个纯函数引擎 + 记忆层 + 记忆桥
> 审计方式：只读读码 + 只读 sqlite（`file:...?mode=ro`）+ 本地跑单测（不联网、不起服务、不写库）
> 行号基准：Python 按字节读 `\n` 计数（不是 PowerShell `Get-Content`）
> 标注约定：**【读码】**=直接读源码确认；**【实测】**=本次只读查询/跑测试确认；**【文档】**=仅见于项目文档，未在代码中证实；**【推断】**=逻辑外推，未实测

---

## 一句话结论

系统层的**机制骨架已经铺满**（三层节律线程、分渠道巩固、三档遗忘、认知准入、混合检索全部有代码且有单测），但它是一个**"写得多、跑得少"**的系统：2026-11-03 22:51 之后 `turns` 再无新对话，`system_audit.jsonl` 里 482 条心跳有 414 条是空转、223 条巩固调用有 211 条连模型都没调；而真正**代码级断头路**集中在三处 —— 认知的四种写入动作（add/replace/merge/conflict）整条流水线**生产零调用**、遗忘的"永不删类型拉高稳定性"**算出来却不落盘**、"问模型"那一档的遗忘门槛与日常清理**用了两套参数**。这三点都不是"没写"，是"写了没接上"，正是这类项目最典型的形态。

---

## 一、板块树（系统层由几个可独立描述的子系统构成）

按"能独立描述、独立失效、独立验证"切，系统层是 **5 个子系统 + 2 个横切设施**：

```
gateway 系统层（system.py 6563 行）
│
├── A. 节律层（三个后台线程，唯一有"时间"的部分）
│   ├── A1 心跳      _heartbeat_loop  :1572   周期 300s×[0.8,1.2] 抖动
│   │    └ 变化检测  _detect_change   :786   纯代码，零模型调用
│   │    └ 主动说话  → role_speak.maybe_speak（角色层，系统层只踩刹车）
│   ├── A2 巩固      _consolidate_loop :4279  启动 90s 后首轮，之后 3600s×[0.9,1.1]
│   │    └ 逐渠道    _consolidate_channel :3803
│   │    └ 人格复盘  _review_persona  :4914   挂在 consolidate() :4260 后面
│   ├── A3 每日梳理  _daily_review_loop :4815 启动 90s 后首轮，之后 600s 轮询
│   │    └ 双判断    _daily_review_should_run :1255 → 信息量 / 昼夜节律两条腿
│   └── A4 人格把关  _persona_apply_worker :5067  常驻阻塞队列消费者
│
├── B. DMN 九项能力（按能力分，九个里有六个落在 system.py）
│   ├── 1 记忆巩固   _consolidate_channel :3803 + _extract_events :2159
│   ├── 2 遗忘       memory_bridge.forget_plan / _maybe_prune_long_memory :4060
│   │                + 中间档 _maybe_forget_review :4116 / apply_forget_review :4178
│   ├── 3 人物建模   update_people_profile :3204 + _apply_profile_hint :3258
│   │                + people_prompt_line :3139（读侧）
│   ├── 4 内心OS     push_inner_os :5153 / consume_inner_os :5187
│   │                + 心情 mood_state :216（两个子区域，见 1.6 口径）
│   ├── 5 计划       _fire_due_plans :2511（触发）
│   │                + _pending_plan_candidates :2637 / apply_plan_verdicts :2727（收尾）
│   ├── 6 自我建模   _review_persona :4914 + _apply_one_suggestion :5028
│   ├── 7 节律       = A 那一整棵（别的能力都在用它）
│   ├── 8 记忆库     gateway/memory/ + memory_bridge.py
│   └── 9 优化       **空占位**，全仓无实现（config 里也没有对应段）
│
├── C. 记忆层（gateway/memory/ 11 个文件 2611 行，自持长期记忆）
│   ├── C1 存储       document_storage.py(815) documents 表 + documents_fts(FTS5)
│   ├── C2 向量       vec_db.py(364) + embedding_storage.py(210) FaissVecDB
│   ├── C3 向量化     dashscope_embedding.py(225) text-embedding-v4 / 1024 维
│   ├── C4 同步门面   store.py(485) MemoryStore + loop_bridge.py(136) 常驻事件循环
│   └── C5 分词       tokenizer.py(39) jieba + FTS5 查询构造
│
├── D. 四个纯函数引擎（2026-11-06 新增，全部无 IO）
│   ├── mood.py(424)       心情：连续值 + 档位 + 线性衰减
│   ├── plan.py(455)       计划：10 天沉默期 + 裁决归一 + 措辞拼装
│   ├── forgetting.py(1050) 遗忘：R=e^(-t/S) + 三档 + 永不删
│   └── cognition.py(1175)  认知：两标准判定 + 四动作解析 + keywords 清洗
│
└── E. 横切设施
    ├── E1 状态读写   load_state :483 / save_state :565 / _read_modify_write :599
    │                 + _STATE_LOCK :54（整份覆盖写 + 锁，四写者）
    ├── E2 审计       _audit :6132 → system_audit.jsonl（只追加 JSONL）
    └── E3 渠道隔离   _inner_channel :1927 / _item_visible :1940 / NON_CONVERSATION_CHANNELS :118
```

**板块边界的三条硬证据（读码）**：

- 系统层**绝不产生台词**：`system.py` 全文无 `deliver_to_pet`、无 `/api/say`、无 `parse_reply`；唯一出口是 `push_inner_os`（写队列）和 `role_speak.maybe_speak`（交给人格层）【读码】
- 系统层**只在函数内 import gateway**（避免循环导入），顶部只 import `migrate_channel / mood / plan / role_speak`（:33-36）【读码】
- 系统层**不 import cognition 和 forgetting**：`forgetting` 由 `memory_bridge.py:448` 惰性 import，`cognition` 由 `memory_bridge.py:328` 惰性 import。system.py 里出现的 `forgetting.` 6 处**全部在注释/docstring 里**，`cognition.` 0 处【读码】

---

## 二、逐能力四问

每条按「从哪来 → 中间干了什么 → 到哪去 → 怎么干的」+ 实现度评估。
**实现度拆两层**：机制完成度（代码有没有、接没接上）vs 效果完成度（生产上跑出过结果没有）。

### 能力 1 · 记忆巩固

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | 读 `turns` 表增量，**按渠道逐个**：`_consolidate_channels()` :1952 产出 `[pet]` + 库里出现过的 `qq_private` + 所有 `qq_group:<有群名>`；渠道过滤 `_route_turns()` :2078（私聊走 `_filter_owner_turns` :2039 只认 `[私聊] 主人:` 前缀，群聊走 `_filter_group_turns` :2058 校对群名）；水位每渠道一份 `consolidate_wm` | 【读码】:3809-3812 |
| **素材量** | 每渠道每轮 `consolidate_max_turns`=200 条；增量门槛 `consolidate_min_new_turns`=8，**两条旁路**：①窗口读满（`len(raw)>=limit`）就必处理 ②距上次成功巩固 >`consolidate_max_wait_hours`=12 且增量≥2（`idle_bypass`） | 【读码】:3811,3818,3835-3852 |
| **中间干了什么** | **两段模型调用**：<br>①`_extract_events` :2159 → `system_extract_events` 工具，`tool_choice` **强制指定**（不是 auto），`max_tokens=600`，prompt 是硬编码中文（:2163-2195）<br>②`_build_consolidate_messages` :3391 → `call_system(tool_choice="auto")`，system prompt = `system_policy.md` + `consolidate.md` 拼接（:85-86），`max_tokens`=`consolidate_max_tokens`=500 | 【读码】:2196-2205, :3976-3983 |
| **到哪去（产出分流）** | 五条分流口：<br>①`events` 表 ← `_save_events` :2225，**只落 `_stateful_event_kinds()`**（默认 care_rejected/deferred_care/promise，config 可覆盖）<br>②`documents` 表（长期记忆）← `_memory_bridge().write_events` :3893，只写 `observation`/`promise`<br>③`inner_os.json` ← `push_inner_os` :4035（观点）<br>④`people/*.json` ← `update_people_profile` :3935<br>⑤`turns`（channel=system_log）← `append_system_log` :3927 | 【读码】:3881-3935, :4035 |
| **怎么干的（可配置项）** | `config.json` → `system` 段：`consolidate_s`/`consolidate_boot_delay_s`/`consolidate_min_new_turns`/`consolidate_max_wait_hours`/`consolidate_max_turns`/`consolidate_max_tokens`/`events_kinds_stateful`/`people_channels`；模型身份类字段可被 `data/system_model.json` 热切换（`_load_system_cfg` :293） | 【读码】config.json:61-78 |

**去重与失败处理**（读码确认）：
- 事件去重：`_save_events` 用 `(kind, subject, payload, channel)` 四元组查重（:2264-2270）
- 水位只前进：`_advance_watermark` :3517 `max(旧,新)` + `_STATE_LOCK`
- **事件写库失败不推水位**（:3887-3913）：`saved < 0` → 提前 return `_CH_SKIP`，让下一轮重读。这是修过的一个"永久丢数据"的坑
- 单条失败不拦整批：`write_events` 每条 try/except
- 模型没回 tool_call：`reply is None and not calls` → `no_reply` 跳过（:3984-3986）
- 模型选了体检：`_CH_HEALTH`，`_consolidate_body` :4248-4250 直接 return（一轮只做一次）

**实现度评估**：
- 机制完成度 **高**。渠道隔离、双水位（turn + system_log）、按需拼工具 schema、幻觉 id 白名单、unknown 不销毁，全部有代码且有专项测试（`test_dmn_20261106.py` 里 `TestPlanLifecycle` 走真链路）
- 效果完成度 **低**。`system_audit.jsonl` 223 条 `consolidate` 事件：`no_new_turns` 147、`no_channel_ready` 50、`not_enough_new` 12 → **209 条根本没走到模型**；真正 ok 的只有 12 条（`idle_bypass` 6 + 正常 5 + `events+profile_only` 1）【实测】

### 能力 2 · 遗忘

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | ①每日清理：`_maybe_prune_long_memory` :4060，24h 节流（`last_long_memory_prune_ts`），调 `bridge.forget_plan()`<br>②中间档：`_maybe_forget_review` :4116，**跟着巩固走**，只在 owner 路（:3970） | 【读码】:4083-4091, :3968-3970 |
| **中间干了什么** | **纯代码，零模型调用**。`MemoryBridge.forget_plan` :419 读全库 → `forgetting.thresholds(system.SYSTEM)` 取参数（:453）→ `forgetting.plan_prune(docs, **cfg)`（:462）→ 三档分流。中间档另有 `review_candidates` 出候选给巩固那次模型调用顺带判 | 【读码】memory_bridge.py:419-480 |
| **到哪去** | `drop` 档 → `store.delete(doc_id)` 真删；`review` 档 → 模型在 `forget_drop` 里答 → `apply_forget_review` :4178 → `bridge.apply_review` :568 真删；`boosted` 档 → **只计数，不落盘**（见 P0-2） | 【读码】memory_bridge.py:465-480, system.py:4178-4217 |
| **怎么干的** | `R = e^(-t/S)`（forgetting.py:298-332）；`S` 按 kind 查表 `DEFAULT_STABILITY_DAYS`（:97-103：promise/birthday=None 永不删、cognition=90、observation=30、emotion=7）；`use_count` 折算 `S_eff = S × 1.5^use_count`（:360-400）；门槛 `keep_above=0.5` / `drop_below=0.05`（config.json:88-103） | 【读码】|

**三档判定**（forgetting.py:461-493，边界已钉死）：
```
R >= 0.5                    → keep   （取等算留）
R <= 0.05                   → drop   （取等算删，正好 t=3S）
中间 (0.05, 0.5)            → review （攒批给模型看内容）
keep_forever=True 时        → drop 变 keep，review 变 keep（不占模型名额）
```

**`touch_recall` 记账链路**（读码确认完整）：
`recall_long_memory` :3652 → `bridge.recall` :376 → `store.search_hybrid` → 命中后逐条 `store.touch_recall(doc_id)`（memory_bridge.py:408-415）→ `store.touch_recall` :271 读 metadata → `use_count += 1`、`last_used = now` → 直接 `update documents set metadata=?`（不走 `update_document_by_doc_id`，避免重建 FTS 行）。
**只对真返回给模型的那几条记账**（memory_bridge.py:384-385 注释），不是全部命中 —— 口径正确。
【实测】现库 13 条里 7 条有 `use_count`/`last_used`，`use_count` 最大 4，`last_used` = 2026-11-07 14:52~14:54 —— **这些是被我今天的只读召回探针写进去的**（`recall` 路径有写副作用，见"待核实"第 1 条）。

**"永不删类型"**：
- 判定 `never_forget()`（:496-530）四条任一命中：`meta.never_forget` 真 / kind 是 promise·birthday / `importance >= 0.85` / 显式 `stability_days=None`
- `never_forget=False` 是总开关，可解除类型保护
- `plan_prune` 里 S=None 时 **R 恒 1.0、根本不调 `decayed_stability`**（:657-664 注释解释得很清楚：不要折成 NEVER_FORGET_STABILITY_DAYS，否则 60 年前的承诺会算出 R=0.55）

**实现度评估**：
- 机制完成度 **高**。三档、边界、fail-safe（缺时间戳不删、S 非法按永不删、乱码全留）全部有单测（`test_forgetting.py` 60 条）
- 中间档接线 **是真的接上了**（与旧文档相反）：`_maybe_forget_review` :4116 → `forget_review_items` → `review_candidates` → `review_prompt`，答案走 `system_consolidate.forget_drop`，`_consolidate_tools` :1728 按需拼 schema。**但**它只在"走到模型那一步"才跑（前面 `no_new_turns`/`not_enough_new`/`events_write_failed` 全提前 return），而实测 223 次巩固只有 14 次走到那一步 → 效果上"每轮巩固都跑"**不成立**，是"每 N 天跑一次"
- 效果完成度 **极低**。`system_audit.jsonl` 29 条 `long_memory_prune`：`scanned` 依次是 1~13、`matched` **恒为 0**、`deleted` **恒为 0**、`review` **恒为 0**、`boosted` **恒为 0**。旧判据（够旧且 importance<0.3）是"永远跨不过去"，新判据（R<0.05）是"根本到不了"—— 因为 `create_time` 最老是 2026-11-03，距今 3.7 天，`emotion` 类 7 天稳定性的 R 还有 0.59。**两种判据在生产上都还没删过任何一条**【实测】
- 【实测】用真实 13 条跑 `plan_prune`：`keep=13 / drop=0 / review=0 / boosted=0`，`no_timestamp=0`

### 能力 3 · 人物建模

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | 巩固时同渠道的 `turns`（`_consolidate_channel` :3931-3935）；梳理时 `theory_of_mind` 活动的 `profile_hint`（`_apply_profile_hint` :4723） | 【读码】|
| **中间干了什么** | **调模型**：`update_people_profile` :3204 拼 prompt（硬编码，:3231-3243，要求输出 5 字段 JSON：称呼/作息/情绪基调/在意的事/互动习惯），`tools=[]`、`max_tokens=500` | 【读码】:3246 |
| **到哪去** | `data/people/<key>.json`，`_write_people_profile` :3099 原子写 | 【读码】|
| **怎么干的** | 文件名 `_people_key` :2981 = 可读前缀 48 字 + `sha1(完整 channel)[:10]`（修过三个撞名通道：不可逆替换 / NTFS 大小写 / 截断）；读侧 `people_prompt_line` :3139 有 7 天降权（`PEOPLE_STALE_DAYS` :2976）+ 300/400 字截断 | 【读码】|

**隔离**（物理分文件，读码确认）：
- `_people_channel` :2087：`qq_private` 归一到 `pet`（= owner，主人可跨场合），其余各存各的
- `_people_enabled` :3110：只认精确值和 `前缀:*`，不做通用 glob
- **`update_people_profile` 对 raw `qq_private` 硬拒绝**（:3218-3225，因为 turns 缺 speaker 列分不清主人/陌生人），走 owner 后是 `pet`，闸门仍有效
- 【实测】`people/` 只有 2 份：`owner.json`（445 B）、`qq_group_示例群__c260963ae2.json`（477 B）

**实现度评估**：机制 **高**（写/读/合并/迁移/降权齐全，`migrate_people_files` :3029 启动时幂等迁移）；效果 **中**（`people_profile_hint` 只成功过 1 次，`updated_ts` 都较新，画像内容具体、有证据）

### 能力 4 · 内心OS + 心情

这条是**两个子区域**，代码上是分开的：

**4a 内心OS（队列）**

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | 四个写入点：①巩固的观点 :4035 ②调度触发 `_fire_due_schedules` :2396 ③计划触发 `_fire_due_plans` :2570 ④每日梳理 :4739、人格建议 :4988 | 【读码】|
| **中间干了什么** | 纯代码。`push_inner_os` :5153 生成 id/state/时间戳，**按 (text, 渠道键) 去重**，`_expire_items` :5083 标过期，`_trim_inner_os` :5092 封顶 20 条 | 【读码】|
| **到哪去** | `data/inner_os.json`（`{"items":[...]}` 格式） | 【读码】|
| **怎么干的** | `INNER_OS_MAX=20`（:71）、`INNER_OS_TTL_S=7 天`（:72）、`INNER_OS_STATES` 五态（:73）；读取 `consume_inner_os` :5187 **读后标 delivered 不清空**，`limit` 由 `inner_os_max_per_turn`=4 控制 | 【读码】|

**渠道键与消费规则**（读码确认）：
- 写入时 `_inner_channel` :1927 归一：`pet`/`qq_private` → `owner`，群聊保留 `qq_group:<群名>`，空/None → 空串（= 全局项）
- 读取时 `_item_visible` :1940：**本渠道 + 空渠道（全局）**，群聊产出绝不出现在私聊/桌宠
- `_trim_inner_os` 排序权重（:5102-5111）：pending(0) < persona_suggestion(-1) < delivered(2) < expired(4)，且 owner 的 pending 再 -1 —— 防止群渠道死数据挤掉主人活数据
- 【实测】`inner_os.json` 20 条**全部 `state=delivered`**、`channel` 全部 `owner`、type 19 insight + 1 care_suggestion、source：12 条 `daily_review` + 8 条无 source。`health_report.json` 报的"积压 20 条（上限 20）"实际是"全部已投递但未过期"——**体检这一项的判据是 `len(inner) >= 20*0.8`，把 delivered 也算成积压，措辞误导**（P2）

**4b 心情（连续值）**

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | ①被动回复 `gateway._note_reply_mood` :1357 → `system.record_mood_event` :271（`emotion` 枚举经 `from_legacy_emotion` 翻档位）②主动说话 `_tick_body` :1522 写 `st["emotion"]`（**不写 mood_***） | 【读码】|
| **中间干了什么** | **纯代码，不调模型**。`mood.apply_event` :300：先 `decayed_state` 衰减，再 `新值 = 旧值×0.5 + 事件值×0.5` | 【读码】mood.py:342 |
| **到哪去** | `system_state.json` 的 `mood_value`/`mood_label`/`mood_reason`/`mood_updated_ts` 四字段 | 【读码】:428-444 |
| **怎么干的** | 常量全在 mood.py：`FLOOR=0.5`(:54)、`DECAY_PER_MIN=0.01`(:56)、`LABEL_VALUES` 13 档(:65-79)、`LEGACY_EMOTION` 映射(:82-91)。衰减 `decay` :244 线性回归，地板 ±0.5 | 【读码】|

**心情迁移已修好**（对齐 lead 事实、并补充区别）：
- `_normalize_mood` :136 返回 `bool`，`load_state` :541 `if mood_migrated: save_state(st)` —— **迁移立刻落盘**，不再"靠下次心跳捎带"。项目地图坑 #61 后果 A **已在代码层修复**【读码】
- **但**：生产 `system_state.json` 的 `mood_updated_ts` = 1794033835.4（2026-11-07 14:43:56），`mood_value=0.0` / `mood_label=平静` / `mood_reason=""`。而该文件在 11-05 13:36 之后就没被生产写过（`last_tick_ts` 停在 11-05 13:36:41）。所以这个 `mood_updated_ts` **是我今天跑测试时某次 `load_state()` 触发迁移写进去的**，不是生产行为。**结论：历史遗留 ≠ 当前 bug**，代码已修，只是这份生产文件早于修复【实测+推断，见"待核实"第 2 条】
- 【实测】`emotion` 恒为 `calm`、`last_speak_ts` = 2026-11-03 15:26 → `_note_reply_mood` 从 `calm` 映射到"平静"（值 0）→ `apply_event` 加权后仍 0。**心情机制接上了但生产上从未动过**，原因是她 3.6 天没说话，不是缺陷

**实现度评估**：内心OS 机制 **高** / 效果 **中**（20 条全部投递过，说明角色层确实在消费）；心情机制 **高** / 效果 **无数据**（值为 0，无法验证衰减与加权）

### 能力 5 · 计划

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | **触发**：心跳里 `_fire_due_plans` :2511 每 5 分钟查 `events` 表 `kind in ('promise','deferred_care') and status in ('open','deferred') and attempts=0`<br>**收尾**：巩固里 `_pending_plan_candidates` :2637 查 `attempts>=1` 的 | 【读码】:2533-2539, :2677-2683 |
| **中间干了什么** | 触发侧**纯代码**（`plan_rules.is_due` 判 10 天沉默期）；收尾侧**调模型**（搭巩固那次调用，`plan_verdicts` 字段） | 【读码】|
| **到哪去** | 触发 → `push_inner_os`（care_suggestion）+ `events.attempts=1` + `system_log` 一行（三件事同一事务，走 `_append_system_log_conn` :2419）；收尾 → `events.status='closed'` + **新建一条 `observation` 留痕事件** + `system_log` 一行 | 【读码】:2570-2581, :2790-2818 |
| **怎么干的** | `plan_silence_days`=10 天（config:105，从**创建时刻**算）；`PLAN_VERDICT_MAX`=10（plan.py:334）；`PLAN_VERDICT_TEXT_LIMIT`=100（:337）；`should_close`（:425）只认 success/failure，**unknown 绝不销毁** | 【读码】|

**成败判定已从词表改为模型**（对齐 lead 事实）：
- 词表（`judge_outcome`/`SUCCESS_WORDS`/`FAILURE_WORDS`/`DENY_WORDS`）**整套删除**。全仓搜 `judge_outcome` 只剩 4 处**注释**（:1459, :2429, :2640, :2730），无代码残留【读码】
- `plan.py` 现在只剩三个与成败有关的公开入口：`normalize_verdict` :214、`parse_verdicts` :243、`should_close` :425

**两处 `order by ts` 的分辨（回答 lead 的疑问）**：
| 位置 | SQL | 用途 | 方向对不对 |
|---|---|---|---|
| :2537 | `order by ts limit 10` | `_fire_due_plans` 取**未触发**的计划 | **正序正确**（最老的先到期，先触发） |
| :2681 | `order by ts limit ?` | `_pending_plan_candidates` 取**已触发待判** | **正序正确**（拖最久的先判） |
| :2704 | `order by ts desc, id desc limit 10` | 取「触发后他说了什么」的说话窗口 | **倒序取最新 10 句**，再 `reversed()` 翻正序 —— 坑 #64 已修 |

三处方向各自正确，不是同一件事。**但 :2537 有一个真实的饥饿缺陷**，见 P1-1。

**实现度评估**：机制 **高**（触发/收尾/留痕/流水账/幻觉 id 白名单/群渠道跳过，全部有代码 + `test_dmn_20261106.py::TestPlanLifecycle` 13 条走真链路）；效果 **零数据**（`events` 里 4 条计划全部 `attempts=0`，从未触发过，因为最老的 2026-11-03 才 3.7 天 < 10 天）

### 能力 6 · 自我建模（人格复盘 + 人格演化）

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | `_review_persona` :4914 读 `_read_persona_review_turns` :1850（**跨 pet + 主人私聊**，私聊过 `_filter_owner_turns`），水位 `last_persona_review_id` | 【读码】:4924 |
| **中间干了什么** | **调模型**：`PERSONA_REVIEW_PROMPT`（`system_policy.md` + `persona_review.md`），`tool_choice="auto"`，工具两个：`system_persona_review` + `system_health_check` | 【读码】:4954-4973 |
| **到哪去** | `need_change=true` → `push_inner_os` 一条 `type=persona_suggestion`（:4988-4993）→ 角色层 `build_messages` 里捞出来 → `apply_persona_suggestions` :5021 入队 → `_persona_apply_worker` :5067 消费 → `_apply_one_suggestion` :5028 用 **`gateway.call_model`（角色层模型，deepseek）** 把关 → 采纳则 `_write_persona` :4884 写 `persona.md` | 【读码】|
| **怎么干的** | 门槛 `persona_review_min_turns`=8（**先判门槛再推水位**，修过"永远凑不满"的坑，:4938-4953）；`_write_persona` 原子写 + 时间戳 `.bak` + 刷 `persona_sync.note_file_written` | 【读码】|

**人格双头同步**（`persona_sync.py` 209 行）：
- 两个真源：`gateway/persona.md`（网关实际用）↔ `qq/astrbot/data/data_v4.db` 的 `personas` 表（WebUI 里看着能用、其实网关会丢弃传入 system）
- **比内容快照不比时间戳**（`data/persona_sync.json` 存两个 hash）：实测过库里 `updated_at` 更新但内容只有 296 字，按时间戳会把真人格覆盖掉【读码】persona_sync.py:15-31
- 四条规则：一致→收手 / 只库变→写文件 / 只文件变→写库 / 两边都变或首次→**文件赢**（git 跟踪的真源）
- 读库走 `file:...?mode=ro`（:65），写库有 `busy_timeout=3000`，任何失败只打日志不抛

**实现度评估**：机制 **高**；效果 **低**：`system_audit.jsonl` 56 条 `persona_review`，`ok=true` 只 3 条，其余全是 `not_enough_new`（增量 <8 条）。**人格演化从未真正改过 `persona.md`**（`persona.md` mtime = 2026-11-01，早于这条链路的所有修复）

### 能力 7 · 节律（三层周期）

同第一节 A 板块，细节见第三章「线程模型」。这里只给结论：

- **三层是真的三层**，`daily_review` 一行都不碰 `_tick_body`（:4644 注释明写"不改 5 分钟心跳"）
- **没有统一调度器**，`DMN审核.md` A5 建议"不合并"，代码与建议一致
- 参数全在 `config.json`，改节律要**重启网关**（`SYSTEM` 在 import 期算成模块级常量 :424）

**实现度评估**：机制 **高**（三线程 + 双判断 + 探路 + 日窗口锚定 + 新颖度度量，全部有代码）；效果 **低**（`daily_review` 只成功跑过 1 次，115 条 `daily_review_skip` 里 96 条 `too_early`、8 条 `not_idle`、7 条 `sleeping`、4 条 `already_ran_today`）

### 能力 8 · 记忆库

| 四问 | 答案 | 证据 |
|---|---|---|
| **从哪来** | 两条写入管道：①`write_events`（巩固抽出的 observation/promise）②`write_thoughts`（每日梳理的念头，**过 `cognition.judge` 准入**） | 【读码】memory_bridge.py:225, :278 |
| **中间干了什么** | 向量化：`DashScopeEmbedding.get_embedding`（`text-embedding-v4`，1024 维，`MAX_BATCH=25`，3 次重试，401/403 立刻抛） | 【读码】dashscope_embedding.py:99-171 |
| **到哪去** | `documents` 表（SQLite）+ `documents_fts`（FTS5 contentless）+ `livingmemory.index`（faiss `IndexIDMap`） | 【读码】|
| **怎么干的** | 见第四章「记忆层」详解 | |

**实现度评估**：存储/检索/遗忘/召回 **高**；**迭代机制（认知的 add/replace/merge/conflict）零接线 —— 这是本报告最严重的断头路**（P0-1）

### 能力 9 · 优化

**空占位**。全仓搜不到任何实现、`config.json` 里也没有对应配置段。`DMN审核.md` 本身写的是「先占位，什么都不做」、「`build_messages` 顺序一条都没挪」【文档】。**审计确认：确实什么都没有，不是遗漏**。

---

## 三、线程模型（回答「到底起了几个线程」）

### 3.1 启动与线程清单

`start()` :6099 是唯一入口，由 `gateway.main()` :3166 调用（`system.start()`）。

| # | 线程名 | target | 周期 | 启动条件 | 证据 |
|---|---|---|---|---|---|
| 1 | `system-heartbeat` | `_heartbeat_loop` :1572 | `heartbeat_s`=300s × `1±20%` 抖动（:1581-1582） | 恒起 | 【读码】:6113 |
| 2 | `system-persona-apply` | `_persona_apply_worker` :5067 | **阻塞等待** `queue.Queue.get()`，无周期 | 恒起 | 【读码】:6114 |
| 3 | `system-consolidate` | `_consolidate_loop` :4279 | 首轮等 `consolidate_boot_delay_s`=90s，之后 `consolidate_s`=3600s × `1±10%`（:4298） | `consolidate_s > 0` | 【读码】:6116-6119 |
| 4 | `system-daily-review` | `_daily_review_loop` :4815 | 首轮等 `boot_delay_s`=90s，之后 `check_s`=600s（下限 30s，:4871） | `daily_review.enabled` | 【读码】:6122-6125 |

**动态创建的临时线程（不是常驻）**：
- 召回线程 `recall`：每次 `recall_long_memory` 起一个，`join(RECALL_TIMEOUT_S=10)`；超时后由 `_recall_release_when_done` :3738 再起一个极轻的 `recall-release` 守护线程等它退（:3710-3721, :3751）
- 记忆事件循环线程 `xiaoyu-memory-loop`：`loop_bridge.AsyncLoopBridge` 在首次 `get_bridge()` 时起，`loop.run_forever()`，`atexit` 注册 `close`
- 后台手动触发的三条：`gateway.py:2797` `system.tick`、`:2801` `system.consolidate`、`:2813` `system.daily_review`，各起一个裸 `Thread`

**结论：常驻 4 个（配置全开时），另有 2 类按需线程。**

### 3.2 互斥

| 资源 | 锁 | 谁用 | 证据 |
|---|---|---|---|
| `system_state.json` | `_STATE_LOCK` (RLock) | 所有 `save_state` / `_read_modify_write` | :54, :592, :618 |
| `novelty_grams.json` | `_NOVELTY_LOCK` (RLock) | `_pending_novel_chars` / `_commit_novelty` | :69, :1248 |
| `inner_os.json` | `_INNER_OS_LOCK` (Lock) | push / consume / mark / clear | :5080 |
| 每日梳理 | `_DAILY_REVIEW_RUN_LOCK`，**非阻塞** acquire，抢不到返回 `busy`（不排队） | `daily_review` :4664 | :4674 |
| 记忆桥 | `_MEMORY_BRIDGE_LOCK` + `_STORE_LOCK`（双检） | `_memory_bridge` :3554, `MemoryBridge.store` | :3551, memory_bridge.py:41 |
| 召回熔断 | `_RECALL_LOCK` | `_RECALL_STATE` 三元组 | :3766 |
| 人格建议队列 | `queue.Queue`（线程安全） | `_PENDING_SUGGESTIONS` | :5018 |

**为什么必须有 `_read_modify_write`**（:599）：`save_state` 是**整份覆盖写**，写者有 4 个。实测后果：巩固线程跨一次模型调用（最多 120s）全程持旧副本，期间心跳推进的 `last_seen_turn_id` 被覆盖回旧值 → 重复抽取 + 重复计费 + 长期记忆重复条目（:608-616 注释）【读码】

### 3.3 崩溃重启

**没有 supervisor，没有看门狗。** 三个循环都是 `while True: try: body() except Exception: _audit(...)` 的形态：

- `_heartbeat_loop` :1573-1582：`tick()` 内部已包一层（:1441-1447），循环再包一层 —— **双保险**
- `_consolidate_loop` :4290-4298：`consolidate()` 内部包（:4262-4268），循环再包
- `_daily_review_loop` :4827-4870：单层包，异常记 `daily_review_error`
- `_persona_apply_worker` :5069-5077：`_apply_one_suggestion` 包一层，**失败不重试**（队列已消费）

**线程死了的后果**：无任何自动重启。`health_check` 会在"心跳 > 3 个周期没动"时报警（:5725），但那是**事后告警**，且要模型主动调 `system_health_check` 才跑（体检不占线程，:6100）→ 线程静默死亡可能长期不被发现。**这是 P1-2。**

### 3.4 优雅退出

**没有优雅退出。** 全仓搜 `system.py` 里的 `.join(` / `atexit` / `shutdown` / `signal.`：
- `.join` 只有两处，都是召回线程的局部等待（:3712, :3747）
- **无** `atexit`、**无** `signal` handler、**无** stop 事件
- 四个线程全是 `daemon=True` → 进程退出时被强杀

后果（推断）：
- 正在写 `system_state.json` 的 `.tmp` 可能残留（`os.replace` 是原子的，最坏是 `.tmp` 孤儿）
- 正在进行的模型调用直接丢
- `_INNER_OS_LOCK` 持锁时被强杀 → 文件内容完整（原子写）但状态可能停在半程
- 实际上"优雅退出"对本项目意义有限：**每次重启都是一次强杀**，代码里所有落盘都是 `tmp + os.replace`，这正是为强杀设计的。**不是缺陷，是明确的取舍**，但没有 `stop.cmd` 之外的清理约定（`stop.cmd` 归 ops 板块）

### 3.5 心跳「变化检测」`_detect_change` :786

```python
sig = {}
latest_id = _latest_turn_id()              # 查 turns 表最新一条「对话」的 id
if latest_id > st["last_seen_turn_id"]:
    sig["new_turns"] = latest_id - last_seen   # 增量条数
    sig["last_turn_id"] = latest_id
presence = _presence_state(_seconds_since_last_turn())
if presence != st["last_presence_state"]:
    sig["presence"] = presence
return sig                                  # 无变化 → 空 dict
```

**什么算变化：只有两个信号**
1. **有新对话**：`turns` 表最新 id > 水位 `last_seen_turn_id`
2. **在场状态跃迁**：`present`(<300s) / `away`(<1800s) / `absent`(≥1800s) 三档（`_presence_state` :775-783）与上次记录的 `last_presence_state` 不同

**为什么零成本**（读码确认）：
- 两次 SQL 都是 `order by id desc limit 1`，走主键
- **且都排除 `NON_CONVERSATION_CHANNELS`**（:118-121 = `system_log` + `internal`）。这是修过的一个严重坑：不排除的话，系统每写一行流水账 → `_latest_turn_id` 变化 → 多一次模型调用；而且 `_seconds_since_last_turn` 归零 → 在场状态永远翻不回 absent → 每日梳理被 `not_idle` 永久挡住（:104-115 注释里有完整推演）
- 无变化时只写 `last_tick_ts` + 一行 audit（:1476-1483），**不调模型**

**但有一个设计后果**：无变化也 `save_state(st)`（:1481）。实测 482 条 tick 里 414 条是 `no_change` → 每次仍写一次 3.3KB JSON。`system_state.json` 的 mtime 会一直在动，无法据此判断"有没有真变化"。【实测】

**变化后的动作链**（:1484-1569）：
1. 先落水位（`last_seen_turn_id` / `last_presence_state`）
2. 读素材 `_read_recent_chat_multi(5)`（pet + 主人私聊，群聊不进，:650）
3. 拼 `signals`（含 `recent_chat_age_minutes`，修过"四天前的对话说成刚刚"）
4. `_allow_speak` 刹车（每日上限 6 + 冷却 1800s，:1391-1409）
5. `role_speak.maybe_speak(signals, st)` —— **决策权在角色层**
6. 真送出去才写 `system_log`（:1542-1543）
7. 写 `recent_thoughts`（保留 10 条）
8. 写 audit

---

## 四、记忆巩固链路（完整时序）

### 4.1 选素材 → 调模型 → 分流

```
_consolidate_loop (:4279)  每 3600s
  └ consolidate() (:4260)  包异常
      ├ _consolidate_body() (:4220)
      │   ├ load_state()
      │   ├ _mark_care_outcomes() (:2887)     ← 安慰三态更新（纯代码）
      │   ├ _maybe_prune_long_memory(st) (:4060) ← 24h 节流的遗忘清理
      │   ├ channels = _consolidate_channels() (:1952)   [pet 常驻 + 库里有群名的]
      │   └ for channel in channels:
      │       └ _consolidate_channel(st, channel) (:3803)
      │           ├ ①读增量 _read_channel_turns (:2003) / pet 走 _read_turns_since (:1827)
      │           ├ ②过滤 _route_turns (:2078)  → 空则推水位跳过
      │           ├ ③门槛判定 (增量<8 且 窗口未满 且 无 idle_bypass) → skip
      │           ├ ④抽事件 _extract_events (:2159) ── 模型调用#1  tool_choice 强制
      │           ├ ⑤落库 _save_events (:2225)  → events 表 + schedules 排班
      │           ├ ⑥写长期记忆 write_events (memory_bridge:225) → documents + faiss
      │           ├ ⑦draw people  update_people_profile (:3204) ── 模型调用#2
      │           ├ [群渠道到此 return] → _CH_OK
      │           ├ ⑧取计划候选 _pending_plan_candidates (:2637)  ← 只 owner
      │           ├ ⑨取遗忘候选 _maybe_forget_review (:4116)      ← 只 owner
      │           ├ ⑩拼 prompt _build_consolidate_messages (:3391)
      │           ├ ⑪巩固调用 call_system(tool_choice="auto") ── 模型调用#3
      │           ├ ⑫模型选体检 → health_check() → return _CH_HEALTH
      │           ├ ⑬落计划裁决 apply_plan_verdicts (:2727)
      │           ├ ⑭落遗忘裁决 apply_forget_review (:4178)
      │           ├ ⑮观点 push_inner_os (:4035)
      │           ├ ⑯推水位 _advance_watermark + _advance_system_log_watermark
      │           └ ⑰审计
      └ _review_persona() (:4914)  ← 独立，不管巩固成功与否都跑
```

**模型调用次数**：owner 渠道最坏 **3 次**（抽事件 + 画像 + 巩固），群渠道 **2 次**（抽事件 + 画像），无增量 **0 次**。

### 4.2 产出如何分流（五个出口，读码确认）

| 出口 | 写入函数 | 存什么 | 渠道隔离 |
|---|---|---|---|
| `events` 表 | `_save_events` :2225 | 只 `_stateful_event_kinds()`（care_rejected/deferred_care/promise）；observation/emotion **照抽不落库** | `events.channel` 保留原始渠道 |
| `documents` 表 | `write_events` memory_bridge:225 | 只 `MEMORY_KINDS=("observation","promise")` | `memory_session_key`：pet/qq_private→`owner`，群聊保留全名 |
| `inner_os.json` | `push_inner_os` :4035 | 观点（type=insight） | `_inner_channel` 归一 |
| `people/*.json` | `update_people_profile` :3204 | 5 字段画像 | 物理分文件 |
| `turns`(system_log) | `append_system_log` :3927 | 「巩固完成：写入 N 条记忆」 | 全局，不隔离 |

### 4.3 去重与失败处理

- **事件去重**：`(kind, subject, payload, channel)` 四元组查重（:2264-2270）
- **记忆去重**：**没有**。`write_events` / `write_thoughts` 都是 `store.add()` 直插，`doc_id` 用 UUID。实测 13 条里"他喜欢蓝色"和"他喜欢灰色"各出现 2 次（id 59/61、60/62），text 完全一致【实测】→ **重复条目已经真实发生**
- **写库失败不推水位**：:3887-3913，这是修过的"永久丢数据"坑
- **失败不拦整批**：单条 try/except（memory_bridge.py:273, :368）
- **`write_thoughts` 的准入**：`cognition.judge(body)` 不通过就 `skip += 1` continue（memory_bridge.py:340-345）

### 4.4 这条链的实际触发率（实测）

`system_audit.jsonl` 996 行、时间跨度 2026-11-02 13:04 ~ 2026-11-05 13:36：

| 事件 | 总数 | ok=true | 说明 |
|---|---|---|---|
| `tick` | 482 | 68 | 414 条 `no_change` 空转 |
| `consolidate` | 223 | 12 | 209 条没走到模型 |
| `daily_review_skip` | 115 | 0 | 96 `too_early` |
| `persona_review` | 56 | 3 | 53 `not_enough_new` |
| `long_memory_prune` | 29 | 29 | **`matched`/`deleted`/`review`/`boosted` 全 0** |
| `health_check` | 25 | 23 | |
| `daily_review` | 9 | 1 | 8 `busy` |
| `extract_events` | 4 | 4 | |
| `long_memory_write` | 1 | 1 | written=0 |
| `inner_os_delivered` | 31 | — | 角色层确实在消费 |
| `speak` / `speak_fail` | 6 / 6 | | 6 次说出去，6 次没送到 |
| `recall_timeout` | 3 | 0 | `>0.2s` —— **与 `RECALL_TIMEOUT_S=10.0` 不符**（见待核实 3） |
| `people_profile_hint` | 1 | 1 | |
| `schedules_fired` | 1 | 1 | |
| `care_outcomes` | 2 | 2 | |
| `heartbeat_error` | 1 | 0 | |

【实测】这组数字是"机制完整、效果稀薄"的直接证据。

---

## 五、四个纯函数引擎

### 5.1 签名与算法总表

| 引擎 | 文件行数 | 输入 | 输出 | 核心常量 |
|---|---|---|---|---|
| **mood** | 424 | `state: dict` + `now: float` | 四字段 dict | `FLOOR=0.5`(:54)、`DECAY_PER_MIN=0.01`(:56)、`LABEL_VALUES` 13 档(:65-79)、`_PROMPT_MAX_CHARS=200`(:96) |
| **plan** | 455 | `events` 行 dict + `now` | 裁决/措辞/摘要 | `DEFAULT_SILENCE_DAYS=10.0`(:62)、`PLAN_VERDICT_MAX=10`(:334)、`PLAN_VERDICT_TEXT_LIMIT=100`(:337)、`_VERDICT_ALIASES` 20 条(:70-90) |
| **forgetting** | 1050 | `docs: list[dict]` + `now` | `{keep,drop,review,stats,rows}` | `DEFAULT_STABILITY_DAYS`(:97)、`NEVER_FORGET_STABILITY_DAYS=36500`(:109)、`MAX_STABILITY_DAYS=3.65e8`(:111)、`NEVER_FORGET_IMPORTANCE=0.85`(:113)、`REVIEW_TEXT_LIMIT=120`(:740) |
| **cognition** | 1175 | `text: str` / `payload` | 判定 dict / actions | `INFERENCE_WORDS` 29 条(:101)、`SINGLE_EVENT_WORDS` 31 条(:135)、`TRANSIENT_WORDS` 12 条(:169)、`COGNITION_ACTIONS` 4 种(:185)、`MAX_COGNITION_CHARS=120`(:352)、`DEFAULT_KEYWORD_LIMIT=6`(:356)、`MAX_NEW_TURNS=200`(:358) |

### 5.2 核心算法

**mood 衰减**（`decay` :244-263，逐字实现规格）：
```
v > 0:  v = max(v - elapsed_min × 0.01, min(v, +0.5))
v < 0:  v = min(v + elapsed_min × 0.01, max(v, -0.5))
v == 0: 保持
```
`min(v, FLOOR)` 这一项保证"地板内的小值不被往外推"（v=+0.2 走 60 分钟仍是 +0.2）。
模块 docstring 主动指出规格正文「-4 经过 180 分钟到地板 -0.5」与公式不符（实际 -2.2，真到地板需 350 分钟），**以公式为准**，测试把两个数都钉住了（:44-46 + test_mood.py:144-156）。这是很负责的处理。

**plan 裁决归一**（`normalize_verdict` :214-240）：
只做**枚举归一**（`"done"`/`"完成"`/`"兑现"` → success），**不做语义判断**。认不出来一律 `unknown`。设计理由写明："语义判断已经交给模型了，这里再猜一遍就等于把被删掉的词表又请回来（而且是在更糟的位置）"（:68-69）。

**forgetting 三档**（`classify` :461-493）：
```
keep_forever=False:  R>=0.5 → keep;  R<=0.05 → drop;  中间 → review
keep_forever=True:   R>=0.5 → keep;  R<=0.05 → keep(应拉高S);  中间 → review(plan_prune 并回 keep)
```
`plan_prune` :561-733 对每条 doc：`_timestamp` 取 last_used 优先、回退 create_time → 缺时间戳 `decisions="keep"` + `no_timestamp += 1`（fail-safe）→ `stability_for(kind, meta, table)` → `decayed_stability(S, use_count, 1.5)` → `retrievability(effective, age)` → `classify` → 出 `rows`（每行带 R/S/age/decision/reason，能回答"为什么这么判"）

**cognition 两标准**（`judge` :439-546，判定顺序：先排除再确认）：
```
1. 空 → noise
2. len > 120 → noise（超长不是主谓宾陈述）
3. _is_pure_noise → noise（纯语气/太短/全是同一字重复）
4. 命中 INFERENCE_WORDS → noise  ← 推断词硬否决（标准②不满足）
5. 命中 SINGLE_EVENT_WORDS → event
6. 命中 TRANSIENT_WORDS → event
7. _has_subject_predicate 不通过 → noise  ← 标准①形态
8. 两条都过 → cognition（含 _numeric_fact 提示）
```
**推断词硬否决在 4 号位，早于形态检查** —— 这是对的：一个形态完美但含"大概"的句子必须判 noise，不能因为"主谓宾完整"就放行。

`_has_subject_predicate` :411-425 是**启发式**（主语 9 个词 + 谓语 60 个词查表），docstring 自己承认"不追求 100% 准确，只保证不会把明显的单次事件/推断句/临时状态判成认知"。方向正确（宁可漏，不可误入）。

### 5.3 是否真的无 IO（核实结论）

| 引擎 | import | 有 IO？ | 时钟依赖 |
|---|---|---|---|
| mood.py | `math`, `time`（:50-51） | **无** | 有（`_now()` 里 `time.time()`，但可传 `now` 覆盖） |
| plan.py | `math`, `time`（:55-56） | **无** | 有（`silence_days` 默认 `time.time()`） |
| forgetting.py | `json`, `math`, `re`, `time`（:65-68） | **无** | 有（`plan_prune` / `age_days` 默认 `time.time()`） |
| cognition.py | `json`, `re`（:74-75） | **无** | **无**（唯一完全无时钟的引擎） |

【读码】全文搜 `open(` / `sqlite3` / `urllib` / `socket` / `subprocess` / `Path(` — 四个文件**全部 0 命中**。
单测也守这一条：`test_mood.py:473-477` 断言 mood.py 源码里不出现 `import os` / `import json` / `import socket` / `requests` / `sqlite3`。

**严格说 mood/plan/forgetting 不是纯函数**（有隐式时钟，同输入不同输出），但**确实无 IO**。生产/测试都通过显式传 `now` 来固定。

### 5.4 生产调用点核实（**这是本次审计最关键的发现**）

用 AST 遍历全仓（排除 `tests/` / `_backup-*` / `tmp/` / `node_modules`）统计每个公开符号的调用点：

#### mood.py — **接线完整** ✅
| 符号 | 生产调用点 | 判定 |
|---|---|---|
| `decayed_state` | system.py:268, :5974 | ✅ |
| `apply_event` | system.py:284（`record_mood_event` 内） | ✅ |
| `prompt_line` | role_speak.py:103 | ✅ |
| `label_of` | system.py:191, :210 | ✅ |
| `value_of` | system.py:186, :197 | ✅ |
| `from_legacy_emotion` | system.py:195, gateway.py:1387 | ✅ |
| `initial_state` | system.py:202 | ✅ |
| `decay` | **无外部调用**（只被 `decayed_state` 内部调） | 合理 |
| 常量 `FLOOR`/`DECAY_PER_MIN`/`LABEL_VALUES`/`LEGACY_EMOTION`/`MOOD_FIELDS`/`FLOOR_REASON` | 无外部引用 | 正常（常量供模块内 + 测试） |

#### plan.py — **接线完整** ✅
| 符号 | 生产调用点 |
|---|---|
| `is_due` | system.py:2542, :5658 |
| `trigger_line` | system.py:2574 |
| `system_log_trigger` | system.py:2580 |
| `system_log_outcome` | system.py:2818 |
| `parse_verdicts` | system.py:2772 |
| `outcome_record` | system.py:2796 |
| `outcome_kind` | system.py:2804, :5636 |
| `closed_status` | system.py:2792 |
| `should_close` | system.py:2783 |
| `plan_summary` | system.py:5654 |
| `PLAN_VERDICT_MAX` | system.py:1765, :2604, :2670-2673 |
| `PLAN_KINDS` | system.py:2416 |
| `PLAN_VERDICTS` | system.py:2620 |
| `_plan_verdict_item` / `plan_verdict_block` | system.py:2711 / :2724 |
| `DEFAULT_SILENCE_DAYS` | system.py:2506, :2508 |
| `normalize_verdict` / `silence_days` / `next_due_ts` / `hours_left` / `PLAN_VERDICT_TEXT_LIMIT` | 无外部调用（被同模块内部用：`parse_verdicts` ← `normalize_verdict`、`plan_summary` ← `silence_days`/`hours_left`、`_plan_verdict_item` ← `silence_days`/`PLAN_VERDICT_TEXT_LIMIT`）；`next_due_ts` 只被 `hours_left` 用 |

#### forgetting.py — **大部分接线，一个明确断头** ⚠️
| 符号 | 生产调用点 | 判定 |
|---|---|---|
| `plan_prune` | memory_bridge.py:462（`forget_plan`）、:511（`review_candidates`） | ✅ |
| `review_prompt` | memory_bridge.py:516 | ✅ |
| `parse_review_reply` | memory_bridge.py:578（`apply_review`） | ✅ |
| `thresholds` | memory_bridge.py:453, :455 | ✅ |
| `REVIEW_TEXT_LIMIT` | memory_bridge.py:562 | ✅ |
| `retrievability` / `classify` / `stability_for` / `decayed_stability` / `never_forget` / `age_days` | **无外部调用**，只被 `plan_prune` 内部用 | 合理 |
| **`apply_recall`** | **全仓生产 0 调用**（只有 `test_forgetting.py`） | ⚠️ **断头**（见 P1-3） |
| **`summary`** | **全仓生产 0 调用**（只有 `test_forgetting.py`） | ⚠️ 死代码 |

#### cognition.py — **两级分化：判定接上了，写入流水线全断** 🔴
| 符号 | 生产调用点 | 判定 |
|---|---|---|
| `judge` | memory_bridge.py:340 | ✅ 接上了 |
| `clean_keywords` | memory_bridge.py:346（+ 模块内 :946） | ✅ 接上了 |
| **`parse_cognition_actions`** :814 | **全仓生产 0 调用** | 🔴 **断头** |
| **`merge_prompt_block`** :1048 | **全仓生产 0 调用** | 🔴 **断头** |
| **`action_to_effect`** :1106 | **全仓生产 0 调用** | 🔴 **断头** |
| **`ingest_route`** :572 | **全仓生产 0 调用** | 🔴 断头 |
| **`is_cognition`** / **`classify_text`** / **`is_thought`** | 0 调用（`is_cognition` 的逻辑被 `judge()["is_cognition"]` 直接取代） | 死代码（薄封装） |
| `INFERENCE_WORDS` / `SINGLE_EVENT_WORDS` / `TRANSIENT_WORDS` / `COGNITION_ACTIONS` / `DMN_ACTION_TABLE` / `MAX_COGNITION_CHARS` / `MAX_NEW_TURNS` / `DEFAULT_KEYWORD_LIMIT` | 无外部引用 | 正常（模块内 + 测试） |

**如何确认"全仓 0 调用"**：AST 遍历 25482 个生产 `.py` 文件（排除 tests/backup/tmp/node_modules），对 `ast.Attribute`（`cognition.xxx`）和 `ast.Name`（`from cognition import xxx` 后裸调）两种形态分别统计；再用 `grep` 正则 `(?<![A-Za-z0-9_])parse_cognition_actions\s*\(` 交叉验证。两轮结果一致：除 `cognition.py` 自身定义处和 `test_cognition.py`，无任何调用点。

**这个断头的实际后果**（不是"少一个功能"，是"记忆永不迭代"）：
- 能力 8 的「迭代机制」= 四种 action（add / replace / merge / conflict）。`merge_prompt_block` 会拼「已有认知 + 今天新对话」让模型判关系，`parse_cognition_actions` 校验动作，`action_to_effect` 翻译成向量增删 —— **三步全部零接线**
- 于是长期记忆**只增不改**：同一个事实每提一次就多一条。**实测已发生**：`livingmemory.db` 13 条里 id 59/61 都是"他喜欢蓝色"、id 60/62 都是"他喜欢灰色"，text 逐字相同【实测】
- `DMN审核.md` 1.2 表格给能力 8 打 60% 并注"⚠ 存储有了，**迭代机制没做**"——**文档自己说的是实话，但要补充一句：代码其实写了 1100 行，只是没接线**【文档+读码】

---

## 六、遗忘链路（逐环节核实）

### 6.1 R = e^(-t/S) 的实现

`forgetting.retrievability` :298-332：
```python
if stability_days is None:  return 1.0      # 永不删类型，不衰减
if S <= 0 or NaN:           return 0.0      # 给不出可信稳定性 = 已经记不住
if t <= 0 or 非法:          return 1.0      # 时间不可信 = 按刚发生算，宁可留
r = math.exp(-age / stability)
```
为什么不用 FSRS（模块 docstring :14-24）：FSRS 是 `R=(1+F·t/S)**C` 幂函数，目标让人"记住"，S=30 天时 R 掉到 0.1 要 **34.7 年**；本项目目标是"让没用的自己消失"，指数在 S=30 时 **69 天**掉到 0.1。方向相反，所以换指数。**这个论证是对的。**

### 6.2 三档判定

见 5.2。边界取等归属已钉死并有测试锁住（`test_forgetting.py:185-233` 共 8 条边界用例）。

### 6.3 touch_recall 记账

链路见能力 2。补充三个读码细节：
- `store.touch_recall` :271 **直接 `update documents set metadata=?`**，不走 `update_document_by_doc_id` —— 因为后者会顺手重建 FTS 行（文本没变，纯浪费）。这是有意的性能取舍（:280-282 注释）
- 只对 `out` 里真返回给模型的那几条记账（memory_bridge.py:408 循环在 `out` 上，不在 `hits` 上）
- `Exception` 全兜，失败只 `logger.warning`，不拦召回

### 6.4 中间档「交模型判」是否真的接上了 — **接上了，但有条件**

**接上了**（读码确认完整链路）：
```
_consolidate_channel :3970  forget_review = _maybe_forget_review() if owner_path else []
        ↓
_maybe_forget_review :4116 → bridge.forget_review_items(limit=10)
        ↓
memory_bridge.forget_review_items :518 → review_candidates(limit) → forgetting.plan_prune → review 名单
        ↓                                                  → forgetting.review_prompt（其实没用它的 prompt，只取 ids）
        ↓ 拿 ids 回 store 补 text（只留叙述句、截 120 字）
system.py :3972 _build_consolidate_messages(..., forget_review=...)  → payload["forget_review"]
system.py :3981 _consolidate_tools(pending_plans, [c["id"] for c in forget_review]) → 拼 forget_drop schema
        ↓
模型在 system_consolidate.forget_drop 里答 id 数组
        ↓
system.py :4013-4026  apply_forget_review(decide_args.get("forget_drop"), forget_review)
        ↓
system.py :4178 apply_forget_review → 白名单过滤（只认 candidates 里的 id）→ json.dumps(ids)
        ↓
memory_bridge.apply_review :568 → forgetting.parse_review_reply → store.delete(doc_id)
```
**关键设计点都对**：
- `:4013` 的 `if forget_review:` **在 `_validate_insights` 之前**，所以 `insights` 为空也照样执行（:4010-4012 注释明说）
- 候选**只喂 owner 那一路**（:3970 + `_build_consolidate_messages` :3450 双守卫），理由写得很清楚："把它喂给群渠道那一路，等于用群的模型调用去删主人私聊里的记忆。这是隔离红线不是省钱"（:3403-3407）
- 候选**不带时间信息**（只给 id + text），因为"模型没有时钟，打分是假精确"
- 白名单防幻觉：`:4189-4191` 先算 `allowed`，`:4206` 逐条校验，`:4208-4211` 一个都不认 → **全留**

**但"每轮巩固都跑"不成立**（P1-4）：
`:3970` 位于 `_consolidate_channel` 的第 ⑧ 步，前面有五个提前 return（`no_new_turns` / `not_enough_new` / `no_routable_turn` / `events_write_failed` / `no_reply`）。实测 223 次巩固里只有 14 次走到这里，而其中 owner 路的更少。**所以它是"每 N 天跑一次"而不是"每小时跑一次"**。这不是 bug（省钱是对的），但文档若写"每轮巩固都会问模型"就是错的。

**并且有两处实质缺陷**：
- **P0-3**：`review_candidates` :511 调 `plan_prune(docs)` **不传 cfg**，用默认门槛；而 `forget_plan` :462 调 `plan_prune(docs, **cfg)` 传了 config。两处口径不同 → 用户改 `config.json` 的 `keep_above` 只影响硬删那一档，"问模型"这一档永远按 0.5 判。代码注释 :4151-4152 自己写了"两处口径一旦漂移，就会出现'问模型的是这批、删的时候按另一批算'，那是比断头路更危险的东西"—— **这句话正好命中它自己**
- **P2-1**：`forget_review_items` :542 调 `review_candidates`（内部 `list_documents()` :508），返回后自己又 `list_documents()` :551。**每轮 owner 巩固对全库扫两遍 + JSON 解析两遍**。13 条无害，2000 条时是每小时 2× 全表

### 6.5 永不删类型

见能力 2。补充：**`boosted` 不落盘是 P0-2**，详见问题清单。

---

## 七、认知准入（cognition.py）

### 7.1 两条标准

| 标准 | 判定函数 | 实现 |
|---|---|---|
| ① **形态**：主谓宾陈述 | `_has_subject_predicate` :411-425 | 查表启发式：9 个主语词（他/她/我/你/您/它/主人/晓/宝贝/大家）+ 60 个谓语词（是/在/有/喜欢/讨厌/…） |
| ② **可验证性**：过场景还有用 | 反向排除 | 命中 `INFERENCE_WORDS`(29) → 否决；`SINGLE_EVENT_WORDS`(31) / `TRANSIENT_WORDS`(12) → 降级成 event |

**分界线**（模块 docstring :19-22）：「他喜欢在深夜聊天」→ 多看几天就知道真假 → 认知；「他嘴硬时在试探」→ 永远无法验证 → 想法，不落库。

### 7.2 推断词硬否决

`judge` :484-500：**第 4 步，早于形态检查**。命中任一词立刻返回 `noise`，`reasons` 里写清"这是推断，后续观察无法判断真假，属于想法不落库"，还会附对照例子。

`is_thought` :554-564 是另一半天：把"想法"从"纯噪音"里区分出来（因为两者都归 `noise`）。**但生产 0 调用** —— 这是审计/调试用的，不是运行路径。

### 7.3 四种 action

`COGNITION_ACTIONS = ("add","replace","merge","conflict")`（:185），`DMN_ACTION_TABLE`（:189-214）原样保留 DMN 审核表。

`parse_cognition_actions` :814-952 的校验规则（fail-safe 方向全部正确）：
```
action 不在四种内        → 丢弃 + rejects 带原因
text 空/非字符串/超 120 字 → 丢弃 + rejects
replace/merge 缺 target_id → **降级成 add**（不是丢弃！文本保留）
replace/merge 的 target_id 不在 known_ids → 降级成 add
conflict 不需要 target_id；多给了也不影响
同一批 text 完全相同      → 只留第一条 + warning
垃圾输入（None/乱码/非dict） → 空 actions，不抛
```
`action_to_effect` :1106-1175 把 action 翻成 `{op, delete_vectors, embedding_calls}`。**`conflict` 的 embedding_calls=1 而不是表里的 0**，模块 docstring :44-70 用 15 行解释了这个差异（表里的 0 指"不动旧条向量"，但新文字仍要算一次才能进索引，否则召回不到等于没写）。**这个解释站得住。**

### 7.4 keywords 清洗

`clean_keywords` :709-750：
```
1. _split_keywords :610  容忍 str（按逗号/顿号/空格切）/ list / 嵌套 dict / None
2. _keep_keyword :635    去单字、去 _STOPWORDS(300+ 词)、去 _GENERIC_WORDS(40+ 词)、去纯数字
3. 去重保序、封顶 limit=6
4. 不足 2 个 → _fallback_keywords :650 从 text 里按长度取实词
```
`_fallback_keywords` 三层降级：切片段（≤6 字直接用）→ 长片段取头尾 2~3 字 → 底线按 2-gram 切。纯启发式，docstring 明说"不追求分词准确，只保证有东西可用且都来自 text"。

### 7.5 `cognition_only` 在 memory_bridge 里怎么生效

`write_thoughts(thoughts, channel, *, origin, importance, cognition_only=True)`（memory_bridge.py:278-373）：

```python
if cognition_only:                              # 默认 True
    verdict = cognition.judge(body)             # :340
    if not verdict["is_cognition"]:
        skipped += 1
        continue                                # :341-345 不落库
    keywords = cognition.clean_keywords([], body)   # :346
extra = "；" + "；".join(keywords) if keywords else "；梳理期念头；分寸"
text = f"{body}{extra}"                         # :349-350 拼成「叙述句；关键词」
store.add(text=text, metadata={
    ..., "kind": "cognition" if cognition_only else kind,   # :362
    "keywords": keywords,                                    # :364
})
```

- **只作用于 `write_thoughts`**（每日梳理的念头）。`write_events`（巩固抽出的 observation/promise）**不过认知准入**，直接写
- `cognition_only=False` 是回滚开关，退回旧行为（每条都写）
- 判成"想法"的**不是丢弃**：它已经在 `inner_os` 队列里，用完即销毁（:342-343 注释）

**效果实测**：拿真实 13 条记忆的叙述句跑 `judge`：
| 结果 | 条数 | 例子 |
|---|---|---|
| `cognition` 通过 | 7 | "他喜欢蓝色"、"他刚给我定了生日，11月1号…"、"他说'我也没有不高兴'的时候…" |
| `event` 不通过 | 2 | "凌晨一口气聊了39条，白天却空了12小时…"（命中"凌晨"→ 实际是"今天"的同族? 不，命中 `SINGLE_EVENT_WORDS` 里的词）、"他凌晨一点还在弄升级，今天又只来了几趟…" |
| `noise` 不通过 | 4 | "他反复追问屌丝的定义，**其实**是在试探…"（推断词）、"他嘴硬说没不高兴，**其实**是在试探…"×3 |

→ **准入是有效的**：4 条推断句被准确拦下，2 条单次事件被降级。但也暴露一个**校准问题**：「凌晨一口气聊了39条…他是在深夜才肯把话交出来的人」实际上是**很好的认知**（可反复验证的人物模型），却被判成 `event`。原因是 `SINGLE_EVENT_WORDS` 里有"说了/讲了/聊了/今天"这类词，句子前半句描述了一次具体事件。**这是启发式的固有代价**，docstring 有免责声明，但值得记一笔（P2-2：漏判率偏高）

**关键结论**：`cognition_only` 准入**只在一条管道上生效**（每日梳理的念头，实测写入 9 条），而**巩固那条管道（`write_events`，环境实测写入 1 条）完全绕过了准入** —— 于是 events 抽查的"observation"里会出现 `"禁止回答与国家、政治相关的事情，记住没有?"`（events id=4）这种**主人下的规定**，而 `_extract_events` 的 prompt :2179-2180 明确要求"主人下的规定、边界、禁令…不算事实，不要抽成 observation"。**prompt 说了，代码没兜**（P1-5）

---

## 八、记忆层（`gateway/memory/`，11 文件 2611 行）

### 8.1 各模块职责

| 文件 | 行数 | 职责 | 来源 |
|---|---|---|---|
| `store.py` | 485 | **同步门面** `MemoryStore`：写/语义检索/关键词检索/混合检索/删除/全量拉/对账/遗忘 | 新写 |
| `document_storage.py` | 815 | `documents` 表 + `documents_fts`（FTS5 contentless）+ 全部 SQL | AstrBot 搬，3 处改写 |
| `vec_db.py` | 364 | `FaissVecDB`：向量 + 文档 + 混合检索编排，`insert`/`retrieve`/`delete` | AstrBot 搬，~10 行改写 |
| `embedding_storage.py` | 210 | faiss 索引读写 + **中文路径 ASCII 桥接** | AstrBot 搬，零改动 |
| `dashscope_embedding.py` | 225 | `text-embedding-v4` / 1024 维 / `MAX_BATCH=25` / 3 次重试 | 新写 |
| `loop_bridge.py` | 136 | **常驻事件循环** + 同步投递（`run_coroutine_threadsafe`） | 新写 |
| `tokenizer.py` | 39 | jieba 分词 + FTS5 查询构造（`"a" OR "b"`） | AstrBot 搬，零改动 |
| `provider.py` | 79 | `EmbeddingProvider` / `RerankProvider` 协议 + `NullRerankProvider` | 新写 |
| `base.py` | 75 | `Result` / `BaseVecDB` | AstrBot 搬，零改动 |
| `__init__.py` | 57 | 惰性导出 `MemoryStore` / `open_store` | 新写 |
| `data/hit_stopwords.txt` | 767 行 | 停用词表（实测加载 749 个非空词） | AstrBot 原样搬 |

### 8.2 `documents` 表结构与 metadata 字段

```sql
CREATE TABLE documents (
    id INTEGER NOT NULL,
    doc_id VARCHAR NOT NULL,          -- 外部 UUID
    text VARCHAR NOT NULL,            -- 给检索用的富文本，不是原文
    metadata TEXT,                    -- JSON
    created_at DATETIME,              -- 本地时间，含微秒
    updated_at DATETIME,
    kb_doc_id TEXT GENERATED ALWAYS AS (json_extract(metadata,'$.kb_doc_id')) STORED,
    user_id   TEXT GENERATED ALWAYS AS (json_extract(metadata,'$.user_id')) STORED,
    PRIMARY KEY (id), UNIQUE (doc_id)
);
CREATE VIRTUAL TABLE documents_fts USING fts5(
    search_text, content='', contentless_delete=1, tokenize='unicode61'
);
```
（`document_storage.py:33-48` 定义 + `:74-105` 建生成列/索引 + `:143-175` 建 FTS5 + 实测 schema 交叉验证）

**`text` 的形状（关键，两个方向都靠它）**：
```
叙述句；关键词1；关键词2
└──┬──┘ └────┬────┘
   │          └─ 给 BM25 精确命中
   └─ 给向量语义
```
写成这种形状的原因（memory_bridge.py:20-21）："原文进向量库会被稀释，关键词也会误命中"。实测 13 条都是这个形状 ✅

**metadata 实际字段**（`write_events` / `write_thoughts` 写入）：
| 字段 | write_events | write_thoughts | 实测 13 条里的分布 |
|---|---|---|---|
| `session_id` | ✅ owner / qq_group:x | ✅ | 13/13（全 owner） |
| `source_channel` | ✅ | ✅ | 13/13 |
| `importance` | 0.7 promise / 0.6 其它 | 0.55 | 实测只有 {0.55, 0.6} |
| `topics` | [kind] | ["daily_review","分寸"] | 13/13 |
| `key_facts` | [evidence] | [body[:120]] | 13/13 |
| `memory_origin` | `dmn_extract_events` | `daily_review` | 13/13 |
| `kind` | `_memory_kind_for()` → promise/observation | `cognition`（cognition_only 时） | **0/13 —— 全部缺失！** |
| `create_time` | `_event_ts()` 对话时间 | **未设**（靠 `store.add` 的 `setdefault(time.time())`） | 13/13 |
| `keywords` | 无 | ✅ 列表 | 0/13 |
| `use_count` / `last_used` | 无 | 无 | 7/13（召回后才有） |
| `status` | `store.add` setdefault → active | 同 | 13/13 |

**`kind` 100% 缺失是关键发现**（P1-6）：`_memory_kind_for` 和 `cognition_only` 都是 11-06 才加的字段，而 13 条真实记忆全部写于 11-03。`plan_prune` 对无 `kind` 的按 `DEFAULT_KIND=observation`（30 天稳定性）处理 —— 于是**已有的 promise 类记忆（如果有）会被当 observation 处理，30 天后可能被删**。目前 13 条里没有 promise，所以暂未暴露。

### 8.3 FTS5 + faiss 混合检索的实现与权重

**没有任何权重。** `store.search_hybrid` :219-246 是**硬拼接 + 去重**：

```python
seen, merged = set(), []
for item in self.search_keyword(query, limit=keyword_limit):   # FTS5/BM25，5 条
    if key in seen: continue
    item["source"] = "keyword"; merged.append(item)
for item in self.search(query, top_k=top_k):                   # faiss L2，top_k 条
    if key in seen: continue
    item["source"] = "vector"; merged.append(item)
return merged[: max(top_k, keyword_limit)]
```

**这个顺序是有意的**（:225-229 注释）："精确词命中是'确定相关'，向量命中是'可能相关'。实测 `生日`/`10月20日` 这类词，纯向量经常不如 BM25。"
**得分不能直接比**：BM25 是负数（越小越相关，实测 -1.98），faiss 那边 `1 - L2/2` 归一化到 [0,1]（vec_db.py:259）。两套分数量纲完全不同，**所以不做加权融合是合理的**，但也意味着"向量那 3 条永远排在关键词那 5 条后面"，语义召回实际上被关键词结果压制。

**两条路径各自实现**：
- 关键词：`store.search_keyword` :196 → `to_fts5_search_text(query, stopwords)` 用 jieba 分词 → `build_fts5_or_query` 拼 `"词1" OR "词2"` → `search_sparse` :527 用 `bm25(documents_fts)` **升序**排序（BM25 负数，升序=最相关在前）
- 语义：`store.search` :175 → `FaissVecDB.retrieve` :230 → `embedding_storage.search`（IndexFlatL2）→ `scores = 1 - L2/2` → `get_documents(ids=indices)` 回表 → 按 faiss 顺序重排

**实测验证**（只读跑真库）：
| 查询 | jieba 分词 | FTS5 命中数 |
|---|---|---|
| 他生日是什么时候 | `['生日']`（"什么时候"被停用词吃掉） | 1（id=55，bm25=-1.98） |
| 喜欢蓝色 | `['喜欢','蓝色']` | 5 |
| 嘴硬 | `['嘴硬']` | 3 |
| 深夜 | `['深夜']` | 1 |
→ **FTS5 链路真的能召回到真记忆** ✅

### 8.4 中文路径 ASCII 桥接（是什么、为什么需要）

**是什么**（`embedding_storage.py:19-59`）：
```python
def _needs_bridge(path: str) -> bool:
    return os.name == "nt" and not path.isascii()

def _safe_temp_dir() -> str:
    # 优先 %SystemRoot%\Temp，其次 tempfile.gettempdir()（必须 isascii）
    # 都不行就 raise OSError

def _make_temp_file(prefix) -> str:
    # 在 ASCII 临时目录建 .faiss 文件

# 读写都桥接：
def _read_index(path):
    try: return faiss.read_index(path)      # 先直接试
    except RuntimeError:
        if not _needs_bridge(path): raise
        tmp = _make_temp_file(...); shutil.copy2(path, tmp)
        return faiss.read_index(tmp)        # 复制到 ASCII 路径再读
    finally: 删 tmp

def _write_index(index, path):
    if not _needs_bridge(path): faiss.write_index(index, path); return
    tmp = _make_temp_file(...); faiss.write_index(index, tmp)
    shutil.move(tmp, path)                  # 先写 ASCII 路径再挪回来
```

**为什么需要**（模块注释 :13-16）：
> Faiss C++ `fopen()` 在 Windows 上使用 **ANSI codepage**。Python 传给 Faiss 的路径是 UTF-8 字节，但 Windows fopen 期望 ANSI 编码，导致含非 ASCII 字符的路径（如 `C:\Users\中文用户名\...`）被解读为乱码而失败。

**本项目必然踩到**：仓库根目录是 `<repo>/`，`path.isascii()` 是 **False**。

**我实测复现了这个 bug 和修复**（这是本次审计最硬的证据之一）：
```
# 直接调 faiss（模拟不桥接）：
faiss.read_index(r"<repo>/gateway\data\livingmemory.index")
→ Error: 'f' failed: could not open G:\С��\gateway\data\livingmemory.index for reading
       ^^^^^^^^ 中文被按 ANSI 解成乱码（С��）

# 走 EmbeddingStorage._read_index（桥接）：
→ 成功，ntotal=13, dim=1024
```
**结论：桥接是必需的，不是防御性代码。没有它，这个项目在 `<repo>/` 下根本读不了索引。**

补充一个 README 强调的点（`memory/README.md:72-75`）："不只是写，读也要桥接（项目地图第 31 条只强调了写）"。代码确实读写都做了。

### 8.5 索引一致性（实测）

`store.consistency` :348 对账 `documents` 行数 vs faiss 向量数：
| 文件 | ntotal | ids |
|---|---|---|
| `livingmemory.index`（当前） | **13** | 49,51-62 |
| `livingmemory.index.bak-before-orphan-cleanup` | **56** | 1-48,50,49,51-62 |
| `documents` 表 | **13** | 49,51-62 |

→ **当前 orphans=0, missing=0，索引一致 ✅**
→ 备份里那 56 个向量有 **43 个孤儿**（id 1-48,50），正是 T-06 那次事故留下的（`store.py:354-358` 记录："documents 7 行、index 56 个向量，49 条孤儿把 top-k 名额占满，语义召回命中真记忆 0 条"）。**已被 `drop_orphan_vectors` 修好**。

**但 `drop_orphan_vectors` 生产 0 调用**（只有 `tools/state_health.py:250` 提示"用 memory_bridge 的 drop_orphan_vectors() 清"，**并不真调**）→ 下次再劈开时，只能靠体检报警 + 人工修（P2-3）。

另外 `store.rebuild_index` :333 的 docstring 说"想修向量用 `rebuild_vectors()`" —— **这个方法不存在**，全仓只有 `rebuild_fts` :344 和 `rebuild_index` :333（别名）。`rebuild_vectors` 只出现在那句注释里（P2-4：注释指向不存在的方法）。

### 8.6 并发模型

`loop_bridge.AsyncLoopBridge` :34：
- 一个后台 daemon 线程跑 `loop.run_forever()`，其他线程用 `asyncio.run_coroutine_threadsafe` 投递
- 实测数据（模块注释 :8-13，来自 `tmp/exp_async_sql.py`）：每次 `asyncio.run()` 40.4ms vs 常驻循环 9.38ms，**快 4.3 倍**；对话热路径每次多 30ms 不可接受
- **进程级单例** `get_bridge()` :123："多个循环会各持一份 faiss 索引，那是真正的数据错乱来源"
- `run(coro, timeout=60.0)` —— **这个 60s 是召回超时链的根源**（system.py:3670 注释："`bridge.run()` 超时 60s → `DashScopeEmbedding` 30s × 3 次重试 → 最坏卡 90 秒"）

**`MemoryStore` 不是线程安全的，但所有 async 调用都投递到同一个事件循环线程**，所以多线程调用是安全的（:84-85，注释称"实验已验证 3 线程并发零错误"）。

---

## 九、渠道隔离

### 9.1 `turns` 的六个渠道值

`migrate_channel.py` 定义（:86-109）：

| 渠道值 | 常量 | 语义 | 实测条数 |
|---|---|---|---|
| `pet` | `CHANNEL_PET` | 本机桌宠，**整条渠道都是主人** | 5 |
| `qq_private` | `CHANNEL_QQ_PRIVATE` | QQ 私聊，**混着陌生人和系统注入**，需前缀过滤 | 346 |
| `qq_group:<群名>` | `CHANNEL_QQ_GROUP + ":"` | 有群名的群聊，群名是隔离键 | 205（示例群） |
| `qq_group` | `CHANNEL_QQ_GROUP` | 群聊但群名没取到 | 4 |
| `qq_unknown` | `CHANNEL_QQ_UNKNOWN` | QQ 那条路但取不到来源标签，**不可信，fail-closed** | 27 |
| `internal` | `CHANNEL_INTERNAL` | 插件注入的整段对话转写，**任何上下文都不进** | **0** |
| `system_log` | `CHANNEL_SYSTEM_LOG` | 系统层操作流水账，**全局不隔离、必须让模型看到** | **0** |

（第七个 `channel=NULL` 实测 17 条，是迁移前的老数据）

`channel_key()` :133 的映射规则：`local=True` → pet；空标签 → `qq_unknown`（不是 pet！这是 H4 第一版踩过的 fail-open 坑）；`私聊` → qq_private；`群聊·X` → qq_group:X；未知 → `qq:X`（保证永远有值且一定不会被误判）。

### 9.2 `NON_CONVERSATION_CHANNELS`

`system.py:118-121`：
```python
NON_CONVERSATION_CHANNELS = (migrate_channel.CHANNEL_SYSTEM_LOG, migrate_channel.CHANNEL_INTERNAL)
```
配套 `_non_conversation_sql(column="channel")` :124 返回 `" and coalesce(channel,'') not in (?,?)"`。

**五个查询必须用它**（读码确认）：
| 查询 | 行号 | 不用的后果 |
|---|---|---|
| `_seconds_since_last_turn` | :730 | 系统写一行 → 在场状态翻不回 absent → 每日梳理永远 `not_idle` |
| `_latest_turn_id` | :754 | 每写一条流水账 → 多一次 `new_turns` 变化 → 白花一次模型调用 |
| `_absence_summary` | :914 | 「他多久没说话了」被系统自己写的话重置 → 场景 C 的素材归零 |
| `_day_timeline` | :816 | 系统记账行为污染"今天说了多少话"的疏密节奏 |
| （`_db_stats` 的 `sources` 统计不用，但要单独看 unknown 比例） | :5295 | — |

注释里有一段非常诚实的记录（:104-117）："这就是坑 #59 的另一半：`system_log` 不只'不进对话历史'，它连'算作对话'都不行。"

### 9.3 `people` 画像物理分文件

见能力 3。补充 `_people_key` :2981 修过的三个撞名通道：
1. **不可逆替换**：`qq_group:a/b` 和 `qq_group:a_b` 都变成 `qq_group_a_b`
2. **NTFS 大小写不敏感**：`qq_group:ABC` 和 `qq_group:abc` 同一个文件
3. **截断**：超 80 字符切掉，两个长群名共享前 80 字符就合并

现在 = `可读前缀[:48]` + `__` + `sha1(完整 channel)[:10]`。实测文件名 `qq_group_示例群__c260963ae2.json` ✅
`migrate_people_files` :3029 启动时幂等迁移旧命名（新文件已存在就不动，旧文件保留不删）。

### 9.4 `inner_os` 的渠道键与消费规则

见 4a。三个要点：
- **写入键**：`_inner_channel` :1927 归一（pet/qq_private → owner；群聊保留全名；空 → 全局）
- **可见性**：`_item_visible` :1940 = 本渠道 + 空渠道（全局）
- **封顶淘汰顺序**：`_trim_inner_os` :5092，owner 的 pending 优先（-1 权重），保证群渠道死数据挤不掉主人活数据

**实测确认**：`inner_os.json` 20 条全部 `channel=owner`、全部 `delivered` → 消费规则在生效（角色层每次拿到 4 条，标 delivered，不再注入）

### 9.5 `system_log` 的语义与读写路径

**语义**（`migrate_channel.py:97-108` 注释）：
> `internal` 的语义是「任何上下文都不进」；`system_log` 的语义**相反**：系统操作记录**必须**让模型看到。两者语义正交，混用会让"系统操作可见"和"内部转写不可见"这两条规则互相打死。

**写入路径（5 个点，全部带 `[系统]` 前缀）**：
| 触发条件 | 行号 | 文案 |
|---|---|---|
| 主动说话**真送出去** | :1543 `_speak_log_text` | `[系统] 主动说话：主人刚回来` |
| 计划临期触发 | :2579 | `[系统] 计划触发：问他升级的事`（`plan.system_log_trigger`） |
| 计划收尾 | :2818 | `[系统] 计划完成：主人兑现了…`（`plan.system_log_outcome`） |
| 巩固**真写入记忆** | :3927 | `[系统] 巩固完成：写入 N 条记忆` |
| 体检**真发现问题** | :6074 | `[系统] 体检：发现 N 个问题` |

**统一标准**（:1528-1541 + :3915-3925 注释）："只在'有结果'时记。判断标准是「这个操作有结果值得未来参考吗」"。空转、被额度拦下、她选择不说、解析失败、投递失败 **一条都不记**。

**两条写入 API，用哪个是硬规则**：
- `append_system_log(text)` :2457 —— 自己开连接。**手上没有未提交写事务时用**
- `_append_system_log_conn(conn, text)` :2419 —— 用调用方的连接。**已在事务里必须用它**，否则第二个连接写同一个库 → `database is locked`，而那行流水账被 `except sqlite3.Error` 吞成一条审计（坑 #60，:2422-2434 有完整记录）

**读取路径**：
- `_pending_system_log(st, limit=20)` :3475 按水位 `last_system_log_id` 取增量
- 只喂 **owner 那一路**（`_build_consolidate_messages` :3450 守卫），放**独立字段** `system_log`，绝不混进 `new_turns`（混进去会被 `_extract_events` 当成对话抽出假承诺）
- `_advance_system_log_watermark` :3501 水位只前进

**实测：`turns` 表里 `channel='system_log'` 0 行** —— 这 5 个写入点全部是 11-06 之后加的，而生产最后一次活动是 11-05 13:36。**机制从未产生过一行数据，无法验证效果**。

---

## 十、数据落点表（逐文件 + 实测值）

### 10.1 系统层会写的文件

| 文件 | 写入函数 | 内容 | 当前大小 | 实测状态 |
|---|---|---|---|---|
| `data/system_state.json` | `save_state` :565 / `_read_modify_write` :599 | 24 个键：emotion/focus/mood_*4/memory/recent_thoughts×10/last_tick_ts/last_speak_ts/speak_count_today/last_consolidate_id+ts/consolidate_wm(3 渠道)/last_persona_review_id/last_seen_turn_id/last_system_log_id/last_presence_state/last_daily_review_day+ts+failed_ts+probe_day/last_long_memory_prune_ts | **3302 B** | 顶层键齐全；`mood_value=0.0`、`last_tick_ts`=11-05 13:36、`consolidate_wm`={qq_private:1469, qq_group:示例群:1315, pet:1469}、`last_persona_review_id`=1500（**> turns max id 1469**，见 P2-5） |
| `data/system_audit.jsonl` | `_audit` :6132 | 每行一个 JSON：`{event, ok, ..., ts, layer}` | **139192 B / 996 行** | 时间跨度 11-02 13:04 ~ 11-05 13:36；17 种事件；分布见 4.4 |
| `data/inner_os.json` | `_write_inner_os` :5143 | `{"items":[...]}`，每条 type/text/channel/id/state/created_ts/expires_ts | **9780 B / 20 条** | 全部 `delivered` / `channel=owner`；19 insight + 1 care_suggestion |
| `data/novelty_grams.json` | `_novelty_save` :1131 | `{"grams":[...], "n":3}`，滚动上限 50000 | **45901 B / 3851 grams** | 基线合理（`tools/state_health.py` 会检查这个） |
| `data/health_report.json` | `_save_health_report` :6078 | 最近一次体检报告 | **1681 B** | **11-05 旧快照**：stats.turns=636 / long_memory_docs=7，与现在 604/13 **不符** |
| `data/people/owner.json` | `_write_people_profile` :3099 | `{channel, updated_ts, profile{5 字段}, review_hints[]}` | 445 B | `updated_ts`=11-07 前后；profile 5 字段齐全 |
| `data/people/qq_group_示例群__c260963ae2.json` | 同上 | 同上 | 477 B | 群画像独立 ✅ |
| `data/persona_sync.json` | `persona_sync._save_state` :124 | `{db: hash, file: hash}` | 54 B | 两个 hash 相同 → 已对齐 |
| `data/system_model.json` | `_save_system_cfg` :328 | 6 个身份字段（含 **key**） | 195 B | base=apihub.agnes-ai.com / model=agnes-3.0-flash / **key 是测试残留值 `sk-TESTSECRET-should-not-be-written`** |
| `persona.md` | `_write_persona` :4884 | 人格正文 + 时间戳 `.bak` | 7769 B | mtime 11-01，**从未被演化改过** |
| `config.json`（回写 system 段） | `_sync_config_system_section` :354 | 只回写非敏感身份字段 | 7937 B | `key` 恒为空（在 git 里，不能写） |

### 10.2 系统层会写的数据库表

| 库 / 表 | 写入函数 | 关键列 | 实测行数 |
|---|---|---|---|
| `gateway.db` / `turns` | `_append_system_log_conn` :2419（channel=system_log） | id/session/role/content/path/channel/ts | **604 行**（system_log **0**） |
| `gateway.db` / `events` | `_save_events` :2225、`record_care_given` :2832、`apply_plan_verdicts` :2727 | id/ts/kind/subject/payload/status/due_ts/attempts/channel | **21 行**：care_given 6（全 responded）/ observation 8 / promise 3 / emotion 2 / deferred_care 1 / care_rejected 1 |
| `gateway.db` / `schedules` | `_save_events` :2303（deferred_care 排班）、`_fire_due_schedules` :2404 标 fired | id/due_ts/kind/event_id/note/fired | **1 行**：due=11-04 01:50，已 fired=1 |
| `livingmemory.db` / `documents` | `write_events` :225、`write_thoughts` :278 | id/doc_id/text/metadata/created_at/updated_at | **13 行** |
| `livingmemory.db` / `documents_fts` | `insert_document` 内部 | rowid/search_text | **13 行**（rowid 对齐 ✅） |
| `livingmemory.index`（faiss） | `embedding_storage.save_index` | 1024 维 IndexIDMap | **ntotal=13**，ids=[49,51-62] ✅ 一致 |

### 10.3 系统层只读不改的文件

`gateway.db` / `turns`（除 system_log 行）、`data/errors.log`（只读统计）、`data/audit.jsonl`（角色层写）、`data/keys.json`、`data/secrets.json`、`qq/astrbot/data/data_v4.db` 的 `personas` 表（**persona_sync 会写**，见能力 6）

### 10.4 实测快照总表

| 项 | 值 |
|---|---|
| 审计日志 | 996 行 / 139192 B / 11-02 13:04 ~ 11-05 13:36 |
| turns | 604 行 / 时间跨度 11-02 15:39 ~ **11-03 22:51**（已 3.6 天无新对话） |
| events | 21 行 / 最新 ts = 11-03 15:26 |
| schedules | 1 行 / 已 fired |
| pending | 0 行 |
| documents | 13 行 / 最新 created_at = 11-05 21:28 |
| inner_os | 20 条 / 全部 delivered |
| 索引一致性 | docs=13, vectors=13, orphan=0, missing=0 ✅ |
| 心情 | `mood_value=0.0` / `平静` / 空理由 / `emotion=calm` |
| 心跳 | 最后 11-05 13:36（已停 2.5 天，服务未运行） |
| 上次成功巩固 | 11-04 20:36 |
| 上次成功梳理 | 11-03 22:28（`last_daily_review_day=2026-11-03`，之后 11-07 仍报 `too_early`） |
| 上次主动说话 | 11-03 15:26 |

**交叉验证（一个很强的一致性证据）**：`health_report.json` 记 `stats.turns=636`，现在是 604。差 32 —— 正好对上 `migrate_channel.py:309` 的注释「2026-11-06 19:44 删掉最后 32 条 internal」。**说明那份 11-05 的报告是真实的，删除也是按计划做的** ✅

---

## 十一、实测：测试条数与通过情况

### 11.1 四个引擎的单测（本次实跑，不联网）

命令：`python -m pytest tests/test_{mood,plan,forgetting,cognition}.py -q`（pytest 9.2.0，Python 3.13.2）

| 文件 | 用例数 | 结果 | 耗时 |
|---|---|---|---|
| `test_mood.py` | **102** | 102 passed | 6.29s |
| `test_plan.py` | **30** | 30 passed | 1.19s |
| `test_forgetting.py` | **60** | 60 passed | 2.67s |
| `test_cognition.py` | **119** | 119 passed | 4.04s |
| **合计** | **311** | **311 passed, 0 failed** | 11.37s |

**测试质量抽查**（读码）：
- `test_mood.py` 用 `pytest.approx` 29 次、`parametrize` 7 组，覆盖档位边界/地板/衰减线性/时间倒流/坏输入
- `test_forgetting.py` 60 条里 8 条专测边界取等（R 恰好 0.5 算留、恰好 0.05 算删），还有一条拿 FSRS 公式做对照（`test_照抄FSRS会几十年不淡`）
- `test_cognition.py` 119 条里 3 组 parameterize 覆盖 BOUNDARY_CASES，还有 `test_word_lists_cover_the_required_words` 钉住词表必须覆盖规格要求的词
- 每个文件都有「坏数据不抛异常」专项（`test_所有函数拿到坏数据都不抛异常`、`test_parse_garbage_never_raises` 等）

### 11.2 引擎+接入层的集成测试

`test_dmn_20261106.py` **1751 行 / 89 个测试**（9 个 TestClass），全部走真链路 + 只桩模型：
- `TestMoodWiring`（10 条）：心情迁移落盘、读不推进时间戳、群聊不动心情
- `TestPlanLifecycle`（13 条）：触发/只触发一次/群渠道不触发/**模型判成功才销毁/unknown 绝不销毁**/幻觉 id 不误伤/schema 按需拼
- `TestSystemLog`（9 条）：流水账带前缀/不进对话历史/**不算他刚说过话**/水位只前进
- `TestActiveSpeakMood`（4 条）：主动说话读新心情/不注入 focus
- `TestSystemLogConsolidate`/`Speak`/`HealthCheck`（11 条）：五个写入点各自"该记/不该记"
- `TestForgetReviewWiring`（9 条）+ `TestForgetReviewBridge`（2 条）：**遗忘中间档 11 条全覆盖**
- `TestDeadCodeDecisions`（6 条）：专门守"哪些函数删了/哪些留下了而且有人用"

**这个文件的存在本身说明团队知道"断头路"这个风险** —— `TestForgetReviewWiring` 的 setup 注释（:246-248）直接写："只测 ④ 的话，①②③ 任何一处退化成'没接上'都测不出来 —— 而这正是审计里 `review_candidates()` 断头路的成因（函数写对了，没人调）"。

### 11.3 全套基线

- **lead 实测**：`pytest gateway/tests -q` = **1012 passed, 1 skipped**（70.95s）
- `项目地图.md` 说 935（gateway）+ 126（desktop）= 1061 —— **实际 gateway 已是 1012**，文档滞后 77 条（对应 11-06/11-07 新增的 cognition/forgetting/mood/plan 四个测试文件 + test_dmn_20261106）
- 我的四个引擎子集 311 条是其中一部分，全绿

---

## 十二、问题清单（P0 / P1 / P2）

### P0（机制写了但没接上，或算出结果却不落盘 —— 能力等于不存在）

#### P0-1 · 认知的四种写入动作整条流水线**生产零调用** → 长期记忆永不迭代
- **证据**：
  - `cognition.py:814` `parse_cognition_actions`、`:1048` `merge_prompt_block`、`:1106` `action_to_effect` —— 全仓生产 0 调用（AST + grep 双验证，见 5.4）【读码】
  - 唯一接线的只有 `judge`（memory_bridge.py:340）和 `clean_keywords`（:346）
  - `system.py` 全文 `cognition.` **0 处**
- **后果**：能力 8「记忆库」的**迭代机制**（add/replace/merge/conflict）完全不存在。记忆只增不改。
- **实证**：`livingmemory.db` 里 id 59/61 都是「他喜欢蓝色」、id 60/62 都是「他喜欢灰色」，text 逐字相同【实测】。这正是 `merge`/`replace` 应该处理的情况。
- **测试为什么没抓到**：`test_cognition.py` 里 119 条**全部在测这个模块本身**，不测"有没有人调它"。而 `test_dmn_20261106.py` 的 `TestDeadCodeDecisions` 只检查了 mood/plan 的几个函数，没覆盖 cognition 的三个。
- **修法方向**：在 `_consolidate_channel` 的巩固调用里加一路「已有认知 + 新对话 → 动作」，或者在 `write_thoughts` 前先查同义/矛盾。**注意**：这会增加一次模型调用，与"不为判断多开调用"的既有原则冲突，需要主人裁决。

#### P0-2 · 遗忘的「永不删类型拉高稳定性」算出来**从不落盘** → 保护只剩半条
- **证据**：
  - `forgetting.plan_prune` :679-698 对 `keep_forever=True` 且 `R <= drop_below` 的条目算出 `boost_to = max(base, never_min, required)`，写进 `rows[i]["boost_to_stability_days"]`，`stats["boosted"] += 1`
  - `memory_bridge.forget_plan` :462-480 只读 `plan["drop"]/["review"]/["keep"]` 和 `stats["boosted"]` **计数**，**从不把 `boost_to` 写回 metadata**
  - 全仓搜 `stability_days"` 的**写**：只有 `forgetting.apply_recall` :452（而 `apply_recall` 生产 0 调用，见 P1-3）
- **后果**：
  1. "拉高稳定性"是空话 —— 下一轮扫描 R 还是那么低，永远 `boosted`，永远不真抬高
  2. `boosted > 0` 时审计会记一条"已保护"，**让人以为保护生效了，实际什么都没做**
  3. 真正生效的保护只有 `classify` 的 `keep_forever → "keep"` 分支（不删）—— 所以**不会误删**，但"越记越牢"这个设计目标完全没有落地
- **当前影响**：实测 `boosted` 恒为 0（还没有 R 掉到 0.05 以下的永不删条目），所以**尚未造成数据问题**，但机制是假的
- **修法**：`forget_plan` 拿到 `plan["rows"]` 后，对 `boost_to_stability_days is not None` 的条目写回 `metadata["stability_days"]`（需要 `store` 加一个 `bump_stability(doc_id, days)` 方法，类似 `touch_recall`）

#### P0-3 · 遗忘中间档的门槛与日常清理**用了两套参数** → "问模型的这批" ≠ "删的时候按那批算"
- **证据**：
  - `memory_bridge.forget_plan` :453 `cfg = forgetting.thresholds(_system.SYSTEM)` → :462 `plan_prune(docs, **cfg)` ← **用了 config.json**
  - `memory_bridge.review_candidates` :511 `plan = forgetting.plan_prune(docs)` ← **没传 cfg，用默认值**
  - 两处都会产出 `review` 名单，但门槛可能不同
- **后果**：主人把 `config.json` 的 `forgetting.keep_above` 从 0.5 改成 0.7 后：
  - 日常清理按 0.7 判 → R 在 [0.5, 0.7) 的条目被判 `review`（该问模型）
  - 中间档候选按 0.5 判 → 这些条目被判 `keep`（根本不问）
  - 结果：**用户调了参数，那一档反而更不问了**
- **代码自己警告过这件事**：`system.py:4151-4152` 注释写"两处口径一旦漂移，就会出现'问模型的是这批、删的时候按另一批算'，那是比断头路更危险的东西"。这句话正好命中 `review_candidates` 自己。
- **测试为什么没抓到**：`test_dmn_20261106.py::TestForgetReviewBridge` 只测"没有候选时返回空"和"候选形状只有 id 和 text"，**没有一条测门槛是否跟随 config**
- **修法**：`review_candidates` 里加同样的 `import system as _system; cfg = forgetting.thresholds(_system.SYSTEM)`，然后 `plan_prune(docs, **cfg)`

### P1（会让某项能力静默失效或误判）

#### P1-1 · `_fire_due_plans` 的 `order by ts limit 10` 有**饥饿缺陷**
- **证据**：`system.py:2533-2539`
  ```sql
  select ... from events
  where kind in (?,?) and status in ('open','deferred') and coalesce(attempts,0) = 0
  order by ts limit 10
  ```
  然后 :2540-2543 逐条 `if not plan_rules.is_due(created, now, days): continue`
- **缺陷**：SQL 的 where 里**没有时间过滤**。取的是"最老的 10 条未触发计划"，但其中可能一条都没到 10 天。只要库里同时存在 >10 条未到期计划，**第 11 条及以后即使到期也永远取不到**
- **为什么现在没暴露**：`events` 里只有 4 条计划（3 promise + 1 deferred_care），全部 `attempts=0`，最老 3.7 天 < 10 天。**纯粹是数据量小**
- **修法**：`order by ts limit 10` 后面加 `and ts <= ?`（`now - days*86400`），或把 `is_due` 判断提前到 SQL

#### P1-2 · 四个线程**没有重启机制，也没有优雅退出**
- **证据**：`start()` :6099-6128 只 `t.start()`，无引用保留、无 join、无 stop 事件；全仓 `system.py` 无 `atexit`/`signal`/`shutdown`；四线程全 `daemon=True`
- **后果**：
  - 线程因未捕获异常静默死亡 → 无自动重启，只能靠 `health_check` 事后报警（而体检要模型主动调）
  - 进程退出 = 强杀，正在进行的模型调用直接丢
- **为什么可以接受**：所有落盘都是 `tmp + os.replace` 原子写，**为强杀设计**。所以"没有优雅退出"是取舍不是缺陷
- **但"没有重启"是真的风险**：`_daily_review_loop` 单层 try/except，若 `cfg` 在循环外抛（:4825 `cfg = _daily_review_cfg()` 在 `while True` **之前**），整个线程直接死。目前 `_daily_review_cfg` 有完整默认值不会抛，但这是脆弱点
- **修法**：把 `_daily_review_loop` 的 `cfg = ...` 移进循环内，或包一层

#### P1-3 · `forgetting.apply_recall` 生产 0 调用 → "每次召回稳定性 ×1.5" 永远不执行
- **证据**：
  - `apply_recall` :403-455，全仓生产 0 调用（只有 `test_forgetting.py` 的 4 条）
  - `store.touch_recall` :271 只写 `use_count += 1` / `last_used = now`，**不写 `stability_days`**
- **这是"方案 A"而不是 bug**：`apply_recall` 的 docstring :407-413 明说「两种接线方式二选一，别混用：**A 推荐（只存计数）**：召回时只写 use_count，稳定性在读取时用 `decayed_stability()` 折算；B（折叠进稳定性）：调本函数」。生产选的是 **A**，所以 `apply_recall` 是**方案 B 的遗留，未被采用**
- **结论：不是断头路，是"未采纳的备选方案 + 有单测"**。但 docstring 里写"二选一"却把 B 保留成公开 API，容易误导后来人（P2 级文档问题）。**"越用越熟"实际是生效的**（`decayed_stability` 在 `plan_prune` :662 被调用）

#### P1-4 · 「遗忘中间档每轮巩固都跑」不成立（文档若这么写就是错的）
- **证据**：`system.py:3970` 位于 `_consolidate_channel` 第 ⑧ 步，前面 5 个提前 return
- **实测**：223 次巩固里只有 14 次走到模型，其中 owner 路更少 → **约每 N 天一次，不是每小时一次**
- **这不是 bug**（省钱是对的），但需要纠正预期。`test_dmn_20261106.py::TestForgetReviewWiring` 测的是"走到那一步之后"的行为，没测触发率

#### P1-5 · `_extract_events` 的 prompt 说了"规定不算事实"，但**代码不兜**
- **证据**：
  - prompt :2179-2180 明确要求："**主人下的规定、边界、禁令、对小菲鱼的行为要求不算事实**——那是人格/策略层的事，不要抽成 observation"
  - `_validate_events` :2099-2156 **只校验 kind 合法、evidence 非空、长度**，不看内容
- **实证**：`events` id=4 是 `kind=observation`，payload 是 `{"evidence": "禁止回答与国家、政治相关的事情，记住没有?", "approach_hint": "硬性边界，遇到敏感话题直接绕开"}` —— **这正是被 prompt 禁止抽的类型**，实际抽出来了【实测】
- **同类**：`_extract_events` 拿到的 turns 已过 `_route_turns`（只留主人说的+她的回复），但这个例子是从 `qq_private` 抽的，说明 `_filter_owner_turns` 放行了这句（它确实是主人说的，前缀对）。所以**过滤层没问题，是内容判定层缺失**
- **修法**：`_validate_events` 里加一条内容黑名单（"禁止/不要/记住…"这类祈使句），或者把这句交给 `cognition.judge` 判成 `noise` —— **后者正好是用上已经写好但没接线的 `ingest_route`**

#### P1-6 · 13 条真实记忆 **100% 没有 `kind` 字段** → 遗忘按 observation（30 天）处理全部
- **证据**：`_memory_kind_for`（memory_bridge.py:116）和 `write_thoughts` 的 `"kind": "cognition"`（:362）都是 11-06 加的字段，而 13 条全部写于 11-03
- **后果**：`plan_prune` :647-650 对无 kind 的用 `DEFAULT_KIND="observation"`（30 天稳定性）。**如果历史数据里有 promise 类，它会被当 observation，30 天后可能被删** —— `forgetting` 的"承诺永不删"对存量数据不生效
- **当前影响**：13 条里没有 promise（1 条 `memory_origin=dmn_extract_events` 是 observation），**暂未造成损失**
- **修法**：存量数据迁移（按 `topics`/`memory_origin` 反推 kind），或让 `plan_prune` 在无 kind 时按 `MEMORY_KINDS` 猜

### P2（不影响正确性，但会误导人或有隐患）

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | `forget_review_items` **每轮 owner 巩固扫两遍全库**（`review_candidates` 内一次 + 自己再一次 `list_documents()`） | memory_bridge.py:508 与 :551 |
| P2-2 | `cognition` 准入**漏判率偏高**：真实认知「凌晨一口气聊了39条…他是在深夜才肯把话交出来的人」被判 `event`（因 `SINGLE_EVENT_WORDS` 含"聊了"） | 实测 13 条里 2 条被误降级 |
| P2-3 | `store.drop_orphan_vectors` :375 生产 **0 调用**（`tools/state_health.py:250` 只提示"用哪个方法清"，不真调）→ 索引劈开后只能人工修 | 全仓扫描 |
| P2-4 | `store.rebuild_index` :339 docstring 说"想修向量用 `rebuild_vectors()`" —— **该方法不存在**，只有 `rebuild_fts` | store.py:333-346 |
| P2-5 | `last_persona_review_id=1500` **> turns max id 1469** → 水位超过了库的实际最大 id，说明它曾被推进到未来（可能是测试残留）。后果：人格复盘永远 `no_new_turns`，直到 id 涨过 1500 | system_state.json + 实测 max(id) |
| P2-6 | 体检的「内心OS 积压」判据 `len(inner) >= 20*0.8` **把 delivered 也算成积压** → 实测 20 条全 delivered 却报"可能没被消费" | system.py:5707 + health_report.json |
| P2-7 | `system_model.json` 里 **key 是测试残留值** `sk-TESTSECRET-should-not-be-written`（`role_model.json` 里是 `sk-SHOULD-NOT-WRITE`）→ 某次测试漏了路径隔离。**当前不影响运行**（系统层不是活跃路径），但下次启用系统模型时会 401 | 实测两个文件 |
| P2-8 | `system_state.json` 的 `mood_updated_ts` 被**我今天的测试触发迁移写入**（11-07 14:43），而文件其余部分停在 11-05。见"待核实"第 2 条 | 实测 mtime + 值 |

---

## 十三、文档不一致

| # | 文档说 | 代码/实测 | 判定 |
|---|---|---|---|
| 1 | `plan.py` 是 278 行（项目地图） | **455 行**（字节计数） | 文档滞后 |
| 2 | gateway 测试 935 条 | **1012 passed, 1 skipped** | 文档滞后 77 条 |
| 3 | 能力 2「遗忘」= 「删掉够旧且不重要的」，对应 `MemoryStore.prune`（DMN审核.md 1.1 表） | 实际主路径是 `MemoryBridge.forget_plan` → `forgetting.plan_prune`（指数衰减），`MemoryStore.prune`（双条件）**只剩 `MemoryBridge.prune` 包装，生产无调用方** | 文档滞后两代 |
| 4 | 能力 8「存储有了，**迭代机制没做**」（DMN审核.md 1.2） | **准确**。但要补充：迭代机制的代码写了 1160 行（含 4 个函数），是"写了没接线"，不是"没写" | 文档对但不完整 |
| 5 | `memory/README.md:115-117` 说"阶段 2：把 `_extract_events` 接入进来 ⏳ 未完成" | **已完成**（`_consolidate_channel` :3893 `write_events`） | 文档滞后 |
| 6 | `memory/README.md:117` 说"阶段 3：切流（停掉插件）⏳ 未完成" | 插件已停用（`system.py:1592` 注释 + 实测 `turns` 里 internal=0） | 文档滞后 |
| 7 | `store.rebuild_index` 注释提到 `rebuild_vectors()` | 方法不存在 | 注释错误 |
| 8 | `forgetting.apply_recall` docstring 说"两种接线方式**二选一**" | 生产只用方案 A，B 从未被采纳 | 表述易误导 |
| 9 | `system.py:4151` 注释警告"两处口径漂移比断头路更危险" | `review_candidates` 自己就是这个漂移 | 注释命中自己（P0-3） |
| 10 | `项目地图.md` 坑 #61 后果 A（心情迁移不落盘） | **已修**（`load_state` :541 立刻落盘） | 已解决但文档未更新 |
| 11 | `项目地图.md` 坑 #63（主动说话读旧 emotion） | **已修**（`role_speak._mood_line` :63-103 读新心情） | 已解决但文档未更新 |
| 12 | `项目地图.md` 坑 #64（计划收尾排序正序） | **已修**（`:2704` 是 `desc` + `reversed()`） | 已解决但文档未更新 |
| 13 | 旧审计说"遗忘中间档没接上" | **已接上**（:3970/:4013-4026） | 已解决但文档未更新 |

---

## 十四、断头路清单

**定义**：代码写好了、有单测、但生产路径上没有调用点。分为三类。

### 14.1 真·断头路（写了不算，能力缺失）

| # | 符号 | 位置 | 行数 | 后果 | 严重度 |
|---|---|---|---|---|---|
| **1** | `cognition.parse_cognition_actions` | cognition.py:814 | ~140 | 记忆迭代的**校验**层不存在 | **P0-1** |
| **2** | `cognition.merge_prompt_block` | cognition.py:1048 | ~50 | 记忆迭代的**prompt 层**不存在 | **P0-1** |
| **3** | `cognition.action_to_effect` | cognition.py:1106 | ~70 | 记忆迭代的**执行层**不存在 | **P0-1** |
| **4** | `cognition.ingest_route` | cognition.py:572 | ~30 | 「认知 vs 事件」的**分流**不存在（调用方各自判） | P1（同源） |
| **5** | `forgetting.apply_recall` | forgetting.py:403 | ~50 | 见 P1-3：**是"未采纳的备选方案"不是缺陷**，但保留为公开 API 有误导 | P1 |
| **6** | `forgetting.summary` | forgetting.py:1036 | ~15 | 一行参数说明，**体检和文档都没用** | P2 |
| **7** | `store.drop_orphan_vectors` | store.py:375 | ~14 | 索引劈开后唯一修法，只能人工调 | P2-3 |
| **8** | `system._read_recent_chat` | system.py:631 | ~17 | 只写死 pet 的老读取器，`_read_recent_chat_multi` :650 取代了它。**生产 0 调用、测试也 0 调用** —— 全文只有定义 :631 和一处 docstring 提及 :655。是**彻底死代码**（`test_dmn_20261106.py` / `test_m0_p0p1_20261103.py` / `test_system_decide.py` 里出现的是 `_read_recent_chat_multi`，不是它） | P2 |
| **9** | `system._parse_review_hint` | system.py:4428 | ~16 | **一行转发**（`return _parse_review_payload(reply)[1]`），生产 0 调用，**只有 3 条测试断言在调**（`test_daily_review_sinks.py:460-461`、`test_dmn_20261106.py:1712`）。**注释 :4431-4442 自己承认了这件事** | P2 |

**合计 9 条**，其中 P0 三条（同一能力的三个环节）、P1 两条、P2 四条。

### 14.2 半断头路（机制在，但某一环空转）

| # | 机制 | 哪一环断了 | 证据 |
|---|---|---|---|
| 1 | 遗忘「拉高稳定性」 | 算出 `boost_to` 但**不写回 metadata** | P0-2 |
| 2 | 遗忘中间档门槛 | `review_candidates` **不读 config** | P0-3 |
| 3 | 计划触发 | `limit 10` **无时间过滤** → 饥饿 | P1-1 |
| 4 | `system_log` | 5 个写入点全在，**生产 0 行数据**（写入点都比最后活动时间晚） | 4.4 实测 |
| 5 | 心情 | `record_mood_event` 接上了，但 `emotion` 恒为 calm → 值恒 0，**从未动过** | 4b 实测 |
| 6 | 人格演化 | `_write_persona` 链路完整，但 `persona.md` mtime 11-01 → **从未改过** | 能力 6 实测 |
| 7 | 认知准入 | 只在 `write_thoughts`（梳理管道）生效，**`write_events`（巩固管道）绕过** | P1-5 |

### 14.3 不是断头路（澄清三个容易误判的）

| 符号 | 为什么不是断头路 |
|---|---|
| `mood.decay` / `plan.silence_days` / `plan.normalize_verdict` / `plan.next_due_ts` / `plan.hours_left` / `forgetting.retrievability` / `forgetting.classify` / `forgetting.stability_for` / `forgetting.decayed_stability` / `forgetting.never_forget` / `forgetting.age_days` | **被同一模块的公开函数内部调用**（AST 统计 internal_refs ≥ 1），属正常的"公开 API + 内部复用"分层 |
| `cognition.is_cognition` / `classify_text` / `is_thought` | 薄封装：`is_cognition` 的逻辑已被 `judge()["is_cognition"]` 直接取代，是**测试/审计用的便利函数**。不是缺陷，但可以删 |
| `system._read_recent_chat` | **生产 0 调用、测试 0 调用**（`grep -P "(?<![A-Za-z0-9_])_read_recent_chat\s*\("` 只命中定义 :631 和 :655 的 docstring）。严格说是死代码，但 docstring 明说"保留它是给'只想知道桌宠那条线'的场景用"，且 `_read_recent_chat_multi` 与它形成对照，**属有意保留的对照实现** |

---

## 十五、待核实（我发现的、但需要澄清或后续确认的）

### 1 · 我今天跑测试/探针时，生产数据被动了三次（需要 lead 知悉）

这三件事我**必须报告**，因为它们违反"不修改任何项目文件"的边界，虽然都是只读意图的副作用：

| # | 时间 | 现象 | 原因分析 |
|---|---|---|---|
| **a** | 14:43:56 | `system_audit.jsonl` 多了 1 行 `long_memory_prune`（scanned=13, matched=0），`system_state.json` 的 `last_long_memory_prune_ts` 被更新为 1794033836 | 我跑了 `audit/_probe_data.py` 和 `_probe_data2.py`（他人 14:38 留下的只读脚本）以及自己的探针，其中某次**调用了 `forgetting.plan_prune` / `MemoryBridge` 的读路径**，触发了 `_maybe_prune_long_memory` 的写入分支。**证明这个函数有"只读调用也有写副作用"的问题** —— 值得作为发现记录 |
| **b** | 14:45:15 | `livingmemory.db` 的 mtime 更新（长度不变 61440） | 只读 sqlite 连接（`mode=ro`）在某些情况下仍会更新文件 mtime（WAL/journal 触碰）。**内容未变**（documents 仍 13 行） |
| **c** | 14:52~14:54 | `documents` 表 7 条记录多了 `use_count`（1~4）和 `last_used` 字段 | 我跑了 `EmbeddingStorage._read_index` 和 FTS5 检索探针，其中某条路径走了 `MemoryBridge.recall` → `touch_recall` → **`update documents set metadata=?`**。**这是真实的写操作** |

**结论**：`documents` 表的 metadata 被写入了 `use_count`/`last_used`（7 条），其他数据未变（`system_audit.jsonl` 多 1 行、`system_state.json` 多 1 个时间戳更新）。**内容层面没有丢失或损坏**，但严格说违反了"只读"要求。请 lead 在汇总时判断是否需要记录。

### 2 · `system_state.json` 的 `mood_updated_ts` 是我跑测试写进去的

- 文件 mtime = **2026-11-07 14:43:56**，但 `last_tick_ts` 停在 **11-05 13:36** → **11-05 之后没有任何心跳**
- `mood_updated_ts` = 1794033835.4 = **11-07 14:43:55**，比文件 mtime 早 1 秒
- 结论：这是某次 `load_state()` 触发 `_normalize_mood` 迁移后 `save_state()` 写的（:541）。**不是生产行为**
- **本报告"心情从未动过"的结论成立**（`mood_value=0.0`、`emotion=calm`），但"`mood_updated_ts` 是历史遗留"这个说法要修正：**它是今天被写进去的**

### 3 · `recall_timeout` 的 reason 是 `>0.2s`，与 `RECALL_TIMEOUT_S=10.0` 不符

- `system.py:3757` `RECALL_TIMEOUT_S = 10.0`
- `system.py:3717` 记的 reason 是 `f">{RECALL_TIMEOUT_S}s"` → 应该是 `">10.0s"`
- 但 `system_audit.jsonl` 里 3 条 `recall_timeout` 的 reason 都是 **`">0.2s"`**
- **推断**：这 3 条写在 `RECALL_TIMEOUT_S` 改成 10.0 **之前**（可能是 0.2s 的时代，那时用于快速验证熔断是否生效）。属历史数据，不是当前 bug。**但需要确认**：如果 0.2s 是当前值，那所有召回都会超时

### 4 · `last_persona_review_id=1500` 超过 `turns` 最大 id 1469

- `system_state.json` 里 `last_persona_review_id = 1500`
- 实测 `turns` 最大 id = **1469**
- 推断：某次测试把水位推进到了未来的 id（`test_*` 里的 fixture 造数据后清理，但 state 是共享的？）。或迁移时算错
- **后果**：人格复盘读 `id > 1500` 恒为空 → `no_new_turns`，**直到真实 id 涨过 1500**。这解释了实测 56 条 `persona_review` 里 53 条 `not_enough_new`
- 需要确认：这是生产遗留还是测试污染

### 5 · 「每日梳理期」在 11-07 仍报 `too_early`

- `health_report.json`（11-05 快照）里 `daily_review_state = "too_early"`，`last_daily_review_day = "2026-11-03"`
- 11-05 13:36 的心跳时间点，`earliest_hour=21` → 13:36 < 21 → `too_early` 正确
- 但这意味着**11-04 和 11-05 两个日窗口都没跑梳理**（`once_per_day=true`，11-03 跑过了）。原因是这两天 21:00 之后网关可能没运行（`last_tick_ts` 停在 13:36）
- **不是 bug**，是"网关没运行就不跑梳理"的必然结果。但对"每天一次"这个设计目标是个现实约束，值得记录

### 6 · 无法确认的部分

- **`config.json` 的 `system.model` = `deepseek-v4.1-flash-1017`，但 `data/system_model.json` 的 `model` = `agnes-3.0-flash`** → `_load_system_cfg` :304-312 会优先用后者。所以**实际生效的是 agnes-3.0-flash**，config.json 是旧值（这正是 `_save_system_cfg` :333-341 注释里记录的 BUG，说"已修"，但现在两个文件又不一致了 —— 因为 `system_model.json` 里有测试残留 key，说明测试覆盖过它）
- **第四个引擎 `cognition.py` 连 `time` 都不 import** —— 完全无时钟依赖，是四个里最"纯"的。这点很好，但它也因此无法自己做任何时间相关判断（所以时间判断都在 `forgetting` 和 `plan` 里）

---

## 附录 A · 逐条回答任务书的 11 个问题（索引）

| # | 问题 | 本报告位置 |
|---|---|---|
| 1 | 主板块 → 子板块树 | 第一章 |
| 2 | 每个能力四问 | 第二章（能力 1-9） |
| 3 | 线程模型（几个线程/周期/互斥/崩溃/退出） + `_detect_change` | 第三章 |
| 4 | 记忆巩固链路（选材→调模型→分流→去重） | 第四章 |
| 5 | 四个纯函数引擎（签名/算法/无 IO/生产调用点） | 第五章 |
| 6 | 遗忘链路（R 公式/三档/touch_recall/中间档/永不删） | 第六章 |
| 7 | 认知准入（两标准/推断词/四 action/keywords/cognition_only） | 第七章 |
| 8 | 记忆层（模块职责/documents 结构/FTS5+faiss/ASCII 桥接） | 第八章 |
| 9 | 渠道隔离（六渠道/NON_CONVERSATION/people 分文件/inner_os/system_log） | 第九章 |
| 10 | 数据落点（逐文件/表/字段/实测值） | 第十章 |
| 11 | 实测（四引擎测试条数/死代码/断头路） | 第十一章 + 第十四章 |

## 附录 B · 审计方法说明

- **行数**：Python 按字节读 `\n` 计数（`data.count(b"\n")`），不用 `Get-Content`（对 UTF-8 中文会少算）
- **调用点**：`ast.walk` 遍历 25482 个生产 `.py`（排除 `tests/`/`_backup-*`/`tmp/`/`node_modules`/`__pycache__`），对 `ast.Attribute`（`mod.xxx`）和 `ast.Name`（`from mod import xxx` 后裸调）两种形态分别统计；正则 `(?<![A-Za-z0-9_])name\s*\(` 交叉验证
- **sqlite**：全部 `file:...?mode=ro` URIs；`documents` / `documents_fts` / `events` / `schedules` / `turns` 只 SELECT
- **faiss**：走 `EmbeddingStorage._read_index`（含 ASCII 桥接），`faiss.vector_to_array(idx.id_map)` 取 ids
- **测试**：`pytest 9.2.0`（临时装到 `%TEMP%\ptylib`，不污染项目环境），`PYTHONPATH` 指向该目录，`-p no:cacheprovider`
- **未做**：未启动任何服务、未发任何网络请求、未改任何项目代码

---

*报告完成时间：2026-11-07 16:2x*
*审计范围：`gateway/system.py` 6563 行 + mood 424 + plan 455 + forgetting 1050 + cognition 1175 + memory_bridge 645 + persona_sync 209 + role_speak 199 + memory/ 2611 + 3 个 md + 7 份 data 文件 + 5 个测试文件*
