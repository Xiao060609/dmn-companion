# 05 · 运维层 / 验收工具 / 文档一致性 / 数据与 git 状态

> 审计者：audit-ops（task-5）　审计时间：2026-11-07 16:1x
> 方式：**全程只读**。未启动任何服务（5099/3000/3001/6185/6199/8848/8849/8850/8898 实测全部无监听），未执行 启动.bat / start.ps1 / restart.ps1 / install 的安装动作，sqlite 一律 `mode=ro`。
> 证据标记：**【读码】**=读源码　**【实测】**=本机执行/查询得到　**【文档】**=文档原文　**【推断】**=由上述推出、未直接观测。

---

## 零、一句话结论

**代码与运维链路的工程质量明显高于文档：脚本的坑几乎都被真实踩过并写进了注释，但文档里的数字（行数 / 测试条数 / 项数 / 完成度）已经系统性落后代码一到两轮，且 `tools\` 里被宣传为「只读」的验收工具实际会写生产状态文件、`restart.ps1` 会杀掉全机任意 uv 宿主 python —— 前者污染真实数据，后者是越权破坏性操作，这两条是本轮最严重的发现。**

---

## 一、运维脚本逐个说明

### 1.1 调用关系图（全部【读码】核对）

```
                    ┌──────────────────────────── 用户双击 ────────────────────────────┐
                    │                                │                                │
              启动.bat(ASCII)                   restart.bat(ASCII)                  安装.bat(ASCII)
                    │                                │                                │
       ┌────────────┴────────────┐            restart.ps1                      install.ps1
       │                         │                 │(停：端口+进程名双杀)              │(只检查，不安装)
  _launch.vbs               status.ps1             │                                └→ 退出码恒 0
  (wscript 无控制台)        (等 90s 打端口表)       ├─ 等待端口释放+electron 退净(≤20s)
       │                         │                 └─ Start-Process hidden start.ps1 -LogPath restart-start.log
       └─ Start-Process hidden start.ps1              （注意：不是 & 同步调用）
                    │                                        │
                    ├─ 1/3  qq\启动-小鱼.ps1 (hidden, 独立 powershell)
                    │        ├─ 守卫：3100 被占 → exit 1（旧 qq-bridge 抢消息）
                    │        ├─ SnowLuma: node.exe index.mjs  → 等 5099 (60s)
                    │        └─ AstrBot : .venv\python -u main.py → 等 6185 (120s)
                    │        最后校验 6199 有 ESTABLISHED
                    ├─ 2/3  electron.exe "."  (cwd=desktop\desktop) → 等 8898 (30s)
                    │        └─ main.js 再 spawn: server.py(py) + gateway.py(py -B)
                    └─ 3/3  DSH: desktop\config.json → ops.dsh_exe（按 ExecutablePath 精确匹配）

  desktop\启动桌宠.bat ─→ electron.exe "<abs app dir>"   （只起桌宠，自己会拉 server+gateway）
  desktop\启动.bat     ─→ python server.py（start /b，前台同窗口）+ 4s 后开浏览器
                          ⚠ 不起 Electron、不起网关；关窗口即停
  gateway\start.cmd    ─→ python -B gateway.py（前台；单实例锁冲突时 gateway.py 自己 exit 3）
  gateway\stop.cmd     ─→ 按端口 8898 找 owner → 校验命令行含 gateway.py → taskkill /F
  gateway\start.sh / stop.sh  ─→ Linux 版（stop.sh 盲信 pid 文件 + pkill -f gateway.py）
  桌宠轮盘「重启」      ─→ main.js:767 RESTART_BAT=restart.bat → explorer 启动（非 spawn）
  桌宠轮盘「QQ 托管」    ─→ main.js:768 QQ_PORTS=[6185,6199,5099,3000,3001]
```

### 1.2 逐个说明

#### `install.ps1`（135 行）— 体检

| 项 | 内容 |
|---|---|
| 干什么 | 只检查不安装。6 段：QQ 客户端 / Python 版本 / 端口占用 / API key / 桌宠素材 / Python 包 |
| 关键行 | `:29` QQ 进程名匹配 `^qq$|^tim$`；`:38-50` Python 版本（正则取 major/minor，要求 ≥3.10）；`:54` 检查 **5 个**端口 `5099,6185,6199,8848,8898`；`:64-75` 读 `gateway\secrets.json` 的 `dashscope_api_key`，只回显末 4 位；`:85-102` 查 `desktop\assets` 与 `desktop\web\vendor`（这两个刻意不进 git）；`:114` 硬编码 **7 个**包 `numpy/faiss/jieba/sqlalchemy/sqlmodel/aiosqlite/greenlet` |
| 端口检查逻辑 | `Test-Port` = `Get-NetTCPConnection -LocalPort` + `OwningProcess -ne 0`，**不按 State 过滤**（`:18` 注释：某些监听器 State 是空串）。这个判据在 6 个脚本里重复了 6 次 |
| 失败会怎样 | **不怎样** —— 全脚本没有一处 `exit`，`$ErrorActionPreference='Continue'`（`:6`）。**【实测】** 退出码 0 |
| 破坏性操作 | 无（纯读） |
| 问题 | 🔸 **退出码恒 0**：任何检查项红/❌ 都不影响退出码，`安装.bat:6` 的 `/nopause` 分支与失败分支永不可达，用户勾不出"体检失败"。🔸 `:114` 只查 7 个包，而 `gateway\requirements.txt` 实际声明 9 个（多 `aiofiles`、`networkx`）—— 漏查的两个装不上也能"全绿" |

#### `start.ps1`（157 行）— 一键启动（被 `_launch.vbs` / `restart.ps1` 拉起）

| 项 | 内容 |
|---|---|
| 干什么 | 三段：QQ 那套 → 桌宠 → DSH，然后打端口表 |
| 关键行 | `:41-44` 端口判据（同上）；`:58` QQ 跳过条件是 **6185 AND 6199 同时在监听**；`:65-69` 用 `Start-Process powershell -File "qq\启动-小鱼.ps1"` 且**给路径手动加引号**（`:63-64` 注释：`-ArgumentList` 数组不补引号）；`:76` 桌宠按 **8848** 判断而非进程名；`:83` 先 `Remove-Item Env:\ELECTRON_RUN_AS_NODE`；`:84` electron 传 `'.'` 且 cwd=`desktop\desktop`；`:99-107` DSH 按 `ExecutablePath` 精确匹配；`:144` 端口表 `5099,3000,6185,6199,8848,8898` |
| 谁调它 | `_launch.vbs:23`（启动.bat 链路）、`restart.ps1:165`（带 `-LogPath restart-start.log`） |
| 失败会怎样 | 每段失败只打红字继续（`$ErrorActionPreference='Stop'` 但都包在 if/else 里）；**脚本无 `exit`，退出码恒 0** → 真失败由后续 `status.ps1` 的 `exit 1` 兜住 |
| 破坏性操作 | 只 `Remove-Item Env:\ELECTRON_RUN_AS_NODE`（进程内环境变量），无杀进程、无删文件 |
| 备注 | `:147-152` DSH 段：`manage_dsh=false` 时完全不碰，这是对的（不动用户自己的程序） |

#### `restart.ps1`（194 行）— 一键重启（**破坏性最强**）

| 项 | 内容 |
|---|---|
| 干什么 | 先停干净 → 再隐藏拉起 `start.ps1` → 等端口 → 打表 → 失败 `exit 1` |
| 停止顺序 | `:108` `Stop-ByPort @(8848,8898)` → `:109` `Stop-ByPort @(5099,3000,6185,6199)` |
| 按端口杀 | `:65-69` `Get-NetTCPConnection` **不按 State 过滤**（`:57-64` 注释：本机监听器 State 可能是空串，带 `-State Listen` 会漏掉 8848/8898）→ 收集所有 `OwningProcess` → `:78` `taskkill /PID <id> /T /F` → 轮询 10×0.5s 看端口是否真释放 → 还占着就 `:98` 再强杀一次 |
| 按命令行杀（双保险） | `:118` electron 命令行含 `小鱼\desktop` / `xiaoyu\desktop`；`:119` node 含 `小鱼\qq\SnowLuma`；`:123` python 含 `小鱼\gateway` / `小鱼\desktop`；`:130` python 含 `小鱼\qq\astrbot`、`xiaoyu\qq\astrbot`、**`uv\python`** |
| 等待 | `:135-145` 最多 20s，条件是「5 个端口全空 **且** 没有 electron 进程」 |
| 拉起 | `:165-167` `Start-Process powershell -File start.ps1 -LogPath restart-start.log -WindowStyle Hidden`（**故意不用 `&` 同步调**，`:151-158` 说明：可见窗口 CTRL_CLOSE 会连坐子进程） |
| 失败会怎样 | `:190-194` 若 6185/8898/8848 未齐全 → `exit 1` → `restart.bat:13` 保持窗口并 pause |
| 破坏性操作 | ✅ **taskkill /T /F**（连同子进程树）。按端口杀的目标是明确的；**按命令行杀的 `uv\python` 项是全机通配**（详见 P0-2） |
| 不碰的东西 | 语音引擎（8849）与 DSH —— `:114-117` 明写"故意不重启"，`gateway/tests/test_script_encoding.py:541-566` 有守门用例 |
| 问题 | 🔴 `:2` 用法注释写"双击 **重启服务.bat**"，该文件**不存在**（【实测】根目录只有 `启动.bat` / `restart.bat` / `安装.bat`）。🔴 `:130` 的 `uv\python` 过滤项（见 P0-2） |

#### `status.ps1`（48 行）— 只报状态

`Test-Port` 同上；`:27-31` 最多等 90s 直到 6185+8898+8848 齐；`:35` 打 6 个端口；`:44-48` 不齐则 `exit 1`。
**这是整条链里唯一有正确退出码语义的脚本**（`启动.bat:13-32` 靠它决定是否留窗口）。无破坏性操作。
🔸 端口表**不含 8849（语音引擎）和 3001**（后者是 QQ 托管要杀的端口，`main.js:768`）—— 语音挂了它看不出来。

#### `启动.bat`（36 行）— ASCII，一键启动入口

`:12` `wscript //nologo "%~dp0_launch.vbs"` → `:17` `status.ps1` → `:20` `/nopause` 支持 → `:27-32` 失败留窗口 pause → `:35` 成功 `timeout /t 10 /nobreak` 自动关。
**【实测】** `nonASCII=0`（符合 `:4-10` 的"batch 不能有非 ASCII 字节"约束）。
设计要点（`:8-10`、`:21-26`）：**启动交给无控制台的 wscript**，所以关这个窗口不带走服务。

#### `restart.bat`（22 行）— ASCII

`:5` 调 `restart.ps1` → `:7` `/nopause` → `:13-18` 失败 pause → `:21` 10s 自动关。**【实测】** `nonASCII=0`。

#### `安装.bat`（8 行）— ASCII

`:5` 调 `install.ps1` → `:6` `/nopause` → `:8` **`pause`（无条件）**。因为 `install.ps1` 无 exit，所以"成功也 pause、失败也 pause"，无法区分。

#### `_launch.vbs`（24 行）— 无控制台宿主

`:15-16` 算自身目录拼 `start.ps1`；`:18-21` 文件不存在则 `WScript.Quit 1`；`:23` `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<target>"`；`:24` `sh.Run cmd, 0, False`（0=隐藏窗口，False=不等待）。
**它是"关窗口不带走服务"的关键**：wscript 没有控制台，子进程也就不挂在会被 CTRL_CLOSE 打到的控制台上。

#### `gateway\start.cmd`（16 行）

`:3` `PYTHONIOENCODING=utf-8`；`:4` `cd /d "%~dp0"`；`:5-8` 有 `python` 就用 `python -B gateway.py`；`:10-13` 退化到 `py -B`；`:15` 都没有则报错 `exit /b 1`。
前台运行、无端口预检 —— 双开由 `gateway.py:3154` 的单实例锁兜（冲突时 `exit 3`）。

#### `gateway\stop.cmd`（77 行）— **工程上最扎实的一个**

`:32-34` 按端口 8898 取 owner PID；`:36-46` 端口没了**才**把 `data\gateway.pid` 当"线索"；`:57-77` `:kill_if_gateway`：`:61` 查 `Win32_Process.CommandLine` → `:66` `findstr /i "gateway.py"` → 不匹配就 `:68` `SKIP pid ...: command line is not gateway.py` → 匹配才 `:71` `taskkill /PID /F`。
`:49-50` 无论成败都删 pid 文件。**【实测】** `nonASCII=0`。
这正面修掉了 `plan/代码审查报告-2026-11-04.md:1105`（P1-2）"盲杀陈旧 pid"的问题。

#### `gateway\start.sh` / `gateway\stop.sh`

`start.sh:5` `exec python3 -B gateway.py`，正常。
`stop.sh:5-8` **盲信 pid 文件**直接 `kill "$(cat ...)"`（无身份校验）；`:11` 回退 `pkill -f "gateway.py"`（匹配本机任意 gateway.py）。
⚠ 与 `stop.cmd` 的安全等级不一致 —— Windows 面修了，Linux 面没修。

#### `desktop\启动.bat`（11 行）

`:4` `start /b python server.py`；`:6` 4s 后开 `http://127.0.0.1:8848`；`:11` pause。
⚠ 名字像"启动桌宠"，实际**只起 Python 后端，不起 Electron、不主动起网关**（网关由 `server.py`/`main.js` 那条链托管）；关窗口即停。容易误解。

#### `desktop\启动桌宠.bat`（20 行）

`:7` `APPDIR=%~dp0desktop`；`:9-13` 校 `package.json`；`:14-18` 校 `node_modules\electron\dist\electron.exe`；`:20` `start "" "<electron.exe>" "<APPDIR>"` —— 传**绝对路径**而不是 `.`（`:3-6` 注释解释：cwd 不对会报 "Unable to find Electron app"）。
**【实测】** `nonASCII=0`。这是唯一正确启动 Electron 的 bat。

### 1.3 端口占用与进程发现逻辑对照表

| 脚本 | 判据 | 是否按 State 过滤 | 杀进程方式 | 是否校验身份 |
|---|---|---|---|---|
| install.ps1 | Get-NetTCPConnection + OwningProcess≠0 | ❌ 不过滤 | 不杀 | — |
| start.ps1 | 同上 | ❌ | 不杀（只启动） | — |
| status.ps1 | 同上 | ❌ | 不杀 | — |
| restart.ps1（按端口） | 同上 | ❌ | `taskkill /T /F` | 端口 owner 本身即身份 |
| restart.ps1（按名） | `Win32_Process.CommandLine` 正则 | — | `taskkill /T /F` | ✅ 路径片段，**但 `uv\python` 例外** |
| gateway\stop.cmd | 端口 8898 → PID | ❌ | `taskkill /F` | ✅ 命令行含 `gateway.py` |
| gateway\stop.sh | pid 文件优先 | — | `kill` / `pkill -f` | ❌ 无校验 |
| main.js（QQ 托管） | probePort(6185) + QQ_PORTS | — | 按端口 | 端口即身份 |
| main.js（网关看门狗） | `fetch(GATEWAY_ORIGIN/health)` | — | 重启子进程 | — |

**结论**：判据是统一的（`Get-NetTCPConnection` 且不按 State 过滤），这一致性做得很好 —— 注释里记着"同一个坑踩了 6 次"（`项目地图.md:551-552`），现在 6 处代码确实都是同一个正确写法【读码】。`netstat` 已被全面弃用，只有注释里还留着历史教训。

### 1.4 非 ASCII / BOM 约束实测

| 约束 | 实测结果 |
|---|---|
| `.bat` / `.cmd` 必须全 ASCII | ✅ 7 个文件 `nonASCII=0`（启动.bat / restart.bat / 安装.bat / gateway\start.cmd / stop.cmd / desktop\启动.bat / 启动桌宠.bat） |
| `.ps1` 必须带 UTF-8 BOM | ✅ 5 个文件 BOM 全在（install / start / restart / status / qq\启动-小鱼.ps1） |

> 【推断】这两条是历史真实事故（`plan/验收结论-2026-11-04.md:51`：缺 BOM 导致"双击安装 100% 失败，4 处语法错误"），目前**已修复且有守门用例**（`gateway/tests/test_script_encoding.py`，26903 字节）。

---

## 二、`tools\` 五个验收脚本：可用性矩阵

### 2.1 矩阵

| 脚本 | 行数 | 验什么 | 连服务？ | 前置条件 | 花钱调模型？ | **服务全停时能跑？** | 我实测的退出码/结果 |
|---|---|---|---|---|---|---|---|
| `acceptance_matrix.py` | 482 | T-01~T-12 + P1-1~P1-11 共 **53 项**（静态读码 + 直接 import 函数 + 读两个库） | 部分（T-01/T-03 的活服务子项会自动跳过） | Python 依赖齐全（faiss 等） | ❌ | ✅ **能** | **exit 0，53/53 通过** |
| `state_health.py` | 420 | 残留文件 / 敏感文件 ACL / 新颖度基线 / 记忆索引一致性 / 状态文件键 / 日志可解析率 / SQLite quick_check | ❌ | 无 | ❌ | ✅ **能** | **exit 0，15 项正常 0 注意 0 错误** |
| `e2e_smoke.py` | 385 | 鉴权 401 / 8848 跨站守卫 403 / 渠道隔离 / 身份伪造 / 真实对话 | ✅ **必须** 8898+8848 | 服务在跑；且**要求隔离数据目录**，否则拒绝（exit 3） | `--no-model` 时不调；不带则调 | ❌ **不能** | **exit 2**（"8898 网关 不通 / 8848 桌宠 不通"） |
| `m6_acceptance.py` | 337 | M6 三段对话（A 时间感 / B 接续昨天 / C 数天数） | ❌ | `gateway.db` + `system_audit.jsonl` 存在 | ❌（只读库和审计） | ✅ **能** | **exit 1**（未全中）+ 打出"数据不足，暂时不能判" |
| `verify_all.py` | 301 | 串起上面 4 个，给一句结论 + 统一退出码 | 视参数 | 取决于子项 | ❌ | ✅ **能**（默认跳过 e2e） | 默认 **exit 0**（2 通过 / 1 还没到判定时机）；`--with-e2e` **exit 0**（e2e 被 skip）；`--quick` **exit 0**（2 通过） |

### 2.2 实测输出摘要（都是真跑过的）

**`acceptance_matrix.py`**（53/53 全过）关键项：
```
✅ [T-06] documents == index.ntotal      documents 13 / index 13
✅ [T-07] 距上次超过 24h → 允许再跑      距上次 71.6h
✅ [T-08] string 伪造 → 落到 qq_unknown   qq_unknown
✅ [P1-1] restart.ps1 用单反斜杠          代码行里单斜杠 1 处、双斜杠 0 处
✅ [T-12] 新颖度基线不是测试残留          3851 个 gram
⏭  [T-01] / [T-03] 活服务检查跳过（8898 / 8848 没起）
=== 汇总：53/53 项通过 ===
```
⚠ **编码坑【实测】**：直接 `python tools\acceptance_matrix.py` 会因 emoji 在 GBK 控制台崩：
```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
```
必须 `python -X utf8` 或 `$env:PYTHONIOENCODING='utf-8'`。5 个工具都有这个问题（它们都 print emoji），**没有任何一个自己在脚本里做了 stdout 编码兜底**。这是一个"中文 Windows 上照文档直接跑就失败"的可用性缺陷（P2）。

**`state_health.py`**（15 项全绿）：
```
✅ [记忆] 一致：documents 13 == vectors 13
✅ [system-state] system_state.json 正常（24 个键）
✅ [日志] system_audit.jsonl：996 行全部可解析
✅ [数据库] gateway.db quick_check ok，journal_mode=wal
🎉 她的状态是干净的。
```

**`m6_acceptance.py`**（exit 1，但**是"还没到时候"不是"坏了"**）：
```
她主动说了：6 句（只算投递成功的）
其中**修复之后**（11-04 18:00 起）说的：0 句
对话：206 条用户消息，渠道分布 {'qq_group:示例群': 152, 'qq_private': 229, 'NULL': 1, 'qq_unknown': 27}
最长空档：721.8 分钟 ／ 距今无人说话：87.88 小时
跨天载体：长期记忆 13 条，画像字段 ['互动习惯','作息','在意的事','情绪基调','称呼']
⚠ **数据不足，暂时不能判 M6。**
✅ 点了  B「我不问了，但我在」（唯一命中，素材：long_memory_docs=13 + 5 个画像字段）
```

**`e2e_smoke.py --no-model`**（exit 2，服务没起 → 按设计跳过，`verify_all` 把它映射成 `skip` 而不是 fail）：
```
❌ 8898 网关 不通：URLError
❌ 8848 桌宠 不通：URLError
服务没起齐，先起它们：...
```

### 2.3 🟠 `acceptance_matrix.py` **不是只读的**（本轮最意外的发现）

`项目地图.md:1310` 宣称"除 e2e 外都只读"，`tools/acceptance_matrix.py:14-15` 也自称"不需要起服务也能跑大部分（静态检查 + 单测级验证）"。**实测相反**：

- 该脚本 `T-07` 段（`:289-315`）会：
  - `:293` `st = _sys_mod.load_state()` —— 读**生产** `gateway\data\system_state.json`
  - `:306` `_sys_mod._maybe_prune_long_memory(dict(st))` —— **直接调生产函数**
  - 该函数（`gateway/system.py:4060-4113`）在 `last_long_memory_prune_ts` 距现在 ≥24h 时会：真跑一次遗忘扫描 → `:4102-4104` `load_state()` → 改键 → **`save_state(fresh)` 落盘** → `:4106-4113` 追加一条 `system_audit` 审计。
- `_sys_mod.load_state()` 本身也带迁移副作用（`项目地图.md:621-655` 第 61 条已经把这个坑写清楚了：`load_state()` 只读不写但会做内存迁移，靠"下一次 save 捎带"落盘）。

**【实测】证据链（跑前 / 跑后对比）**：

| 观测点 | 跑 `acceptance_matrix.py` 之前 | 跑之后 |
|---|---|---|
| `system_state.json` 键数 | **19** | **24** |
| `mood_value` / `mood_label` / `mood_reason` / `mood_updated_ts` | 四个键**都不存在** | `0.0` / `'平静'` / `''` / `1794033835.38` |
| `last_system_log_id` | 不存在 | 出现 |
| `last_long_memory_prune_ts` | `1793776045.27`（= 11-04 15:07） | `1794033836.09`（= 11-07 14:43，**被改成"刚刚"**） |
| `system_state.json` mtime | 11-05 13:36 | 11-07 14:43 |
| `system_audit.jsonl` 末行 | `{"event":"tick","ok":false,"reason":"no_change"}` | 新增 `{"event":"long_memory_prune","ok":true,"scanned":13,"matched":0,"deleted":0,"review":0,"boosted":0}` |

**后果（按严重度）**：
1. **把遗忘节流时间戳推到了"现在"** → 真实网关下次起来后 24 小时内**不会再跑每日遗忘**（`system.py:4083`）。审计需求"不能改变生产状态"被违反。
2. **把 §14.5 / 第 61 条"心情四字段至今没落盘"的现场改掉了** → 后续任何人再跑这条检查会看到"已落盘"，**证据被审计工具自己抹掉**。这条最讽刺：`项目地图.md:621` 正是靠"四个键都不存在"来证明那个 bug 还在。
3. `verify_all.py` 默认链路会调它（`verify_all.py:62-71` STEPS[1]），所以**`python tools/verify_all.py` 这个"一键验收"也会写生产**。

> 【实测】另外补一句：`state_health.py` 我也做了同样的哈希对照实验（跑前跑后 6 个数据文件 SHA256 全等 + mtime 不变），它**确实**是只读的 —— `:229` 虽然 `import memory_bridge` 并调 `MemoryBridge().health()`，但 `health()` 只走 `consistency()`（`memory/store.py:348-373`，纯 `get_documents` + `all_ids`），不写。唯一会写的是 `--fix`（`:119-131`，删 `.tmp` / 孤儿 `.bak`），默认不开。

---

## 三、数据实测快照

### 3.1 `gateway\data\` 文件清单（29 个文件 + 1 子目录）

| 文件 | 大小 | 最后修改 | 说明 |
|---|---|---|---|
| `gateway.db` | 593,920 B | 2026-11-06 19:44:50 | 主库（WAL） |
| `gateway.db-shm` / `-wal` | 32,768 / 0 B | 2026-11-07 14:47 | WAL 附属（wal 为空=已 checkpoint） |
| `livingmemory.db` | 61,440 B | 2026-11-07 14:53【我跑 recall 导致】 | 长期记忆库 |
| `livingmemory.index` | 53,442 B | 2026-11-05 21:28 | faiss 索引 |
| `livingmemory.db.bak-before-orphan-cleanup` | 61,440 B | 2026-11-03 22:29 | 安全网 |
| `livingmemory.index.bak-before-orphan-cleanup` | 229,914 B | 2026-11-03 22:28 | 安全网（**比现役索引大 4.3 倍**，说明当时清掉了大量孤儿向量） |
| `system_state.json` | 3,302 B | 2026-11-07 14:43【被 acceptance_matrix 改写】 | 系统层状态 |
| `system_state.json.bak-20261103-004829-before-cleanup` | 3,581 B | 2026-11-03 00:48 | 安全网 |
| `system_audit.jsonl` | 139,192 B / 996 行 | 2026-11-07 14:43【+1 行】 | 系统审计，**0 坏行** |
| `audit.jsonl` | 70,031 B / 411 行 | 2026-11-04 23:26 | 请求审计 |
| `v1.log` | 270,960 B / 943 行 | 2026-11-05 12:45 | QQ 链路对账 |
| `empty_reply.log` | 248,113 B / 31 行 | 2026-11-03 13:22 | 模型吐空现场（**单行平均 8KB，说明截断逻辑生效但内容仍很肥**） |
| `errors.log` | 1,818 B / 10 行 | 2026-11-01 17:15 | |
| `health_report.json` | 1,681 B | 2026-11-05 13:07 | **旧快照**（见 3.5） |
| `inner_os.json` | 9,780 B | 2026-11-04 20:37 | 内心OS 队列 |
| `novelty_grams.json` | 45,901 B | 2026-11-04 20:47 | 新颖度基线 3851 gram |
| `novelty_grams.json.polluted-by-test-20261104` | 799 B | 2026-11-04 19:54 | 污染样本留存（安全网） |
| `people\owner.json` | 445 B | 2026-11-04 20:36 | 主人画像 |
| `people\qq_group_示例群__c260963ae2.json` | 477 B | 2026-11-03 01:06 | 群画像（哈希后缀） |
| `persona_sync.json` | 54 B | 2026-11-07 14:45【我跑 state_health 导致】 | `{"db":"b21841dd98aee985","file":"b21841dd98aee985"}` 两侧一致 |
| `role_model.json` | 211 B | 2026-11-05 14:20 | 角色层模型 |
| `system_model.json` | 195 B | 2026-11-05 14:20 | 系统层模型 |
| `gateway.token` | 48 B | 2026-11-02 11:15 | 鉴权 token |
| `gateway.pid` | 5 B | 2026-11-05 13:03 | **陈旧**：内容 `29096`，【实测】该 PID 已不存在 |
| `keys.json` | 1,253 B | 2026-11-04 19:46 | 明文 key 列表（ACL 已收紧） |
| `soul_state.json` / `soul_audit.jsonl` | 449 / 246 B | 2026-11-02 13:34 | 旧版状态，已不在 load_state 路径 |
| `cmd_config.json.bak-20261103-164453` | 10,544 B | 2026-11-03 01:46 | AstrBot 配置备份 |

**⚠ 我自己的操作对数据的改动（如实披露）**：跑 `acceptance_matrix.py` 改了 `system_state.json` 与 `system_audit.jsonl`（见 2.3）；跑 `MemoryBridge.recall()` 验证实验给 3 条文档的 metadata 加了 `use_count`/`last_used`（见 3.3 注）；`persona_sync.json` 是 `state_health` 触发的 `current_persona()` 顺带写回（内容哈希两侧相同，实质无变化）。**除此之外未写任何库**；`gateway/persona.md` **未被修改**【实测】SHA256 前 16 位 `DFC97349C9C0DFF5`、mtime 仍是 11-01 22:20。

### 3.2 `gateway.db`（只读打开，journal_mode=wal）

| 表 | 行数 | 备注 |
|---|---|---|
| `turns` | **604** | `sqlite_sequence` 记到 1526 → **历史上有 922 条被删**（internal 清理 + 迁移） |
| `events` | **21** | |
| `schedules` | **1** | |
| `pending` | **0** | 无待审批 |
| `sqlite_sequence` | 3 行 | turns=1526 / events=21 / schedules=1 |

**turns 分渠道【实测】**：

| channel | 条数 | 字符数 |
|---|---|---|
| `qq_private` | 346 | 13,163 |
| `qq_group:示例群` | 205 | 6,273 |
| `qq_unknown` | 27 | 1,370 |
| `NULL` | 17 | 233 |
| `pet` | 5 | 73 |
| `qq_group`（无群名） | 4 | 101 |
| **`system_log`** | **0** | ← 与文档一致 |
| **`internal`** | **0** | ← 11-06 已清空，与文档一致 |
| **合计** | **604** | |

role 分布：`user` 306 / `assistant` 298。session 只有一个：`main`（604）。
**时间跨度**：2026-10-29 22:32:40 → **2026-11-03 22:51:26**【实测】→ 距今（11-07 16:xx）**约 3.7 天无新对话**。
`path` 分布：`chat` 574 / `tool` 27 / `approve` 2 / `approval` 1。

**events 全 21 条【实测】**（kind / channel / status）：

| kind | channel | status | attempts | 条数 |
|---|---|---|---|---|
| `care_given` | `pet` | `responded` | 0 | 6（id 11/17/18/19/20/21） |
| `observation` | `qq_private` | `open` | 0 | 6 |
| `observation` | `qq_group:示例群` | `open` | 0 | 2 |
| `promise` | `qq_private` | `open` | 0 | 2（id 3、14） |
| `promise` | `qq_group:示例群` | `open` | 0 | 1（id 6） |
| `emotion` | `qq_private` | `open` | 0 | 1（id 16） |
| `emotion` | `qq_group:示例群` | `open` | 0 | 1（id 9） |
| `care_rejected` | `qq_group:示例群` | `open` | 0 | 1（id 10） |
| `deferred_care` | `qq_private` | `deferred` | 0 | 1（id 13，due_ts=11-04 01:50） |

→ **与文档"实测生产 4 条计划（#3 #6 #14 promise + #13 deferred_care）"完全吻合**【实测】，且全部 `attempts=0`（没触发过）。
⚠ 但注意：`observation`/`emotion` 两类共 11 条仍在库里，而 `gateway/config.json:73-78` 明写 `events_kinds_stateful` 只允许 `care_rejected/deferred_care/promise` 落库，`"observation/emotion 照抽但不再进库"`。**这 11 条是旧代码遗留**，不是 bug，但"events 只当状态机用"这个说法在生产数据上只对了 10/21。

**schedules 1 条【实测】**：`(id=1, due_ts=2026-11-04 01:50, kind='recheck_care', event_id=13, note='...问问她睡没...', fired=1)` → 已触发。

### 3.3 `livingmemory.db`（journal_mode=`delete`，注意：**不是 WAL**）

表：`documents`(13) / `documents_fts`(13) / `documents_fts_config`(1) / `documents_fts_data`(17) / `documents_fts_docsize`(13) / `documents_fts_idx`(14)。索引 4 个。`page_count=15`、`page_size=4096`。

**13 条文档全量【实测】**（正文合计仅 433 字符）：

| id | 正文摘要 | metadata 关键字段 |
|---|---|---|
| 49 | 主人：说自己是平静的人，喜欢独处，讨厌人群… | `memory_origin=dmn_extract_events`、`source_channel=qq_private`、`importance=0.6`、`create_time=1793688525`（11-03 14:48） |
| 51 | 凌晨一口气聊了39条，白天却空了12小时… | `daily_review`、`importance=0.55`、`create_time=11-03 22:28` |
| 52 | 他反复追问屌丝的定义… | 同上 |
| 53 | 他说'我也没有不高兴'的时候… | 同上 |
| 54 | 他凌晨一点还在弄升级… | 同上 |
| 55 | 他刚给我定了生日，11月1号… | 同上 |
| 56 | 他嘴硬说没不高兴… | 同上 |
| 57 | 他嘴硬说没不高兴，其实是在试探… | `create_time=11-05 21:25` |
| 58 | 他嘴硬否认时其实更在意… | 同上 |
| 59 | 他喜欢蓝色 | `create_time=11-05 21:27` |
| 60 | 他喜欢灰色 | 同上 |
| 61 | 他喜欢蓝色 | `create_time=11-05 21:28` |
| 62 | 他喜欢灰色 | 同上 |

**metadata 键频次【实测】**：`session_id`/`source_channel`/`importance`/`topics`/`key_facts`/`memory_origin`/`create_time`/`status` 各 **13**（全覆盖）；`use_count`/`last_used` 各 **3**（是我做 recall 实验写进去的，不是网关写的 —— 见下注）。

> 🔴 **`kind` 键一个都没有（0/13）**。`项目地图.md:327` 称"**`kind` 全是 `observation`**"，`:1546` 又说"13 条真实记忆，`kind` 全是 `observation`"。**数据里根本没有这个键**。之所以遗忘算法还能跑，是因为 `forgetting._normalize_kind(None)` 回落到默认 `observation`（`gateway/forgetting.py:202-210`、`DEFAULT_STABILITY_DAYS` 见 `:97`）。→ 这是"文档声称的数据形状"与"真实数据形状"的不一致。若按文档做数据迁移/统计，会得到全 0。
> 🔴 **`keywords` 键也是 0/13**（`项目地图.md:318` 描述的是"认知类记忆多一个 keywords"，生产上目前没有 cognition 类文档，所以不算矛盾 —— 但也没有证据支持它已生效）。

**注（我造成的那 3 条 `use_count`）**：为验证 `state_health.py` 的 `MemoryBridge().health()` 是否写库，我单独跑了一次 `MemoryBridge(...).recall("主人是什么样的人","qq_private",top_k=1)`，命中文档 51，而 `memory_bridge.py:408-415` 会对**所有返回给模型的 hit** 调 `store.touch_recall()` → `memory/store.py:290-309` 直接 `update documents set metadata=...`。这一条**顺带证明了另一个事实：`recall()` 会写库**（对生产只有"记录被用过"的正面意义，属设计内行为，不是缺陷）。

### 3.4 状态文件实际键（`system_state.json`）

**我在跑 `acceptance_matrix.py` 之前**的 19 个键（这才是审计开始时的真实状态）：
```
version, emotion(=calm), focus, memory, recent_thoughts, last_tick_ts, last_speak_ts,
speak_count_today, last_consolidate_id, consolidate_wm, last_consolidate_ts,
last_persona_review_id, last_seen_turn_id, last_presence_state,
last_daily_review_day, last_daily_review_ts, last_daily_review_failed_ts,
last_daily_review_probe_day, last_long_memory_prune_ts
```
→ **`mood_value` / `mood_label` / `mood_reason` / `mood_updated_ts` 四个键确实不存在**，`last_system_log_id` 也不存在。**这印证了 `项目地图.md:200` / `:317` / `:621-627` 第 61 条的判断**（"生产上这四个键至今一个都没有"）—— 这条文档是对的【实测】。

`internal` 渠道清理后的水位键 `last_seen_turn_id=1469`，而当前 `turns` 最大 id 未超过该值（`sqlite_sequence.turns=1526` 但现存 604 行）→ 水位比若干被删行还大，属清理后的正常残留【推断】。

**`inner_os.json`**【实测】：`{"items": [...20 条...]}`，**20 条全部 `state=delivered`**、**20 条全部 `channel=owner`**、type 分布 `insight` 19 / `care_suggestion` 1。
→ 与 `health_report.json` 的 warning `"内心OS 积压 20 条（上限 20），可能没被角色层消费"` **表面上矛盾**：状态是 `delivered` 而非 `pending`，所以"积压"这个措辞不准确（`delivered` = 已读，只是没被 `acted/declined/expired` 收尾）。这是体检文案与状态机语义的不一致（P2）。

**`people\`**【实测】：2 个文件 `owner.json`（445B）+ `qq_group_示例群__c260963ae2.json`（477B），profile 字段都是 5 个：`称呼` / `作息` / `情绪基调` / `在意的事` / `互动习惯`。**与文档"2 份画像（owner + 群，哈希命名已迁移）"一致**。

### 3.5 `health_report.json` 陈旧度

| 字段 | 报告里 | 现在实际 | 差 |
|---|---|---|---|
| `ts` | 2026-11-05 13:07 | — | 2.1 天前 |
| `stats.turns` | **636** | **604** | -32（正是 internal 清理掉的量） |
| `stats.sources.internal` | **32** | **0** | -32 |
| `long_memory_docs` / `long_memory_vectors` | **7 / 7** | **13 / 13** | +6 |
| `inner_os` | 20 | 20 | ✓ |
| `persona_len` | 2856 | — | |
| `problems` | 3 条 warn | — | `inner_os 积压 20`、`audit 3 次"该成功但失败" {speak_fail:5, recall_timeout:3}`、`long_memory 没有 memory_write_ops 表` |

→ `项目地图.md:369` 已经**自己指出**这份报告是 11-05 旧快照（"库里此刻实际是 13 条 —— 体检要重新跑一次才准"）。**文档是对的**，但这也意味着：**凡是以 `health_report.json` 为数据源的结论都慢了 2 天**。

### 3.6 遗忘算法实测（用真实数据只读跑了一遍 `forgetting.plan_prune`）

```
scanned=13  keep=13  drop=0  review=0  never_forget=0  no_timestamp=0  boosted=0
每条: kind=observation(默认) S=30.0d
  R 值范围 0.8752 ~ 1.0000
  其中 3 条 timestamp_source=last_used（age=0.00d, R=1.0）—— 这 3 条正是我刚做 recall 实验写进去的 use_count
```
与 `项目地图.md:377` / `:1714` 声称的"R 都在 0.89~0.97"**基本吻合**（我这边多出 1.0000 是因为我的 recall 实验把 3 条的 `last_used` 更新到了"现在"，属我造成的偏差，已披露）。**`drop=0 review=0` 与文档一致。**

### 3.7 QQ 侧库（按分工只做存在性 + 大小）

| 库 | 大小 | 最后修改 |
|---|---|---|
| `qq\astrbot\data\data_v4.db` | 5,177,344 B | 2026-11-04 07:58 |
| `...\plugin_data\astrbot_plugin_self_learning\messages.db` | 3,309,568 B | 2026-11-05 12:45 |
| `...\astrbot_plugin_livingmemory\livingmemory.db` | 884,736 B | 2026-11-03 14:51（**已停用，不再写入**） |
| `...\livingmemory.index` | 53,442 B | 2026-11-03 14:24 |
| `...\livingmemory_graph_documents.db` | 86,016 B | 2026-11-03 14:23 |
| `...\conversations.db` | 737,280 B | 2026-11-03 14:51 |
| `qq\astrbot\data\cmd_config.json` | 10,169 B | 2026-11-03 16:45 |

插件库 11-03 之后无写入 → **停用状态属实**【实测】。

---

## 四、git 状态与风险

### 4.1 基线

| 项 | 值 |
|---|---|
| HEAD | `4e44453d2efe97fe8198b1e5f87c39c57f67070a` |
| 提交时间 | 2026-11-05 14:25:35 +0800 |
| 标题 | `fix(test): 测试会污染真实 config.json —— 我加的回写逻辑踩到了旧 fixture` |
| 分支 | master |
| 跟踪文件总数 | **169** |
| `.git` 体积 | 7.2 MB |
| 工作树 | **14 个已修改 + 16 个未跟踪**（`git status --porcelain` 顶层项；`--untracked-files=all` 会把 `audit/` 拆成 2 个文件、plan 文档 6 个） |
| `git diff --stat` | **2819 insertions / 119 deletions**（14 个已跟踪文件，不含未跟踪） |

> 📌 **与 Lead 给我的两条数字有出入，以我的实测为准并在此说明**：
> - Lead 说"15 个未跟踪"，我数到 **16**（多的是 `audit/` 目录，里面是另一位审计者的 `_probe_data.py` / `_probe_data2.py`；若只数文件则 `audit/` 下 2 + 4 引擎 + 5 测试 + 6 plan 文档 = 17，口径不同）。
> - Lead 说"项目地图.md 1088 行左右"，**实测 1767 行**（`\n` 计 1767、无 CRLF；末尾有换行故 `wc -l` 同值 1767）。用 `(Get-Content).Count` 也是 1767，**不是 Lead 说的"PowerShell 少算"**。HEAD 版本是 1191 行，`git diff --numstat` 显示 **+589/−13**（1767 ≈ 1191+589−13 = 1767 ✓ 自洽）。任务书里的"1085 行"同样不符。**这条我建议对外统一用 1767 行。**

### 4.2 14 个已修改文件（`git diff --numstat` 实测）

| 文件 | + | − | 是什么 |
|---|---|---|---|
| `gateway/system.py` | **1349** | 60 | DMN 四引擎接线（心跳/巩固/梳理/遗忘/计划/心情） |
| `gateway/memory_bridge.py` | 278 | 6 | `touch_recall` / `forget_plan` / `review_candidates` / `apply_review` / `cognition_only` / `kind` / `create_time` |
| `gateway/gateway.py` | 88 | 7 | `_note_reply_mood`、`mood_prompt_line` 注入、`max_tool_rounds` |
| `gateway/consolidate.md` | 74 | 0 | `system_log` 段说明 + "不许从里面抽事实" |
| `gateway/role_speak.py` | 67 | 1 | 计划触发/收尾文案 |
| `gateway/memory/store.py` | 66 | 0 | `list_documents` / `touch_recall` |
| `gateway/migrate_channel.py` | 32 | 9 | `relabel_internal()` SQL 预筛 |
| `gateway/config.json` | 23 | 2 | `system.forgetting` / `plan_silence_days` / `mood` 占位 / `max_tool_rounds` 挪段 |
| `gateway/tests/test_tool_overflow.py` | 121 | 0 | 新增用例 |
| `gateway/tests/test_daily_review.py` | 40 | 9 | 适配 |
| `gateway/tests/test_daily_review_sinks.py` | 42 | 2 | 适配 |
| `gateway/tests/test_channel_isolation.py` | 26 | 5 | 适配（`system_log` 渠道） |
| `gateway/tests/test_model_errors_health.py` | 24 | 5 | 适配 |
| `项目地图.md` | **589** | 13 | 第十四节全文（DMN 改造） |

### 4.3 16 个未跟踪（**这是真正的风险**）

| 路径 | 大小 | 是什么 |
|---|---|---|
| `gateway/mood.py` | 16,170 B | 心情引擎（424 行） |
| `gateway/plan.py` | 20,110 B | 计划状态机（455 行） |
| `gateway/forgetting.py` | 42,736 B | 遗忘算法（1050 行） |
| `gateway/cognition.py` | 44,867 B | 认知准入（1175 行） |
| `gateway/tests/test_mood.py` | 18,984 B | 102 条 |
| `gateway/tests/test_plan.py` | 13,658 B | 30 条 |
| `gateway/tests/test_forgetting.py` | 26,957 B | 60 条 |
| `gateway/tests/test_cognition.py` | 28,628 B | 119 条 |
| `gateway/tests/test_dmn_20261106.py` | 90,015 B | 79 条（集成测试） |
| `plan/DMN审核.md` | 114,045 B | 2857 行，本轮设计真源 |
| `plan/完工报告-DMN改造-2026-11-06.md` | 17,241 B | 338 行 |
| `plan/审计报告-全局-2026-11-06.md` | 17,389 B | 421 行 |
| `plan/项目审计-2026-11-06.md` | 33,382 B | 658 行 |
| `plan/查证-D1-DashScope兼容模式缓存.md` | 19,917 B | 271 行 |
| `plan/.md` | 3,187 B | **无文件名**（只有扩展名），内容是 DMN 完成度评估（"机制约 76%，效果约 40%"） |
| `audit/` | 2 文件 | 另一位审计者的探针脚本 |

**风险**：
1. 🔴 **DMN 四个引擎 + 5 个测试文件是"1061 条测试全绿"的全部内容来源，却完全未跟踪**。任何人执行 `git clean -fd`、`git stash -u`、或重新 clone，都会**永久失去这一轮的全部成果**（4 个引擎 3,300 行 + 5 个测试 2,900 行）。
2. 🔴 **HEAD 之后无任何 commit**：工作树 = 唯一副本。`git reset --hard HEAD` **不会**丢未跟踪文件，但会丢 14 个已修改文件里的全部 DMN 接线（含 `system.py` 的 +1349 行）→ **回滚风险极高，且无法"回滚到 11-06 状态"**，因为那个状态从未被 commit。
3. 🟡 `plan/.md` 这个文件在 Windows 上是合法的（`name=".md"`），但**几乎所有工具都会把它当隐藏文件或忽略**；`git add` 时需要 `-f`。建议改名。
4. 🟡 `audit/` 被未跟踪，`.gitignore` 里**没有** `audit/` 规则 → 未来 commit 时会把审计中间产物（`_probe_data.py` 等）一起带进仓库。

### 4.4 `.gitignore` 是否漏网（重点核查 5.5GB assets）

**【实测】`git ls-files` 结果**：

| 路径 | 跟踪文件数 | `.gitignore` 规则 | 结论 |
|---|---|---|---|
| `desktop/assets/` | **0** | `:21` `desktop/assets/` | ✅ 正确排除 |
| `desktop/web/vendor/` | **0** | `:22` `desktop/web/vendor/` | ✅ 正确排除 |
| `desktop/desktop/node_modules/` | **0**（全仓 `node_modules` 0 个） | `:6`、`:23` | ✅ 正确排除 |
| `gateway/data/` | **0** | `:48` `gateway/data/` | ✅ 正确排除（真实记忆不入库） |
| `gateway/secrets.json` | **0** | `:47` | ✅ 正确排除 |
| `qq/SnowLuma/`、`qq/astrbot/`、`qq/qq-bridge/`、`qq/_归档/` | **0** | `:30-33` | ✅ 正确排除（`qq/` 只跟踪 6 个文档/脚本） |
| `*.db` / `*.index` / `*.log` / `*.bak*` | **0** | `:88-92`、`:57-59`、`:62-63` | ✅ |

**大文件实测**（本机 >1MB 且未被跟踪的文件，按大小排序）：

```
1,098,339,328 B  desktop\assets\...\torch\lib\torch_cuda.dll          [ignored]
  602,384,936 B  desktop\assets\...\torch\lib\cudnn_cnn_infer64_8.dll  [ignored]
  544,070,144 B  desktop\assets\...\torch\lib\cublasLt64_11.dll        [ignored]
  479,276,657 B  desktop\assets\...\G_953000.pth                       [ignored]
  ...
  210,149,888 B  desktop\desktop\node_modules\electron\dist\electron.exe [ignored]
```
→ **`desktop/assets` 实测 5.43 GB / 10,125 个文件，全部未被跟踪**；`desktop/` 整体 5,902 MB / 11,227 文件，`qq/` 1,687 MB / 47,388 文件。而**整个 `.git` 只有 7.2 MB**、**被跟踪文件只有 169 个**、**最大被跟踪文件是 `gateway/system.py` 315 KB**。
→ ✅ **结论：`.gitignore` 没有漏网，5.5GB 素材完全正确地被排除在版本库之外。** 这是本项审计里少数"文档说对了且代码也做对了"的部分（`install.ps1:77-102` 还会主动检查这两个目录缺失并给出提示，配 `gateway/tests/test_script_encoding.py:322-347` 的守门用例）。

### 4.5 工作树比文档新 —— 文档一致性的**头号结论**

按 mtime 排序【实测】（Lead 提供的清单我已逐条复核，全部成立）：

| 文件 | mtime | 文档最后更新 | 差 |
|---|---|---|---|
| `gateway/system.py` | **2026-11-07 01:21:53** | 项目地图 11-06 22:36 | **+2h45m** |
| `gateway/plan.py` | **2026-11-07 01:21:40** | 同上 | +2h45m |
| `gateway/migrate_channel.py` | 2026-11-07 01:03:33 | | +2h33m |
| `gateway/gateway.py` | 2026-11-07 01:01:47 | | +2h31m |
| `gateway/consolidate.md` | 2026-11-07 01:01:27 | | |
| `gateway/config.json` | 2026-11-07 01:01:59 | | |
| `gateway/memory_bridge.py` | 2026-11-07 00:59:58 | | |
| `gateway/cognition.py` | 2026-11-06 20:27:33 | | |
| `gateway/role_speak.py` | 2026-11-06 23:39:36 | | +1h03m |
| `项目地图.md` | 2026-11-06 22:36:04 | —（基准） | — |

→ **`项目地图.md` 里第十四节的所有数字（行数、条数、diff 规模）都是 11-06 22:1x~22:3x 的快照，而代码在 11-07 00:59~01:21 又被改了一轮**。这就是下面第五节里绝大多数不一致的**共同根因**——不是文档写错，而是**文档写完之后代码又动了，没有回填**。
> 佐证：`项目地图.md:1358-1362` 自己已经写了"本节的数字全部来自 2026-11-06 22:1x~22:3x 的实测……`plan\完工报告-DMN改造-2026-11-06.md` 里有一部分行数是写报告时的快照，已经和现在不一致"。**文档作者知道这个机制，但只校准了到 11-06 22:3x，没覆盖 11-07 01:2x 那一轮。**

---

## 五、测试基线复核结果

### 5.1 实测（我跑了两次：全量 + 分目录）

| 命令 | 结果 | 耗时 |
|---|---|---|
| `pytest gateway/tests desktop/tests -q` | **1138 passed, 1 skipped, 1 warning** | 104.93 s |
| `pytest gateway/tests -q` | **1012 passed, 1 skipped** | 82.80 s |
| `pytest desktop/tests -q` | **126 passed** | 23.24 s |

（1012 + 126 = 1138 ✓ 自洽；Python 用 `<venv>\Scripts\python.exe` = **3.12.11**，pytest 9.1.1。系统 Python 3.13.2 **没装 pytest**，所以文档里那句"`cd gateway && python -m pytest tests -q`"直接用系统 python 会失败。）

### 5.2 与文档声称的对照

| 口径 | 文档声称 | 实测 | 差 |
|---|---|---|---|
| 全量 | **1061 passed / 1 skipped** | **1138 passed / 1 skipped** | **+77** |
| 只 gateway | **935** | **1012** | **+77** |
| 只 desktop | **126** | **126** | ✓ |
| gateway 测试文件数 | 51 | **51** | ✓ |
| desktop 测试文件数 | 10 | **10** | ✓ |
| 11-06 新增 5 文件合计 | **329**（102+20+60+119+28） | **390**（102+**30**+60+119+**79**） | **+61** |
| `test_plan.py` | 20 条 / 168 行 | **30 条 / 310 行** | +10 / +142 |
| `test_dmn_20261106.py` | 28 条 / 597 行 | **79 条 / 1751 行** | +51 / +1154 |
| `test_mood.py` | 102 条 / 477 行 | 102 条 / 477 行 | ✓ |
| `test_forgetting.py` | 60 条 / 634 行 | 60 条 / 634 行 | ✓ |
| `test_cognition.py` | 119 条 / 665 行 | 119 条 / 665 行 | ✓ |
| `完工报告:269` "4 个文件期望 301 passed" | 301 | **311**（102+30+60+119） | +10 |

**结论**：`1061` 这个数是 **11-06 22:3x 快照**。11-07 01:2x 那一轮改 `plan.py`(+177 行) 和 `test_dmn_20261106.py`(+1154 行) 之后，基线已经涨到 **1138**。文档没有回填。

### 5.3 数据隔离验证（这是"敢不敢跑"的关键，已实测确认安全）

`gateway/tests/conftest.py`（288 行）做了两层兜底：
1. **导入期**（`:62-88`）：在 `import gateway/system/persona_sync` **之前**就把 `GATEWAY_DATA_DIR` 指到 `tempfile.mkdtemp()`；`:91-143` 把 `persona_sync` 的 `PERSONA_PATH` / `ASTRBOT_DB` / `STATE_PATH` 全部换成临时副本（影子库内容与影子人格一致 → 走"已对齐"分支，既不写库也不写文件）。
2. **用例级**（`:228-239` autouse）：每条用例一个 `tmp_path` + 建好 `turns/pending/events/schedules` schema，并把 `gateway.*` / `system.*` 共 12 个路径常量 + `SYSTEM["long_memory_db_path"]` 全部指过去。
3. **出网点守门**（`:265-288` autouse）：把 `DashScopeEmbedding.get_embedding(s)` 换成抛错 → 忘 mock 的用例**立刻红**，而不是偷偷花钱打阿里云。

**【实测】跑前跑后指纹对照**：

| 项 | 跑前 | 跑后 |
|---|---|---|
| `gateway\data` 文件 hash（6 个关键文件） | — | **全等**（我做的 SHA256 对照实验） |
| `turns` / `events` / `schedules` / `pending` | 604 / 21 / 1 / 0 | **604 / 21 / 1 / 0** |
| `documents` | 13 | **13** |
| `gateway/persona.md` SHA256(16) | `DFC97349C9C0DFF5` | **`DFC97349C9C0DFF5`** |
| `persona.md` mtime | 2026-11-01 22:20:45 | **未被改动** |

→ ✅ **测试可以放心跑，隔离有效**。`项目地图.md:355` 那段"跑完生产 `gateway\data` 与真人格逐字节不变"**是真的**【实测确认】。

### 5.4 未能复核的项

- `完工报告:75` 称"完工后的全量测试**连跑了 3 次**，每次都是 1061 passed / 1 skipped" → **未复核**（`项目地图.md:1697-1698` 自己也说"没复核第三次"）。我只跑了 1 次全量 + 1 次分目录。
- `731 passed` 基线 → **无法复核**（那是 11-06 之前的历史状态，已不可重现）。
- desktop 侧的 126 条是否随 DMN 改动受影响 → **未逐条核对**（desktop 无 DMN 改动，只跑了总数）。

---

## 六、文档不一致清单（逐条给双方证据）

> 共 **19 条**。严重度：🔴 会误导判断/影响操作　🟠 数字失真但结论不变　🟡 断链/措辞
> 共同根因见 4.5：**项目地图.md 定稿于 11-06 22:36，代码在 11-07 00:59~01:21 又改了一轮**。

### D-01 🔴 `restart.ps1` 会杀全机 uv 宿主 python，文档说"只杀本项目"

- **文档【文档】**：`项目地图.md:756` — "**`restart.ps1` 不再按 `Python313` 全局匹配**，只杀本项目的 python。"
- **注解【文档】**：`restart.ps1:120-123` — "过滤条件必须是「本项目的路径片段」，不能是解释器名字。曾经这里还有一条按解释器版本匹配的过滤项：那会杀掉本机所有用 Python 3.13 的进程，跟第 19 行的承诺正好相反。已删除。"
- **代码【读码】**：`restart.ps1:130` — `Stop-ByProcessName @('python') @("小鱼\qq\astrbot", "xiaoyu\qq\astrbot", "uv\python") 'AstrBot python'` ← **`"uv\python"` 还在，它不是项目路径片段**
- **实测【实测】**：用 `[regex]::Escape("uv\python")` 对样例命令行匹配：
  ```
  MATCH  ...\Roaming\uv\python\cpython-3.12.11-...\python.exe -m pytest gateway/tests
  MATCH  D:\OtherProject\.venv\Scripts\python.exe uv\python-like
  MATCH  C:\SomeOtherTool\uv\python\cpython-3.11\python.exe app.py
  -      <venv>\Scripts\python.exe -u main.py   ← 反而不匹配！
  ```
  → 这个过滤项**既误伤全机（前 3 条本不该杀），又对本项目 AstrBot 的 venv 宿主无效**（第 4 条是 astrbot 的真实形态，不含 `uv\python`；实际的 AstrBot 若是 uv 安装才命中）。
- **根因**：`plan/小鱼-执行计划-2026-11-04.md:912` 记录"P1-1 ✅ `"uv\\python"` → `"uv\python"`（双反斜杠改单）"—— 只修了正则转义 bug，**没质疑这个过滤项本身该不该存在**。`代码审查报告:1104` 当初报的是"永远匹配不上"，修成"能匹配上"之后**就变成了能杀全机**。

### D-02 🔴 `tools/acceptance_matrix.py` 自称只读，实际写生产状态

- **文档【文档】**：`项目地图.md:1310` — "全是独立 Python 脚本，**除 e2e 外都只读**。"
- **代码自述【读码】**：`tools/acceptance_matrix.py:14-15` — "它不需要起服务也能跑大部分（静态检查 + 单测级验证）"
- **实际【读码】**：`:306` `_sys_mod._maybe_prune_long_memory(dict(st))` → `gateway/system.py:4102-4105` `fresh = load_state(); fresh["last_long_memory_prune_ts"] = time.time(); save_state(fresh)` + `_audit(...)`
- **实测证据【实测】**：见 §2.3 表格（19 键 → 24 键；`last_long_memory_prune_ts` 从 11-04 15:07 被改成 11-07 14:43；`system_audit.jsonl` +1 行 `long_memory_prune`）。
- **放大效应**：`verify_all.py:62-71` 默认就把这项当 STEPS[1] 跑 → **"一键验收"也写生产**。

### D-03 🔴 项目地图行数：任务书/Lead 口径与实测均不符

- **任务书【文档】**："一份 **1085 行**的 项目地图.md（分14节）"
- **Lead【文档】**："项目地图.md（**1088 行**左右，14 节）"
- **实测【实测】**：`bytes=144576`、`LF=1767`、`CRLF=0`、末尾有换行 → **1767 行**。`(Get-Content).Count` 也是 1767（**不存在"PowerShell 少算"**）。
- **git 侧自洽性验证**：HEAD 版本 1191 行，`git diff --numstat` = +589/−13 → 1191+589−13 = **1767** ✓
- **"14 节"是对的**：`^## ` 标题实测 14 个（HEAD 版本 13 个，第十四节是工作树新增）✓

### D-04 🟠 `system.py` 行数：文档 5865，实测 6563

- **文档【文档】**：`项目地图.md:84` "`system.py` 已经 **5865 行**"；`:169` "**5865 行，全仓最大**"
- **实测【实测】**：`bytes=315405`、`LF=6563`、`CRLF=6563` → **6563 行**（+698）
- 另一份文档 `plan/项目审计-2026-11-06.md:33` 又说 **6075** —— 同一事实三个数（5865 / 6075 / 6563）

### D-05 🟠 `plan.py` 行数：文档 278，实测 455

- **文档【文档】**：`项目地图.md:171` "（新 11-06，**278 行**）"；`:1431` "### 14.3 计划（`plan.py`，**278 行**）"
- **实测【实测】**：`bytes=20110`、`LF=455` → **455 行**（+177）
- **交叉证据**：`plan/完工报告:18` 说 **229 行**（更早的快照）；三个数 229 / 278 / 455 → **`plan.py` 在 11-06 之后被大幅扩写过**

### D-06 🟠 测试文件行数/条数：`test_plan` 与 `test_dmn_20261106` 双错

- **文档【文档】**：`项目地图.md:212` — "`test_plan.py`(**168** 行/**20** 条) / … / `test_dmn_20261106.py`(**597**/28)"
- **实测【实测】**：`test_plan.py` = **310 行 / 30 条**（`--collect-only` 得 30）；`test_dmn_20261106.py` = **1751 行 / 79 条**
- 合计：文档 329 → 实测 **390**

### D-07 🟠 测试总数：文档 1061，实测 1138

- **文档【文档】**：`项目地图.md:16`、`:212`、`:1693`、`:1703`（"# 期望：1061 passed, 1 skipped"）、`完工报告:6`、`:71`、`:265`
- **实测【实测】**：`1138 passed, 1 skipped`（gateway 1012 + desktop 126）
- **只 gateway**：文档 `:1694` 说 935 → 实测 **1012**

### D-08 🟠 `acceptance_matrix` 项数：文档 61，实测 53

- **文档【文档】**：`项目地图.md:1316` — "T-01~T-12 + P1×11 的验收条件还成立吗（**61 项**）"
- **实测【实测】**：脚本自己汇总 `=== 汇总：53/53 项通过 ===`（`tools/acceptance_matrix.py:467`）
- 另一份文档 `plan/小鱼-执行计划-2026-11-04.md:1123` 说 "**52→53 项**" —— **这份是对的**，地图的 61 错

### D-09 🟠 工作树变动项数：文档 22，实测 30

- **文档【文档】**：`项目地图.md:13` "**未提交 git，22 项全在工作树**"；`:1358` "当前 commit `4e44453`，工作树 **22 项**变动"
- **实测【实测】**：`git status --porcelain` = **14 已修改 + 16 未跟踪 = 30 项**

### D-10 🟠 第十四节 diff 规模：文档 +1075/−46 与 9+9 文件，实测不同

- **文档【文档】**：`项目地图.md:1376` — "规模：**改 9 个文件 +1075/−46，新增 9 个文件（4 引擎 + 5 测试）**，`system.py` 一个文件就 +651"
- **实测【实测】**：
  - 已跟踪代码文件 diff（8 个 gateway 文件）= **+1977 / −85**（`system.py` 单个 = **+1349/−60**，不是 +651）
  - 未跟踪 = 4 引擎 + 5 测试 + `audit/` + **6 份 plan 文档** = 16 项（不是 9 个）
- 括号里的"4 引擎 + 5 测试"是对的 ✓，但"9 个文件"漏了 6 份 plan 文档

### D-11 🟠 `documents.metadata.kind`：文档称"全是 observation"，数据里**没有这个键**

- **文档【文档】**：`项目地图.md:327` — "**实测 13 条文档**，`kind` 全是 `observation`（11-06 之前写的，没有新字段）"；`:1546` — "13 条真实记忆，`kind` 全是 `observation`"；`:319` 还描述了 `kind` 的五种取值
- **实测【实测】**：13 条文档的 metadata **键频次** `{session_id:13, source_channel:13, importance:13, topics:13, key_facts:13, memory_origin:13, create_time:13, status:13}` → **`kind` 出现 0 次**，`json_extract(metadata,'$.kind')` 全 NULL
- **真相**：能跑是因为 `forgetting._normalize_kind(None)` 回落到默认 `observation`（`gateway/forgetting.py:202-210`）。文档写的是**算法视角的等效值**，不是**数据的真实形状**。→ 若按文档做数据迁移/统计会得全 0。
- **同类**：`keywords` 键也是 0/13（文档 `:318` 描述为"认知类记忆多一个"，生产上无 cognition 文档，不算矛盾但无证据）

### D-12 🟠 全仓 "只读"承诺被 `verify_all` 继承污染（与 D-02 同源，单列为流程问题）

- **文档【文档】**：`项目地图.md:1314` — "`verify_all.py` | **全部**（串起下面四个，给一句结论）"
- **实测【实测】**：`verify_all.py` 默认跑 state_health + **acceptance_matrix** + m6 → 只要它跑一次，就带一次 D-02 的生产写入

### D-13 🟡 `restart.ps1` 注释引用不存在的 `重启服务.bat`

- **文档【读码】**：`restart.ps1:2` — "用法：双击 **重启服务.bat**（或在 cmd 里跑）"
- **实测【实测】**：根目录 `.bat` 只有 `restart.bat` / `启动.bat` / `安装.bat`，**没有** `重启服务.bat`
- 地图里也没提这个文件名（`项目地图.md:344` 正确写的是"双击根目录 `restart.bat`"）→ 只有 `restart.ps1` 的注释是旧的

### D-14 🟡 项目地图引用不存在的 `plan\小鱼-总计划.md`

- **文档【文档】**：`项目地图.md:18` — "**计划与待办看 `plan\小鱼-总计划.md`**（2026-11-03 把 plan\ / deliverables\ / 桌面上的 23 份旧计划去重合并成这一份…）"
- **实测【实测】**：`plan\` 下 12 个文件，**无 `小鱼-总计划.md`**。实际存在的是 `plan\.md`（3187 B，无标题，内容是 DMN 完成度评估"机制约 76%，效果约 40%"）→ **疑似就是它被改名/误存成了 `.md`**
- 另有 `项目地图.md:369` 引用 "`plan\小鱼-总计划.md` P0-3" → 同样断链

### D-15 🟡 `gateway\README.md` 对 `stop.cmd` 的描述已过时

- **文档【文档】**：`gateway/README.md:14` — "停止：`./stop.cmd` / `./stop.sh`（**按 `data/gateway.pid` 找进程**；Windows 上直接关窗口带不走…）"
- **代码【读码】**：`gateway/stop.cmd:29-50` 已重写为"**按端口 8898 找 owner**，pid 文件只在端口没了时当线索"，且 `:57-77` 校验命令行含 `gateway.py` 才杀
- 即：README 描述的是**重写前**的行为（而 `stop.cmd:5-15` 的注释里完整记录了这次重写）

### D-16 🟡 `安装说明.md` 称 gateway "纯标准库，零依赖"

- **文档【文档】**：`安装说明.md:54` — "`gateway\` | 对话网关（人格、工具、记忆、审批）。**纯标准库，零依赖**"
- **实测【实测】**：`gateway/requirements.txt` 有 9 个第三方依赖（`faiss-cpu / numpy / jieba / sqlalchemy / sqlmodel / greenlet / aiosqlite / aiofiles / networkx`）；`install.ps1:114` 明确查 7 个包；`install.ps1:105-108` 的注释也在讲"以前不查 pip 包，装完全绿但网关起不来"
- → **同一份说明书里，第 54 行说零依赖、第 12-16 行又在讲装 Python，内部矛盾**

### D-17 🟡 地图称 `:8787` 的 `shim.py` 已清掉，实测仍在监听

- **文档【文档】**：`项目地图.md:225` — "`qq-bridge\` | **旧方案**（DSH 桥接），已停用；**`:8787` 的 `shim.py` 也已清掉**"
- **实测【实测】**：
  ```
  Get-NetTCPConnection -LocalPort 8787  →  127.0.0.1:8787  OwningProcess=10028
  Win32_Process 10028: python.exe -u shim.py --port 8787 --deadline 90 --confirm-timeout 45
                       --approval auto --auto-reload-delay 5
  shim.py 实际在 E:\workplase\dmagent\shim.py（2026-10-24 10:05）
  ```
  → `<repo>` 内确实没有 shim.py（这部分对），但**8787 端口仍被外部进程（E: 盘的 dmagent）占着**，且它就是地图说的那个"旧方案"。**"已清掉"只对仓库成立，对运行环境不成立。**
- 影响：`qq\启动-小鱼.ps1:88` 守卫的是 **3100**（旧 qq-bridge），不是 8787 → 这条守卫抓不到 8787 上的旧 shim

### D-18 🟡 `health_report.json` 与库不符（文档已自认，仍列出以便交叉引用）

- **数据【实测】**：`stats.turns=636` vs 实际 604；`sources.internal=32` vs 实际 0；`long_memory_docs=7` vs 实际 13；`ts` = 11-05 13:07
- **文档【文档】**：`项目地图.md:369` **已经指出**这一点（"那份报告是 11-05 的旧快照……体检要重新跑一次才准"）→ **文档正确，但体检报告本身陈旧**
- **附带**：`problems` 里 `inner_os 积压 20 条（上限 20）` 与实测"20 条全部 `delivered`"语义不符（`delivered` 是已读，不是待消费）→ 体检文案问题

### D-19 🟡 地图"精度到端口"的表述与实测端口清单不完全吻合

- **文档【文档】**：`项目地图.md:116` — "`desktop\` … 8848，语音 **8849（ws 8850）**"；`:118` — "`qq\` … 5099 / 3000 / 6185"
- **实测【实测】**：
  - 8849 ✅ 有代码依据（`desktop/config.json:30` `"port": 8849`、`server.py:701` `ENGINE_PORT`、`tts_breeze.py:270`）
  - **8850 只在 `desktop/BREEZE_PLAN.md:74,142,144` 出现**（"ws 网关预留"），`tts_breeze.py:17` 也提"8849/8850"但那是描述坑的文案；**没有代码在读/写 8850** → **8850 是否真实监听：未确认**
  - `qq\` 的 **6199**（AstrBot 的 ws_reverse 端口，`qq\astrbot\data\cmd_config.json:319` 有 `"ws_reverse_port": 6199`）和 **3001**（`main.js:768` QQ_PORTS 里有）**地图这两个表都没列**，但 `start.ps1:144` / `status.ps1:35` 的端口表列了 6199、没列 3001
  - → **三处端口清单各不相同**：地图(5099/3000/6185) vs start.ps1(5099/3000/6185/6199/8848/8898) vs main.js(5099/3000/3001/6185/6199)

---

## 七、问题清单

### P0（会破坏数据 / 越权 / 阻断验收）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| **P0-1** | `tools/acceptance_matrix.py` 的 T-07 段**写生产状态**：改 `system_state.json`（新增 4 个 mood 键 + `last_system_log_id`，并把 `last_long_memory_prune_ts` 推到"现在"）+ 追加 `system_audit.jsonl` | `tools/acceptance_matrix.py:293,306`；`gateway/system.py:4102-4105`；实测 19→24 键、`1793776045→1794033836` | ① 真实网关下次启动后 **24h 内不再跑遗忘**；② **抹掉"心情四字段未落盘"这个 bug 的现场证据**（第 61 条正是靠它证明）；③ 被 `verify_all.py` 默认链路继承 |
| **P0-2** | `restart.ps1:130` 的 `"uv\python"` 过滤项会**杀掉全机任意 uv 宿主 python**（含 IDE、其它项目、pytest） | `restart.ps1:130`；实测正则对 3 条非本项目样例全 MATCH、对 astrbot venv 反而 MATCH 不上 | "一键重启"变成全机 taskkill /T /F；与 `:120-123` 自己的承诺和 `地图:756` 直接矛盾 |
| **P0-3** | DMN 全部成果（4 引擎 3,300 行 + 5 测试 2,900 行 + 6 份 plan 文档）**完全未跟踪，且 HEAD 后无 commit** | `git status`：16 项 `??`；`git rev-list`：HEAD 之后 0 commit | `git clean -fd` / 重新 clone / 换机 = **永久丢失整轮成果**；无任何回滚点 |

### P1（会误导判断 / 影响可维护性）

| # | 问题 | 证据 |
|---|---|---|
| **P1-1** | 测试基线口径全错：文档 1061 / gateway 935 / 5 文件 329 → 实测 **1138 / 1012 / 390** | §5.2 表 |
| **P1-2** | 地图第十四节所有行数与 diff 规模失真：`system.py` 5865→**6563**、`plan.py` 278→**455**、`test_plan` 168行/20条→**310/30**、`test_dmn_20261106` 597/28→**1751/79**、工作树 22→**30** 项、diff +1075/−46→**+1977/−85** | D-04~D-10 |
| **P1-3** | `acceptance_matrix` 项数文档 61 → 实测 **53**（`小鱼-执行计划:1123` 的 53 才对） | D-08 |
| **P1-4** | `install.ps1` **无任何 `exit`**，体检失败也返回 0 → `安装.bat` 的失败分支永不可达 | `install.ps1` 全文无 exit；实测 exitcode=0；`安装.bat:6` |
| **P1-5** | 5 个 tools 脚本在**中文 Windows 默认 GBK 控制台直接跑会崩**（emoji 触发 `UnicodeEncodeError`），必须 `-X utf8` 或 `PYTHONIOENCODING` | 实测 `acceptance_matrix.py` 崩栈；5 个脚本都 print emoji，无一做兜底 |
| **P1-6** | `documents.metadata` 没有 `kind` 键，文档称"全是 observation" → 数据形状与文档不符 | D-11 |
| **P1-7** | 地图 `:18` 与 `:369` 引用的 `plan\小鱼-总计划.md` **不存在**（疑似被存成 `plan\.md`） | D-14 |
| **P1-8** | `8787` 上旧 `shim.py`（E:\workplase\dmagent）仍在监听，地图称"已清掉"；而 `qq\启动-小鱼.ps1:88` 的守卫只查 3100，抓不到它 | D-17 |
| **P1-9** | `audit/` 无 gitignore 规则 → 下次 commit 会把审计探针脚本带进仓库 | `.gitignore` 无 `audit/`；`git status` 见 `audit/` |
| **P1-10** | `plan\.md` 是只有扩展名的文件，`git add` 需 `-f`，多数工具会忽略 → 内容（DMN 完成度评估）有丢失风险 | `Test-Path 'plan\.md'` = True |

### P2（整洁度 / 措辞 / 次要不一致）

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | `restart.ps1:2` 引用不存在的 `重启服务.bat` | D-13 |
| P2-2 | `gateway/README.md:14` 对 `stop.cmd` 的描述是重写前的旧行为 | D-15 |
| P2-3 | `安装说明.md:54` 称 gateway"纯标准库零依赖"，与 `requirements.txt` 9 个依赖矛盾 | D-16 |
| P2-4 | `gateway/stop.sh:5-11` 盲信 pid 文件 + `pkill -f "gateway.py"`，安全等级低于已重写的 `stop.cmd` | 读码 |
| P2-5 | `desktop\启动.bat` 实际只起 `server.py`，不起 Electron/网关，名字像"启动桌宠" | 读码 |
| P2-6 | 端口清单三处不一致（地图 / start.ps1 / main.js），`status.ps1` 不含 8849 与 3001 | D-19 |
| P2-7 | `health_report.json` 陈旧 2 天（turns 636 vs 604、docs 7 vs 13） | D-18 |
| P2-8 | 体检文案"内心OS 积压 20 条"与实测"20 条全部 delivered"语义不符 | §3.4 |
| P2-9 | `gateway.pid` 内容 `29096` 对应的进程已不存在（陈旧 pid）；靠 `stop.cmd` 的命令行校验兜住 | 实测 |
| P2-10 | `install.ps1:114` 只查 7 个包，`requirements.txt` 有 9 个（漏 `aiofiles`/`networkx`） | 读码 |
| P2-11 | `events` 表 21 条里有 11 条 `observation`/`emotion`，而 `config.json:73-78` 声明这两类"不再进库" → 旧数据遗留，文档未说明 | 实测 + 读码 |
| P2-12 | `项目地图.md:369` / 完工报告的行数快照与当前代码差 1~2 轮（地图自己已声明"以本节实测为准"，但 §14 本身也过期了） | §4.5 |

---

## 八、待核实（不确定，不编）

| # | 待核实项 | 为什么没确认 |
|---|---|---|
| 1 | **`8850` 端口是否真实监听** | 仅 `desktop/BREEZE_PLAN.md:74,142,144` 提到"ws 网关预留"；`tts_breeze.py:17` 只是描述坑的文案；**没有代码读/写 8850**，服务未启动也无法实测 |
| 2 | **1061 这个基线是否曾经成立** | 只能确认现在 1138；11-06 那次的中间状态不可重现（未 commit） |
| 3 | **`e2e_smoke.py` 能否在隔离模式下真跑通** | 它要求 `GATEWAY_DATA_DIR` 指向隔离目录，而文档给的示例路径 `E:\workplase\_e2e_data` **不存在**（实测）；且必须起 2 个服务（本轮禁止），故未验证 |
| 4 | **语音引擎（3.5GB 模型）当前是否占着 8849** | 8849 无监听（实测），但 `breeze-server.exe` 是否以非监听状态残留未查 |
| 5 | **`完工报告:75`"连跑 3 次全绿"** | 只跑了 1 次全量，未复核 |
| 6 | **`项目地图.md:1299-1300` 的 30.7s / 48.9s 启动耗时** | 需真启动服务，本轮禁止 |
| 7 | **`plan\小鱼-总计划.md` 是改名还是丢失** | `plan\.md` 内容和地图描述（"待办合并"）不符，无法判定是否同一份 |
| 8 | **`audit/` 里 2 个探针脚本的作者归属** | 是另一位审计者的中间产物，非本任务产出，未动 |

---

## 九、附：本次审计对生产数据的全部影响（如实披露）

| 动作 | 对生产的改动 | 是否可逆 |
|---|---|---|
| `pytest gateway/tests desktop/tests -q` 全量 | **无**（conftest 双层隔离，实测 6 文件 hash 全等、persona.md `DFC97349C9C0DFF5` 未变） | — |
| `tools/state_health.py`（含 `--json` × 2） | **无**（hash 对照 6 文件全等，mtime 不变） | — |
| `tools/m6_acceptance.py` | **无**（只读 `mode=ro`） | — |
| `tools/e2e_smoke.py --no-model` | **无**（服务没起，exit 2 直接返回） | — |
| `tools/acceptance_matrix.py`（跑了 3 次） | ① `system_state.json` 19→24 键、`last_long_memory_prune_ts` → 11-07 14:43；② `system_audit.jsonl` +1 行 | ❌ 不可逆（未备份，且这正是 P0-1） |
| `MemoryBridge.recall()` 验证实验 1 次 | 3 条 documents 的 metadata 加 `use_count`/`last_used` | ❌ 不可逆（设计内行为，非缺陷） |
| `persona_sync.json` | 被 `current_persona()` 顺带写回，**两侧哈希值相同，实质无变化** | 无影响 |
| sqlite 全部打开 | 一律 `file:...?mode=ro` + `uri=True` | — |

> **未执行**：启动.bat / start.ps1 / restart.ps1 / status.ps1 的实际运行；任何 `taskkill`；任何 `--fix`；任何配置修改；任何 git commit/stash/reset。
