# 02 · gateway 角色层审计（被动对话链路）

> 审计对象：`<repo>/gateway\` 的**角色层**（被动请求、拿主意、台词唯一出口）。
> 系统层（`system.py` 主动心跳/巩固/梳理/体检）**不在本报告范围**，只在与角色层有契约交界处引用。
> 审计时间：2026-11-07。**全程只读**：未启动任何服务、未修改任何项目文件（本报告是唯一新增文件）。
> 落盘实验一律用 `GATEWAY_DATA_DIR=<临时目录>` 隔离，生产 `gateway/data/` 未被写入。

---

## 一句话结论

角色层的架构骨架是**健康**的：身份收成 `Identity` 类型、来源隔离 fail-closed、写删走 pending 审批、四道闸不经过模型，这四条都是真的代码而不是文档宣称。
但它有**三个真实的洞**：① 生产 `data/role_model.json` 里躺着一个测试哨兵 key，**优先级压过钥匙串里的真 key**，角色层下一次启动会全线 401；② `Identity.is_owner` 仍然只看 `speaker == "主人"`，**群友把 QQ 昵称改成「主人」就能拿到主人权限（含写文件 + 自确认）**——文档宣称的 T-08 修复只堵了「正文伪造」，没堵「昵称伪造」；③ 只要主人 QQ 昵称不叫「主人」，`hide_private` 就会把**主人自己**当成陌生人，桌宠私房话与他自己的工具往返全部读不到。
另有 1 个跨渠道审批死锁、1 处审批分支不落盘、1 处 `qq_unknown` 桶内互相可见，以及 12 处文档与代码不一致（其中 5 处文档落后于工作区未提交代码）。

---

## 零、审计方法与坐标系

| 项 | 值 |
|---|---|
| `gateway.py` 真实行数 | **3175 行**（147041 字节，纯 LF，`\n` 计数 3174 + 末行） |
| ⚠ 行数陷阱 | 本机 PowerShell 5.1 `Get-Content` 默认按 ANSI 读，会把同一文件算成 **2590 行**；任务书里的「2590 行」即由此而来。**本报告所有行号以 read 工具 / Python 计数为准** |
| 其他文件行数 | tools.py 540、hands.py 177、role_speak.py 199、migrate_channel.py 394、persona_sync.py 209、persona.md 148、persona_apply.md 20、config.json 146 |
| 测试基线 | `pytest gateway/tests -q` → **1012 passed, 1 skipped**（80.99s）。生产库 turns 604 / documents 13 条不变 |
| 证据标记 | 【读码】= 直接读到实现；【实测】= 本次隔离实验/只读统计跑出来；【文档】= 项目地图或注释的宣称；【推断】= 由证据推出但未直接观测 |

本报告做了 **12 个隔离实验**（临时 `GATEWAY_DATA_DIR`，桩掉模型/工具，进程内起随机端口 Handler 而非 8898）。跑完即删的临时脚本清单见附录 A（`<repo>/tmp\_audit_*.py`，**已清理，不属于项目代码**）。

---

## 一、板块树（角色层可独立描述的 11 个子系统）

```
gateway.py（角色层，3175 行）
├─ A. HTTP 服务与路由 ................ L2548-3060（Handler）+ L3151-3174（main）
│    ├─ 单实例锁 + pid ............ L3084-3148
│    └─ 端口探活 /health 判活 ...... L3063-3081
├─ B. 鉴权与守卫 ..................... L2549-2608（_client_is_loopback / _v1_guard / _require_token）
│    └─ token 生成与 ACL 收紧 ....... L74-166
├─ C. 身份解析与渠道标签 ............. L1916-2243（_openai_user_text → Identity）
│    ├─ 可信块判定 ................. L2020-2078
│    └─ 来源键映射（跨模块） ........ migrate_channel.py 全文
├─ D. 请求分流 / 短路 ................ L2743-2827（do_POST 路由）+ L2820-3017（/v1 七分支）
├─ E. 提示词拼装 ..................... L1083-1239（build_messages）+ L55-64 + persona.md
├─ F. 模型调用与角色配置 ............. L875-967（call_model/raw）+ L1746-1859（probe/apply）
│    └─ 钥匙串 ..................... L327-506
├─ G. 工具系统 ....................... tools.py 全文 + hands.py 全文 + L1544-1632（工具循环）
│    └─ 审批（挂起/确认/拒绝） ...... L813-863 + L1494-1527 + L1634-1652
├─ H. 清洗与出口 ..................... L1242-1339（parse_reply）+ L2306-2350（_as_spoken）+ L2527-2545（_openai_reply）
├─ I. 记忆读写与来源隔离 ............. L595-863（schema/turns/pending）+ L722-810（recent_turns）
│    └─ 审计与日志 ................. L1342-1354 + L2478-2525
├─ J. 主动说话（跨层出口） ........... role_speak.py 全文（199 行）
└─ K. 人格同步与演化 ................. persona_sync.py 全文（209 行）+ L51-64 + persona_apply.md
```

**职责边界一句话版**：

| 板块 | 归谁 | 边界在哪 |
|---|---|---|
| A/B | 角色层 | 只管到「请求进得来/进不来」，不做业务判断 |
| C | 角色层 | **唯一**授权依据 `Identity.is_owner`；正文里的任何自称都不该越过这里 |
| D | 角色层 | 判断「这句话是不是人话」；短路分支一律不写记忆 |
| E | 角色层 | 人格只认 `persona.md`；插件塞的 system 整条丢弃（两块例外见 §5） |
| F | 角色层 | 只认 `data/role_model.json` + 钥匙串；**role["key"] 优先于钥匙串** ← 见 P0-1 |
| G | tools.py 声明 / hands.py 执行 | 模型只决定「调哪个」，**边界全在代码**；写删不在这里落地 |
| H | 角色层 | 唯一的「说人话」出口；表情/贴纸/语音/语气都从这里定型 |
| I | 角色层 | 来源隔离的唯一依据是 `turns.channel`，不是正文前缀、不是 path |
| J | role_speak（角色层） | 系统层只给「事实信号 + 刹车」，**台词永远不经过系统层** |
| K | persona_sync | 文件是真源；库只是镜像；两边都改时文件赢 |

---

## 二、逐板块四问（从哪来 → 中间干什么 → 到哪去 → 怎么干的）

### A. HTTP 服务与路由

- **从哪来**：`main()`（L3151）→ `init_db()`（L595）→ `_acquire_single_instance_lock()`（L3084）→ `ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)`（L3162）→ `system.start()`（L3166）。每请求一线程，无并发上限。
- **中间干什么**：`do_GET`（L2632）/`do_POST`（L2743）/`do_OPTIONS`（L2624）用 **`self.path.startswith(...)` 前缀匹配**分发。
  **路由点实测（AST 精确清点）**：`self.path` 判定 **GET 9 处 + POST 6 处 = 15 处**（`do_OPTIONS` 无判定，无条件 204）；加两个 `else → 404` 与 OPTIONS，**终点分支 18 个**。`POST /admin/system` 之下再按 `action` 分 **4 个**（`tick` :2795 / `consolidate` :2800 / `inner-os/clear` :2804 / `daily-review` :2809），未知 action 落 404（:2816）。
- **到哪去**：`do_POST` 先 `json.loads(body)`（L2746）再分发；`/chat` → `handle_chat`；`/v1/.../chat/completions` → §4；`/admin/*` → 配置面。
- **怎么干的**：单实例判据三条（L3098-3101）：端口上能否拿到 `/health` → pid 文件里的进程命令行是否含 `gateway.py` → 都否则接管。启动打印有**一行重复**（L3164 与 L3165 内容完全相同，都是 `角色层: ... 模型: ...`，P2）。

### B. 鉴权与守卫

- **从哪来**：`PET_TOKEN = _load_or_create_token()`（L166）：`GATEWAY_TOKEN` 环境变量 → `data/gateway.token`（≥16 字符才认）→ 生成 `secrets.token_hex(24)`（48 字符）并 `icacls` 收紧 ACL（L110-163）。
- **中间干什么**：
  - `_require_token()`（L2595）：`hmac.compare_digest` 比对 `X-Gateway-Token`，不等就 401。
  - `_v1_guard()`（L2557）：① `_client_is_loopback()`（127.0.0.1/::1/::ffff:127.0.0.1）否则 403；② `Content-Type` 必须精确 `application/json` 否则 415；③ **仅当** `GATEWAY_V1_REQUIRE_TOKEN` 打开时才校验 token（L179-184，**默认关**）。
  - **跨站守卫（Origin/Host 校验）：`gateway.py` 里一行都没有**（grep `Origin|Host|Referer` 只命中 CORS 响应头 L2618/2626/2993）。Origin 守卫在 `desktop\server.py`，不在网关。
- **到哪去**【实测，进程内随机端口】：
  | 路由 | 无 token | 有 token |
  |---|---|---|
  | `/health` `/capabilities` `/v1/models` | **200** | 200 |
  | `/audit` `/admin/model` `/admin/keys` `/admin/system` `/admin/system-model` `/admin/daily-review` | **401** | 200 |
  | `POST /chat` `POST /admin/*` | **401** | — |
  | `POST /v1/chat/completions`（回环 + JSON） | **200（无 token 也放行）** | — |
  | `POST /v1/...`（`text/plain`） | **415** | — |
- **怎么干的 / 旁路**：
  1. **`/v1` 无需 token 是本机任意进程的白嫖口**（代码自己在 L2562-2568 承认）。实测 200 通过。
  2. **路径是前缀匹配 → `/chatbox` 也进 `/chat` 分支**（实测 401，因为 token 挡住了；但语义上是误入）。`/healthxyz` 返回 200（实测）。
  3. **任意路径只要 `endswith("/chat/completions")` 就读作 `/v1`**（实测 `POST /anything/chat/completions` → 200）。设计如此（兼容 base_url），但意味着路由边界不是「路径」而是「后缀」。
  4. `/chat` 的 token 校验是**手抄的一份**（L3024-3027，返回 `{"error":"unauthorized"}`），没走 `_require_token()`（返回 `{"ok":false,...}`）——两套 401 形状不一致（P2）。
  5. `do_POST` **先解析 body、后鉴权**（L2744-2749），且没有请求体大小上限（`desktop\server.py` 有 8MB 上限，网关没有）→ 未鉴权即可触发大 body 解析（P2）。

### C. 身份解析与渠道标签（Identity）

- **从哪来**：`_openai_identity(messages)`（L1988）→ `_openai_user_text(messages)`（L1916）。
- **中间干什么**：
  - content 是 **list**：逐段分流，`_is_trusted_meta_block`（L2051）认得的块进 `meta_parts`，其余进 `body_parts`；**身份只从 `meta_parts` 解析**（L1957-1958）→ 结构信息，伪造不了。
  - content 是 **string**：**默认完全不解析身份**（L1979-1984，`_TRUST_STRING_IDENTITY` 默认关），一律落到 `qq_unknown`。
  - 可信前缀表 `TRUSTED_META_PREFIXES`（L2040）：`<system_reminder>` / `<RAG-Faiss-Memory>` / `[梗百科` / `[语音能力]`；有闭合标签的必须闭合标签也在（L2061-2064）。
  - `_parse_identity`（L2081）：`User ID:` → uid、`Nickname:` → speaker、`Group name:` → group。
  - `_channel_label`（L2092）：group 非空 → `群聊·{group}`；wake_ctx → `群聊`；uid 非空 → `私聊`；否则 `""`。
  - `migrate_channel.channel_key(label, local)`（L133）把展示标签折成协议键：`pet` / `qq_private` / `qq_group:<名>` / `qq_group` / `qq_unknown` / `qq:<未知标签>`。
- **到哪去**：`Identity`（L2163，`__slots__` 7 字段）→ `handle_chat` / `build_messages`；`trusted_identity` 是**唯一该被查询的授权依据**（L2158-2159 注释的自我要求）。
- **怎么干的（授权判定，L2201-2218）**：
  ```
  if local_pet: return True                    # 桌宠，token 已校验
  if not trusted_identity: return False        # 不可信来源，一票否决
  if uid and uid in OWNER_IDS: return True     # 命中 owner_ids（生产：<OWNER_UID>）
  return speaker == "主人"                     # ← 昵称等于「主人」就算主人
  ```
  **最后一行就是 P0-2 的根**（见 §8）。

### D. 请求分流 / 短路

见 §3（/v1 七个分支）与 §4（完整调用链）。

### E. 提示词拼装

- **从哪来**：`handle_chat` 在 L1529 调 `build_messages(session, spoken, context=…, speaker=…, meme_prompt=…, living_memory=…, channel=…, local_pet=…, images=…, identity=_ident)`。
- **中间干什么 / 到哪去**：见 §5（逐块条件表）。
- **怎么干的**：`msgs` 是纯 `list[dict]`，**顺序即语义**；人格永远是第 0 条；当前时间被特意放在**历史之后、用户消息之前**（L1221-1228），因为「历史里有旧时间它会照旧的编」（L1136-1138）。

### F. 模型调用与角色配置

- **从哪来**：`call_model(messages, use_tools)`（L875）。
- **中间干什么**：拼 body（`temperature 0.75`、`max_tokens 400`；`use_tools` 时挂 `tools.TOOLS` + `tool_choice:"auto"`）；`dashscope` 域名自动加 `enable_thinking: False`（L896-897）；`key = _role_key(ROLE)`（L900）→ `Authorization: Bearer`。
- **到哪去**：`base + "/chat/completions"`；失败返回 `(None, {"error","elapsed_ms"}, None)`；2xx 但结构不对也走同一条降级路（L917-928，修过一次 KeyError→500）。
- **怎么干的（key 优先级，L306-324）**：
  1. `role["key"]` —— **显式指定的那把，优先级最高**；
  2. 钥匙串 `data/keys.json` 里 `active` 的那把；
  3. `dashscope` 时 `DASHSCOPE_API_KEY` 环境变量 / `secrets.json`。
  `_probe_role`（L1746）另有一条**防泄漏规则**：base 与当前不同 → **绝不回落**到钥匙串真 key（L1771-1779）。
  `_max_tool_rounds()`（L574）：读 `dialog.max_tool_rounds`，夹在 `[1,8]`，坏值回退 4；**`MAX_TOOL_ROUNDS` 是 import 期常量**（L588），改配置必须重启。

### G. 工具系统（含审批与四道闸）

见 §6、§7。

### H. 清洗与出口

- **从哪来**：模型原始文本 → `parse_reply(raw, max_sentences)`（L1279）。
- **中间干什么（顺序不能乱）**：抠语音标记（L1284-1287）→ 抠 `&&分类键&&` 并**先补句号**（L1291-1296）→ 抠 `{语气:…}` 并清洗（L1298-1302）→ 定情绪（标签优先，裸词兜底 L1303-1310）→ 删贴纸/标签/自报家门/空花括号/裸情绪词/emoji/Markdown 粗体/全类标点（L1312-1319）→ 半角转全角（L1320）→ 去空白（L1321）→ `_tidy` 收标点（L1322）→ 按句截断（L1324-1329）→ 补句号（L1330-1331）。
- **到哪去**：`{"text","emotion","tone","sticker","meme_markers","voice"}`；QQ 路径再经 `_as_spoken`（L2330）加 `[语音]` / `{语气:…}`；最后 `_openai_reply`（L2527）包成 OpenAI 形状。
- **怎么干的（几条硬规则）**：
  - `silent`（`[观望]`）原样返回，**不加前缀、不补句号**（L2334-2335）——否则插件会把「观望」念给全群。
  - `meme_markers` 非空时**不加语音前缀**（L2340-2341）——插件认裸文本。
  - `_speakable`（L2306）：≤28 字且不含数字/字母/冒号/斜杠才自动念。
  - 情绪只有 8 个值（`_EMO` L510），贴纸由情绪决定而非模型自造（`STICKER_BY_EMOTION` L519）。

### I. 记忆读写与来源隔离

- **从哪来**：`init_db()`（L595）建 `turns` / `pending` / `events` / `schedules`，开 WAL + busy_timeout，跑 `migrate_channel.migrate()`（L673）。
- **中间干什么**：`add_turn`（L700）写 6 列（session/role/content/path/channel/ts）；`recent_turns`（L722）读最近 N 条。
- **到哪去**：`build_messages` 的历史段；系统层巩固/梳理读同一张表（不同水位）。
- **怎么干的（隔离，L762-810）**：
  1. `hide_private` 时先滤 `path in ('tool','approval','approve')`（L765）；
  2. **无条件**排除 `internal` + `system_log`（L773-774）；
  3. `source` 显式指定时等值匹配，跳过场合判断（L775-784）；
  4. 否则 `key = channel_key(...)`；**三条任一命中就按 key 等值匹配**（L798-802）：群聊 / `hide_private` / 来源键认不出来；
  5. `channel is null` 的老行**不放行**（等值天然不命中，L795-797）。
- **实测隔离矩阵**（临时库）：

  | 请求者 | 能看到 |
  |---|---|
  | `pet`（桌宠） | 全部（`pet` + `qq_private` + `qq_group:X` + `qq_unknown`，仅排除 internal/system_log） |
  | 群聊 | **只有本群** |
  | QQ 私聊（非主人） | 只有 `qq_private` |
  | `qq_unknown` | **只有 `qq_unknown` 桶**（含别人的话 + 插件注入），见 P1-4 |

### J. 主动说话（role_speak）

见 §10。

### K. 人格同步与演化

- **从哪来**：`current_persona()`（L55）每轮被 `build_messages` L1145 调用。
- **中间干什么**：`persona_sync.sync_persona(PERSONA)`：读文件 → 读 AstrBot `personas` 表 → 比**内容 hash 快照**（`data/persona_sync.json`）判谁改过 → 单向写回。
- **到哪去**：返回值作为**第 0 条 system**；副作用可能改写 `persona.md`（留 `.bak`）或 AstrBot 的库。
- **怎么干的**：四分支（L179-205）：内容一致→收手；只有库变→以库为准；只有文件变→以文件为准；两边都变/首次→**文件赢**（文件在 git 里，能回滚）。失败只打日志不抛（L206-209）。
  **实测**：生产 `persona_sync.json` 的 `db` 与 `file` hash 一致（`b21841dd…`），当前处于「已对齐」稳态。
  演化链路：系统层 `apply_persona_suggestions(suggestions)`（L1166 在 build_messages 里被就地调用）→ 后台线程用 `persona_apply.md` 判断 → 采纳则由**代码**写 `persona.md`（提示词在 `persona_apply.md`，是角色层把关，不是系统层）。

---

## 三、`/v1` 的七个分支（最容易出错的地方）

入口判定：`self.path.rstrip("/").endswith("/chat/completions")`（**L2820**）。
守卫：`_v1_guard()`（**L2826**）→ 失败即 403/415/401 返回。

**公共前置（L2828-2846，所有分支共用）**：
`messages = payload["messages"] or []` → `identity = _openai_identity(messages)`（L2831）→ `text,speaker,uid,group = _openai_user_text(messages)`（L2832，**再解析一遍**）→ `images = _extract_image_urls(messages)`（L2835）→ `meme_prompt = _extract_meme_prompt(messages)`（L2839）→ `living_memory = _extract_memory_prompt(messages)`（L2842）→ `text, temp_ctx = _split_wake_context(text)`（L2844）→ `channel = _channel_label(group, uid, bool(temp_ctx))`（L2846）。

| # | 分支 | 判定条件（行号） | 走哪条路径 | 写记忆？ | 返回 |
|---|---|---|---|---|---|
| 1 | **空正文补位** | `not text and temp_ctx`（L2847） | `text = "（叫了你一下，没说话）"`，**继续往下走正常链路** | 是 | 正常 |
| 2 | **空消息兜底** | `not text and not images`（L2850） | 直接 `_openai_reply(_as_spoken({...}))` 返回 | **否** | 200 固定话术 |
| 3 | **memory_task** | `_is_memory_task(text)`（L2865）：命中 `# 任务说明` 或「请以第一人称回顾并总结以下对话」 | `call_model_raw(messages, max_tokens=1200)` → `_extract_json` → 失败给 `MEMORY_TASK_FALLBACK` | **否** | 200 纯 JSON |
| 4 | **learn_task** | `_is_learn_task(text)`（L2888）：命中 `LEARN_HINTS`（L2384，含裸子串「黑话」） | `call_model_raw(messages)` → `_extract_json` → 失败 `_learn_answer(text)` | **否** | 200 纯 JSON |
| 5 | **meta** | `_is_meta(text)`（L2908）：命中 `META_HINT`（L2355） | `_meta_label(text)` 纯代码枚举（L2373，`_INTERACTION_HINTS` 9 组关键词） | **否** | 200 单词 |
| 6 | **正常对话** | 以上都不命中（L2921-2964） | 拼 `ctx`（来源提示 + `VOICE_NOTE` + 背诵要求）→ `speaker` 若是主人改写成 `主人`（L2929-2930）→ `handle_chat(text, SHARED_SESSION, …, identity=identity)` | **是** | 200（含 stream 子分支 L2989） |
| 7 | **异常** | `except Exception`（L2965） | `_log_error("v1", …)` | 视异常点而定 | **500** |

**子分支**：分支 6 内部 `payload.get("stream")`（L2989）→ SSE 两帧 + `[DONE]`（L2989-3015），**此路径绕过 `_send`，自己写 `Access-Control-Allow-Origin: *`**（L2993），与其余路由的 `ACAO: 8848` 不一致（P2）。非流式走 `_send(200, body)`（L3016）。

**顺序陷阱（代码自己在 L2861-2864 写明）**：`_is_memory_task` **必须**排在 `_is_learn_task` 之前。原因：记忆任务的正文里嵌着整段对话转写，`LEARN_HINTS` 里的「黑话」是裸子串，群里有人提过这个词就会把摘要请求误判成学习任务、回一个 `[]`，插件照样存成空壳。**这条顺序不能动。**

**另外两个顺序事实**：
- 分支 1/2 都在分类之前 → 空消息永远不会进 memory/learn 判定。
- 分支 3 的兜底 `MEMORY_TASK_FALLBACK`（L2429）是**结构合法的最小 JSON**，故意用空数组而不是编内容（L2426-2428）。

---

## 四、一轮对话的完整调用链（从 socket 到响应）

### 4.1 桌宠链路（`/chat`，8898）

| # | 函数 | 文件:行 | 说明 / 短路 |
|---|---|---|---|
| 1 | `Handler.do_POST` | gateway.py:2743 | 先读 body + `json.loads`（L2744-2749），坏 JSON → 400 |
| 2 | 路由判定 `startswith("/admin…")` 全部落空 | :2751-2821 | — |
| 3 | `self.path.rstrip("/").endswith("/chat/completions")` 落空 | :2820 | — |
| 4 | `self.path.startswith("/chat")` | :3019 | 不是 → 404 |
| 5 | **token 校验**（手抄版） | :3024-3027 | 不过 → 401 `{"error":"unauthorized"}`，**短路** |
| 6 | `text` 空判定 | :3028-3031 | 空 → 400 `empty text`，**短路** |
| 7 | `handle_chat(...)` | :3039-3052，`local_pet=True`、`identity=identity_from_pet(...)` | 主链路 |
| 7.1 | 身份归一 | :1448-1463 | 有 identity 就用它覆盖 speaker/channel/local_pet |
| 7.2 | 拼 `spoken` 前缀 | :1475-1476 | `[channel] speaker: text` |
| 7.3 | `chan_key`（来源键） | :1483-1487 | 有 identity 直接用 `channel_key` |
| 7.4 | 句数上限 + 背诵放宽 | :1490-1492 | `RECITE_RE` 命中 → 16 句 |
| 7.5 | `peek_pending(session, chan_key)` | :1496 | 有 pending **且是主人** → `take_pending` → `CONFIRM_RE` |
| 7.5a | **确认命中** → `tools.confirm` | :1500 | 成功/失败各写 2 条 `approve` 记忆 → 组装 result → `audit` → **直接 return（短路）** :1509-1526 |
| 7.5b | **不是确认词** | :1527 | 这一句当「取消」，pending 已被 `take_pending` 吃掉，继续当新消息处理 |
| 7.6 | `build_messages(...)` | :1529-1541 → :1083 | 见 §5 |
| 7.7 | **工具循环** `for round_no in range(MAX_TOOL_ROUNDS)` | :1544 | 最多 4 轮（+1 次收尾） |
| 7.7a | `call_model(messages, use_tools=ROLE["mode"]=="cloud")` | :1545-1547 | **本地模式无工具**（`use_tools=False`） |
| 7.7b | 失败且无 tool_calls | :1548 | 401/403 → 固定「去后台换密钥」`status=auth_error`，**break**；否则第 0 轮悄悄重试一次；再失败 `model_error`，**break** |
| 7.7c | 无 tool_calls | :1569 | `SILENT_RE` 命中 → `status=silent`，**break**；否则 `parse_reply` → 若第 0 轮且没用过工具且 `PRETEND_RE` 命中「我说我提交了」→ **追加 1 次纠正提示再 continue**（:1579-1589）；否则 `ok`，**break** |
| 7.7d | 有 tool_calls | :1594-1630 | 逐条：解析 name/args；**`write_file`/`delete_file` 且 `not _ident.is_owner` → 拒绝**（:1604-1618，写 `status=refused`、给 tool 回 `{"ok":false}`、`refused=True`、continue）；否则 `tools.run`；`need_approval` → 记 `approval`，tool 回「已经去问了，这一轮不要重复调用」 |
| 7.7e | `approval is not None or refused` | :1631-1632 | **break 出循环**（不把工具结果再喂模型一轮） |
| 7.8 | `approval` 存在 → `save_pending` | :1634-1652 | 成功 → `status=approval` + 审批话术；**失败 → `status=approval_busy` +「还有一件事没确认呢」** |
| 7.9 | `parsed is None`（轮数用尽） | :1653-1668 | **再问一次，这次不给工具**（`call_model(..., use_tools=False)` :1657）；成功 → `ok`；连收尾都失败 → `tool_overflow` |
| 7.10 | `parsed["text"]` 为空 | :1669-1686 | 记 `empty_reply.log`（`in` 截 80 字）→ `model_empty` 兜底 |
| 7.11 | `path` / `reason` | :1688-1689 | `used_tools` 或 approval → `tool`，否则 `chat` |
| 7.12 | 写记忆 | :1691-1701 | `status != silent` 才写 user；`ok` 写 assistant（剥 `&&…&&`）；`approval` 也写 assistant |
| 7.13 | 组装 result + `_note_reply_mood` + `audit` | :1703-1731 | 心情只在 owner 且 `status∈{ok,approval}` 且有正文时写 |
| 8 | `_send(200, result, cors=False)` | :3057 | 返回 |

**兜底/异常分支清单（桌宠链路）**：400 坏 JSON / 401 token / 400 空文本 / 401(审批确认路径无) / `auth_error` / `model_error` / `silent` / `refused` / `approval` / `approval_busy` / `tool_overflow` / `model_empty` / `except Exception → _log_error("chat") + 500`（:3053-3056）。

### 4.2 QQ 链路（`/v1/chat/completions`）

`do_POST` :2743 → 路由 :2820 → `_v1_guard` :2826 → 公共前置 :2828-2846 → **七个分支**（§3）→ 分支 6 拼 `ctx`（来源提示 :2933-2935 + `VOICE_NOTE` :2938 + 背诵要求 :2941-2945）→ 主人改写 `speaker` :2929-2930 → `handle_chat(..., local_pet=False, identity=identity)` :2949-2964 → `_as_spoken` :2969 → 拼回 `&&分类键&&` :2973-2977 → `_log_v1` :2978 → 流式或 `_send`。

### 4.3 记忆与上下文的**不一致点**（重点核过）

**问题（lead 指定核查）**：审批挂起时 `:1631` break，`:1634` 才 `save_pending`。`save_pending` 返回 False 时进 `approval_busy`（:1638-1645）。
- **`status="approval_busy"` 的回复不会被写进记忆**：:1691 写 user（因为 `status != "silent"`），但 :1696 只处理 `ok`、:1698 只处理 `approval` → **busy 话术既不落 assistant，也不进 `empty_reply.log`**。
- 【实测】跨 origin 触发（桌宠挂起 → QQ 私聊主人再要写）：`status=approval_busy`，turns 里**只有 1 条 user**，没有 assistant；下一轮模型看到「主人: 帮我写进 b.txt」后面**什么都没有**。模型极可能再调一次工具或自行编一个答复。
- 工具轮的 `assistant(tool_calls)` 与 `tool` 消息本来就**不落库**（只在本次请求的 `messages` 里，:1594/:1626），所以「上下文与记忆不一致」在工具路径上是**设计如此**；但 `approval_busy` 这一条是**回复丢失**，性质不同。
- 更严重的是**跨渠道死锁**：`pending` 表以 `session` 为 PK（:630-637），全局只有一行；`peek_pending(session, chan_key)` 认 origin（:848-851）。→ 桌宠挂起一件后，**QQ 私聊里的主人说「好」确认不了**（origin 不匹配，`peek` 返回 None，实测 `status=ok` 正常聊天、待办仍在），**也不能挂自己的新件**（`save_pending` 返回 False → `approval_busy`）。两渠道互锁 300 秒（`PENDING_TTL` L590），而用户**完全得不到「去另一个渠道确认」的提示**。
- 【实测】生产库 604 行里**有 11 行 `user` 紧跟另一行 `user`（即没有 assistant 回复）**，`path` 全是 `chat`，分布 `qq_private 4 / qq_group 4 / pet 3`。说明「问了没答」在真实数据里确实存在（成因不限于 approval_busy，`silent`/异常也会造成）。

---

## 五、提示词拼装（逐块：在哪拼 / 什么条件下拼 / 什么条件下不拼）

`build_messages`（:1083）的**拼接顺序就是下表从上到下**，共 11 段：

| # | 块 | 行号 | 条件 | 不拼的条件 | 上限 |
|---|---|---|---|---|---|
| 1 | **人格** | :1145 | **恒拼**，第 0 条 | 永不 | 无（整个 `persona.md`，148 行） |
| 2 | **内心OS** | :1157-1163 | `_ident.is_owner` **且** `consume_inner_os` 返回非空 | 群聊 / 陌生人私聊 / 根本没有 pending | 每轮 `inner_os_max_per_turn`（config=4） |
| 2b | 人格建议（副作用） | :1160-1166 | 从内心OS 里挑出 `type=="persona_suggestion"` | 没有建议 | — |
| 3 | **人物画像** | :1172-1177 | `system.people_prompt_line(cur_chan)` 非空 | 该渠道没有画像 / 读失败（异常吞掉） | 400 字（system 侧） |
| 4 | **表情包教学** | :1178-1181 | `meme_prompt` 非空（只 `/v1` 有，桌宠恒空） | 桌宠链路 / 插件没注入 | `MEME_PROMPT_MAX`=3000（:1064） |
| 5 | **历史窗口** | :1182-1189 | **恒拼**（可能为空列表） | 永不 | `dialog.history_turns`=**30 条消息**（≈15 轮问答），按 `id desc` 取再正序 |
| 6 | **插件长期记忆** | :1190-1194 | `living_memory` 非空（`<RAG-Faiss-Memory>`，只 `/v1`） | 桌宠 / 插件停用 | `LIVING_MEMORY_MAX`=4000（:1063） |
| 7 | **自建记忆召回** | :1200-1207 | **`not living_memory`** 且 `system.recall_long_memory(text, cur_chan)` 非空 | 已有插件块 / 召回失败 / 熔断 / 在飞超限 | `RECALL_BLOCK_MAX`=2000（:1067），`top_k=3` |
| 8 | **心境行** | :1212-1218 | `_ident.is_owner` **且** `system.mood_prompt_line()` 非空 | 群聊 / 陌生人 / 平静且无 focus | 无（system 侧拼） |
| 9 | **临时上下文** | :1219-1220 | `context` 非空（唤醒前文 / 来源提示 / `VOICE_NOTE` / 背诵要求） | 桌宠（显式传 `""`） | 无 |
| 10 | **当前时间** | :1221-1228 | **恒拼**（倒数第 2 条） | 永不 | 一句话 |
| 11 | **用户消息** | :1229-1238 | **恒拼**（最后一条）；有图时转多模态 `[{text},{image_url}…]` | 永不 | 无 |

**几个必须知道的细节**：
- **历史窗口单位是「消息条数」不是「轮数」**：`recent_turns(session, 30, …)` :1184 直接 `limit 30`，所以一期 ≈15 轮问答。文档说「30 条，约 15 轮」是对的。
- **来源隔离在 `recent_turns` 里做**（`hide_private` / `channel` / `local_pet` 三个入参 :1185-1187），`build_messages` 只传不判。
- **`hide_private` 的算法是字符串比较**（:1144）：`speaker not in ("", OWNER_LABEL)` —— 不看 `Identity.is_owner`，见 P1-2。
- **第 6/7 块互斥**：有插件块就不召回自建库（:1200 `if not living_memory`），所以插件恢复后自建库会被静默停用。
- **噪音清洗发生在进 `build_messages` 之前**：`_strip_plugin_notes`（:2272，按 `NOTE_MARKERS` 找**最早**切点）在 `_openai_user_text` 里已经切过一次（:1959/:1984）；`_split_wake_context` 再把唤醒前文剥出来当 `temp_ctx`（:2462）。
- **两块「切了又拼回来」的例外**：`[表情图片工具]`（`_extract_meme_prompt` :994）与 `<RAG-Faiss-Memory>`（`_extract_memory_prompt` :1862）。它们是**逐请求的局部变量**，绝不放模块全局（:1018-1029 记录了跨会话泄漏的历史教训 H3）。
- **人格只认 `persona.md`**：插件塞进来的 system 消息整条丢弃，唯二例外就是上面那两块。

---

## 六、工具系统

### 6.1 function calling schema（`tools.TOOLS`，tools.py:27-125）

| 工具 | 参数 | 落地位置 | 要不要审批 |
|---|---|---|---|
| `web_search` | `query`(必) `count`(1-8) | `_web_search`（tools.py:399）→ Tavily / Brave / Bing HTML / Bing RSS 四级 | 否 |
| `get_weather` | `city`(必) `when`(today/tomorrow) | `_weather`（tools.py:206）→ wttr.in，缓存 600s | 否 |
| `list_dir` | `path`(必) | `hands.list_dir`（hands.py:126） | 否 |
| `read_file` | `path`(必) | `hands.read_file`（hands.py:107） | 否 |
| `write_file` | `path` `content`(必) | **不执行**，回 `need_approval` | **是** |
| `delete_file` | `path`(必) | **不执行**，回 `need_approval`；先查存在性 | **是** |

统一入口 `tools.run(name, args)`（tools.py:466）：6 类异常全部转成 `{"ok":false,"error":人话}`（`PolicyDenied` / `FileNotFoundError` / `OSError` / `ValueError` / 兜底），**绝不让异常冒到 `handle_chat`**（:512-523）。

### 6.2 工具循环

- 轮数：`MAX_TOOL_ROUNDS = _max_tool_rounds()`（gateway.py:588）→ 默认 **4**，夹在 `[1,8]`；**用满后还有 1 次「不给工具」的收尾调用**（:1657），所以最坏 **5 次模型调用**。
- 只读工具可连调；`write_file`/`delete_file` 一旦 `need_approval` → 记 `approval` → **立刻 break**（:1631），不再给模型第二轮。
- 非主人调写删：**直接拒绝**，不算 approval（:1604-1618），`status="refused"`。

### 6.3 hands.py 的「四道闸」（具体代码）

| 闸 | 实现 | 行号 | 越权时 |
|---|---|---|---|
| **① 根白名单** | `ROOTS = [realpath(_abs_root(p)) for p in hands.roots]`（config: `["sandbox"]`）；`_norm(full) == _norm(root) or startswith(_norm(root) + "/")` | hands.py:27、:83-84 | `PolicyDenied("这个位置不在我能访问的范围里")` |
| **② 敏感黑名单** | `DENY_GLOBS` 13 条（`.ssh` / `id_rsa*` / `.env*` / `secrets.json` / `credentials*` / `.git/*` / `appdata/*` / `ntuser.dat` / `cookies*` / `desktop.ini`），`fnmatch` 比对已归一化的路径 | hands.py:28-36、:80-82 | `PolicyDenied("这个位置属于敏感路径，我不能碰")` |
| **③ 写扩展名白名单** | `WRITE_EXT = {".txt",".md",".json",".log",".csv"}`；`_check_ext` **只在 `write_file` 里调** | hands.py:37、:101-104、:139 | `PolicyDenied("不能写 .exe 这类文件")` |
| **④ 读上限** | `MAX_READ = 200000` 字节；超了就不读 | hands.py:38、:112-113 | `PolicyDenied("文件太大（N 字节）")` |

**另有第 5 道（后来补的，比四道闸更关键）**：
- **`realpath` 防 symlink/junction**（hands.py:51-59，:75/:77）：`abspath` 只做字符串规范化，`mklink /J link C:/Users/x/.ssh` 能绕过白名单；`realpath` 会把链接解析成真实目标。
- **NTFS ADS（备用数据流）冒号检测**（hands.py:93-97）：`secrets.json:note.txt` 不以 `secrets.json` 结尾、能绕过黑名单，但写的是 `secrets.json` 的备用流。判定在 `realpath` **之后**（realpath 不处理流名）。
- `delete_file` 额外拒绝目录（:150-151）。

**【实测】越权行为**（直接调 `hands.resolve`，临时工作目录）：

| 输入 | 结果 |
|---|---|
| `note.txt` | → `sandbox\note.txt` ✅ |
| `sandbox/note.txt` | → `sandbox\note.txt`（自动剥掉重复的根名，:68-72）✅ |
| `../../secrets.json` | **DENIED** |
| `secrets.json:note.txt` | **DENIED**（ADS 闸） |
| `C:/Windows/win.ini` | **DENIED**（根白名单） |
| `../data/gateway.token` | **DENIED** |

**越权后发生什么**（完整链）：`hands` 抛 `PolicyDenied` → `tools.run` 捕获转成 `{"ok":false,"error":"这个位置属于敏感路径，我不能碰"}` → 作为 `role:"tool"` 消息回给模型 → 模型据此说人话（persona.md:120「工具回来说做不到，就照实说做不到，别假装做过」）。
写/删确认路径上：`tools.confirm` → `hands.write_file/delete_file` → 同样捕获 `PolicyDenied` 返回 `(False, msg)` → `handle_chat` 回 `status="failed"` + 难过情绪（:1501、:1505）。

**读的副作用**：`hands.read_file` 把内容**压成一行并截到 120 字**（:116-121），因为换行会毁掉 TTS 断句。这意味着 `read_file` 对长文件**天然只能看到开头**——模型问「文件里写了什么」时拿到的是 120 字摘要，不是全文。这是设计，但容易让模型编后续内容。

---

## 七、审批机制

### 7.1 `pending` 表结构（gateway.py:630-637 + :666-669）

```sql
create table if not exists pending(
    session text primary key,   -- 全局只有 main 一个槽 → 全局只有 1 行
    action  text not null,      -- write_file / delete_file
    args    text not null,      -- JSON: {"path":..., "content":...}
    ts      real not null,      -- 挂起时刻
    origin  text)               -- 挂起来源键（pet / qq_private / qq_group:X）
```
`origin` 列是**后加的**（:666-669 幂等 `ALTER TABLE`）。

### 7.2 一次性语义

- `save_pending(session, action, args, origin)`（:813）：先**清过期**（`ts < now-300`），再查同 session 是否有未过期行——**有就返回 False，绝不覆盖**（:824-828）。注释记录了「先 delete 再 insert 会让主人对 A 说的『好』落地成 B」的历史事故。
- `peek_pending(session, origin)`（:837）：只看不清；过期当不存在；`origin` 给了就**只认同一来源**（:850-851，迁移前 origin 为空的行放行）。
- `take_pending(session, origin)`（:855）：`peek` 成功才 delete，**一次性**。
- TTL = `PENDING_TTL = 300`（:590）——「挂太久，主人过一会儿随口一句『好』就会误执行」。

### 7.3 确认 / 拒绝的判定

- 确认词 `CONFIRM_RE`（:544）：`好|好啊|好呀|可以|行|嗯|对|是的|确认|删|删吧|删掉|写|写吧|执行|干吧|ok`，**整句匹配**（`^\s*…\s*[。！!，,]?\s*$`），其余一律当取消。
- 确认前置条件（:1497）：`pending is not None and _ident.is_owner` —— **只有主人**这一句能决定做不做（群里谁接话都不算）。
- 命中确认 → `tools.confirm(action, args)`（tools.py:527）→ 写 2 条 `path="approve"` 记忆 → `status="executed"` 或 `"failed"` → **直接 return**（:1509-1526）。
- **不是确认词** → 这一句当「取消」，**pending 已被 `take_pending` 吃掉**，然后照常当新消息处理（:1527）。→ 主人说「等等」或「改一下」都会静默取消，且**没有任何提示**。
- 只有主人说话才会走到 `take_pending`；非主人连 pending 都看不到（`_ident.is_owner` 为假直接跳过 L1497）。

### 7.4 `[观望]` 与空回复兜底

| 场景 | 判定 | 行为 | 落记忆 |
|---|---|---|---|
| **观望** | `SILENT_RE`（:2459）`^\s*([\[【](?:观望\|SILENT\|silent)[\]】])\s*$` 匹配 `TAG_RE.sub("", reply).strip()`（:1571） | `status="silent"`，text 原样返回 | **什么都不写**（:1691 `if status != "silent"`）——「她没开腔，群友的闲话不该占记忆」 |
| **观望不被污染** | `_as_spoken`（:2334） | `silent` 直接 return，**不加 `[语音]`、不补句号** | — |
| **空回复** | `parse_reply` 洗完成空串（:1669） | 记 `empty_reply.log`（`in` 截 80 字，修过一次 248KB 日志，:1673-1680）→ 固定话术「嗯……我没太听明白」`status="model_empty"` | user 写、assistant 写（status≠ok 且≠approval → **assistant 不写**，:1696-1701） |
| **模型报错** | `reply is None and not tool_calls`（:1548） | 401/403 → `auth_error`「我这边连不上了，主人去后台换一下密钥」；其余第 0 轮重试 1 次，仍失败 → `model_error`「我这会儿有点卡」 | user 写、assistant 不写 |
| **轮数用尽** | `parsed is None`（:1653） | 先**不带工具**再问一次；仍失败才 `tool_overflow`「我想了想还是没弄好」 | user 写、assistant 不写 |

> 归纳：**只有 `ok` 和 `approval` 会写 assistant**。所以「她回了但没记」的四种状态（silent/model_empty/model_error/tool_overflow/auth_error/refused/approval_busy）在下一轮的历史里表现为「用户说了，她没答」。

---

## 八、鉴权与安全

### 8.1 token 从哪来

`_load_or_create_token()`（:74-99）三级：`GATEWAY_TOKEN` 环境变量 → `data/gateway.token`（内容 ≥16 字符才认）→ 新生成 `secrets.token_hex(24)`（48 hex）。写完调 `_harden_file_acl`（:110）：`icacls /inheritance:r` + 只授权 `当前用户:(R,W,D)` + `SYSTEM:(F)` + `Administrators:(F)`，**失败静默**。桌宠侧 `desktop\agent_bridge.py:25-43` 读同一个文件，`server.py:457-476` 也读它做代理。

### 8.2 哪些路由要求 token

| 路由 | 守卫 | 实测无 token |
|---|---|---|
| `GET /admin/model`、`/admin/keys`、`/admin/system-model`、`/admin/daily-review`、`/admin/system` | `_require_token`（:2667/2672/2678/2684/2688） | 401 |
| `POST /admin/keys`、`/admin/model`、`/admin/system-model`、`/admin/system` | `_require_token`（:2753/2770/2783/2792） | 401 |
| `GET /audit` | `_require_token`（:2724） | 401 |
| `POST /chat` | 手抄 `hmac.compare_digest`（:3024） | 401 |
| `POST /v1/chat/completions` | `_v1_guard`（回环 + JSON + **可选** token） | **200** |
| `GET /health`、`/capabilities`、`/v1/models`、`/models` | **无** | 200 |

### 8.3 跨站守卫覆盖范围

- **`gateway.py` 无 Origin / Host / Referer 校验**（grep 实测只命中 CORS *响应*头）。
- 跨站防护实际由三件事拼出来：① `/v1` 的 `Content-Type: application/json` 强制（浏览器跨站发 JSON 必预检，而 `do_OPTIONS` 只回 `ACAO: 127.0.0.1:8848`，别的源拿不到）；② `/chat`、`/admin/*` 的 token（浏览器拿不到文件）；③ `desktop\server.py` 自己的 Origin 守卫（不在网关）。
- 所以「跨站守卫覆盖范围」= **网关层 0 行**；能挡住跨站的是 token + content-type + CORS 白名单的副作用，不是显式校验。

### 8.4 旁路（真实存在的）

1. **本机任意进程可无 token 调 `/v1`**（实测 200）。代码自己在 :2562-2568 写明危害：白嫖主人的 key 跑任意 LLM，`# 任务说明`/`黑话` 裸子串直接命中 `call_model_raw` 原样转发，`max_tokens` 还能给到 1200。`_V1_REQUIRE_TOKEN` 默认关（:179-184），要闭环必须在 AstrBot provider 配 `custom_headers` 后才能打开。
2. **生产日志证实这条口被用过**：`v1.log` 943 行里 **`learn_task` 401 条（42.5%）**；`system.py:1963-1965` 的调查结论说「走 qq_unknown 的请求全部是 content 为字符串的 /v1 调用」。
3. **昵称伪造 → 主人权限**（P0-2，见下）。
4. **前缀匹配导致的路由误入**（`/chatbox` → `/chat`；`/healthxyz` → 200）。

### 8.5 P0-2：昵称等于「主人」就算主人【实测】

`Identity.is_owner`（:2201-2218）的最后一行 `return self.speaker == OWNER_LABEL`。
`speaker` 来自 `<system_reminder>` 里的 `Nickname:`，而**这个值是 QQ 群昵称/群名片，发送者自己能改**。

实测（隔离库，桩掉模型与 `hands`）：
```
群友 A 的 identity = Identity(uid='999999', speaker='主人',
                             channel='qq_group:示例群', trusted=True, pet=False)
  trusted = True   is_owner = True
第一轮 status = approval  | text = 要我把内容写进 note.txt 吗？回一个「好」我就写。
pending 表 = [('main', 'write_file', 'qq_group:示例群')]
第二轮（同一群友说「好」）status = executed | text = 写好了 note.txt。
实际执行了: [('write_file', {'path': 'note.txt', 'content': '群友写的内容'})]
```
即：**群友改昵称 → 拿到主人权限 → 让模型写文件 → 自己再说一句「好」就落地**，全程不需要真正的 master。
同一个伪造身份还能拿到：内心OS 注入、心境行、`consume_inner_os`、以及 `call_model`（花主人的钱）。

文档 `项目地图.md:1243-1257` 宣称 T-08 已修：「正文里写一句 `Nickname: 主人` 就能冒充」→ 修的是**正文（string 形式）**，**没修昵称（list 形式可信块）**。这是「文档声称 ≠ 代码事实」里最贵的一条。

**修法方向（不改行为也能修）**：`is_owner` 只认 `uid in OWNER_IDS`（生产已配 `<OWNER_UID>`），把 `speaker == "主人"` 这条降级为「仅当 uid 为空时用于桌宠/兼容」或直接删掉；`OWNER_LABEL` 应只作展示，不作协议（:2155 的注释自己就说了「展示给人看的措辞」和「机器判断的协议」不该同一个串）。

### 8.6 P0-1：生产 `role_model.json` 里躺着测试哨兵 key【实测】

生产文件 `gateway/data/role_model.json`（mtime 2026-11-05 14:20）：
```json
{"mode":"cloud","provider":"dashscope","base":"https://dashscope.aliyuncs.com/compatible-mode/v1",
 "model":"deepseek-v4.1-flash-1017","key":"sk-SHOULD-NOT-WRITE","timeout_s":120}
```
`_load_role`（:199-216）会把它并入 `ROLE`；`_role_key`（:306-324）**优先级 ① 就是 `role["key"]`**：
```
_role_key(ROLE) → 'sk-SHOULD-NOT-WRITE'（长度 19）
钥匙串 active  → 'sk-ws-…LfEg'（真 key，被完全屏蔽）
```
→ 角色层每次 `call_model` 都会发 `Authorization: Bearer sk-SHOULD-NOT-WRITE` → **全链路 401**。
**旁证**：`config.json` 的 `endpoint` 段仍是 `http://127.0.0.1:1234/v1` + `gemma-4-e4b`，而 `_save_role`（:223-245）会同步该段——说明这个文件**不是**通过 `_save_role` 写的，是某脚本直接写 `ROLE_PATH` 的【推断，写入者未能确认】。
**为什么日志里看不出来**：`v1.log` 最后一条是 2026-11-05 12:45，早于该文件 mtime，所以还没有「上线后 401」的痕迹。**下次启动就会全哑**。
**修复**：删掉 `key` 字段（或整份删掉，让它回落 `config.json`），或走一次后台「试连并切换」——`_apply_role` 的 candidate 里 `key` 恒为 `""`（:1844），会自然洗掉它。
> ⚠ 同一形态在系统层也中招（不属本报告范围，交系统层审计）：`data/system_model.json` 里 `"key": "sk-TESTSECRET-should-not-be-written"`。

---

## 九、渠道标签与 `hide_private`

### 9.1 标签怎么生成

`_channel_label(group, uid, wake_ctx)`（:2092-2107）：

| 输入 | 输出 |
|---|---|
| `group` 非空 | `群聊·{group}` |
| `wake_ctx=True`（插件塞了群聊前文） | `群聊` |
| `uid` 非空 | `私聊` |
| 都没有（桌宠那条路） | `""` |

`migrate_channel.channel_key(label, local)`（migrate_channel.py:133-162）折成协议键：

| 标签 | local | 键 |
|---|---|---|
| 任意 | `True` | `pet` |
| `""` | `False` | `qq_unknown`（**不可信，fail-closed**） |
| `私聊` | False | `qq_private` |
| `群聊·X` | False | `qq_group:X` |
| `群聊` | False | `qq_group` |
| 其它 | False | `qq:{标签}`（认不出 → 走过滤分支 → 只读同键） |

**说话人前缀**在 `handle_chat` 拼（:1475-1476）：`tag = f"[{channel}] "`，`spoken = f"{tag}{speaker}: {text}"`（没 speaker 就 `f"{tag}{text}"`）。生产库里的样子：`[私聊] 主人: 记好了，是11月1号`、`[群聊·示例群] 群友甲: …`、桌宠是无前缀的 `主人: …`（因为 `channel=""`）。
**主人前缀**：`/v1` 里若 `identity.is_owner and identity.speaker` → `speaker = OWNER_LABEL`（:2929-2930）。⚠ 但随后 `handle_chat` 会被 `identity` **覆盖回去**（:1449 `speaker = identity.speaker`），所以**这个改写实际无效**——库里存的是昵称（如「主人」），不是「主人」（实测：`[私聊] 主人: 在吗`）。

### 9.2 `hide_private` 怎么防止群友看到主人的私有工具记录

两层（都在 `recent_turns`）：
1. **路径过滤**（:764-765）：`hide_private` 时 `coalesce(path,'') not in ('tool','approval','approve')` → 主人的工具往返、审批话术、审批记录**整段不出现**。
2. **来源过滤**（:798-806）：`need_filter = is_group_channel(key) or hide_private or not is_known_source(key)` → 命中就 `channel = ?` **等值**匹配 → 群聊只读本群。

【实测】群友在群里请求看到的只有本群那一行；桌宠的私房话、主人的工具往返、QQ 私聊全部不可见。

**但有一个反方向的洞（P1-2）**：`hide_private` 的判定是 `speaker not in ("", OWNER_LABEL)`（:1144），**不看 `Identity.is_owner`**。主人用 QQ 时昵称往往不是字面「主人」（生产里就是「主人」），而 uid 命中了 `owner_ids` → `Identity.is_owner=True` 但 `hide_private=True`：
- 主人的工具往返被滤掉（他自己看不到自己刚批过什么）；
- 来源过滤被强制成 `qq_private` → **桌宠里说的话他读不到**。
【实测】`Identity(speaker='主人', uid=<OWNER_UID>)` → 只看到 `[私聊] 主人: 只有私聊的这句`；`speaker='主人'` 的旧路能看到全部 4 条。
方向是 fail-closed（不泄漏），所以是**功能/一致性缺陷**而非安全洞，但它直接违反 `build_messages` docstring（:1106「speaker: 说话人。不是主人时，历史里会滤掉主人的文件操作」）。

---

## 十、`role_speak.py` 与 `system.py` 的接口契约

### 10.1 `maybe_speak` 入参 / 出参

```python
maybe_speak(signals: dict, st: dict) -> tuple[dict | None, str]     # role_speak.py:106
```
- **`signals`**（系统层 `_tick_body` 构造，system.py:1493-1502）：`now` / `weekday` / `seconds_since_last_turn` / `recent_chat`(5 条) / `recent_chat_age_minutes` / `change`。
- **`st`**：`system.load_state()` 的结果（心跳里刚 save 过的那份）。
- **返回 `(rendered, status)`**，`status ∈ {`"spoken"`, `"declined"`, `"parse_failed"`, `"delivery_failed"`}`（role_speak.py:109-119）。`rendered` = `parse_reply` 的产物（`text`/`emotion`/`tone`/`sticker`/`meme_markers`/`voice`）。
- 调用方（system.py:1517）按 `rendered` 是否为真来判定 `spoke`，并写 `st["emotion"]` / `st["focus"]` / `last_speak_ts` / `speak_count_today`。

### 10.2 它读了哪些系统层状态（逐条）

| 读什么 | 行号 | 用途 | 失败处理 |
|---|---|---|---|
| `system.people_prompt_line("pet")` | role_speak.py:129 | 当前场合人物画像 | 异常吞掉 → `""` |
| `system.recent_care_hint("pet")` | :138 | 「上次关心被拒过就别重复」 | 异常吞掉 → `""` |
| `_mood_line(st)` → **`mood.prompt_line(state)`** | :161-165、:63-103 | 此刻心情 | 异常吞掉 → `""` |
| `st["mood_updated_ts"]` | :92-102 | 判断要不要退回 `system.load_state()` | ≤0 时重读一次 |
| `gateway.current_persona()` | :168 | 人格 | 同被动侧 |
| 写：`system.record_care_given(rendered, channel="pet")` | :196 | 记 care_given 事件 | 异常吞掉 |
| 写：`system._audit({...})`（经 `deliver_to_pet`） | :39-59 | 投递成败留痕 | — |

**注意：主动说话走的是 `gateway.call_model`（:182）**，也就是用**角色层**的模型与 key（`ROLE`），不是系统层的。

### 10.3 是否读 `mood_*`？—— 文档说「没接上」，**代码已经接上了**

- 【文档】`项目地图.md:174`：「⚠ 它读的还是旧 `st["emotion"]`，**不读 `mood_*`**」
- 【文档】`项目地图.md:676`：「`role_speak.maybe_speak()` → 拼 prompt 时读的是 **`st.get('emotion')`**（旧枚举）」
- 【文档】`项目地图.md:1429`：「主动说话不读新心情」
- 【代码事实】**工作区版本已经改了**（`git diff gateway/role_speak.py` 有 +68 行未提交）：新增 `_mood_line()`（:63-103），`maybe_speak` 里 `mood_line = _mood_line(st)`（:161），拼 prompt 时用 `+ mood_line` 取代 `f"你现在的心情：{st.get('emotion')}"`（:174）。
- 且**是有意不调 `system.mood_prompt_line()`**：那个函数会额外注入 `focus`（「你心里还挂着刚才那句：…」）和「N 分钟前刚主动找他说过话」（system.py:3623-3634），主动说话注入 focus 会让她复读自己（role_speak.py:73-77 有完整理由）。`mood.prompt_line()` 只读四字段（mood.py:355-379），且**平静时也返回一行**、不会空串。
- **结论**：文档描述的是 **HEAD（已提交版本）**，不是工作区现状。三条文档全部过期 → 见 §12。

### 10.4 契约风险

- `consume_inner_os` 的 `limit` 默认 0，`build_messages` 显式传 `inner_os_max_per_turn`（:1158），**但只传了位置参 `cur_chan`**，`limit=` 是关键字——OK。
- `mood.prompt_line` **永不返回空串**（平静也有一行），而 `system.mood_prompt_line` **平静且无 focus 时返回空串**（system.py:3614-3616）。两条路对「平静」的处理不同**是有意的**，但两个同义函数行为相反，是后来者最容易踩的坑（P2）。
- `role_speak` 通过 `import gateway` 反向依赖角色层主模块（:19/:121），而 `gateway` 在 import 期就读 `config.json`/`persona.md`/token 并可能建库 —— 循环依赖目前靠「函数内 import」规避（:19/:121/:88-89/:195），脆弱但可用。

---

## 十一、实测快照

### 11.1 规模

| 指标 | 值 | 来源 |
|---|---|---|
| `gateway.py` | **3175 行 / 147041 字节 / 纯 LF** | Python `count(b"\n")` |
| 顶层函数 | **72 个** + 2 个类（`Identity` 4 方法、`Handler` 8 方法），函数+方法合计 **84** | AST |
| 路由条件 | **15 处 `self.path` 判定**（GET 9 + POST 6）+ 2 个 404 + OPTIONS = 18 个终点；`/admin/system` 内再 4 个 action | AST + read |
| `/v1` 分支 | 7（+ 守卫 + stream 子分支 + 500） | §3 |
| 工具 | 6 个 schema，写删 2 个挂审批 | tools.py |
| 模块级常量 | **61 个全大写常量**，其中 **`EMOTIONS`（:511）本文件零使用** | tokenize 扫描 |

### 11.2 测试

`pytest gateway/tests -q` → **1012 passed, 1 skipped**（80.99s）。角色层直接相关文件全部绿：
`test_identity.py` / `test_channel_isolation.py` / `test_pending_origin.py` / `test_tool_overflow.py` / `test_hands.py` / `test_model_errors.py` / `test_role_speak.py` / `test_tools_json_body.py` / `test_image_forward.py` / `test_image_notes.py` / `test_m0_p0p1_20261103.py` / `test_extract_prompt.py` / `test_recall_guard.py` / `test_people_prompt.py` / `test_inner_os_channel.py` / `test_key_probe.py` / `test_system_admin.py` / `test_memory_task_shortcircuit.py` / `test_people.py`。
**注意**：1012 条全绿**测不出**本报告的 P0-2（昵称伪造）——`test_identity.py:32-41` 用的正是 `Nickname: 主人` 且断言 `is_owner is True`，**测试把漏洞钉成了预期行为**。

### 11.3 生产数据（只读）

| 表/文件 | 事实 |
|---|---|
| `turns` | 604 行，session 全为 `main` |
| `channel` 分布 | `qq_private` 346 / `qq_group:示例群` 205 / `qq_unknown` **27** / `NULL` **17** / `pet` **5** / `qq_group` 4 |
| `path` 分布 | `chat` 574 / `tool` 27 / `approve` 2 / `approval` 1 |
| `internal` / `system_log` | **各 0 条**（已清空） |
| `pending` | 0 条 |
| `events` / `schedules` | 21 / 1 |
| 「user 后面紧跟 user」 | **11 行**（`chat`，私聊 4 / 群 4 / 桌宠 3）→ 问了没答 |
| `v1.log` | 943 行：`learn_task` **401** / `ok` 307 / `meta` 185 / `model_empty` 31 / `model_error` 9 / `silent` 7 / `approval` 1 / `executed` 1 / `memory_task` 1 |
| `audit.jsonl` | 411 行：`ok` 360 / `model_empty` 31 / `model_error` 9 / `silent` 7 / `tool_overflow` 2 / `approval` 1 / `executed` 1 |
| `errors.log` | 10 条，**9×HTTP 401 + 1×503**，全在 10-29～11-01 |
| `system_state.json` | **`mood_*` 四字段已落盘**（`mood_value=0.0` / `平静` / 空理由 / ts=11-07 14:43） |
| `role_model.json` | **含哨兵 key**（P0-1） |
| `persona_sync.json` | `db` 与 `file` hash 一致（`b21841dd…`）→ 已对齐 |

### 11.4 死代码 / 冗余（P2）

| 符号 | 位置 | 判定 |
|---|---|---|
| `EMOTIONS` | gateway.py:511 | 本文件**零使用**（`system.EMOTIONS` 是另一份，system.py:95 用了） |
| `is_owner(speaker, local_pet)` 模块级函数 | gateway.py:2127 | 生产代码**零调用**，只有测试用；已被 `Identity.is_owner` 取代 |
| `_extract_meta_blocks` | gateway.py:2068 | 仅 `_TRUST_STRING_IDENTITY` 分支（**默认关**）里用到 → 默认配置下等价死代码 |
| `hands.execute` | hands.py:171 | 只有测试调用（test_hands.py:387） |
| `hands.READ_ACTIONS` / `WRITE_ACTIONS` | hands.py:156-157 | **零使用** |
| `schemas/fact.json` / `frame.json` | — | 无人读取（文档自己也说可删） |
| 重复启动打印 | gateway.py:3164-3165 | 两行内容完全相同 |
| `/chat` 手抄 token 校验 | gateway.py:3024-3027 | 与 `_require_token` 重复，401 响应形状还不一致 |
| `migrate_channel.is_local_trusted` | 已删（git diff） | 本次工作区删除，无引用残留 ✅ |

---

## 十二、问题清单

### P0（会让功能整体失效 / 可被利用拿主人权限）

| ID | 问题 | 证据 | 影响 |
|---|---|---|---|
| **P0-1** | 生产 `data/role_model.json` 含 `"key":"sk-SHOULD-NOT-WRITE"`，而 `_role_key` **优先级 ① 就是 `role["key"]`** | gateway.py:306-324（`if role.get("key"): return`）、:199-216；实测 `_role_key` 返回该串、真 key 被屏蔽 | 角色层下次启动**全线 401**（`auth_error` 固定话术），QQ/桌宠同时哑。目前 `v1.log` 最后一条早于该文件 mtime，所以尚未暴露 |
| **P0-2** | `Identity.is_owner` 仍以 `speaker == "主人"` 收尾；`speaker` 来自可被发送者自行设置的 QQ 昵称 | gateway.py:2218；实测伪造身份 → `is_owner=True` → `approval` → 自己说「好」→ **写文件落地** | 群友改昵称即可：写/删文件（sandbox 内）、读内心OS/心境、花主人 key。文档宣称 T-08 已修，实际只修了正文伪造 |

### P1（功能/隔离实质受损，但不至于全线瘫）

| ID | 问题 | 证据 |
|---|---|---|
| **P1-1** | **跨渠道审批互锁**：`pending` 表 PK 是 `session`（全局只有 main 一行），`peek/take` 按 origin 匹配 → 桌宠挂起一件后，QQ 私聊主人既**确认不了**也**挂不了新件**，互锁 300 秒，且**没有任何「去另一个渠道确认」的提示** | gateway.py:630-637、:813-863、:1496-1498；实测：QQ 私聊说「好」→ `status=ok`（当普通聊天），待办仍在；再挂新件 → `approval_busy` |
| **P1-2** | **`hide_private` 把主人当陌生人**：判定用 `speaker not in ("", OWNER_LABEL)`，不看 `is_owner`。主人 QQ 昵称不是字面「主人」时（生产就是「主人」）→ 自己的工具往返被滤、桌宠上下文读不到 | gateway.py:1144；实测：`speaker='主人'+uid 命中 owner_ids` → 只看到 1 条私聊，桌宠那 2 条不可见 |
| **P1-3** | **`approval_busy` 的回复不落库**：status 既非 `ok` 也非 `approval`，assistant 分支不写 → 下一轮模型看到「主人说了要求、她一个字没回」 | gateway.py:1638-1645 与 :1696-1701；实测 turns 只有 1 条 user |
| **P1-4** | **`qq_unknown` 不是「什么都看不到」，而是「桶内全部互相可见」**：`channel = 'qq_unknown'` 是等值匹配，所以任何认不出来源的调用者都能读到别人的 `qq_unknown` 行 + 插件注入的 `[系统任务：主动对话]` | gateway.py:798-806；migrate_channel.py:17-18/92 注释宣称「过滤时什么都看不到」；实测 3 行互相可见；生产该桶有 27 行 |
| **P1-5** | `/v1` 无 token 即可用主人的 key（已知设计缺陷，非回归） | gateway.py:179-184、:2562-2568；实测 200；`v1.log` 里 401 条 learn_task 说明这条口被大量使用 |
| **P1-6** | `read_file` 把内容压成一行并**截到 120 字**，模型问「文件里写了什么」只能拿到开头 120 字 → 容易编造后续 | hands.py:116-121 |

### P2（健壮性 / 一致性 / 卫生）

| ID | 问题 | 证据 |
|---|---|---|
| P2-1 | 路由用 `startswith` 前缀匹配：`/chatbox` 误入 `/chat`、`/healthxyz` 返回 200；`/v1` 用 `endswith`，任意前缀都收 | 实测；gateway.py:2633/3019/2820 |
| P2-2 | `do_POST` **先解析 body 后鉴权**，且无 body 大小上限（desktop 有 8MB） | gateway.py:2744-2749 |
| P2-3 | 流式 `/v1` 自己写 `ACAO: *`，绕过 `_send`，与其余路由的 `ACAO: 8848` 不一致 | gateway.py:2993 vs :2618 |
| P2-4 | `/chat` 手抄 token 校验，401 形状与 `_require_token` 不一致 | gateway.py:3024-3027 vs :2595-2608 |
| P2-5 | `MAX_TOOL_ROUNDS` 是 import 期常量，改 `config.json` 必须重启（文档未提示） | gateway.py:588 |
| P2-6 | 「不是确认词 = 取消」：主人说「等等/改一下/不对」会**静默丢弃**待办且不提示 | gateway.py:1527-1528 注释 |
| P2-7 | `mood.prompt_line`（永不空）与 `system.mood_prompt_line`（平静可空）语义相反，命名极近 | mood.py:355-379 vs system.py:3614-3636 |
| P2-8 | 死代码 9 处（见 §11.4） | — |
| P2-9 | 启动打印两行完全重复 | gateway.py:3164-3165 |
| P2-10 | `_note_reply_mood` 在 `approval` 状态下也记心情，而审批话术是固定模板（情绪恒 `calm`）→ 每批一次就注入一个「平静」锚点，可能稀释真实心情 | gateway.py:1385（`status in ("ok","approval")`）、:1649-1651 |
| P2-11 | `recent_turns` 里两处 `sqlite3.connect(DB_PATH)` **没有 busy_timeout**（只有 `_connect_rw` 有） | gateway.py:782、:808 |

---

## 十三、文档不一致（文档声称 vs 代码事实）

| # | 文档位置 | 文档说 | 代码事实 | 性质 |
|---|---|---|---|---|
| 1 | **本次审计任务书**（非项目地图） | `gateway.py` **2590 行** | **3175 行**（147041 字节）。项目地图本身**没有**写过行数，这个数字来自任务书的转述 | **行数假象**：PS 5.1 `Get-Content` 按 ANSI 读 UTF-8 文件时少算 585 行（同一文件 `-Encoding utf8` 读就是 3175） |
| 2 | `项目地图.md:268` | 「跑工具（**最多三轮**）」 | `MAX_TOOL_ROUNDS` 默认 **4**（`dialog.max_tool_rounds`，夹 [1,8]），另有 1 次收尾 → 最坏 5 次调用 | 文档落后于工作区（L544-588 是 11-07 的改动） |
| 3 | `项目地图.md:174` | `role_speak` 读旧 `st["emotion"]`、**不读 `mood_*`** | 工作区已改：`_mood_line()` 读 `mood.prompt_line()`（四字段） | 文档落后（未提交改动） |
| 4 | `项目地图.md:676` | 同上（坑 #63 正文） | 同上 | 文档落后 |
| 5 | `项目地图.md:1429` | 「主动说话不读新心情」 | 同上 | 文档落后 |
| 6 | `项目地图.md:317` | 「`mood_*` 四个键**生产上至今一个都没有**」 | `system_state.json` 里四字段**已落盘**（ts 2026-11-07 14:43） | 文档落后（`load_state` 迁移即时落盘已修，system.py:541-561） |
| 7 | `migrate_channel.py:17-18` 注释 / `:90-92` | `qq_unknown`「**过滤时什么都看不到**」 | 是 `channel = 'qq_unknown'` 等值匹配 → **桶内互相可见** | 注释与实现不符（P1-4） |
| 8 | `gateway.py:1106`（`build_messages` docstring） | 「`speaker`: 不是主人时，历史里会滤掉主人的文件操作」 | 只用 `speaker` 字符串判，`Identity.is_owner=True` 但昵称≠「主人」时**主人也被滤** | 实现与文档意图不符（P1-2） |
| 9 | `项目地图.md:1243-1257`（T-08） | 「正文里写 `Nickname: 主人` 就能冒充」**已修** | 正文（string）确实 fail-closed；但**可信块（list）里的昵称仍算主人** | 文档只覆盖了一半（P0-2） |
| 10 | `项目地图.md:180` | 「`hands.py` **四道闸**」 | 实际四道闸 + realpath 防链接 + **ADS 冒号第五道**（hands.py:93-97） | 文档少算一道 |
| 11 | `项目地图.md:1235` | 「`/admin/*` **6 条**以前无 token」 | 现在 **9 条** admin 路由（GET 5 条 + POST 4 条）全部要求 token（实测无 token 全 401） | 历史计数 vs 现状，不算错 |
| 12 | `项目地图.md:303` | `hide_private` 让「群友看不到主人在桌宠这边调过什么工具」 | 成立 ✅ 但同一机制**也让主人看不到**（见 #8） | 描述不完整 |

---

## 十四、待核实（不确定，不编）

1. **P0-1 的写入者是谁**：`role_model.json`（mtime 11-05 14:20）不是 `_save_role` 写的（否则 `config.json` 的 `endpoint` 段会被同步，实测没同步）。全仓搜 `SHOULD-NOT-WRITE` 只命中这一个文件，git 历史里也没有。**推测**是 11-05 那次「后台改模型回写 config.json」开发时的手工探针写了裸 `ROLE_PATH`，但**未能确认**。
2. **AstrBot 的 `<system_reminder>` 里 `Nickname:` 到底取自哪个字段**（QQ 群名片 / 好友备注 / 账号昵称）：决定 P0-2 的利用门槛。网关侧只能看到「这个值客户端可改」这一点；需要在 AstrBot 侧确认（`qq/astrbot/` 是 venv 第三方目录，本报告未展开）。
3. **`qq_unknown` 的 27 条生产数据里有多少属于主人**：若是主人直连 `/v1`（string 形式），则 P1-4 的「桶内互相可见」影响面更大。需要逐条比对 uid 才能定论。
4. **`_note_reply_mood` 与系统层「每日梳理」是否重复写心情**：本报告只确认了被动路径会写；梳理/巩固路径是否也调 `record_mood_event` 未逐条核实（属系统层范围）。
5. **`approval` 挂起后 `used_tools` 已经包含工具名 → `path="tool"`**，而 `reason` 只记工具名不记动作（"write_file"），审计里分不清「问了」和「做了」——是否需要区分是产品决策，未定。
6. **`persona_sync` 的双向写**在 AstrBot 库被占用时是否真的「只打日志不抛」：代码有 try/except（persona_sync.py:110-112、:206-209），但未在锁竞争下实测。
7. **`/v1` 的 `identity` 与 `_openai_user_text` 各解析一遍**（:2831 与 :2832）：目前两次结果一致，但将来若只改一处会静默分叉。属结构性隐患，未观察到实际故障。
8. **`system_state.json` 里 `focus` 字段含非法代理字符**（实测末尾出现 `\udc80\udc82`）：`json.load` 能读（Python 容忍），但它是 `_tick_body` 从模型正文 `[:40]` 截断产生的半个 UTF-8 字符。是否会写坏下游未验证。

---

## 附录 A：本次隔离实验清单

| 脚本 | 验证什么 | 结论 |
|---|---|---|
| `tmp/_audit_deadscan.py` | 行数 / 死符号 / 路由数 | §11.1、§11.4 |
| `tmp/_audit_dbstats.py` | 生产库只读统计 | §11.3 |
| `tmp/_audit_dangling.py` | 「user 后紧跟 user」统计 | 11 行 |
| `tmp/_audit_logtime.py` | 日志时间分布 vs role_model mtime | P0-1 尚未暴露 |
| `tmp/_audit_final_numbers.py` | 生产快照终值 | §11.3 |
| `tmp/_audit_http_probe.py` | 进程内随机端口 Handler，鉴权矩阵 | §2-B、§8.2、§8.4 |
| `tmp/_audit_exp_hide_private.py` | `hide_private` 与来源隔离 | P1-2 |
| `tmp/_audit_exp_owner_forge.py` | 昵称伪造 → 读全部主人记忆 | P0-2 |
| `tmp/_audit_exp_group_owner.py` | 群聊昵称伪造 → 写文件 + 自确认 | P0-2 |
| `tmp/_audit_exp_busy3.py` | 跨来源 approval_busy | P1-1、P1-3 |
| `tmp/_audit_exp_crossorigin.py` | 跨渠道确认失败、待办不被清 | P1-1 |
| `tmp/_audit_exp_unknown_bucket.py` | qq_unknown 桶内可见 | P1-4 |

> 全部脚本用 `GATEWAY_DATA_DIR=<tempfile.mkdtemp()>` 隔离；`init_db()` 只建临时库；未绑定 8898；未触碰 `gateway/data/`（除只读统计）。

## 附录 B：关键常量一览（角色层）

| 常量 | 值 | 位置 | 作用 |
|---|---|---|---|
| `MAX_TOOL_ROUNDS` | 4（配置夹 [1,8]） | :588 | 工具循环轮数 |
| `PENDING_TTL` | 300s | :590 | 审批有效期 |
| `dialog.history_turns` | 30（条消息） | config.json:11 | 历史窗口 |
| `dialog.max_sentences` | 3 | config.json:10 | 普通回复句数上限 |
| `RECITE_MAX_SENTENCES` | 16 | :2262 | 背诵/朗读放宽 |
| `LIVING_MEMORY_MAX` / `MEME_PROMPT_MAX` / `RECALL_BLOCK_MAX` | 4000 / 3000 / 2000 | :1063-1067 | 注入块上限 |
| `OWNER_IDS` | `{"<OWNER_UID>"}` | :2123 / config.json:14-16 | 主人 QQ 号 |
| `OWNER_LABEL` | `"主人"` | :2124 | 展示标签（**被当协议用了，见 P0-2**） |
| `PENDING` 一次性语义 | 同 session 已有未确认 → 不覆盖 | :813-834 | — |
| `hands.roots` | `["sandbox"]` | config.json:19-21 | 唯一可写范围 |
| `hands.write_ext` | `.txt .md .json .log .csv` | config.json:37-43 | 写白名单 |
| `hands.max_read_bytes` | 200000 | config.json:44 | 读上限 |
| `_V1_REQUIRE_TOKEN` | False（默认） | :179 | /v1 是否强制 token |
| `_TRUST_STRING_IDENTITY` | False（默认） | :2046 | string 形式是否解析身份 |
