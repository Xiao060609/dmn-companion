# 对话网关

一个模型：读人格、自己决定要不要用工具、把结果说成人话。
代码只守边界（路径、扩展名、写删确认），不猜用户在说什么。

## 跑起来

```bash
python gateway.py            # 或者 ./start.cmd / ./start.sh
```

默认监听 `http://127.0.0.1:8898`。只需要 Python 3.9+，**没有任何第三方依赖**。

停止：`./stop.cmd` / `./stop.sh`（按 `data/gateway.pid` 找进程；Windows 上直接关窗口带不走
Python 进程，所以别用关窗口的方式停）。

## 一次请求发生了什么

```
用户说话
  ↓
拼提示词（人格 + 最近 12 轮记忆 + 这句）
  ↓
模型 ──要动手？──是──► 执行工具 ──► 结果塞回对话 ──► 模型接着说（最多三轮）
  │
  └──不用──► 直接回话
  ↓
清洗（标点、emoji、情绪标记、句数）──► 返回
```

工具是标准 function calling，模型看到的是 `tools.py` 里那份声明。
模型说「我查一下」却不调工具，就没下文了——所以提示词里明确要求它直接动手。

## 记忆

桌宠和 QQ 共用**一份**记忆（`session='main'`），不是两套。区分靠每句话前面的说话人标签：

```
主人: 我叫阿晓，喜欢喝绿茶          ← 桌宠里，本机的那个人
小林: 刚才那个人喜欢喝什么         ← QQ 上的群友
```

「主人」是在自己电脑上聊天的那个人，其他名字都是 QQ 上的。人格里写清楚了：
谁说的回应谁，别人的事别当成主人说的，主人私下讲的事也不许讲给别人听。
实测——群友问「刚才那个人喜欢喝什么」，回的是「这个不方便讲啦，别人的事我不能随便说」。

窗口是 12 轮。群聊很热闹的时候，主人自己的对话会被顶出去——这是共用一份记忆的代价，
要缓解就调大 `config.json` 里的 `dialog.history_turns`。

## 群聊唤醒窗口

`astrbot_plugin_group_wake_window` 会在唤醒时把前 10 条群聊和一份「什么时候该说话」的规则
拼进用户消息。网关对这两样东西各有处理：

| 插件塞进来的 | 网关怎么处理 | 为什么 |
|---|---|---|
| `[群聊上下文…]` + 前 10 条 + `[群聊发言规则]` | 拆出来，只当**这一轮**的系统消息，不写记忆 | 否则每次唤醒都往记忆里灌 10 条群聊，把真对话挤没 |
| `[观望]`（表示不回） | **原样返回**，不加语音前缀、不补句号 | 插件靠这个标记决定闭嘴；动了它就变成把「观望」两个字发到群里 |

第二条踩过坑：以前网关会把 `[观望]` 清洗成「观望。」再套上 `[语音]`，插件认不出来，
直接把「观望」念给全群听。

改过插件的 `inject_header` 的话，`WAKE_CONTEXT_MARKERS` 里也要跟着加一条，否则拆不干净。

## 工具

| 工具 | 做什么 | 谁把关 |
|---|---|---|
| `get_weather` | 今天/明天某城市天气 | wttr.in，缓存 10 分钟 |
| `list_dir` | 看文件夹里有什么 | L0 路径检查 |
| `read_file` | 读文本文件 | L0 路径检查 + 200 KB 上限 |
| `write_file` | 写文件 | L0 + **人工确认** |
| `delete_file` | 删文件 | L0 + **人工确认** |

时间不占工具：每次请求都把当前时间紧贴着用户那句话给模型，并声明「忽略对话里的旧时间」。
实测过——时间只写在人格里没用，历史里一旦有过旧答案，它就会顺着旧答案编（报过 2 月 18 日）。

`hands.py` 是真正碰文件的那一层，四道闸写死在代码里，不经过模型判断：

1. **roots 白名单**——默认只有 `sandbox/`，越界直接拒，连审批都不弹
2. **敏感路径黑名单**——`.ssh`、`id_rsa`、`.env`、`secrets.json`、`.git`、`AppData` 等
3. **写扩展名白名单**——只允许 `.txt .md .json .log .csv`，不能写 `.exe/.bat/.ps1`
4. **读取上限**——超过 200 KB 不读

## 审批

写和删不会直接执行。模型调了工具之后，网关把动作挂起，回一句代码生成的话：

> 要我把 note.txt 删掉吗？回一个「好」我就删。

你回「好」→ 执行；回别的 → 当取消，并按新消息处理。审批只挂起一次。

**只有主人能点头。** 记忆是共用的、群里谁都能接话，所以这两道闸都按说话人算：

| 场景 | 结果 |
|---|---|
| 主人说「好」 | 执行 |
| 群友说「好」 | 不算数，那条待审批还挂着 |
| 群友让她写 / 删 | 直接拒：「这种事得主人自己开口，我不能听别人的。」 |

「主人」的认定见 `config.json` 的 `dialog.owner_ids`（QQ 号），桌宠那边固定是主人。

## 换模型（后台里点一下就行）

桌宠后台（`http://127.0.0.1:8848`）右侧有「对话模型」卡片：

| 项 | 说明 |
|---|---|
| 本地 / 云端 | 用哪个模型，切换即时生效 |
| 服务商 | 本地（LM Studio）· 阿里云百炼 · DeepSeek · OpenAI · 自定义 |
| 服务地址 / 模型名 | 选服务商会自动填地址，模型名自己填 |
| API Key | 留空表示不修改；百炼会复用 `secrets.json` 那把 key |

每次切换都会**先试连一次**，连不上就不动当前配置，并告诉你错在哪。

> **工具只在云端模式下发送。** 本地小模型对 function calling 支持不稳，
> 切到本地就退化成纯聊天（问时间天气它只能瞎说），想用工具就留在云端。

对应接口（跨端口，已开 CORS）：

```
GET  /admin/model   当前配置（key 只返回掩码）
POST /admin/model   {mode, provider, base, model, key}
```

配置存在 `data/role_model.json`，重启后仍然生效。

## 接口

> ⚠ **2026-11-04 起，大部分接口要带 token。** 详见下面「鉴权」。

| 方法 | 路径 | 要不要 token | 说明 |
|---|---|---|---|
| POST | `/chat` | **要** | `{"text": "...", "session": "desk"}` → 回复 + 决策 |
| POST | `/v1/chat/completions` | 可选¹ | OpenAI 兼容（AstrBot 等客户端直接用） |
| GET | `/v1/models` | 不要 | 模型列表（假的，给客户端识别用） |
| GET | `/health` | 不要 | 存活与当前模型 |
| GET | `/capabilities` | 不要 | 协议能力声明 |
| GET | `/audit` | **要** | 最近 20 条决策记录 |
| GET/POST | `/admin/model` | **要** | 查看/切换模型 |
| GET/POST | `/admin/keys` | **要** | 钥匙串（多把 key 增删切） |
| GET/POST | `/admin/system` | **要** | 系统层配置 + 体检 |
| GET/POST | `/admin/daily-review` | **要** | 每日梳理期配置 / 手动触发 |

¹ `/v1` 默认**不强制** token，因为 AstrBot 的 provider 目前没配
`custom_headers`，一上来就强制会让 QQ 直接哑掉。现状防线是
「本机回环 + 强制 JSON」（挡跨站与远程，挡不住本机其它进程）。
要闭环：给 AstrBot provider 加
`custom_headers: {"X-Gateway-Token": "<token>"}`，再设
`GATEWAY_V1_REQUIRE_TOKEN=1`。

## 鉴权

**token 在哪**：`gateway\data\gateway.token`，首次启动自动生成（32 字节 hex），
已在 `.gitignore` 里，不进仓库。

**怎么带**：请求头 `X-Gateway-Token: <token>`。

**谁读它**：
- 桌宠的 `agent_bridge.py`（本机进程，直接读文件）
- 后台控制台（浏览器**不持有** token：所有要进管理接口的请求都由
  8848 的 `server.py` 代转，token 只在服务端读、只在服务端用）

**手工调接口**：

```powershell
$tok = (Get-Content <repo>/gateway\data\gateway.token -Raw).Trim()
curl.exe -H "X-Gateway-Token: $tok" http://127.0.0.1:8898/admin/system
```

无 token 时 `/admin/*`、`/audit`、`/chat` 一律 **401**。

⚠ **别把 token 写进前端代码或日志**。它等价于「本机桌宠」这个身份，
拿到就能以主人名义调写删接口。

`/chat` 的返回：

```json
{
  "text": "凌晨十二点十三分啦，星期四了。",
  "emotion": "calm",
  "sticker": "平静",
  "path": "tool",
  "reason": "get_time",
  "status": "ok",
  "latency_ms": 1489,
  "fact": null
}
```

## 接到别处

**桌宠**：`dmbrowser/agent_bridge.py` 里的 `ChatAgent.chat()` 就是这个网关的客户端。
`server.py` 懒加载它，**改完要重启桌宠**。

**QQ（AstrBot）**：服务商 `api_base` 指到 `http://127.0.0.1:8898/v1`，key 随便填。
网关会顺手处理三件 AstrBot 特有的事：

- 剥掉插件拼在消息后面的提示词（`[梗百科…]`、`[语音能力]`、`<system_reminder>`）
- 插件的内部分类请求（「请分析以下用户消息属于什么类型的交互行为」）回一个枚举词，
  不进对话记忆
- 短句回复前加 `[语音]` 标记，让语音插件合成语音

## 可移植性

换一台电脑时**不需要改代码**，只改配置或设环境变量：

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `GATEWAY_HOST` | 监听地址 | `127.0.0.1` |
| `GATEWAY_PORT` | 监听端口 | `8898` |
| `GATEWAY_MODEL_BASE` | 模型服务地址 | `http://127.0.0.1:1234/v1` |
| `GATEWAY_MODEL` | 模型名 | `gemma-4-e4b` |
| `GATEWAY_DATA_DIR` | 数据目录（数据库 + 审计） | `./data` |
| `DASHSCOPE_API_KEY` | 百炼密钥 | 读 `secrets.json` |

约束：

- 代码里没有任何绝对路径，全部相对 `__file__`。
- 只用标准库（`http.server` / `urllib` / `sqlite3`），Windows / Linux / macOS 都能跑。
- 文件读写一律显式 `utf-8`，控制台启动时重设为 UTF-8。

## 行为约束

| 约束 | 怎么保证 |
|---|---|
| 只说 1–3 句 | 提示词 + 代码截断 |
| 标点只允许 `，。？！` | `_normalize_punct` 转全角、删非法符号 |
| 不用 emoji | 提示词 + 正则清除 |
| 情绪标记 | 解析 `{happy}` 等七种，缺省 `calm` |
| 空回复 | 兜底一句话，绝不给调用方空字符串 |
| 写删不越过用户 | 代码挂起 + 「好」确认，模型说了不算 |

## 目录

```
gateway.py        服务 + 编排 + 工具循环
tools.py          工具声明与执行
hands.py          文件操作与 L0 策略
persona.md        人格（小菲鱼）
config.json       配置
schemas/          fact / frame 的 JSON Schema
tools/probe.py    对话探针
data/             运行时生成：gateway.db、audit.jsonl、role_model.json
```