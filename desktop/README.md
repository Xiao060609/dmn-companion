# dmbrowser —— 小菲鱼 · 桌宠

把 Digital Mate 的 **2D 形象 + 语音引擎 + agent 框架** 拆出来，做成一只贴在桌面上的桌宠；
浏览器退成后台控制台。和原来的 DigitalMate **并存**：不改它的文件、不关它的进程，两边可以同时开。

## 形态

```
┌──────────────────────────────────────────┐
│ 桌面窗口（Electron）                      │
│   透明 · 无边框 · 置顶 · 不占任务栏        │
│   只有人物 + 气泡                          │
└───────────────┬──────────────────────────┘
                │ SSE 收消息 / HTTP 取语音
┌───────────────▼──────────────────────────┐
│ Python 本地服务 (server.py)               │
│   agent_bridge · tts_fastvits · 模型资源   │
└───────────────▲──────────────────────────┘
                │ HTTP
┌───────────────┴──────────────────────────┐
│ 浏览器：后台控制台 http://127.0.0.1:8848  │
│   设置卡片 / 推送说话 / 状态 / 对话记录标签 │
└──────────────────────────────────────────┘
```

## 快速开始

1. 双击 `启动桌宠.bat` —— 桌面右下角出现人物，后端没起的话它会自己拉起（约 3 秒）。
2. 双击人物 → 浏览器打开后台控制台；右键人物 → 轮盘菜单：
   一级 `对话 / 大小 / 归位 / 后台 / 隐藏 / 退出`，`大小`（小·中·大·复位）和 `退出`（确定·取消）还有二级。
   再右键一次、按 `Esc`、点窗口空白、或者鼠标移出窗口约 0.7 秒都会收起。
3. 拖动人物可以换位置，位置会记住。
4. 右键 → **对话**：人物头顶冒出一个灰色输入条，光标已经停在里面，直接打字回车即可聊天
   （回复走气泡 + 表情 + 语音）。`Esc` 关闭；输入框空着时点到别处会自动收起，正在打的内容不会丢。
5. 后台控制台的**对话记录**默认是收起的，设置卡片会铺满整屏；点右上角「对话记录」才展开。
   收起期间来了新消息，按钮上会显示一个数字角标；展开/收起状态会被记住。

也可以分开跑：

```powershell
cd <repo>/desktop\desktop
node_modules\electron\dist\electron.exe .
```

## 目录

| 文件 | 作用 |
|---|---|
| `server.py` | 本地 HTTP 服务：静态页、模型映射、`/api/*`、SSE |
| `tts_breeze.py` | 语音引擎（Breeze-TTS-2.cpp，**当前**）：起 `breeze-server.exe` 子进程 + HTTP 合成 |
| `tts_fastvits.py` | 语音引擎（FastVITS，**保留可回滚**：`config.json` → `tts_engine:"fastvits"`） |
| `probe_breeze.py` | Breeze 端到端冒烟脚本 |
| `BREEZE_PLAN.md` | Breeze 引擎更换计划 + 实施记录 |
| `agent_bridge.py` | agent.py 桥接：会话记忆 + 情绪标记解析 |
| `config.json` | 全部可调参数 |
| `desktop/` | Electron 外壳（`main.js` / `preload.js` / `icon.png`） |
| `web/pet.html` `pet.js` `pet.css` | 桌宠页：只有人物和气泡 |
| `web/chatbox.html` `chatbox.js` `chatbox.css` | 右键「对话」弹出的灰色输入条（独立小窗口） |
| `web/index.html` `console.js` `console.css` | 后台控制台：不渲染人物 |
| `web/stage.html` `app.js` `style.css` | 调试页：带输入框的完整舞台（排查用） |
| `workspace/` | agent 的工作目录 |
| `tts_cache/` | 临时语音，启动时自动清空 |
| `probe/` | 前期可行性验证脚本，可删 |

## 语音引擎在哪

本目录**不含**语音引擎和声音模型：`tts_fastvits.py` 只是驱动脚本（伪造宿主窗口 + WM_COPYDATA），
`tts_cache/` 只是临时 wav。真东西都在 Steam 创意工坊里，靠 `config.json` 的三行路径找到：

| 用途 | config 字段 | 实际位置 |
|---|---|---|
| Live2D 模型 | `live2d_item` | `…\2488350\3051599132\`（约 14 MB） |
| TTS 引擎 | `fastvits_item` | `…\2488350\3010132930\FastVITS.exe`（自带 Python + torch/CUDA，约 5 GB） |
| 音色模型 | `vits_model_item` | `…\2488350\3010104972\G_953000.pth`（约 457 MB） |

`…` 是 `config.json` 里的 `steam_workshop`。所以：**换音色**只改 `vits_model_item`（配合 `speaker`）；
**引擎换位置**改 `fastvits_item`；本目录一个模型文件都不用复制。
反过来，订阅更新 / 取消订阅工坊内容会让这几个路径失效，症状就是前端一直「语音加载中」。

## 配置（config.json）

```jsonc
{
  "port": 8848,
  "speaker": 126,                  // 126 = 早柚
  "length_scale": 1.0,             // 语速，越小越快
  "noise_scale": 0.67,
  "noise_scale_w": 0.8,
  "start_tts_on_boot": true,
  "hidden_parts": ["Part5"],       // 模型里默认隐藏的部件（Part5 = WM，见下）
  "agent": {
    "enabled": true,
    "cwd": "workspace",
    "approval_mode": "auto",
    "max_history": 8
  }
}
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 语音 / 智能体 / 桌宠连接数 |
| GET | `/api/events` | SSE：`{"type":"say","text","emotion"}` |
| GET | `/api/pet-config` | 桌宠页的配置（`hidden_parts` 等） |
| GET | `/model/settings.json` | 自动生成的模型配置 |
| GET | `/model/<path>` | 模型资源（`.moc3` 在内存里解密后发送） |
| POST | `/api/tts` | `{"text":"..."}` → `audio/wav` |
| POST | `/api/chat` | `{"text":"...","client":"..."}` → `{"reply","emotion"}` |
| POST | `/api/say` | 推送到桌宠朗读 |
| POST | `/api/reset` | 清空对话记忆 |
| POST | `/api/shutdown` | 关服务（**只接受 POST**） |

### 入口守卫（2026-11-04 加）

这个服务跑在 8848，浏览器能直接打开，所以任何网页都能向它发请求。
以下是现在的防线：

| 规则 | 行为 |
|---|---|
| **跨站 Origin** | 带 `Origin` 且不在白名单 → **403**（挡掉别的网站替你点按钮） |
| **只读 GET 白名单** | `/api/status` `/api/pet-config` `/api/wake-keywords` `/api/touch-regions` `/api/touch-voice-status` `/health` 允许跨站读（否则浏览器里打不开） |
| **带 body 的 POST** | 必须 `Content-Type: application/json`，否则 **415** |
| **请求体大小** | 上限 **8MB**，超出 → 413 |
| **安全响应头** | `X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` |
| **`/api/shutdown`** | 以前 **GET 也能关**（任何网页塞个 `<img src="...api/shutdown">` 就能关掉她）；现在只接受 POST，GET 返回 405 |

### 一个容易踩的坑：POST 必须把 body 读完

`protocol_version = "HTTP/1.1"` + keep-alive 时，**服务端必须读完请求体**，
否则剩余字节留在 socket 里，下一次读会当成新请求行；或者关连接时
客户端那边正在 `getresponse()` 的 socket 被 RST —— 抛
`ConnectionAbortedError [WinError 10053]`。

所以 `do_POST` 一进来就把 body **整段读完存进 `_body_cache`**，
`_read_json()` 只解析缓存。任何提前 return 的分支（403/415/404/405）
都要先 `_drain_body()`。

实测症状：单跑那条测试 10/10 过，整个类连跑 2/15 失败 —— 典型时序竞态。

## 几个关键技术点

1. **透明窗口**
   Electron `transparent: true` + `backgroundColor: "#00000000"` + 页面 `background: transparent`；
   `show: false` 等 `ready-to-show` 再 `showInactive()`，避免白闪。

2. **点击穿透**
   渲染进程判断鼠标是否在人物身上，通过 IPC 让主进程切
   `setIgnoreMouseEvents(flag, { forward: true })`。`forward` 保证穿透时仍能收到 mousemove，
   鼠标一回到人物身上就恢复可点。
   注意**不能直接拿模型包围盒当命中框**：画布把大头饰、宽袖子都算进去了，框会比人大一圈，
   很容易误点。现在按实测轮廓收成"头 + 身子"（横向 33%~67%，纵向 10%~64%），
   宽袖子和腿都在框外、可以穿透。

3. **拖动**
   渲染进程只上报屏幕坐标（`screenX/screenY`），真实位移由主进程算并 `setBounds`，
   这样鼠标拖出窗口也不会丢事件。松手后位置写进 `desktop/window-state.json`。

   **拖动时窗口会被系统放大**：在 125% 缩放（DPI 1.25）下，只调 `setPosition` 时 Windows
   会按物理像素取整把窗口一点点撑大——实测拖 600px，窗口从 384×568 涨到 423×612，
   人物跟着"自己变大"。修法：`setBounds` 每次都带上固定尺寸、松手时再还原一次尺寸，
   前端同时忽略 8px 以内的 resize 事件。

   **按住拖动 + 滚轮缩放**：滚轮按 `exp(-deltaY * 0.0007)` 调倍率（0.45~2.2），
   以窗口底边中点为锚点缩放，人不会乱跳；倍率存进 `window-state.json`，
   托盘菜单里的「重置缩放」可以还原。

4. **FastVITS 脱离 DigitalMate**
   FastVITS 启动时用 `FindWindowW("FMTFrmManageEngine")` 判断"宿主在不在"，不存在就直接退出；
   而且**运行期间也会一直检查**——那个窗口一消失，它 1~2 秒后自己退出（code 0，没有任何报错）。
   所以我们：
   - DigitalMate 在跑 → 直接用它的窗口，自己什么都不造；
   - DigitalMate 没跑 → 自己造一个同名的"启动门"窗口，并且**一直留着**，等 DigitalMate 起来
     再把门让出去（窗口类是按进程注册的，不妨碍游戏之后自己启动）；
   - 真正的消息宿主是我们自己的 `DMBrowserHost` 窗口，和游戏的窗口完全隔离。
   服务端还有一个守护线程：引擎掉线就自动重拉，不用重启服务。

5. **`.moc3` 是 XOR 0x73 加密的**
   首字节 `3e 3c 30 40` 与 `MOC3` 异或正好都是 `0x73`。服务端在**内存里**解密后发送，
   原文件一个字节都没改。

6. **口型是自己驱动的**
   模型 `Groups.LipSync.Ids` 是空数组。前端在 `beforeModelUpdate` 写入 `ParamMouthOpenY`，
   用 Web Audio Analyser 取实时音量。

7. **目光跟随鼠标**
   桌宠窗口只有几百像素宽，只在窗口内跟鼠标会经常"断线"。所以由主进程每 60ms 轮询
   `screen.getCursorScreenPoint()`，换算成窗口内坐标推给渲染进程。
   视线起点取**头部**（包围盒竖直方向 21% 处）：直接用 `model.focus()` 的话，它的原点是
   模型画布中心（大概在胯部），鼠标放在肚子上时眼睛反而往上看。

8. **模型原点在左上角**
   布局必须用 `getBounds()` 居中，不能直接 `position.set(w/2, h/2)`。

9. **对话输入条为什么是独立窗口**
   桌宠主窗口是"透明 + 按区域穿透"的，把 `input` 塞进去会很难点、还会被穿透逻辑打断。
   所以右键「对话」时另开一个小窗口（430×62，无边框、置顶、不占任务栏），
   位置算在人物头顶上方并水平居中；打开后立刻 `focus()` + 渲染端 `input.focus()`，
   光标直接落在输入框里。中文输入法组字时 `isComposing` / `keyCode 229` 会被跳过，
   回车不会误发。

10. **默认隐藏的部件**
   模型 `cdi3.json` 里 `Part5` 的名字是 `WM`，是一块灰色水印板；`item.json` 里没有对应配置项，
   所以「默认不显示」是我们自己的取舍，通过 `config.json` 的 `hidden_parts` 控制，改配置即可，不写死在代码里。

11. **右键用轮盘而不是原生菜单**
    原生 `Menu.popup` 出不来"系统菜单"以外的样子，也和透明风格不搭，所以菜单是画在桌宠窗口里的 HTML：
    右键按光标位置弹一级轮盘（位置会被夹在窗口内），点带小箭头的项时一级先收掉、再弹二级。
    菜单开着时窗口强制 `setIgnoreMouseEvents(false)`（否则按钮那块会被判成"人物以外"而穿透），
    关闭后再按鼠标位置恢复；托盘菜单仍然是原生菜单。

12. **后台的对话记录是二级标签**
    设置卡片才是后台的主角，所以对话记录默认收起、不占版面，点右上角「对话记录」才展开；
    收起时右侧卡片自动改成自适应多列。收起期间的新消息只在按钮上记角标，不打断当前操作。

## 已知限制

- **模型是试用版**：文件名就是 `Salt - Trial Version`，贴图里还有两处 `sample`。
  这里只是按游戏的处理方式隐藏了 `WM` 部件；模型版权与授权仍归作者（小柴起司 / kornos_42），
  自用可以，别商用、别二次分发。
- **Live2D Cubism SDK 许可**：个人 / 小规模（年营收 < 1000 万日元）免费，商用需授权。
- **智能体会真的动手**：`approval_mode: "auto"` 下读写文件、跑命令都是直接执行的，
  所以默认工作目录是 `workspace/`。不确定就改成 `manual` 或 `enabled: false`。
- 只监听 `127.0.0.1`，局域网访问不了（这是有意的）。

## 出问题怎么办

- **桌宠窗口没出来**：看 `desktop/pet.log`；后端没起来的话看 `desktop/server.log`。
- **一直是透明空白**：多半是页面加载失败，`pet.log` 里会有 `did-fail-load`。
- **一直「语音加载中」**：看 `server.out.log` 里的 `[tts]` 报错，通常是工坊路径变了。
- **点不到人物 / 挡住了桌面**：鼠标移到人物身上才可点，其它区域是穿透的。
- **想完全关掉**：托盘右键 → 退出；Electron 退出时会一起关掉它拉起的 `server.py`。
