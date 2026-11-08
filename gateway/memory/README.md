# `gateway/memory/` — 小鱼记忆层（自主实现）

从 AstrBot 的 `FaissVecDB` 与 `livingmemory` 插件剥离而来，**表结构保持 1:1**，
插件的现有数据可以直接读（已验证 13/13 逐条一致）。

## 为什么自己养

原来长期记忆在第三方插件里：

- 插件 provider 指向网关 `/v1`，摘要请求被当成聊天 → 13 条记忆全是 `"对话记录"`
- 桌宠的记忆进不了那个库
- 插件升级 / 改 schema / 换 provider，我们只能跟着跑
- 插件断了自己看不见（要等主人问「你记得我生日吗」才暴露）

现在写入权、召回权、存储格式都在本仓库内。

## 结构

| 文件 | 干什么 | 来源 |
|---|---|---|
| `base.py` | `Result` / `BaseVecDB` | AstrBot，**零改动** |
| `tokenizer.py` | jieba 分词 + FTS5 查询构造 | AstrBot，**零改动** |
| `embedding_storage.py` | faiss 索引读写（自带中文路径 ASCII 桥接） | AstrBot，**零改动** |
| `document_storage.py` | `documents` 表 + FTS5 全文索引 | AstrBot，**3 处改写** |
| `vec_db.py` | `FaissVecDB`：向量 + 文档 + 混合检索 | AstrBot，**~10 行改写** |
| `provider.py` | Provider 协议（鸭子类型） | **新写** |
| `dashscope_embedding.py` | DashScope `text-embedding-v4` | **新写** |
| `loop_bridge.py` | 同步 → 异步桥（常驻事件循环） | **新写** |
| `store.py` | `MemoryStore` 门面 | **新写** |
| `data/hit_stopwords.txt` | 767 行停用词 | AstrBot，原样搬 |

## 依赖

```
faiss-cpu  sqlalchemy  sqlmodel  greenlet  aiosqlite  aiofiles
numpy  jieba  networkx
```

> `greenlet` 最容易漏：不装它 `from sqlalchemy.ext.asyncio import ...` 直接
> `ImportError`，报错信息里才提示。**必须写进 requirements。**

## 用法

```python
import sys; sys.path.insert(0, "gateway/memory")
import store

s = store.open_store(db_path="gateway/data/livingmemory.db")
s.add("主人的生日是10月20日；生日；10月20日",
      {"importance": 0.9, "session_id": "owner"})

s.search("他生日是什么时候", top_k=3)      # 语义
s.search_keyword("10月20日", limit=3)      # 精确词（BM25）
s.search_hybrid("生日", top_k=3)           # 关键词优先 + 向量补齐
s.delete(doc_id)
s.rebuild_index()                          # 索引坏了不丢记忆
```

## 三个坑（都是实测踩出来的）

### 1. `search_sparse` 收的是**已分词列表**，不是原始字符串

```python
# ❌ 错：传字符串会被逐字符拆开
build_fts5_or_query("人群")   # → '"人" OR "群"'
# ✅ 对：
build_fts5_or_query(["人群"]) # → '"人群"'
```

`MemoryStore.search_keyword()` 已替调用方分好词，**别绕过它直接调底层**。

### 2. faiss 的**读和写**在中文路径下都会挂

`embedding_storage.py` 自带 ASCII 桥接（`path.isascii()` 自动判断），
不需要自己写。但要知道：**不只是写，读也要桥接**（项目地图第 31 条只强调了写）。

### 3. 必须用常驻事件循环，不能每次 `asyncio.run()`

| 方案 | 耗时 |
|---|---|
| 每次 `asyncio.run()` | 40.4 ms/次 |
| **常驻循环 + `run_coroutine_threadsafe`** | **9.38 ms/次** |

对话热路径上每次多 30ms 不可接受。`loop_bridge.get_bridge()` 是进程级单例
—— 多个循环会各持一份 faiss 索引，那是真正的数据错乱来源。

## 数据形状

```
documents      id / doc_id / text / metadata(JSON) / created_at / updated_at
documents_fts  FTS5 虚拟表，rowid 对齐 documents.id，列 search_text
```

`text` 是**给检索用的富文本**，不是原始对话：

```
"主人的生日是10月20日；生日；10月20日"
 ↑ 叙述句          ↑ 关键词（供 BM25 精确命中）
```

原始对话在网关的 `turns` 里。**别把对话原文直接塞进来** —— 向量会被稀释，
关键词也会误命中。

## 没搬的东西（有意）

| 模块 | 行数 | 为什么 |
|---|---|---|
| `companion`（陪伴人格 / 画像 / 情绪） | 4,896 | 是另一个产品，与记忆无强耦合 |
| `page_api_modules`（WebUI 后端） | 1,768 | 用途有限，需要时自建 |
| `i18n` / `scripts` | — | 用不上 |
| `schedulers`（衰减 / 遗忘调度） | 573 | 阶段 2 再定 |

## 当前状态

- ✅ 阶段 1 完成：存储 + 检索可独立运行，380 个网关单测全绿
- ⏳ 阶段 2：把 `system._extract_events()` 抽出的结构化事实接进来
- ⏳ 阶段 3：切流（停掉插件），桌宠渠道并入
