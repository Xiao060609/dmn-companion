# 查证 D1：DashScope 兼容模式支不支持 prompt caching

> 对应 `plan/DMN审核.md` 第四节「需要查证」的 D1。
> 查证日期：2026-11-06。查证方式：**官方文档 + 真机实测**（对 `deepseek-v4.1-flash-1017` 发了约 40 次真实请求，合计花费 ≈0.1 元）。
> 所有代码/数据文件**未改动**，只新建本文件。

---

## 一、结论先行

**支持。而且不需要改任何代码 —— 现在就已经在命中。**

| 问题 | 答案 |
|---|---|
| 兼容模式支不支持？ | **支持**，走的是**隐式缓存（Implicit Cache）**，自动生效、无法关闭 |
| 要不要额外参数？ | **不要**。不加 `cache_control`、不加 `extra_body`，什么都不用加 |
| 能省多少？ | 命中部分按输入价的 **10%** 计费（即省 **90%**，约 **10 倍**） |
| 最小可缓存 token | 官方说 **1024**（实测 708 token 的前缀也命中了，见 §4.3，这点与文档不一致，存疑） |
| 缓存活多久 | 隐式缓存**没有固定有效期**，系统定期清理「长期未使用」的。实测**空闲 7 分钟后仍命中** |
| 命中怎么确认 | 响应 `usage.prompt_tokens_details.cached_tokens` 大于 0 |
| 人格那部分实测命中多少 | **1792 / 2048 token ≈ 87%**（人格净长约 2040 token） |
| 对 `build_messages()` 安全吗 | **安全**：人格是第 1 条 system 且每轮字节级一致，正是缓存最理想的位置。**但**后面的易变块会让「历史」永远缓存不上（见 §3） |

**一句话：46% 的人格成本这一块，折扣是真实的、已经在吃到了；文档里「省 4~10 倍」的说法对人这部分成立，但不要指望整体账单降到 1/10 —— 人格只占 46%，整体输入成本大约能降 37%。**

---

## 二、两种缓存：显式 vs 隐式

百炼的上下文缓存（Context Cache）分两种模式，**同一个请求只能用一种**。

### 2.1 隐式缓存（Implicit Cache）—— 我们用的是这个

| 项目 | 说明 |
|---|---|
| 怎么开 | **自动**，无需配置，**且无法关闭**（官方 FAQ 明确） |
| 触发条件 | 系统按**前缀匹配**自动识别多个请求的公共前缀；相同前缀 ≥ 1024 token 就「具备命中条件」 |
| 命中确定性 | **不确定**。官方原话：「上下文缓存命中概率并非 100%，即使请求上下文完全一致，仍可能未命中」 |
| 计费（本模型） | 命中部分 `cached_token` = `input_token` 的 **10%**；未命中部分原价；输出原价 |
| 有效期 | 无固定有效期，系统定期清理长期未使用的缓存 |
| 最小 token | 1024（达到只代表「有资格」，不代表必然命中） |
| 额外参数 | **无** |

> 注意：`deepseek-v4.1-flash-1017` 的隐式缓存折扣是 **10%**，比百炼大多数 qwen 模型的 20% 更便宜。
> 官方原文：「deepseek-v4.1-flash、deepseek-v4.1-flash-1017、deepseek-v4-pro-0813、deepseek-v4-flash-0731：`cached_token` 单价为 `input_token` 单价的 **10%**」，且 1017 版在六个地域的**忙时/闲时都适用**。

### 2.2 显式缓存（Explicit Cache）—— **这个模型不支持**

| 项目 | 说明 |
|---|---|
| 怎么开 | 在 messages 的 content 里加 `"cache_control": {"type": "ephemeral"}` |
| 格式要求 | content **必须改成数组形式**，字符串形式不支持 |
| 计费 | 创建缓存按输入价 **125%**；命中按 **10%** |
| 有效期 | **5 分钟**，每次命中重置 5 分钟 |
| 最小 token | 1024 |
| 上限 | 单请求最多 **4 个**标记；从标记位置向前回溯最多 **20 个 content 块** |
| **支持本模型吗** | **不支持**。显式缓存的官方模型清单里**没有任何 deepseek 模型**（清单是 qwen 全系 + kimi + glm）。模型页对 `deepseek-v4.1-flash-1017` 的能力表写的是「上下文缓存：**支持隐式缓存**」，不是「支持」 |

**实测印证**：我带着 `cache_control` 发请求（§4.1 测试 J），服务端不报错、正常返回，但返回的 `usage` 里**没有** `cache_creation_input_tokens` 字段，命中数走的仍是隐式的 `cached_tokens`。也就是说 **`cache_control` 被静默忽略了** —— 不报错，但也不产生显式缓存行为。

### 2.3 三种接入方式有没有区别

官方文档里缓存是通过 **HTTP 协议层**实现的，与 SDK 无关。OpenAI 兼容（`/compatible-mode/v1`）、DashScope 原生（`/api/v1`）、Anthropic 兼容（`/apps/anthropic`）三种入参格式下**隐式缓存的规则完全一样**，只是 `usage` 里字段名不同：

| 接入方式 | 命中 token 看哪里 |
|---|---|
| **OpenAI 兼容**（我们用这个） | `usage.prompt_tokens_details.cached_tokens`（含在 `usage.prompt_tokens` 里） |
| DashScope 原生 | `usage.prompt_tokens_details.cached_tokens`（部分地域/模型是 `usage.cached_tokens`） |
| Anthropic 兼容 | `usage.cache_read_input_tokens`（**不**计入 `input_tokens`，单独报） |

**所以「兼容模式」不背锅** —— 缓存能力和原生模式一模一样，只是显式缓存这个模型压根不支持，而它也不需要用。

---

## 三、对本项目的具体含义

### 3.1 拼装顺序：✅ 吻合，而且是最优的那一种

`gateway/gateway.py:1104` 的 `build_messages()` 第一条就是：

```python
msgs: list[dict] = [{"role": "system", "content": current_persona()}]
```

这就是缓存想要的形态：**最稳定、最长的内容放在最前面**，且 `current_persona()` 在 `persona.md` 不变时每轮返回**完全相同**的字符串。官方明确的最佳实践就是「将重复内容置于提示词开头，差异内容置于末尾」。

实测（§4.2）：连续两次只改 user 的话、人格不动，第二次 `cached_tokens = 1792`。
**人格净长实测约 2040 token**（`persona.md` 3004 字符；空 system + 一句「一」是 5 token，加人格后是 2048 token）。

### 3.2 最小 token 门槛：✅ 够，而且够得很宽裕

人格约 **2040 token**，门槛是 **1024**，是门槛的 2 倍。

一个必须说清的细节：**缓存按 256 token 粒度对齐**。实测见到的命中值全是 256 的倍数（256 / 512 / 1024 / 1536 / 1792 / 2048 / 2304 / 2816 / 3328 / 3584）。所以 2040 token 的人格，实际命中报的是 **1792**（不是 2040，也不是 2048）—— 尾部那 ~250 token 因粒度对不齐而落在缓存外。**这是正常损耗，不是 bug。**

### 3.3 「前缀必须字节级一致」的坑：⚠️ 有，但**当前实现已经躲开了**，只是躲得不够彻底

**这确实是个真坑，而且很狠。** 实测（§4.1 测试 K/L）：

- 把**易变块放在第 1 条**（固定长内容放到它后面）→ 连续两次请求 `cached_tokens` = **0**。因为前缀从第 1 条就开始不一样，后面再长也全部作废。
- 把**易变块放到最后**、固定内容放前面 → 同样两次请求，命中 **1024**。

**规则：缓存 = 从 messages[0] 开始、到第一处字节差异为止的最长公共前缀。差异点之后的一切，不管多长、多稳定，都不缓存。**

对照 `build_messages()` 的实际顺序（易变的用 ⚠ 标出）：

| # | 内容 | 每轮是否变 |
|---|---|---|
| 1 | **人格** `current_persona()` | ❌ 稳定（改 `persona.md` 才变） |
| 2 | ⚠ 内心OS `_inner_os_block()` | **每轮都可能变** |
| 3 | ⚠ 人物画像 `people_prompt_line()` | 可能变 |
| 4 | ⚠ 表情包教学 `meme_prompt` | 可能变 |
| 5 | 对话历史 `recent_turns()` | 每轮追加 |
| 6 | ⚠ 长期记忆 `living_memory` / `recall_long_memory()` | **每轮都变** |
| 7 | ⚠ 心境 `mood_prompt_line()` | 可能变 |
| 8 | ⚠ 群聊上下文 `context` | 每轮变 |
| 9 | ⚠ 时间戳 `现在的时间是 …` | **每轮必变** |
| 10 | user 消息 | 每轮变 |

**结论：**

- ✅ **人格在第 1 条，前面没有任何易变内容 → 人格必定命中。**（这是最重要的一条，占 46% 成本的那块稳了）
- ⚠️ **但第 2 条就是内心OS。** 它一变小，缓存前缀就断在人格末尾。所以**对话历史、长期记忆召回这两块（加起来可能上千 token）实际上一直吃不到缓存**，不管它们自己有多稳定。
- 💡 顺带一个反直觉的事实：**就算把易变块全挪到后面，对话历史也基本缓存不上** —— 因为 `history_turns=30` 是**滑动窗口**，满窗之后每轮都是「丢最旧的、加最新的」，messages 开头就在变，前缀天天对不上。早期没满窗时（历史一直往后追加）才缓存得到。

**这些都不影响人格那 46% 的命中。** 所以：`build_messages()` **当前顺序对人格是安全的，不需要为了缓存去改**。若要再榨一点，可做的事是把第 2~4 条、第 6~9 条易变块挪到 `recent_turns()` **之后**（收益仅限「窗口没满的早期对话」，量级不大，且会改变模型看到的信息顺序，属于有风险的改动）——**不建议为了这点收益动它**。

### 3.4 时间戳在后面：✅ 安全

时间戳在**第 9 条**、紧贴 user 消息，是整份 prompt 的**最后**一个 system 块。按前缀匹配规则，它变不变都不影响它前面的任何东西。**实测数据也确认了**：§4.2 里带随时变化时间戳的请求，人格部分照常命中 1792。**这一条不用担心。**

### 3.5 计费口径

`deepseek-v4.1-flash-1017` 华北2（北京）官方价（人民币，每百万 token）：

| 计费项 | 闲时 | 忙时 |
|---|---|---|
| 输入（cache miss） | 1 元 | 2 元 |
| **输入（cache hit）** | **0.1 元** | **0.2 元** |
| 输出 | 4 元 | 8 元 |

- **命中部分打 1 折（10%）**，不是免费。
- **写入不加价**：隐式缓存官方原话是「用于创建缓存 Token 计费 = 输入 Token 单价的 **100%**」，也就是**创建缓存本身不额外收钱**（这点和显式缓存的 125% 不一样）。所以对隐式缓存来说**没有「写贵了」的风险，只有「没命中也不亏」**。
- 闲时窗口：**UTC+8 22:00 – 次日 08:00**。凌晨聊天更便宜。

**按本项目算一笔账**（人格取 46%、总输入取 4400 token、忙时）：

| | 无缓存 | 有缓存（命中 1792） |
|---|---|---|
| 人格部分 | 2040 × 2/1M = **0.00408 元** | 1792×0.2/1M + 248×2/1M = **0.00085 元** |
| 其余部分 | 2360 × 2/1M = 0.00472 元 | 0.00472 元 |
| **输入合计** | 0.00880 元 | **0.00557 元** |

→ **输入成本降 36.7%**；**人格那一项本身降 79%**（受 256 粒度影响，没到理论上的 90%）。
→ 文档里「能缓存可省 4~10 倍」：**对人这部分成立（约 10 倍的理论值 / 实测约 5 倍净值）**，对**整账单**是 **1.6 倍**。别按 10 倍做预算。

---

## 四、可执行的验证方法

### 4.1 方法一（推荐，最省事）：直接读 `usage` 里的 `cached_tokens`

**做什么**：对同一个 API key，连发两次请求 —— 人格相同、只有 user 不同 —— 打印 `usage.prompt_tokens_details.cached_tokens`。

**预期结果**：

```
第 1 次：cached_tokens = 0        ← 冷启动，正常
第 2 次：cached_tokens ≈ 1792     ← 命中，= 人格长度按 256 对齐后的值
```

**零改动版**：项目里 `gateway.py:894-898` 的 `call_model()` 只取了 `prompt_tokens` / `completion_tokens`，**没有取 `cached_tokens`**，所以日志里看不到。想做一次性验证，用下面这段独立脚本（不改项目任何文件，粘到临时 py 里跑即可）：

```python
# 临时验证脚本 —— 不要放进项目，跑完删掉
import json, urllib.request, os

ROOT = r"<repo>/gateway"
key = active  # 从 data/keys.json 里取 active 那把
persona = open(os.path.join(ROOT, "persona.md"), encoding="utf-8").read()

def probe(user_text):
    body = {"model": "deepseek-v4.1-flash-1017",
            "messages": [{"role": "system", "content": persona},
                         {"role": "user", "content": user_text}],
            "temperature": 0.2, "max_tokens": 8, "enable_thinking": False}
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        u = json.loads(r.read())["usage"]
    print(u["prompt_tokens"], u["prompt_tokens_details"]["cached_tokens"])

probe("第一次"); probe("第二次")
```

成本：两次请求，**不到 0.001 元**。

> ⚠️ 连发的两次要**间隔 2~5 秒**，别并发 —— 官方说缓存创建发生在响应之后，太快可能还没写完。

### 4.2 方法二：看「同前缀不同 user」的命中数是否稳定

**做什么**：连发 3~5 次，每次只改 user 内容。人格、时间戳之外的东西全不动。

**预期结果**：第 2 次起 `cached_tokens` 稳定在 **1792**；`prompt_tokens` 保持 **≈2048** 不变。

**如果发现 `cached_tokens` 一直是 0**，按这个顺序排查：

1. **两次之间隔了太久**（我实测空闲 7 分钟还能中，但如果几小时没请求，缓存被清理是正常的）；
2. **换过 key 或 model** —— 官方 FAQ：缓存**账号级隔离**，不同模型之间也**不共享**；
3. **`persona.md` 被改了**（哪怕只多一个空格）—— 实测加一个空格，`cached_tokens` 从 1792 直接掉到 **0**。`persona_sync.sync_persona()` 每次请求都会跑，如果它把 AstrBot 库里的版本写回文件、且内容有差异，人格就会变，缓存全灭；
4. **命中率天然不确定** —— 官方明说命中概率不是 100%，偶发未命中属于正常，多试几次。

### 4.3 我实测的完整结果（供对照）

| 测试 | 目的 | 结果 |
|---|---|---|
| 人格 + 不同 user ×3 | 人格能不能命中 | 1792 / 1792 / 1792 ✅ |
| 人格 + `cache_control` | 显式缓存支不支持 | 不报错，但**无** `cache_creation_input_tokens`，走隐式 → **不支持** |
| 人格 + 后面插易变 system 块 | 易变块在后面安不安全 | 命中 1792，**安全** ✅ |
| 人格 + `tools`（= 网关 cloud 模式） | 工具定义会不会坏事 | 命中 1792→2304，**tools 也一起被缓存**，不坏事 ✅ |
| 人格加一个空格 | 字节级一致的严格程度 | 命中 **0** ✅（严格） |
| 全新唯一长前缀 708 token | 下限是不是 1024 | 第二次命中 **512** ⚠️ 与文档不符 |
| 全新唯一长前缀 1126 token | 对照 | 第二次命中 **1024** ✅ |
| 易变块在最前 + 固定长块在后 | 前缀断裂的后果 | 连续两次命中 **0** ✅（证明前缀匹配严格） |
| 空闲 7 分钟后再发 | 缓存活多久 | 仍命中 1792 ✅ |
| 手动补写人格以外的变化 | 256 粒度 | 命中值全是 256 的倍数 |

**关于 708 token 也命中这一点，我标为「未查到官方说明的不一致」**：官方文档写最小值 1024，实测 708 也中了 512。可能是官方口径含了服务端追加的内部 token，也可能是策略比文档宽松。**不影响结论**（人格 2040 远超两种口径），但别拿 1024 当硬门槛用。

---

## 五、来源

### 官方文档（结论主要依据，可信度高）

| 内容 | 链接 | 用到了什么 |
|---|---|---|
| 上下文缓存（中文主站） | [help.aliyun.com/zh/model-studio/context-cache](https://help.aliyun.com/zh/model-studio/context-cache) | 显式/隐式对比表、1024 门槛、5 分钟有效期、计费比例、前缀匹配原理、FAQ |
| 上下文缓存（国际站，**内容最全**） | [alibabacloud.com/help/zh/model-studio/context-cache](https://www.alibabacloud.com/help/zh/model-studio/context-cache) | 隐式缓存支持模型清单（**含 deepseek-v4.1-flash-1017**）、**10% 折扣原文**、`cached_tokens` 返回样例、命中率不确定性说明 |
| 显式缓存最佳实践 | [help.aliyun.com/zh/model-studio/explicit-cache-guide](https://help.aliyun.com/zh/model-studio/explicit-cache-guide) | `cache_control` 用法、content 必须数组、最多 4 标记、20 块回溯、125%/10% 计费、tools 参与缓存 |
| `deepseek-v4.1-flash` 模型页 | [help.aliyun.com/zh/model-studio/deepseek-v4-1-flash](https://help.aliyun.com/zh/model-studio/deepseek-v4-1-flash) | **能力表：「上下文缓存：支持隐式缓存」**（不是「支持」二字）、**输入/缓存命中/输出的忙闲时价格** |
| 上下文缓存（控制台文档域） | [docs.bailian.console.aliyun.com/zh/model-studio/context-cache](https://docs.bailian.console.aliyun.com/zh/model-studio/context-cache) | 与主站同源，交叉核对 10% 折扣那句 |

**判定「显式缓存不支持本模型」的依据**：`explicit-cache-guide` 与 `context-cache` 的「支持的模型」清单里，显式缓存只列了 qwen 全系、kimi、glm，**没有任何 deepseek 条目**；而模型页明确写的是「支持隐式缓存」。两者一致指向：**本模型只有隐式缓存**。

### 真机实测（本研究自己的数据，可信度最高）

约 40 次真实请求，`model=deepseek-v4.1-flash-1017`，`endpoint=https://dashscope.aliyuncs.com/compatible-mode/v1`，人格用 `gateway/persona.md` 原文。数据见 §4.3。**这是唯一能证明「你这个账号、这个模型、这个 endpoint 真的会命中」的证据** —— 官方文档只能证明「应该支持」。

### 第三方经验（仅作旁证，未采信为结论）

| 内容 | 链接 | 备注 |
|---|---|---|
| 百炼 vs DeepSeek 官方缓存价格对比 | [catcat.blog/2026/10/deepseek-v4-flash-api-benchmark](https://catcat.blog/2026/10/deepseek-v4-flash-api-benchmark) | 第三方实测帖，说百炼缓存命中价（¥0.1/0.2）是 DeepSeek 官方（¥0.02/0.04）的 5 倍。**这个数字与官方价格表对得上**（官方表正是 0.1/0.2），可作旁证；但「值不值得换服务商」不在 D1 范围内 |
| DeepSeek 官方定价页 | [api-docs.deepseek.com/zh-cn/quick_start/pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) | 不是百炼的价格，**不要混用**。同模型直连 DeepSeek 官方比走百炼便宜，但那是另一个决策（D1 只问「兼容模式支不支持缓存」） |

### 明确不确定 / 未查到官方说明的点

1. **隐式缓存的准确存活时间** —— 官方只有「无固定有效期，系统会定期清理长期未使用的缓存数据」。实测 7 分钟仍命中，但**多久一定失效没有官方数字**。
2. **最小 1024 门槛为何实测 708 也命中** —— 官方只给了 1024，没有解释。可能是服务端追加内部 token 导致实际计数不同，未证实。
3. **命中率为什么不 100%** —— 官方只说「由系统判定」，没有给概率，也没给影响因素。本项目低频、单账号场景下实测命中率很高（几乎每次都中），但**不能承诺永远中**。
4. **`persona_sync` 会不会频繁改人格** —— 这次没查。它是唯一能让「人格缓存全灭」的现实风险点（实测改一个空格就 0 命中），建议单独确认一次它的写回频率。

---

## 六、一句话给主人

**D1 结论：支持，而且已经在生效，不用改代码、不用加参数。** 人格是第 1 条 system、字节级稳定，正好是缓存最想要的位置 —— 实测每轮命中 1792/2048 token，命中部分按 **10%** 计价（省 90%），**写入不额外收钱**，所以**零风险纯赚**。整体输入成本大约降 **37%**（人格那项降约 79%），不是文档设想的「整单省 4~10 倍」，那 4~10 倍只对人这部分成立。唯一值得记一笔的隐患：**`persona_sync` 只要把人格改动一个字节，缓存就全灭** —— 这是唯一会让这笔收益消失的现实风险。
