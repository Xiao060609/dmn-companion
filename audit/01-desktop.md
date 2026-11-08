# 01 · desktop（桌宠板块）审计报告

> 审计对象：`<repo>/desktop\`（Electron 外壳 + 本地 HTTP 服务 + 语音驱动 + 浏览器页面）
> 审计方式：只读代码 + 离线实测（不启动任何服务、不联网）
> 基线：git `4e44453`，`git status --porcelain -- desktop/` 为空（工作区干净）
> 标注约定：**【读码】**＝代码/文件里直接可见；**【文档】**＝文档声称；**【推断】**＝我的推理，未经运行验证

---

## 1. 一句话结论

桌宠板块是一个**自洽但已明显漂移**的四层本地栈（Electron 壳 → 8848 HTTP 服务 → 8898 网关 → 8849 语音引擎）：主链路（说话、表情、动作、触摸、语音）代码完整且有 126 条测试兜底；但 **11-04 那次安全加固把 11-02 刚上线的「自定义触摸区域」编辑器整块打死了**（实测 iframe 被 `X-Frame-Options: DENY` 拒载），**FastVITS 回滚路径也已断**（签名不兼容，实测 `TypeError`），并且 8848 这个后端**没有任何单实例/端口占用保护**（Windows 上两个实例能同时 bind 成功）。文档（README + 项目地图）比代码落后 1～2 轮，多处接口/引擎描述已不成立。

---

## 2. 板块树

### 2.1 ASCII 图

```
desktop\                                  ← 主板块（桌宠）
├── desktop\                              ← A. Electron 外壳（唯一真正的进程入口）
│   ├── main.js         1198 行          主进程：窗口/托盘/轮盘 IPC/拉起后端与网关/看门狗/退出
│   ├── preload.js        24 行          pet 窗口 IPC 桥（petBridge，12 个方法）
│   ├── chatbox-preload.js 6 行          chatbox 窗口 IPC 桥（petChat，onFocus/close）
│   ├── package.json      13 行          electron ^38.0.0（实装 38.8.6）
│   └── node_modules\    329.9 MB        Electron 运行时（不入库）
│
├── server.py           1999 行          B. 本地 HTTP 服务（8848）：静态页 + /api/* + SSE + 模型资源
│   ├── config.py         24 行          配置装载（路径解析、引擎选择）
│   └── config.json       51 行          唯一配置真源（端口/引擎/语气/agent/ops）
│
├── agent_bridge.py      118 行          C. 对话桥：8848 → 8898/chat（带 token/session/speaker）
│
├── tts_breeze.py        821 行          D1. 语音引擎驱动（当前）：spawn breeze-server.exe + /v1/audio/speech
├── tts_fastvits.py      525 行          D2. 语音引擎驱动（回滚用）：伪造宿主窗口 + WM_COPYDATA
│
├── web\                  0.9 MB         E. 前端（4 个页面，3 个"前端" + 1 个后台）
│   ├── pet.html/pet.js/pet.css           E1. 桌宠窗口（Electron 里跑，965 行 JS）
│   ├── chatbox.html/chatbox.js           E2. 对话输入条（独立小窗口，98 行 JS）
│   ├── stage.html/app.js                 E3. 浏览器舞台 / 触摸区域编辑器 iframe（558 行 JS）
│   ├── index.html/console.js/console.css E4. 后台控制台（1387 行 JS，11 张卡片）
│   ├── motions\*.motion3.json           9 个自造小动作（模型本身只有 Idle）
│   └── vendor\                           PIXI 6 + Cubism4 + CubismCore（0.75 MB，不入库）
│
├── tests\                               F. 测试：10 个 test_*.py + conftest（126 条）
├── probe\ + probe_breeze.py             G. 探测脚本（8 个脚本 + 6.8 MB 参考音频，可删）
├── data\touch_regions.json              触摸区域（主人手画，测试必须隔离）
├── tts_cache\ + tts_cache\touch\        合成缓存（顶层保留 60 个 + 14 个触摸预合成）
├── workspace\delete_temp.py              agent 遗留物（只删桌面文件的小脚本）
└── assets\steam_workshop\  5.43 GB       H. 第三方素材（Steam 创意工坊，不入库）
    └── content\2488350\
        ├── 3051599132\   28.4 MB        Live2D 模型（Salt - Trial Version，含 25 个 exp3 + Idle）
        ├── 3010132930\ 5072.6 MB        FastVITS 引擎（自带 python310 + torch，10006 文件）
        └── 3010104972\  457.1 MB        音色模型 G_953000.pth（+ config.json）
```

### 2.2 表格（职责边界）

| 板块 | 文件/行数 | 职责边界（它管什么 / 不管什么） | 谁能调它 |
|---|---|---|---|
| **A. Electron 外壳** | `desktop\desktop\main.js` 1198 / `preload.js` 24 / `chatbox-preload.js` 6 | 只管**窗口与进程生命周期**：建透明置顶窗、托盘、轮盘 IPC、拖拽/缩放、全局鼠标轮询、拉起/看门狗重启 `server.py`+`gateway.py`、退出清理。**不做**业务逻辑、不解析对话、不合成语音 | 用户（双击 bat / 托盘 / 轮盘） |
| **B. 本地 HTTP 服务** | `server.py` 1999 | 8848 全站：静态页、`/model/*` 资源（含 moc3 内存解密）、全部 `/api/*`、SSE 事件总线、触摸区域存储、唤醒词存储、QQ 托管、网关代理、TTS 生命周期 | 4 个前端 + QQ 插件（`/api/tts`）+ 网关（`/api/say`） |
| **C. 对话桥** | `agent_bridge.py` 118 | 只做一件事：把 `{text}` 变成网关 `POST /chat`（带 `X-Gateway-Token`、`session=main`、`speaker=主人`），把返回收成 `{reply, emotion, tone, sticker, path}`。**不碰**记忆、不碰工具、不碰人格 | `server.py` 的 `/api/chat` |
| **D1. Breeze 语音** | `tts_breeze.py` 821 | 管**引擎进程归属**（复用/新建/杀僵尸）+ 分片合成 + WAV 缝合/修头。音色由 `voice_id` 锁死，`instruction` 只当语气方向 | `server.py` 的 `_launch_tts/supervisor/prewarm` |
| **D2. FastVITS 语音** | `tts_fastvits.py` 525 | 老引擎：伪造 `FMTFrmManageEngine` 启动门 + `DMBrowserHost` 宿主窗，`WM_COPYDATA` 传参，float32→PCM16。**当前已不可用**（见 P1-2） | 同上（`tts_engine:"fastvits"` 时） |
| **E. 前端** | `web\` 24 文件（12 源 + vendor 3 + motions 9） | 4 个页面各自渲染；**没有**任何跨页状态，全靠 8848 的 HTTP/SSE | 用户 |
| **F. 测试** | `tests\` 10 个 `test_*.py` + `conftest.py`，共 126 条 | 钉住：触摸区域存取/命中、TTS 缓存清理、TTS 引擎 spawn 闸门与 adopted 语义、QQ 托管命令、系统代理、唤醒词、conftest 隔离 | 开发者 |
| **G. 探测脚本** | `probe\` 7 py + 1 sh + `probe_breeze.py` | 一次性验证/调音色脚本，不参与运行 | 开发者手工跑 |
| **H. 第三方素材** | `assets\steam_workshop` 10125 文件 / 5.43 GB | Steam 创意工坊内容：Live2D 模型 + FastVITS 引擎 + 音色模型。**不是本仓库代码**，被 `.gitignore` 整体排除（`.gitignore` 的 `desktop/assets/`）；`config.py` 用相对路径指进去，从而"不依赖本机 Steam 库" | `config.py` / `server.py` 只读 |

**依赖方向（单向，无环）**：A → B →（C → 8898）、（D1/D2 → 8849）、（E → B）。

---

## 3. 逐板块详述（四问）

### 3.1 A · Electron 外壳（`desktop\desktop\main.js`）

**从哪来（输入/上游）**
- 配置：`readConfig()` 读 `desktop\config.json`（`main.js:43-52`），取 `host/port`（→ `ORIGIN`）、`breeze.port`、`ops.*`。
- 用户操作：托盘菜单（`main.js:670-699`）、渲染进程 IPC（`main.js:712-755`、`1114-1133`）、单实例第二启动（`main.js:1140-1142`）。
- 环境变量：`GATEWAY_URL`（`main.js:31`）、`GATEWAY_DIR`（`main.js:32`）、`DM_PYTHON`（`main.js:144`）、`DM_DEBUG_PORT`（`main.js:35`）、`DM_CAPTURE`（`main.js:478`）。

**中间干了什么**
1. 启动流程：`app.whenReady()` → `ensureServer()` → `ensureGateway()` → `createWindow()` → `createTray()` → `startCursorTracking()` → `startDebugBridge()` → `startWatchdog()`（`main.js:1147-1180`）。
2. 探测：`serverAlive()` = `fetch(ORIGIN/api/status)` 900ms 超时判 `res.ok`（`main.js:134-141`）；`gatewayAlive()` = `fetch(8898/health)`（`main.js:207-214`）。
3. spawn 参数（**逐字**）：
   - 后端：`spawn(py, ["server.py"], {cwd: BASE=desktop\desktop\.., windowsHide:true, env:{...process.env, PYTHONIOENCODING:"utf-8"}, stdio:["ignore","pipe","pipe"]})`，stdout/stderr 追加写 `desktop\desktop\server.log`（`main.js:143-178`）。
   - 网关：`spawn(py, ["-B","gateway.py"], {cwd: GATEWAY_DIR=..\gateway, ...})`，日志写 `gateway.log`（`main.js:216-243`）。
4. 等待：后端 120×500ms（上限 60s，进程早死则在第 5 轮退出，`main.js:190-195`）；网关 60×500ms（`main.js:253-258`）。
5. 看门狗：每 30s 检查两个 `/health`-ish 端点，指数退避 3/6/12/24/48s，连续 5 次停手并 `notify()`（`main.js:280-357`）。日志实测存在自愈成功记录：`server.log` 里 `[server] 重拉成功` 4 条。
6. 窗口：透明/无边框/置顶/`skipTaskbar`/`show:false` + `ready-to-show` → `showInactive()`（`main.js:402-457`）；`contextIsolation:true, nodeIntegration:false, sandbox:false`（`main.js:420-426`）。
7. 交互：点击穿透 `setIgnoreMouseEvents(flag,{forward:true})`（`main.js:712-715`）；拖拽由主进程算位移并**每次带固定尺寸** `setBounds`（`main.js:724-753`）；滚轮缩放 `exp(-deltaY*0.0007)`，0.45~2.2（`main.js:528-546`）；全局鼠标 60ms 轮询推 `pet:cursor`（`main.js:552-576`）。
8. 进程外启动：`spawnOutside()` 交给 `explorer.exe` 启动（因为 Node 的 `detached:true` 没有 `CREATE_BREAKAWAY_FROM_JOB`，会被 Job 一起杀；`main.js:809-854`）。重启走 `restart.bat`（`main.js:868-888`）。

**到哪去（输出/下游）**
- 子进程 `python server.py`（8848）、`python -B gateway.py`（8898）。
- 窗口加载 `http://127.0.0.1:8848/pet`（`main.js:476`）与 `/chatbox`（`main.js:651`）。
- 退出：`quitAll()` → `stopHeavy()` → `POST /api/shutdown`（8s）→ 等 8849 释放 6s → 失败则 `killEngineByPort(8849)` 兜底（`main.js:981-1011`、`1051-1079`）；`will-quit` 再 `stopServer()/stopGateway()`（`main.js:1194-1197`）。
- 调试桥（仅 `DM_DEBUG_PORT` 时）：`/eval`、`/shot`、`/blur`（`main.js:360-394`）——**默认关闭**，但 `/eval` 能执行任意 JS，属"默认安全、开了就危险"的开关。

**怎么干的（关键机制）**
- 单实例：`app.requestSingleInstanceLock()`，抢不到直接 `app.quit()`；第二实例只把窗口再顶出来（`main.js:1136-1142`）。
- 状态持久化：`window-state.json`（scale/zoom/x/y），读时去 BOM（`main.js:70-85`）；实测内容 `{"scale":"small","zoom":1,"x":1709,"y":693}`。
- 端口占用策略：**只有"探测-拉起"**，没有"被别的程序占着怎么办"的分支（`ensureServer` 只看 `/api/status` 是否 200）。
- 死代码：`killDsh()` 仍定义在 `main.js:1014-1036`，但自 11-05"退出保留 DSH"改规格后**没有任何调用点**（全文件只有定义 + 注释提及）。

---

### 3.2 B · 本地 HTTP 服务（`server.py`，8848）

**从哪来**
- `config.json` → `config.py` 解析出 `HOST/PORT/MAX_CHARS/TTS_ENGINE/BREEZE_*`（`config.py:1-24`）。
- 请求：4 个前端 + QQ 语音插件（`qq\astrbot\data\plugins\astrbot_plugin_voice_reply\main.py:29` 硬编码 `http://127.0.0.1:8848/api/tts`）+ 网关主动说话（`gateway\role_speak.py:16,26` → `/api/say`）。
- 磁盘：`data/touch_regions.json`、`tts_cache/`、AstrBot 的 `plugin_data/group_wake_keywords.json`、`gateway/data/gateway.token`。

**中间干了什么**
- 静态与资源：`/`、`/pet`、`/stage`、`/chatbox` 四个页面 + `web/**` 兜底（`server.py:1555-1581`）；`/model/settings.json` 由工坊目录**现场生成** model3.json（`server.py:806-886`）；`/model/*` 映射工坊目录并在内存里 XOR `0x73` 解密 `.moc3`（`server.py:710-731`、`1540-1553`）；`/model/motions/*` 单独走 `web/motions`（`server.py:1523-1538`）。
- 触摸：区域表归一化/存盘/`重置`（`server.py:152-260`）、命中框 `TOUCH_HIT_BOX={x0:.33,x1:.67,y0:.10,y1:1.0}`（`server.py:138`）、预合成语音（`server.py:580-659`）。
- 唤醒词：真源写 AstrBot 插件目录（mtime 触发插件热读，`server.py:293-344`）。
- QQ 托管：按端口停 + 按命令行补杀孤儿（`server.py:347-450`）。
- 网关代理：`X-Gateway-Token` 只在服务端读（`server.py:453-491`）。
- 引擎生命周期：`boot_tts()`（`974-992`）、`_tts_supervisor()` 3s 轮询 + 熔断 3 次（`995-1063`）、`stop_tts_force()`（`1067-1087`）。

**到哪去**
- SSE 事件总线：`{type:"say", text, emotion, tone, client, source}`、`{type:"touch-regions"}`（`server.py:748-789`、`1902-1909`、`1922-1930`、`1746`）。
- 下游：8898 网关（`/api/chat` → agent_bridge；`/api/system*`、`/api/daily-review*` 代理）、8849 引擎（`/api/tts`）、PowerShell/taskkill（QQ 托管）、`os.startfile`（打开缓存目录）。

**怎么干的**
- 线程模型：`ThreadingHTTPServer` + `daemon_threads=True`（`server.py:1983-1984`）；每个 SSE 连接一条常驻线程（15s 心跳 ping，`server.py:1322-1347`）；TTS 启停 + supervisor + prewarm 三条后台线程（`server.py:1977-1981`）；触摸语音重合成按区域起线程（`server.py:1740-1742`）。
- 协议：HTTP/1.1 + keep-alive（`server.py:1172`），因此**必须读完 body**（`_body_cache`，`server.py:1617-1632`、`1275-1306`）。
- 数据结构：`EventBus`（`dict`→`set[Queue]`，队列满丢最旧，`server.py:749-789`）；`_moc_cache: dict[path→(mtime, bytes)]`；`_TOUCH_VOICE_STATUS: dict[region_id→{state,error}]` + 锁。
- 算法：moc3 XOR 解密（首字节 `3e 3c 30 40` ^ `73` = `MOC3`，**实测**）；WAV 拼接（见 D1）；触摸命中（矩形直判 / 椭圆方程，`server.py:508-519`）。

---

### 3.3 C · 对话桥（`agent_bridge.py`）

**四问**
- **从哪来**：`server.py:handle_chat()` → `ChatAgent.chat(text)`；环境变量 `GATEWAY_URL/GATEWAY_SESSION/GATEWAY_SPEAKER/GATEWAY_TIMEOUT`（`agent_bridge.py:16-20`），token 来自 `GATEWAY_TOKEN` 或 `..\gateway\data\gateway.token`（`agent_bridge.py:25-43`）。
- **中间**：组 `{"text","session","speaker"}`，带 `X-Gateway-Token`，`urllib` POST `8898/chat`，超时 90s（`agent_bridge.py:61-71`）。
- **到哪去**：返回 `{"reply","emotion","tone","sticker","path"}`（`agent_bridge.py:98-105`）；`` 全部错误都**翻译成人话**回给前端：401→"网关不认我这个身份"、其它 HTTP→"我这边刚刚卡了一下"、连不上→"网关没连上"（`agent_bridge.py:72-95`）。
- **怎么干的**：无状态薄转发；`reset()` 是空实现（只清 `last_error`，`agent_bridge.py:107-109`）——这直接导致 `/api/reset` 假清空（见 P2-3）。
- **接口契约（对网关）**：请求 `POST /chat {text, session, speaker}` + `X-Gateway-Token`；响应取 `text/emotion/tone/sticker/path`。与网关实测代码一致：网关 `gateway.py:3024-3057` 校验 token 后返回 `handle_chat` 结果，`tone` 在 `gateway.py:1707` 产出。**【读码】** 两侧字段名对得上。

---

### 3.4 D1 · Breeze 语音引擎（`tts_breeze.py`）

**从哪来**：`server.py:_launch_tts()` 传 8 个参数（`server.py:935-955`）+ `config.json.breeze.*`；文本来自 `/api/tts`、触摸预合成、`synthesize_touch_voices`。

**中间干了什么**
1. **所有权判定**（`start()`，`tts_breeze.py:471-643`）：①自己的进程还活着且 `/health` 为 `ok` → 什么都不做；②8849 上有健康引擎 → `adopted=True` 复用、不 spawn、不 kill；③端口有占用者但名字不含 `breeze` → 直接抛错，不启动（`505-512`）；④按 `ExecutablePath` 精确枚举同款进程（`pids_with_exe_path`，`218-263`），有则最多等 `load_wait=45s` 让它加载完并接管（`541-554`），等不到则**只在名字像 engine 时**才 `taskkill`（`555-571`）；⑤确认没人 → spawn。
2. spawn 参数：`[server, model, "--host","127.0.0.1","--port",8849,"--no-warmup"]`，`cwd=exe 目录`，stdout/stderr 追加写 `desktop\breeze-server.log`（`tts_breeze.py:612-627`）。
3. 分片：`split_for_tts()` 先按句末标点、再按逗号、最后硬切，片长 `chunk_chars`（默认 28），小句回拼（`tts_breeze.py:56-97`）。
4. 合成：`POST /v1/audio/speech {model,input,instructions,response_format:"wav",voice,cfg_scale,seed,max_new_tokens}`，409 忙则 1/2/4s 退避共 3 次（`tts_breeze.py:715-755`）。
5. 拼接：逐片解析 `fmt/data`，格式不一致直接报错；片间补 `chunk_gap_ms` 静音（`breeze_block_align` 由 sr×ch/位深算）；最后 `_build_wav()` **重写 RIFF/data 长度**（引擎流式写盘时长度是占位）（`tts_breeze.py:757-805`、`100-133`）。
6. 缓存/落盘：输出到 `tts_cache\breeze_000NN.wav`（自增计数，进程内不复用文件名）；`server.py` 侧只保留最近 `TTS_KEEP_FILES=60` 个（`server.py:903-927`）。触摸语音单独存 `tts_cache\touch\<id>-<idx>-<sha1[:12]>.wav`，hash 由 `text|emotion|engine|voice_id|instruction` 决定（`server.py:526-542`）——**改台词/换声线/改语气都会自动换文件名**。
7. 失败降级：`/api/tts` 未就绪 → 503 + `{"loading":true}`（`server.py:1869-1880`），pet.js 据此在气泡上加"（声音引擎还在加载）"（`pet.js:511-515`）；触摸语音 404 → 前端静默只做动作（`pet.js:411`）；引擎掉线 → supervisor 重拉，连续 3 次失败熔断并留因（`server.py:1045-1060`）。
8. `stop(force=True)`（退出专用）：adopted 也杀；无 Popen 句柄时按 `adopted_pid` → 端口反查，且**必须先确认进程名像 breeze** 才 taskkill（fail-closed，`tts_breeze.py:660-712`）。

**到哪去**：WAV 文件路径 → `server.py` 读字节 → HTTP `audio/wav` → 浏览器 Web Audio → 口型。

**怎么干的**：单锁串行化合成（`_lock`，引擎本身单线程）；引擎端口反查用 `Get-NetTCPConnection`（**明确不用 netstat**，因为本机 netstat 看不到 8848/8849，`tts_breeze.py:164-199`）；控制台输出用 `mbcs` 解码避免 GBK 崩溃（`141-161`）。

**实测旁证**：`server.log` 里 80 处 `复用已在运行的语音引擎`；`breeze-server.log` 反复出现 `listening on http://127.0.0.1:8849` 与 `websocket on ws://127.0.0.1:8850`。

---

### 3.5 D2 · FastVITS 引擎（`tts_fastvits.py`）——当前不可用

**四问（简述，因为它现在是"回滚选项"）**
- **从哪来**：`config.json.tts_engine="fastvits"` 时 `server.py:956-968` 传 `exe/model/config/outdir/speaker/length_scale/noise_scale/noise_scale_w`。
- **中间**：起独立线程注册窗口类 `DMBrowserHost` 并跑消息泵（`tts_fastvits.py:281-341`）；若 `FMTFrmManageEngine` 不存在则自造"启动门"并常驻（`350-386`）；`subprocess.Popen([exe, host, model, config])` 并塞进 `KillOnCloseJob`（Job Object，父进程被杀时子进程一起收，`132-158`、`407-414`）；`WM_COPYDATA` 发送 `speaker|length_scale|noise|noise_w|out|[ZH]text[ZH]`（`443-476`）；合成完成后 `to_pcm16()` 把 float32/32bit 转 16bit PCM（`203-248`）。
- **到哪去**：PCM16 WAV 路径，接口与 Breeze 相同。
- **怎么干的**：`SendMessageTimeoutW` + 轮询文件出现且 >1000 字节（`464-476`）。
- **⚠ 已坏**：`speak_wav(self, text)` **没有 `instruction` 参数**，而 `server.py` 两个调用点都传了（`server.py:597`、`1660`）。实测（离线 import 后调用）：`TypeError: FastVITS.speak_wav() got an unexpected keyword argument 'instruction'`，`instruction=None` 与字符串都抛。**结论：`tts_engine:"fastvits"` 这条回滚路径已经跑不通**，README:51「保留可回滚」不成立。

---

### 3.6 E · 三个前端 + 后台

| 页面 | 入口 | 和后端怎么通信 | 能力 | 明确没有的能力 |
|---|---|---|---|---|
| **E1 pet 窗**（Electron） | `main.js:476` 加载 `/pet` | `EventSource /api/events` 收 `say`；`POST /api/tts` 取语音；`GET /api/pet-config`（隐藏部件）、`GET /api/touch-regions`（区域+hit_box）、`GET /api/touch-voice`（预合成语音）；窗口能力全走 `window.petBridge`（IPC） | 气泡/表情/动作/口型/目光跟随/触摸分区/轮盘菜单/拖拽/穿透 | 没有输入框（对话在 chatbox）；不发 `/api/chat`；不显示错误详情 |
| **E2 chatbox 窗**（Electron） | `main.js:651` 加载 `/chatbox` | `POST /api/chat {text, client:"chatbox"}`；回复**不在这里渲染**，靠 pet 窗的 SSE 接（`chatbox.js:2-4,41-45`）；`window.petChat` 只有 `onFocus/close` | 输入、Enter 发送、Esc/失焦关闭、中文输入法保护 | 看不到回复本体，只显示"正在想…/错误"；无历史 |
| **E3 浏览器 stage** | `/stage` → `app.js` | `POST /api/chat` 自己渲染回复并**自己** `POST /api/tts` 播放（`app.js:498-521`）；`/api/status` 轮询；`?edit=1` 时变触摸编辑器，与父窗 `postMessage`（`app.js:12,306-493`） | 完整可对话+可说话（不走 Electron）、`autoInteract:true`、表情下拉、试听按钮、编辑器画矩形/椭圆 | 没有 SSE、没有触摸反馈、没有穿透、没有轮盘 |
| **E4 后台 console** | `/index.html` → `console.js` | 8848 全套（status/chat/say/reset/tts-cache/qq-hosting/wake-keywords/touch-regions/touch-voice-status/system*/daily-review*）+ **直连 8898**（`/admin/model`、`/admin/keys`） | 11 张卡（实测 `<h2>` 计数）：让桌宠说话、对话模型、系统模型、每日梳理期、API 密钥、群聊唤醒、自定义触摸区域（iframe）、运行状态、她现在的状态、内心OS、其它入口；对话记录 SSE 镜像 | 直连 8898 的两张卡**必然 401**（见 P1-5）；iframe 编辑器被 XFO 拒载（见 P1-1） |

**能力差异要点**：只有 E1 有"穿透 + 轮盘 + 触摸分区 + 口型"；只有 E3 能在纯浏览器里独立说话；E2 是个"只负责输入"的壳。同一个后端，三套完全不同的交互语义。

---

### 3.7 F · 测试、G · 探测脚本、H · 素材

- **F 测试（实测 126 passed / 22.31s）**：10 个文件，覆盖触摸区域存取与命中、TTS 缓存 prune、Breeze spawn 闸门与 adopted、QQ 托管命令文案、系统层代理、唤醒词、以及 conftest 的 autouse 隔离守门（`test_desktop_isolation.py`）。用例名多为中文。
- **G 探测**：`probe_breeze.py` 是"端到端冒烟"（README:52、项目地图:148 都推荐它）——但它**语法错误，根本不能跑**（见 P2-6）。`probe\` 下 8 个脚本 + 6.8 MB 参考 wav，`.gitignore` 排除了 `probe/cry|voice_candidates|tone|qq_tone`。
- **H 素材**：10125 文件 / 5.43 GB（实测）；性质＝Steam 创意工坊内容（三个 item id 目录），用途＝Live2D 模型 28.4 MB + FastVITS 引擎 5.07 GB（自带 python310/torch）+ 音色 457 MB；`config.py:10-13` 用**仓库内相对路径**指向它（"不再依赖本机 Steam 库"）。版权：模型是 `Salt - Trial Version`（README:219 已声明试用/不可商用），`.gguf` 在仓库外（`config.json.breeze_dir` 绝对路径 `G:\workplase\...`，实测存在 3.32 GB）。

---

## 4. 数据流与接口契约

### 4.1 主链路（桌宠说话）

```
chatbox.js --POST /api/chat {text,client}--> server.py:1891
   -> agent_bridge.ChatAgent.chat (8848→8898, X-Gateway-Token, session=main, speaker=主人)
   -> gateway.py:3019 /chat -> handle_chat -> {text,emotion,tone,sticker}
   <- {reply=text, emotion, tone, sticker, path}
server.py EVENTS.publish({type:"say", text, emotion, tone, client, source:"chat"})   [1902-1909]
   -> pet.js EventSource onmessage -> say() -> 气泡 + applyEmotion(表情+动作)          [pet.js:528-535,342-353]
   -> pump() -> POST /api/tts {text, emotion, tone}                                  [pet.js:239-254]
   -> server._clean_tone(tone) 优先当 instruction，否则 EMOTION_INSTRUCTION[emotion]   [1644-1665]
   -> Breeze.speak_wav(text, instruction)                                             [tts_breeze.py:757]
        -> split_for_tts(28字) -> N×(POST 8849 /v1/audio/speech) -> 拼 PCM + 静音 + 重写 WAV 头
   -> audio/wav -> Web Audio -> Analyser -> ParamMouthOpenY（自己驱动口型）             [pet.js:172-220]
```

**`{语气:...}` 的两条路**：结构化 `tone` 字段（桌宠）；正文内联标记（QQ 路径）+ 清 `[语音]` 标记 —— 全部收口在 `/api/tts`（`server.py:670-694`、`1864-1865`）。这是全项目唯一的语气入口。

### 4.2 8848 对外接口契约（逐条，行号＝`server.py`）

**GET（12 条 API）**

| 路径 | 行 | 入参 | 出参 | 鉴权/守卫 |
|---|---|---|---|---|
| `/api/status` | 1498 | — | `{tts_ready,tts_error,tts_alive,tts_engine,tts_adopted,tts_pid,agent_enabled,agent_ready,agent_error,model,events}` | **跨站可读**（`PUBLIC_GET_PATHS`, 1195-1202） |
| `/api/pet-config` | 1448 | — | `{hidden_parts, model}` | 同上 |
| `/api/wake-keywords` | 1397 | — | `{keywords,source,max,max_len}` | 同上 |
| `/api/touch-regions` | 1407 | — | `{hit_box,custom,emotions,regions[]}` | 同上 |
| `/api/touch-voice-status` | 1413 | — | `{regions:{id:{total,ready,state,error}}}` | 同上 |
| `/api/touch-voice` | 1420 | `?r=<id>&i=<idx>` | `audio/wav` 或 404 | 需过 Origin |
| `/api/events` | 1435 | — | `text/event-stream`（`say`/`touch-regions`，15s ping） | 需过 Origin |
| `/api/shutdown` | 1444 | — | **405**（只接 POST） | 需过 Origin |
| `/api/qq-hosting` | 1455 | — | `{online,ports,script,script_exists}` | Origin + `_client_is_local()` |
| `/api/system-model` | 1468 | — | 网关 `/admin/system-model` 原样 | Origin + 本机 |
| `/api/system` | 1479 | — | 网关 `/admin/system` 原样 | Origin + 本机 |
| `/api/daily-review` | 1488 | — | 网关 `/admin/daily-review` 原样 | Origin + 本机 |
| `/model/settings.json` | 1516 | — | 生成的 model3.json | Origin |
| `/model/motions/*` | 1523 | 相对路径 | JSON | Origin + `relative_to(root)` |
| `/model/*` | 1540 | 相对路径 | 资源（moc3 内存解密） | Origin + `relative_to(LIVE2D_DIR)` |
| `/` `/pet` `/stage` `/chatbox` + `web/**` | 1555-1581 | — | 页面/静态 | Origin（页面本身也带 XFO） |
| `/health` | — | — | **不存在，404**（虽在 `PUBLIC_GET_PATHS` 里） | — |

**POST（16 条，AST 实测）**

| 路径 | 行 | 入参 | 出参 | 鉴权/守卫 |
|---|---|---|---|---|
| `/api/shutdown` | 1685 | — | `{ok,tts_stopped,qq_stopped}` 后进程退出 | Origin + 本机 |
| `/api/wake-keywords` | 1689 | `{keywords:[str]}` | `{ok,keywords,message}` | Origin + JSON |
| `/api/touch-regions` | 1706 | `{regions:[{id,label,motion,lines,shapes}]}` | `{ok,rebuilding,voice_errors,...regions_payload}` | Origin + JSON |
| `/api/touch-regions/reset` | 1770 | — | `{ok,...regions_payload}` | Origin |
| `/api/touch-voices/rebuild` | 1755 | `{id}` | `{ok,state:"running"}` / 404 / 503 | Origin + JSON |
| `/api/qq-hosting` | 1776 | `{action:"start"\|"stop"\|"toggle"}` | `{ok,action,online,message}` | Origin + JSON + 本机 |
| `/api/system-model` | 1808 | 网关同构 payload | 网关原样 | Origin + JSON + 本机 |
| `/api/system/tick`、`/api/system/consolidate`、`/api/system/inner-os/clear` | 1819 | `{}` | 网关原样 | Origin + JSON + 本机 |
| `/api/daily-review/run` | 1831 | `{}` | 网关原样 | Origin + JSON + 本机 |
| `/api/open-tts-cache` | 1843 | — | `{ok,path,count}` | Origin + 本机 |
| `/api/tts` | 1861 | `{text, emotion?, tone?}` | `audio/wav`；400/503/500 | Origin + JSON |
| `/api/chat` | 1891 | `{text, client?}` | `{reply,emotion,tone,sticker,path}` | Origin + JSON |
| `/api/say` | 1916 | `{text, emotion?, tone?, client?}` | `{ok,subscribers}` | Origin + JSON |
| `/api/reset` | 1934 | — | `{ok:true}`（**实际不清记忆**） | Origin |

**统一守卫（每个请求都过）**
- Origin：有 `Origin` 必须命中 `{http://127.0.0.1:8848, http://localhost:8848}`；无 `Origin` 时看 `Sec-Fetch-Site != cross-site`；`PUBLIC_GET_PATHS` 的 GET/HEAD 免检（`server.py:1204-1232`）。
- `POST` 带 body 必须 `Content-Type: application/json`，否则 415（`1234-1254`）。
- body 上限 8 MB，超限 413 且关连接（`1188`、`1628-1632`）。
- 安全响应头：`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`（`1264-1266`）——**每一条响应都有**（实测见 §5.4）。
- 本机判定：`client_address[0] ∈ {127.0.0.1, ::1, ::ffff:127.0.0.1}`（`1350-1356`）。
- **没有**任何 token/身份概念：本机进程 ＝ 完全信任。

### 4.3 进程与端口

| 端口 | 归属 | 绑定 | 证据 |
|---|---|---|---|
| **8848** | `python server.py`（桌宠本地 HTTP） | `HOST=config.host=127.0.0.1`（`server.py:697,1983`） | 代码 + 实测监听 |
| **8849** | `breeze-server.exe` 的 OpenAI 兼容 TTS HTTP | spawn 显式 `--host 127.0.0.1 --port 8849`（`tts_breeze.py:616-617`） | `breeze-server.log`：`listening on http://127.0.0.1:8849` |
| **8850** | `breeze-server.exe` 自己的 WebSocket | 引擎内部，不由我们配置 | `breeze-server.log`：`websocket on ws://127.0.0.1:8850`；`server.log`：`ws_port=8850` |
| **8898** | `python -B gateway.py` | `gateway\config.json.host=127.0.0.1` | `gateway.py:3162` |
| 5099/3000/3001/6185/6199 | QQ 那套（SnowLuma/AstrBot），**不属本板块** | — | `server.py:353`、`main.js:768` |

**拉起与探测**：见 §3.1。补充实测：`server.log` 有 82 次 `[spawn]`、50 次 `[server exit]`、4 次 `[server] 重拉成功`；`gateway.log` 有 85 次 `[spawn]`、44 次 `[gateway exit]`、8 次重拉日志——说明"起了又死、靠看门狗自愈"是**常态**而不是例外。

**端口占用怎么办（逐端口）**
- 8848：**没有专门处理**。`ensureServer()` 只看 `/api/status` 是否 200；而且因为 `HTTPServer.allow_reuse_address = 1`，**Windows 上第二个实例能 bind 成功**（实测：两个 `ThreadingHTTPServer` 同绑一个端口都成功，请求全进第一个；第一个关掉后第二个静默接管）。`desktop\启动.bat:4` 还是无条件 `start /b python server.py`。→ P1-3。
- 8849：有完整闸门（复用 / 等加载 / 拒非引擎 / fail-closed 枚举）。
- 8898：网关自己有单实例锁（`_acquire_single_instance_lock`，`gateway.py:3084`，按"端口能回 /health"判活）。

**退出收尾**：`quitAll()` → `POST /api/shutdown`（服务端先停 QQ 端口段、再 `tts.stop(force=True)`、回响应、再 `os._exit(0)`，`server.py:1358-1374`、`1116-1125`）→ JS 侧等 8849 释放 6s → 兜底 `killEngineByPort`（只杀名字含 breeze 的）→ `stopServer()`。DSH **保留不动**（`main.js:1069-1076`）。

### 4.4 对网关的接口约定（越界但必须记）

| 方向 | 接口 | 桌面侧 | 网关侧 | 结论 |
|---|---|---|---|---|
| 对话 | `POST 8898/chat` + `X-Gateway-Token` | `agent_bridge.py:61-71` | `gateway.py:3024-3057` | ✅ 对齐 |
| 系统卡 | 8848 `/api/system*` 代理，token 服务端加 | `server.py:1475-1496, 1815-1841` | `gateway.py:2675-2690, 2782-2817` | ✅ 对齐 |
| 每日梳理 | 同上 | `server.py:1494, 1838` | `gateway.py:2681-2686, 2809-2815` | ✅ 对齐 |
| **对话模型卡/密钥卡** | 浏览器**直连** `8898/admin/model`、`/admin/keys`，**不带 token** | `console.js:295,321,354,651,675,715,751` | `gateway.py:2667-2674`、`2770-2771`、`2753-2754`（全部强制 token） | ❌ **必然 401**；且网关 `Access-Control-Allow-Headers` 只放 `Content-Type`（`gateway.py:2619`），浏览器就算想加 token 也过不了预检 |

---

## 5. 实测证据快照

> 环境：Windows；系统 Python 3.13.2（**无 pytest**）、`<venv>`（Python 3.12.11 + pytest 9.1.1）。全部服务未运行（8848/8849/8850/8898/5099/6185/6199/3000/3001 实测 `closed`）。

### 5.1 行数（按字节 `\n` 计，非 `Get-Content`）

| 文件 | 行数 | 字节 | 行尾 |
|---|---|---|---|
| `desktop\server.py` | **1999** | 84952 | CRLF |
| `desktop\tts_breeze.py` | **821** | 40841 | LF |
| `desktop\tts_fastvits.py` | **525** | 19876 | LF |
| `desktop\agent_bridge.py` | **118** | 4824 | LF |
| `desktop\config.py` | 24 | 1113 | LF |
| `desktop\config.json` | 51 | 1576 | LF |
| `desktop\probe_breeze.py` | 69 | 2213 | LF（**语法错误**） |
| `desktop\README.md` | 233 | 14071 | CRLF |
| `desktop\desktop\main.js` | **1198** | 45829 | LF |
| `desktop\desktop\preload.js` | 24 | 1128 | LF |
| `desktop\desktop\chatbox-preload.js` | 6 | 224 | LF |
| `desktop\web\pet.js` | **965** | 35353 | CRLF |
| `desktop\web\console.js` | **1387** | 51758 | CRLF |
| `desktop\web\app.js` | **558** | 18571 | CRLF |
| `desktop\web\chatbox.js` | **98** | 2793 | LF |
| `desktop\web\index.html` | 265 | 13942 | LF |
| `desktop\web\pet.html` / `stage.html` / `chatbox.html` | 18 / 32 / 23 | 504 / 1074 / 553 | LF |
| `desktop\data\touch_regions.json` | 173 | 3342 | CRLF |

> ⚠ 与 lead 给的数字有一处出入：`web\chatbox.js` 我实测 **98 行 / 2793 B**，不是 92。其余（server.py 1999、tts_breeze 821、tts_fastvits 525、agent_bridge 118、main.js 1198、console.js 1387、pet.js 965、app.js 558）完全一致。

### 5.2 体积

| 目录 | 文件数 | 大小 |
|---|---|---|
| `desktop\assets`（steam_workshop） | 10125 | **5.43 GB**（=5558 MiB，与 lead 的 5558.2 MB 一致） |
| └ `3051599132` Live2D 模型 | 114 | 28.4 MB |
| └ `3010132930` FastVITS 引擎 | 10006 | 5072.6 MB |
| └ `3010104972` 音色模型 | 5 | 457.1 MB（`G_953000.pth`） |
| `desktop\desktop`（含 Electron 38.8.6 运行时） | 953 | 329.9 MB |
| `desktop\web`（含 vendor 3 个 + motions 9 个） | 24 | 0.9 MB |
| `desktop\probe`（含 6.8 MB 参考 wav） | 39 | 6.8 MB |
| `desktop\tests` | 23（含 `__pycache__`） | 0.38 MB |
| `desktop\tts_cache`（顶层 20 + touch 14） | 34 | 5.22 MB |

### 5.3 路由数

- `server.py`（AST 精确枚举，见 `_route_get`/`_route_post`）：**GET 21 个相等分支**（12 条 `/api/*` + `/model/settings.json` + 4 个页面的 `*.html` 别名 + `/` + `/index.html`）+ 2 条 `startswith` 前缀分支（`/model/motions/*`、`/model/*`）；**POST 16 个相等分支**。去重后**唯一 `/api/*` 路径 23 条**（GET 12 + POST 16；5 条同名重叠：`/api/shutdown`、`/api/wake-keywords`、`/api/touch-regions`、`/api/qq-hosting`、`/api/system-model`）。
- 网关（对照，AST 枚举 `startswith/endswith` 分支）：GET 9 条（`/health`、`/admin/model`、`/admin/keys`、`/admin/system-model`、`/admin/daily-review`、`/admin/system`、`*/models`、`/capabilities`、`/audit`）+ POST 6 条（`/admin/keys`、`/admin/model`、`/admin/system-model`、`/admin/system`、`*/chat/completions`、`/chat`）。

### 5.4 守卫/契约实测（临时实例，`127.0.0.1:0` 系统分配端口，未碰 8848）

| 用例 | 结果 |
|---|---|
| `GET /api/status`、`pet-config`、`touch-regions`、`touch-voice-status`、`wake-keywords` | 200 |
| `GET /health`（README 说可跨站读） | **404** ← 路由不存在 |
| `GET /api/status` 带 `Origin: https://evil.example` | **200**（PUBLIC_GET_PATHS 按路径豁免，设计如此） |
| `GET /api/shutdown` | **405** ✅（README 一致） |
| `POST /api/shutdown` 带外部 Origin | **403** ✅ |
| `POST /api/say` `text/plain` | **415** ✅ |
| `POST /api/say` JSON | 200 `{"ok":true,"subscribers":0}` |
| `GET /model/../../desktop/config.json` | **403** ✅（路径穿越被 `relative_to` 挡住） |
| `GET /model/motions/Happy.motion3.json` | 200 |
| 安全响应头（`/`、`/pet`、`/stage?edit=1`、`/api/status`、`/model/settings.json`） | 全部带 `X-Frame-Options: DENY` + `nosniff` + `no-referrer` |

### 5.5 测试

```
$ cd <repo>/desktop
$ <venv>\Scripts\python.exe -m pytest tests -q --no-header -p no:cacheprovider
126 passed in 22.31s
```
- 分布：`desktop_isolation 3`、`qq_hosting 17`、`system_proxy 5`、`touch_regions 20`、`touch_voice 17`、`touch_voice_rebuild 12`、`tts_adopted 11`、`tts_cache 9`、`tts_spawn_guard 17`、`wake_keywords 15`。
- **隔离验证（前后比对生产数据）**：跑前跑后 `git status --porcelain -- desktop/` 均为空；`desktop\data\touch_regions.json` mtime 仍是 `2026-11-02 13:28:11`、3342 B；`tts_cache` 顶层仍 20 个；`qq\astrbot\data\plugin_data\group_wake_keywords.json` mtime 仍是 `2026-11-01 17:28:03`。→ `conftest.py` 的 autouse 隔离**确实生效**。
- 顺带发现：系统 Python 3.13 没装 pytest，只有 `qq\astrbot\.venv` 里有——项目地图:1702 给的正是这条命令，可用。

### 5.6 关键离线验证（我自己写的临时探针，跑在 TEMP，未进仓库）

| 验证 | 方法 | 结果 |
|---|---|---|
| **P1-1 XFO 打死编辑器** | 临时实例 + 无头 Chrome，A/B 两个 iframe（A=`/plain` 无 XFO，B=`/stage?edit=1` 有 XFO） | `A_plain=loaded:canvas \| B_stage=blocked(no-contentDocument)`；Chrome 日志 `Refused to display ... 'X-Frame-Options' to 'deny'` |
| **P1-2 FastVITS 回滚断** | `inspect.signature` + 实际调用 | `FastVITS.speak_wav(self, text)` vs `Breeze.speak_wav(self, text, instruction=None)`；调用抛 `TypeError` |
| **P1-3 双实例可同时 bind** | 两个 `ThreadingHTTPServer` 同端口 | 两个都 bind 成功；请求全进第一个；关掉第一个后第二个**静默接管**（`S1,S1,S1` → 关 S1 → `S2,S2,S2`） |
| **P2-1 drain 二次读** | A/B：原样 vs `_drain_body=lambda s: None` | A：403 后同连接第二请求 `BadStatusLine: <!DOCTYPE HTML>`；B：第二请求 200 |
| moc3 加密 | 读首 8 字节 | `3e 3c 30 40` ^ `73` = `4d 4f 43 33`（`MOC3`）✅ 与 README:179 一致 |
| 触摸预合成 | 数 `tts_cache\touch` | 14 个（7 区域 × 2 句）✅ 与 `TOUCH_REGIONS_DEFAULT` 一致 |
| 表情/动作资源 | 对照 EXP 目录与 `web/motions` | `EMOTION_MAP` 用到的 10 个表情**全部存在**；`EMOTION_MOTION` 用到的 6 个动作全部存在 |
| `probe_breeze.py` | `compile()` | `SyntaxError: unterminated triple-quoted string literal (detected at line 69)` |
| `killDsh` 死代码 | 全文件 grep 调用点 | 只有定义（`main.js:1014`）+ 注释，**零调用** |
| `touch_region_at` 死代码 | 全仓 grep | 只有定义（`server.py:494`）+ 4 个测试文件引用；**生产代码零调用**（前端自己做 `regionAt`，`pet.js:395`） |

---

## 6. 问题清单

> 分级标准：P0＝会造成数据损坏/安全事故/主链路完全不可用；P1＝明确功能失效或明显误伤风险；P2＝健壮性/可维护性/文档失真。
> **本板块未发现 P0。**

### P1（6 条）

**P1-1 后台「自定义触摸区域」编辑器被 `X-Frame-Options: DENY` 整块打死（回归）**
- 证据：`index.html:235` 有 `<iframe id="te-frame">`；`console.js:1016` 设 `frame.src = "/stage?edit=1"`；`app.js:12` 读 `?edit=1` 进入编辑器模式；`server.py:1264` 对**所有**响应（包括 `/stage`）发 `X-Frame-Options: DENY`。实测见 §5.6：同源 iframe 也被拒，`blocked(no-contentDocument)`。
- 时间线：编辑器 `90584b3`（11-02）先落地，XFO 是 `7fdba62`（11-04）安全提交才加的 → **安全加固顺手打断了一个功能**。全仓 `grep -i 'iframe|X-Frame|edit=1|touch-edit' desktop\tests\` **零命中**，126 条测试没有一条覆盖浏览器侧 iframe。
- 影响：后台"自定义触摸区域"永远卡在空白 iframe（4s 兜底 `sendInit` 也发不进去），用户只能改 JSON 文件；而 `data/touch_regions.json` 里确实有主人手画的 7 个区域（含改过的台词，如 feet 的「为什么我没有鞋穿？」），说明这个功能**被真实使用过**。
- 修法建议（不在本次范围）：`/stage` 用 `SAMEORIGIN` 或只对页面设 `frame-ancestors 'self'`。

**P1-2 FastVITS 回滚路径已断（`speak_wav` 签名不兼容）**
- 证据：`tts_fastvits.py:478` `def speak_wav(self, text: str)`；`server.py:597` 与 `server.py:1660` 都调 `tts.speak_wav(text, instruction=instruction)`。实测抛 `TypeError: got an unexpected keyword argument 'instruction'`。
- 影响：`config.json` 改 `tts_engine:"fastvits"` → `/api/tts` 全部 500、触摸语音预合成全失败。README:51 和 项目地图:146 都写"保留可回滚"，**实际上回不去了**。

**P1-3 8848 无单实例/端口占用保护；Windows 上两个 `server.py` 能同时"启动成功"**
- 证据：`server.py:1983` 直接 `ThreadingHTTPServer((HOST, PORT), Handler)`，没有任何"端口在用"判断；`HTTPServer.allow_reuse_address = 1`（实测），Windows 语义下**第二个 bind 成功**（实测两个实例并存，前一个关掉后后一个静默接管）；`desktop\启动.bat:4` 是 `start /b python server.py`（无条件）。
- 影响：双实例时第二个进程"看起来起来了"（日志打印 URL、`/api/status` 由第一个应答），但它的 `_tts_supervisor`、`prewarm_touch_voices`、SSE 订阅全在空转，且会与第一个抢 8849 —— 这正是 `tts_breeze.py:1-23` 大段注释在描述的"双开 3.5GB 显存"事故的同源问题。桌面侧有 `requestSingleInstanceLock`（`main.js:1136`），网关侧有锁（`gateway.py:3084`），**只有中间这层没有**。

**P1-4 「关 QQ 托管」按端口盲杀 3000/3001/5099/6185/6199，不校验进程身份**
- 证据：`server.py:415-427`（`Get-NetTCPConnection | Where LocalPort -in 6185,6199,5099,3000,3001 | taskkill`）；`main.js:1086-1096` 同一套逻辑。对比：杀语音引擎时**必须**先确认进程名含 `breeze`（`tts_breeze.py:555-561, 686-699`，fail-closed），QQ 这条**没有任何身份校验**。
- 影响：`:3000`/`:3001` 是极常见的开发端口（AstrBot dashboard 用它，但别的项目也用）。点一次"关托管"就可能连带杀掉用户别的 dev server。测试 `test_qq_hosting.py:66-74` 只断言"不能碰 8848/8898"，没有覆盖这个风险。
- 同类：`/api/shutdown` 的退出路径也会走这一段（`server.py:1090-1113`）。

**P1-5 后台「对话模型」「API 密钥」两张卡必然 401（与网关契约不符）**
- 证据：`console.js:295` `GATEWAY = location.protocol + "//" + location.hostname + ":8898"`；`:321/:354/:651/:675/:715/:751` 直接 `fetch(GATEWAY + "/admin/model" | "/admin/keys")`，**从不带 `X-Gateway-Token`**（全文件只有注释提到 token）。网关 `gateway.py:2667-2674, 2753-2754, 2770-2771` 对这四条全部 `_require_token()` → 401；且 `gateway.py:2619` 的 `Access-Control-Allow-Headers` 只放 `Content-Type`，浏览器即使想加 token 也过不了 CORS 预检。
- 旁证：同一文件的「系统模型」卡**走对了**（`console.js:385-388` 注释明确说"走 8848 代理，不直接打网关"）——说明作者知道正确做法，只是这两张卡没改。
- 影响：后台"对话模型"永远显示"网关未连接"、"API 密钥"永远显示"读不到 key 列表"（`console.js:659` 把 401 也说成"没连上"，误导排查方向）。
- 归属：`console.js` 在我的范围，`gateway.py` 不在；**建议交给 gateway 组确认是否要放开 CORS header 或改成代理**。

**P1-6 本机任意进程＝完全主人权限（无鉴权的本地信任边界）**
- 证据：8848 的全部写操作只校验"来源 IP 是本机"（`server.py:1350-1356`）或 Origin 头（无 Origin 即放行，`server.py:1204-1218`）；`/api/chat` → `agent_bridge` → 网关 `/chat`（带 token、`local_pet=True`）→ 拥有写文件/跑命令能力（沙箱在 `gateway\hands.py:27` 的 `sandbox`）。
- 影响：本机任何用户级进程（含浏览器扩展/不被信任的脚本）可以直接 `POST http://127.0.0.1:8848/api/chat {"text":"把那句话写进 xxx"}` 以主人身份驱动 agent；`/api/say` 还能让它说任意台词。代码注释（`server.py:1177-1182`）承认了网页 CSRF 面并已修，但**本机进程**这一层是显式接受的信任假设。
- 判断：不算缺陷（本机信任是合理设计），但必须写进风险清单——尤其当 `desktop\workspace\delete_temp.py` 这种"能删桌面文件"的脚本出现在 agent 工作目录里时，说明这个假设已经在被真实使用。

### P2（9 条）

**P2-1 `do_POST` 已读完 body，403/415 分支又 `_drain_body()` 二次读，破坏 keep-alive**
- 证据：`server.py:1617-1632` 把 body 读进 `_body_cache`；`server.py:1678-1683` 在守卫失败后调 `_drain_body()`；`_drain_body`（`1583-1611`）又按 `Content-Length` 从 `rfile` 读一遍 → 把**下一条请求的字节**吃掉了。
- 实测（A/B）：A 组（原样）403 后同连接再发 GET → `BadStatusLine: <!DOCTYPE HTML>`；B 组（`_drain_body` 改空操作）→ 200。
- 影响：跨站被拒/415 之后，这条连接上的后续请求会失败。浏览器通常会重开连接所以用户感知不明显，但**正是 README:129-140 声称已经修好的那一类问题**（10053），修法方向对、落点重复了。

**P2-2 `/api/tts` 存在"检查通过但 `tts` 变 None"的竞态，会返回空响应**
- 证据：`server.py:1869-1880` 先查 `tts_ready.is_set()` 和 `tts.ready`，再调 `_tts_with_emotion`；后者 `server.py:1650-1651` 若 `not (tts and tts.ready)` **直接 return，不发任何响应**。supervisor 在 `995-1036` 会把 `tts` 置 None。
- 影响：极窄窗口内客户端会挂到超时（无 500/503）。建议改成抛异常走 500 分支。

**P2-3 `/api/reset` 是假清空，后台却提示"对话记忆已清空"**
- 证据：`server.py:1934-1938` → `agent_bridge.py:107-109` `reset()` 只把 `self.last_error` 置空；真实记忆在网关 SQLite（`gateway.py` 的 `turns` 表）。`console.js:234-235` 直接 `addMsg("sys","对话记忆已清空")`。
- 影响：用户以为清空了，实际下一轮还带着上下文。属**误导性 UI**。

**P2-4 死代码 2 处**
- `server.py:494-505 touch_region_at()`：生产代码零调用（前端 `pet.js:395 regionAt` 自己做命中），只有 2 个测试文件共 11 处引用（`test_touch_regions.py:87,93`、`test_touch_voice.py:84-100`）。
- `main.js:1014-1036 killDsh()`：零调用（11-05 改规格"退出保留 DSH"后遗留）。
- 另有 `agent_bridge.ChatAgent` 的 `cwd/approval_mode/max_history/audit_file` 四个参数**存了不用**（`agent_bridge.py:49-54`），`server.py:1146-1152` 仍然在传，`audit.log` 因此从未生成（实测 `Test-Path <repo>/desktop\audit.log` = False）。

**P2-5 引擎/显存相关的"兜底"依赖 PowerShell 与进程名，失败是静默的**
- 证据：`tts_breeze.py:218-263` 枚举失败返回 `None`（fail-closed 是对的）；但 `pid_listening_on` 在 PowerShell 起不来时返回 `None`（`191-198`），`stop(force=True)` 于是打一行"找不到监听进程，无需终止"就返回（`708-709`）——**显存没释放但看起来成功**。`server.log:1646` 有 `breeze-server 退出，code=3221226505`（0xC0000409 STATUS_STACK_BUFFER_OVERRUN）的实例。

**P2-6 `probe_breeze.py` 语法错误，文档推荐的冒烟脚本跑不了**
- 证据：`compile()` → `SyntaxError: unterminated triple-quoted string literal (detected at line 69)`（文件只有 1 个 `"""`）。README:52 / 项目地图:148 都把它当"端到端冒烟"推荐。

**P2-7 文档多处与代码不符**（详见 §7）

**P2-8 配置不可移植 + 引擎路径在仓库外**
- `config.json.breeze_dir` 是绝对路径 `G:\workplase\Breeze-TTS-2.cpp\...`，换机器必挂（`tts_breeze.py:597-610` 已经加了"人话报错"来缓解，但仍是硬编码）；`config.py:21` 里有同一个绝对路径作默认值。

**P2-9 Electron 窗口未限制导航/新窗口**
- 证据：`main.js:420-426` 与 `629-635` 只设了 `contextIsolation/nodeIntegration/sandbox`，**没有** `setWindowOpenHandler`、没有 `will-navigate` 拦截；`preload.js` 暴露了 `menuAction("quit"/"restart"/"hide")` 等能力。
- 影响：窗口加载的是 `http://127.0.0.1:8848/pet`；若 8848 被别的程序抢先占用（见 P1-3 的双绑语义 / 首次探测只看 `res.ok`），渲染进程就会获得一个带 IPC 桥的窗口。风险有限（只有窗口操作，没有 shell），但属于"本地服务被冒充"的放大器。

---

## 7. 文档 vs 代码不一致

| # | 文档说法 | 代码事实 | 性质 |
|---|---|---|---|
| 1 | README:104-113 接口表只有 10 条 | 实际 **23 条** `/api/*`（GET 12 + POST 16，5 条重叠）；README 完全没提 `wake-keywords`、`touch-regions*`、`touch-voice*`、`qq-hosting`、`system*`、`daily-review*`、`open-tts-cache` | 严重过时 |
| 2 | README:123 `/health` 在"只读 GET 白名单"里，可跨站读 | `server.py:1201` 确实把它写进 `PUBLIC_GET_PATHS`，但**没有任何 `/health` 路由** → 实测 404（`server.py:1390-1581` 的 `_route_get` 里搜不到 `/health`） | 文档+代码双错（代码侧是死配置） |
| 3 | README:62 "`tts_cache/` 临时语音，启动时自动清空" | `server.py:890-898` 启动只 `_prune_tts_cache()`，保留最近 60 个；注释明确说"不要再整锅倒掉" | 过时（功能已改） |
| 4 | README:50,65-78 引擎表：TTS 引擎 = `fastvits_item` 的 `FastVITS.exe`（约 5 GB） | `config.json:26` `tts_engine:"breeze"`；`config.json:27-28` 用 `breeze_dir` + `breeze-tts-2-q8_0.gguf`（3.32 GB，仓库外）；`tts_fastvits` 已是回滚项且**已坏**（P1-2） | 过时 |
| 5 | README:51 "FastVITS 保留可回滚" | 不可回滚（P1-2） | 与代码不符 |
| 6 | README:67 "本目录不含语音引擎…真东西都在 Steam 创意工坊里" | `assets\steam_workshop` 就在本目录内 5.43 GB；`config.py:10` 注释也写着"默认用包内副本，不再依赖本机 Steam 库" | 自相矛盾 |
| 7 | README:223-224 "`approval_mode:"auto"` 下读写文件、跑命令都是直接执行的，所以默认工作目录是 `workspace/`" | `agent_bridge.py:49-54` 把 `cwd/approval_mode/max_history` 存了**完全不用**；真实沙箱在网关 `gateway\hands.py` 的 `sandbox\`，与 `desktop\workspace` 无关 | 危险过时（让人误以为桌面侧有 workspace 隔离） |
| 8 | README:28 "后端没起的话它会自己拉起（约 3 秒）" | 冷启动等满 60s 上限很常见（`main.js:190-195`）；日志里 82 次 spawn / 4 次重拉成功 | 过于乐观 |
| 9 | 项目地图:142 "`desktop\main.js`" 是 Electron 主进程 | 实际 `desktop\desktop\main.js`（项目地图:116 自己写对了，:142 写错，同文档内部矛盾） | 路径错 |
| 10 | 项目地图:144 server.py 路由只列 6 条 | 实际 23 条（同 #1） | 过时 |
| 11 | 项目地图:116 "8848，语音 8849（ws 8850）" | ✅ 正确（8850 是 breeze-server 自己的 websocket，实测日志确认） | 一致 |
| 12 | 项目地图:212 "desktop\tests 另有 10 个；合计 1061 passed" | desktop 实跑 **126 passed**；文件数 10 个 ✅ | 行数/条数口径需 gateway 侧确认，desktop 部分一致 |
| 13 | 项目地图:1702 测试命令 `.\qq\astrbot\.venv\Scripts\python.exe -m pytest gateway/tests desktop/tests -q` | ✅ 可用（我正是用它跑的 desktop） | 一致 |
| 14 | README:115-127 入口守卫表 | ✅ 基本准确（Origin/405/415/8MB/安全头全部实测成立），只有 `/health` 一条错（#2） | 基本一致 |
| 15 | README:140 "单跑 10/10 过，整个类连跑 2/15 失败 —— 典型时序竞态" | 现在实测 126/126 全过；但 P2-1 说明同一类问题仍在另一处 | 已过时（旧症状） |
| 16 | README:30 一级轮盘 = `对话 / 大小 / 归位 / 后台 / 隐藏 / 退出`（平铺） | `pet.js:665-674` 的 `MENU.root` 只有 4 项：`对话 / 大小 / **操作** / 后台`；归位/隐藏/退出被收进二级 `ops`（`pet.js:675-698`），重启/QQ 托管也在 `ops` 里 | 过时（菜单已重构成两级） |
| 17 | 项目地图:155 一级 `对话 / 大小 / 操作 / 归位 / 后台 / 隐藏` | 同 #16，实际 4 项 | 过时 |
| 18 | README:72 Live2D 模型"约 14 MB" | 实测该 item 目录 **114 文件 / 28.4 MB**（`moc3` 7.23 MB + `texture_00.png` 6.01 MB + 25 个 exp3 + voice 参考 wav） | 数字过时 |

---

## 8. 未确认 / 需要别人核实的点

1. **网关 `console.js` 直连 8898 的两张卡**：我判"必然 401"是纯【读码】结论（服务未运行，没法实测）。请 gateway 组确认：是否计划放开 `Access-Control-Allow-Headers` 或给 8848 加代理。若他们已改过，我的 P1-5 自动降级。
2. **`8850` 是否只 bind 在 127.0.0.1**：`breeze-server.log` 只写 `websocket on ws://127.0.0.1:8850`，但引擎源码不在仓库（`G:\workplase\...`），无法确认它是否接受非本机连接。当前服务未运行，`netstat` 也看不到。
3. **Breeze 引擎 `/health` 是否真的永远不带 `pid`**：`tts_breeze.py:459-469` 说"引擎当前只回 `{"status","ws_port"}`"，`server.log` 里 80 条 adopted 日志全是 `PID=未知`。如果新版本引擎加了 `pid` 字段，`_instance_hint` 会自动用上——需要跑起来才能确认。
4. **`data/touch_regions.json` 的实际生效区域**：文件里有 7 个区域，但 `legs` 的 shape 是 `y=0.5278,h=0.3263`（0.5278~0.8541），和内置默认 `legs`（0.72~0.92）不同；`feet` 是 0.8703~0.9604。这是**主人手画过的证据**，但我无法确认它和模型当前姿态是否对齐（需要跑起来实测命中）。
5. **`desktop\workspace\delete_temp.py` 是不是 agent 真跑出来的**：内容会删 `%USERPROFILE%\Desktop\新建 Text Document*`。文件 mtime 2026-10-27，`config.json.agent.cwd="workspace"` 指的就是这个目录，但我没有执行历史可证。若确认是 agent 产物，说明"本机信任"假设已被用过（P1-6 的实证）。
6. **`main.js` 的 `DM_DEBUG_PORT` 调试桥**是否曾开过：`/eval` 可执行任意 JS。默认 `0`＝关闭，配置里也没开，但开过就会留下无鉴权 RCE 面（仅监听 127.0.0.1）。
7. **gateway/tests 的 1012 passed**（lead 实测）vs 项目地图:1694 的 935：不在我范围，但既然 desktop 侧 126 与文档一致、gateway 侧多了 77 条，建议在总报告里统一口径。
8. **`web\chatbox.js` 行数**：我两次按字节测得 98（2793 B），lead 给的是 92。差异可能是统计口径（如是否算末尾空行/是否用了 `Get-Content`）。结论不受影响，但请在总报告里用同一口径。

---

*报告完。所有结论均可按文中 `文件:行号` 复核；实测命令与输出见 §5。本次审计未修改任何项目源文件（`git status --porcelain -- desktop/` 为空），仅新增本报告。*

*副作用声明：为做离线探针，我用系统 Python 3.13 `import server` 若干次，`desktop\__pycache__\` 下因此多了 4 个 `*.cpython-313.pyc`（`agent_bridge/config/server/tts_breeze`，mtime 均为 `2026-11-07 14:44:41`）。它们是 `.gitignore` 里的派生缓存、不属于源文件，未删除以免造成额外改动；如需零足迹可自行清理该目录下的 `*-313.pyc`。所有正式测试均用 `qq\astrbot\.venv`（3.12）跑，只更新了 `*-312*.pyc`。*
