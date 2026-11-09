# 小鱼 TTS 引擎更换计划：FastVITS → Breeze-TTS-2.cpp

> 创建：2026-10-31 00:30。目标：把 `<repo>/desktop` 的语音引擎从 FastVITS（DigitalMate 的 exe + Steam 工坊资源）换成
> `<workspace>/Breeze-TTS-2.cpp`（本地预编译 Vulkan 二进制 + 3.5GB 模型，Apache 2.0 源码 / 权重 BreezeBlue 研究许可）。
> 本文件是上下文压缩前的恢复锚点：每一步做完打勾，中断后读这里接着干。

## 0. 验收标准

1. 桌宠（8848）和 QQ（AstrBot voice_reply）说话改用 Breeze 的声音，文字链路不变
2. 前端 `/api/tts` 返回的仍是**标准 16-bit PCM WAV**（浏览器 `<audio>` 直接播），采样率 24000
3. 引擎崩溃自动重拉（保留 server.py 的 supervisor 模式）
4. FastVITS 相关代码保留但不再启动（config 开关，可回滚）

## 1. 现状核查（已完成）

### 小鱼这边

- `desktop\server.py`：`/api/tts` → `tts.speak_wav(text)`；`_launch_tts()` 起 FastVITS，`_tts_supervisor()` 3s 轮询自动重拉
- `desktop\config.py`：`FASTVITS_EXE` / `VITS_MODEL` / `VITS_CONFIG` 指向 `assets\steam_workshop\content\2488350`
- `desktop\tts_fastvits.py`：伪造 `FMTFrmManageEngine` 宿主窗口 + WM_COPYDATA + float32→PCM16 转换（525 行，保留不动）
- 前端 `web\pet.js` / `web\app.js`：`POST /api/tts {text}` → `res.arrayBuffer()` → Web Audio `decodeAudioData` → 口型（Analyser）驱动。
  **只要 /api/tts 还是 WAV，前端零改动。**
- `agent_bridge.py` 只走网关 8898，不涉及 TTS。QQ 侧 AstrBot `voice_reply` 插件合成路径独立，最后一条统一测。

### Breeze 这边（实测结果）

- 位置：`<workspace>/Breeze-TTS-2.cpp\breeze-tts-2-v0.1.3-windows-x64-vulkan\`
  - `breeze-cli.exe`（59MB）/ `breeze-server.exe`（60MB）/ `libbreeze.dll`（59MB）
  - `models\breeze-tts-2-q8_0.gguf`（3.5GB，已下载完毕）
  - `models\ref_voice.wav`（257KB，BreezeBlue 官方参考音频，**不是早柚的声音**）
- 硬件：RTX 4060 Laptop GPU，Vulkan 识别正常
- **CLI 合成已验证**：`breeze-cli <model> --text "..." --output out.wav` 成功，输出 24kHz 16-bit WAV
- **server 已验证**（PID 37484，仍在跑）：
  - `GET /health` → `{"status":"ok","sample_rate":24000,"ws_port":8081}`
  - `POST /v1/audio/speech` form 字段 `text` → 流式 PCM s16le（107520 字节，4.48s 音频）
  - 字段：`text`(必填) `instruction`(音色描述/演绎方向) `voice_id`(已存声线) `cfg_scale`(默认 1.0) `seed`(默认 42)
  - `response_format` 走 OpenAI JSON 模式可要 `wav`/`mp3`/`pcm`
  - ⚠ **端口实测**：`--port` 参数没生效，二进制默认 HTTP=8080 / WS=8081。部署必须固定端口或验证 `--port` 修复，
    否则和已有 8848/8898/5099 不冲突但和别的 8080 服务冲突
- 单线程模型：一个 server 同时只处理一个生成请求，并发第二个返回 409 busy（小场景够用）
- `voices` 目录：用 `--save-voice` 可把声线编码成 `.breeze` 文件持久保存（CLI 和 server 共享同一目录，`--voices-dir` 指定）

## 2. 架构决定

**走 HTTP server 模式**（breeze-server.exe 子进程 + urllib 调 /v1/audio/speech），不用 ctypes 内嵌 DLL。

理由：
1. model 3.5GB 加载一次常驻子进程，server.py 本身不占显存
2. supervisor 模式现成：掉线就重启子进程（比 FastVITS 的"窗口活着就活"简单）
3. libbreeze.dll 路径写进 Python 进程里会多一份 3.5GB 常驻（每进程加载权重），内存翻倍
4. 前端不用改

**不改前端**。`/api/tts` 继续返回完整 WAV（非流式）：桌宠一句最多几百字，合成 1.5x 实时速度，
等完整 WAV 比流式更稳（口型驱动靠 Analyser，整段解码最简单）。

## 3. 声线问题（需用户拍板）

Breeze 是**指令式/克隆式**引擎，没有"speakerID=126 早柚"这种固定编号声线。两条路：

| 方案 | 做法 | 效果 | 成本 |
|---|---|---|---|
| A. 描述式（默认） | `instruction` 写一句早柚的音色描述 | 不克隆、不依赖参考音频，冷启动最快 | 0，但音色是"近似" |
| B. 克隆式（推荐试） | 早柚找/录一段 5-15s 干净干声 + 逐字转写 → `--save-voice zhaoyou` 存成 `.breeze` | 音色真·早柚 | 需要主人提供早柚参考音频 |

**默认先上 A**（config 里 `breeze.instruction` 可写），B 等用户给参考音频再补。

## 4. 实施步骤

### 4.1 新增 `desktop\tts_breeze.py`（FastVITS 同接口：`start()` / `ready` / `speak_wav(text)` / `stop()`）

职责：
- `start()`：spawn `breeze-server.exe <model.gguf> --host 127.0.0.1 --port <P> --no-warmup`（Job Object 挂父进程，同 KillOnCloseJob）
  - 轮询 `GET /health` 直到 `status=ok`（模型 3.5GB 加载可能 10-30s，warmup 省 2s 但首句慢 300ms，取舍见 4.4）
  - 端口固定 `8849`（8848 后端、8850 网关预留；避开 8080/8081 默认段）—— **先验证 `--port` 是否真生效**
- `speak_wav(text)`：
  1. `POST http://127.0.0.1:8849/v1/audio/speech`（OpenAI JSON 模式，`response_format=wav`）
  2. 请求体：`input=text`，`instructions=CFG["breeze"]["instruction"]`，`voice=CFG["breeze"]["voice_id"]`（若配了）
  3. 响应是 WAV 直接拿 → 写 `TTS_OUTDIR/tts_NNNNN.wav`（复用现有 outdir 约定）→ 返回 Path
  4. **不做 PCM16 转换**（response_format=wav 就是标准 WAV）
- `stop()`：terminate 子进程 + 关 Job
- 409 busy 时 sleep+retry（最多 3 次，退避 1s/2s/4s）

### 4.2 `config.py` 新增

```python
BREEZE_DIR = Path(CFG.get("breeze_dir", r"<workspace>/Breeze-TTS-2.cpp\breeze-tts-2-v0.1.3-windows-x64-vulkan"))
BREEZE_MODEL = BREEZE_DIR / "models" / CFG.get("breeze_model", "breeze-tts-2-q8_0.gguf")
BREEZE_SERVER = BREEZE_DIR / "breeze-server.exe"
BREEZE_VOICES = Path(CFG.get("breeze_voices_dir", "breeze_voices"))  # 相对 BASE
TTS_ENGINE = CFG.get("tts_engine", "breeze")   # "breeze" | "fastvits"
```

### 4.3 `config.json` 新增段

```json
"tts_engine": "breeze",
"breeze": {
  "port": 8849,
  "instruction": "温柔清亮的年轻女声，像早柚一样",
  "voice_id": "",            // 空 = 描述式；配好克隆声线后填 "zhaoyou"
  "cfg_scale": 1.0,
  "seed": 42,
  "max_new_tokens": 0
}
```

### 4.4 `server.py` 改造

- `_launch_tts()` 按 `TTS_ENGINE` 分支：`breeze` → `from tts_breeze import Breeze`；`fastvits` → 原逻辑
- `/api/tts` 不变（都调 `tts.speak_wav`）
- supervisor 不变（都查 `tts.ready`）
- `/api/status` 加 `"tts_engine": "breeze"|"fastvits"`（前端控制台显示用）
- `main()` 退出时 `tts.stop()` 不变

### 4.5 验证顺序

1. **端口验证**：先手动起 `breeze-server.exe --port 8849`，`Get-NetTCPConnection -LocalPort 8849` 确认监听 8849 而不是 8080
2. 单独跑 `python -c "from tts_breeze import Breeze; ..."` 合成一句，听 WAV
3. 起 server.py（`python server.py`），`curl /api/tts`，浏览器开 8848 控制台点「对话」听声音 + 口型
4. 杀掉 breeze-server 进程，等 supervisor 自动重拉，再合成一句
5. QQ 链路：AstrBot 发一句带 `[语音]` 前缀的，确认 voice_reply 插件也能出 Breeze 声音
6. 写回 `config.json` + `项目地图.md`（第八节外部依赖表把 FastVITS 换成 Breeze）

## 5. 回滚

`config.json` 里 `"tts_engine": "fastvits"` 一键切回（tts_fastvits.py 和 Steam 工坊资源都不动）。

## 6. 已知风险 / 待办

- [ ] `--port` 实测没生效（起在 8080）。若 `--port` 修不了，固定 8080/8081，config 写死 8080
- [ ] 显存：Breeze Q8_0 常驻 ~4GB 显存。FastVITS（torch+CUDA 那 5GB exe）还在 Steam 工坊里但**不再启动**，
      桌宠+网关+Electron 的显存总账要跑一次看 4060 8GB 够不够。不够就换 q4_k（2.4GB，~3GB 显存，需重新下载）
- [ ] `max_chars=200` 现在截断文本，Breeze 的 `split_chars` 默认 600、支持更长的整段，可以放宽（前端/网关不受影响）
- [ ] 早柚克隆声线：等主人给一段早柚干声（5-15s、安静、无 BGM），我跑 `--save-voice zhaoyou` 存进 `breeze_voices/`
- [ ] 权重许可：BreezeBlue 研究/非商用。小鱼自用没问题，别把 .gguf 分发出去（项目地图第八节补一行）

## 7. 实施结果（2026-10-31 完成）

**已完成，链路全通：**

- `tts_breeze.py` 写好（同 FastVITS 接口，HTTP server 模式，409 退避重试）
- `config.py` / `config.json` 接入（`tts_engine: "breeze"`，端口 8849/8850）
- `server.py` `_launch_tts()` 按 `TTS_ENGINE` 分支，`/api/status` 多返回 `tts_engine`
- **端口坑已解决**：之前 `--port` 没生效是 PowerShell `ArgumentList` 把模型路径引号吃掉导致解析回退默认 8080。用引号包裹模型路径后 `--port 8849` 正常监听 8849/8850。
- 冒烟测试通过：`/api/tts` 返回 24kHz/16bit/单声道标准 WAV（前端零改动）
- **supervisor 自动重拉验证通过**：杀掉 breeze-server 进程，5 秒内 server.py 自动重拉，`/api/tts` 恢复
- 新增 `probe_breeze.py` 冒烟脚本

**遗留待办（不影响运行）：**
- QQ 链路（AstrBot voice_reply）未单独验证，但 `/api/tts` 通用，预期正常
- 早柚克隆声线：描述式指令已生效，要真克隆等主人给 5-15s 干声
- 权重许可：BreezeBlue 研究/非商用，自用没问题，别分发 .gguf
- FastVITS 保留在 `tts_fastvits.py`，`tts_engine: "fastvits"` 一键回滚

## 8. 分片合成 + 缝合（2026-10-31 追加）

**问题**：Breeze 是自回归逐帧生成，合成速度只有实时的 1/2。长文本（背诗、报数据）
一次请求要等很久，而且耗时**超线性**。

**实测数据**（同一台 4060，慢速声线）：

| 文本 | 合成 | 音频 | 合成/播放 |
|---|---|---|---|
| 16 字 | 7.5 s | 4.2 s | 1.81× |
| 32 字 | 20.5 s | 11.0 s | 1.87× |
| 64 字 | 45.9 s | 22.6 s | 2.03× |
| 192 字（单次） | 139.4 s | 60 s | — |

**方案**：`tts_breeze.py` 里 `split_for_tts()` 按句号切分（超长句退到逗号，再不行硬切），
逐片调 `/v1/audio/speech`，再 `_parse_wav()` + `_build_wav()` 把 PCM 缝成**一条** WAV，
片间补 `chunk_gap_ms` 静音当换气。对调用方完全透明——`/api/tts` 仍然返回一个文件。

**效果**：

| 文本 | 分片前 | 分片后 | 提升 |
|---|---|---|---|
| 64 字 | 45.9 s | **30.7 s** | 快 33% |
| 192 字 | 139.4 s | **96.3 s** | 快 31% |

**关于显存（实测澄清）**：分片**并不降低显存峰值**。16 字 +661 MiB、64 字 +503 MiB、
192 字 +493 MiB——峰值基本恒定，说明它由每步生成的激活决定，与输入长度无关
（总显存 8188 MiB，峰值约 7300 MiB，余量 ~900 MiB）。分片的价值是**速度**和
**单次请求不超时**，不是省显存。

**配置**：`config.json` → `breeze.chunk_chars`（默认 28）、`breeze.chunk_gap_ms`（默认 250）。
`chunk_chars` 设很大（如 600）即可退回「整段一次合成」的旧行为。

**注意**：QQ 侧 voice_reply 插件 `max_chars=100`，所以一条语音最长约 35 s；
桌宠不受此限（`server.py` 的 `max_chars=200` 约 64 s 音频）。
