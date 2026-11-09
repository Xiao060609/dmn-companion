/* 小菲鱼 · 桌宠外壳（Electron 主进程）
 *
 * 形态：透明、无边框、置顶、不占任务栏的窗口，只显示 Live2D 人物。
 * 浏览器 http://127.0.0.1:8848 只是后台控制台。
 *
 * 关键点：
 * - 透明窗口里 WebGL 正常渲染，需要 backgroundColor 全透明 + 页面自身 background: transparent
 * - 点击穿透：渲染进程判断鼠标是否在人物包围盒内，通过 IPC 通知这里切换 setIgnoreMouseEvents
 * - 拖拽：渲染进程只报屏幕坐标，窗口位移在这里算，避免跨窗口丢事件
 * - 后端：启动时探测 8848，没起来就自己拉一个 python server.py，退出时一并关掉
 * - 生命周期（2026-11-05 按主人规格固定）：
 *     启动 = 全部拉起；DSH + 语音引擎已在跑就跳过
 *     重启 = 除语音引擎 + DSH 外全关，再重开
 *     退出 = 除 DSH 外全关（含 QQ 机器人），**DSH 保留**
 *   注意 QQ 托管不在这一层停：它在 server.py 的 /api/shutdown 里做，因为
 *   那时 server 还活着，而这一层要 app.quit() 了、普通子进程会被 Job 带走。
 */
"use strict";

const { app, BrowserWindow, Menu, Tray, ipcMain, shell, nativeImage, screen, Notification } = require("electron");
const path = require("path");
const fs = require("fs");
const net = require("net");
const { spawn } = require("child_process");

const BASE = path.resolve(__dirname, "..");
const STATE_FILE = path.join(__dirname, "window-state.json");
const SERVER_LOG = path.join(__dirname, "server.log");
const GATEWAY_LOG = path.join(__dirname, "gateway.log");
// 网关默认在同级目录 gateway，可用环境变量覆盖
const GATEWAY_ORIGIN = (process.env.GATEWAY_URL || "http://127.0.0.1:8898").replace(/\/$/, "");
const GATEWAY_DIR = process.env.GATEWAY_DIR
  || path.resolve(BASE, "..", "gateway");
const PET_LOG = path.join(__dirname, "pet.log");
const DEBUG_PORT = Number(process.env.DM_DEBUG_PORT || 0);

function logLine(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try { fs.appendFileSync(PET_LOG, line); } catch { /* ignore */ }
  console.log(msg);
}

function readConfig() {
  try {
    const raw = fs.readFileSync(path.join(BASE, "config.json"), "utf-8").replace(/^\uFEFF/, "");
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

const CFG = readConfig();
const HOST = CFG.host || "127.0.0.1";
const PORT = Number(CFG.port || 8848);
const ORIGIN = `http://${HOST}:${PORT}`;

const SCALES = { small: 0.45, normal: 0.62, large: 0.78 };
const ASPECT = 0.68; // 窗口宽 / 高

let win = null;
let tray = null;
let serverProc = null;
let gatewayProc = null;
let quitting = false;
let dragState = null;
// T-04：spawn 直接失败（只有 "error" 没有 "exit"）时置位，让 ensureServer 别死等
let serverSpawnFailed = false;

// ---------------------------------------------------------------- 窗口状态
function loadState() {
  try {
    // 记事本等编辑器可能给 JSON 加 BOM，先去掉再解析
    const raw = fs.readFileSync(STATE_FILE, "utf-8").replace(/^\uFEFF/, "");
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function saveState(patch) {
  const state = { ...loadState(), ...patch };
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2), "utf-8");
  } catch { /* 状态文件写不了不影响运行 */ }
}

function currentScale() {
  const name = loadState().scale;
  return SCALES[name] ? name : "normal";
}

// 缩放倍率（滚轮调），和「大小」预设相乘
function currentZoom() {
  const z = Number(loadState().zoom);
  if (!Number.isFinite(z) || z <= 0.2) return 1;
  return Math.min(2.2, Math.max(0.45, z));
}

function windowSize(scaleName, zoom = 1) {
  const area = screen.getPrimaryDisplay().workAreaSize;
  const height = Math.round(area.height * (SCALES[scaleName] || SCALES.normal) * zoom);
  return { width: Math.round(height * ASPECT), height };
}

function clampPosition(x, y, width, height) {
  const display = screen.getDisplayNearestPoint({ x, y });
  const area = display.workArea;
  const maxX = area.x + area.width - width;
  const maxY = area.y + area.height - height;
  return {
    x: Math.max(area.x, Math.min(x, maxX)),
    y: Math.max(area.y, Math.min(y, maxY)),
  };
}

function defaultPosition(width, height) {
  const area = screen.getPrimaryDisplay().workArea;
  return {
    x: area.x + area.width - width - 30,
    y: area.y + area.height - height - 10,
  };
}

function targetPosition(width, height) {
  const saved = loadState().x;
  const savedY = loadState().y;
  if (typeof saved === "number" && typeof savedY === "number") {
    return clampPosition(saved, savedY, width, height);
  }
  return defaultPosition(width, height);
}

// ---------------------------------------------------------------- 后端
async function serverAlive() {
  try {
    const res = await fetch(`${ORIGIN}/api/status`, { signal: AbortSignal.timeout(900) });
    return res.ok;
  } catch {
    return false;
  }
}

function spawnServer() {
  const py = process.env.DM_PYTHON || "python";
  const log = fs.createWriteStream(SERVER_LOG, { flags: "a" });
  const stamp = new Date().toISOString();
  log.write(`\n===== ${stamp} start =====\n`);

  serverProc = spawn(py, ["server.py"], {
    cwd: BASE,
    windowsHide: true,
    // 强制子进程按 UTF-8 写 stdout/stderr：
    // 否则 Python 在管道下用系统代码页（cp936）输出，日志里中文会变乱码。
    // 只设 IO 编码、不开 PYTHONUTF8，避免改变 agent 读写文件时的默认编码。
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  serverProc.stdout.pipe(log, { end: false });
  serverProc.stderr.pipe(log, { end: false });
  serverProc.on("error", (err) => {
    log.write(`[spawn error] ${err && err.message ? err.message : err}\n`);
    // ⚠ 2026-11-04（T-04）：这一句原来缺失，是个很隐蔽的坑。
    // 进程压根没创建起来时（比如 python 不在 PATH），Node 只发 "error"、**不发 "exit"**，
    // 于是 serverProc 一直指向一个坏掉的 ChildProcess 对象，下面 ensureServer 的
    // `if (!serverProc ...)` 短路永远不成立 → 白等满 60 秒才放弃。
    serverProc = null;
    serverSpawnFailed = true;
  });
  serverProc.on("exit", (code) => {
    log.write(`[server exit] code=${code}\n`);
    serverProc = null;
    // ⚠ 2026-11-04（T-04）：原来只写日志就完事 —— 后端崩了以后桌宠永久空窗。
    // 现在按退避重拉，连续失败到上限就停手并告诉用户（而不是无限重试刷日志）。
    if (quitting) return;
    scheduleServerRestart(log);
  });
  log.write(`[spawn] ${py} server.py (pid ${serverProc.pid})\n`);
}

async function ensureServer() {
  if (await serverAlive()) return true;
  console.log("[pet] 后端未启动，正在拉起 server.py");
  serverSpawnFailed = false;
  spawnServer();
  if (serverSpawnFailed) {
    // spawn 直接失败（python 不在 PATH 之类）：别等 60 秒，立刻报
    console.warn("[pet] server.py 拉不起来");
    return false;
  }
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await serverAlive()) return true;
    if (!serverProc && i > 4) return false; // 进程已经挂了，别死等
  }
  return false;
}

function stopServer() {
  if (serverProc) {
    try { serverProc.kill(); } catch { /* ignore */ }
    serverProc = null;
  }
}

// ---------------------------------------------------------------- 网关（:8898）
// 对话现在走双模型网关：本地模型说话，云端查事实。它没起来就自己拉一个。
async function gatewayAlive() {
  try {
    const res = await fetch(`${GATEWAY_ORIGIN}/health`, { signal: AbortSignal.timeout(900) });
    return res.ok;
  } catch {
    return false;
  }
}

function spawnGateway() {
  const py = process.env.DM_PYTHON || "python";
  const log = fs.createWriteStream(GATEWAY_LOG, { flags: "a" });
  log.write(`\n===== ${new Date().toISOString()} gateway start =====\n`);
  gatewayProc = spawn(py, ["-B", "gateway.py"], {
    cwd: GATEWAY_DIR,
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  gatewayProc.stdout.pipe(log, { end: false });
  gatewayProc.stderr.pipe(log, { end: false });
  gatewayProc.on("error", (err) => {
    log.write(`[spawn error] ${err && err.message ? err.message : err}\n`);
    // 同 serverProc：spawn 失败时只有 "error" 没有 "exit"，必须自己置空
    gatewayProc = null;
  });
  gatewayProc.on("exit", (code) => {
    log.write(`[gateway exit] code=${code}\n`);
    gatewayProc = null;
    // ⚠ 2026-11-04（T-04）：网关是**唯一**的模型出口（桌宠 8848 → 8898，
    // QQ → 8898/v1）。它死掉时两个入口同时哑，而原来这里只写一行日志。
    // 实测 gateway.log：start 60 次 / exit 31 次 / 零自愈。
    if (quitting) return;
    scheduleGatewayRestart(log);
  });
  log.write(`[spawn] ${py} gateway.py (pid ${gatewayProc.pid})\n`);
}

async function ensureGateway() {
  if (await gatewayAlive()) return true;
  if (!fs.existsSync(path.join(GATEWAY_DIR, "gateway.py"))) {
    console.warn(`[pet] 找不到网关目录 ${GATEWAY_DIR}`);
    return false;
  }
  console.log("[pet] 网关未启动，正在拉起 gateway.py");
  spawnGateway();
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await gatewayAlive()) return true;
    if (!gatewayProc && i > 4) return false;
  }
  return false;
}

function stopGateway() {
  if (gatewayProc) {
    try { gatewayProc.kill(); } catch { /* ignore */ }
    gatewayProc = null;
  }
}

// ---------------------------------------------------------------- 看门狗（T-04）
// 2026-11-04 加。原来 ensureServer / ensureGateway **只在启动流程里各调一次**
// （main.js:1011 / :1015），进程死了以后没有任何东西重拉。
// 实测 gateway.log：gateway start 60 次、[gateway exit] 31 次、[spawn error] 0 次
// —— 不是起不来，是起来之后死，然后一直哑到用户手动重启。
//
// 设计取舍（照抄 server.py 里 _tts_supervisor 那套成熟做法）：
//   * 指数退避，别忙循环把日志刷爆；
//   * 连续失败到上限就**停手并通知用户**，而不是无限重试；
//   * 重拉成功后计数器归零；
//   * quitting 时一律不再重拉（退出/重启流程不能被看门狗抢跑）。

const RESTART_MAX = 5;              // 连续失败几次后放弃
const RESTART_BASE_MS = 3000;       // 首次等 3s，之后 3/6/12/24/48 秒

let serverRetries = 0;
let gatewayRetries = 0;
let serverRestartTimer = null;
let gatewayRestartTimer = null;

function backoffMs(n) {
  return Math.min(RESTART_BASE_MS * Math.pow(2, Math.max(0, n - 1)), 120000);
}

function scheduleGatewayRestart(log) {
  if (quitting || gatewayRestartTimer) return;
  gatewayRetries += 1;
  if (gatewayRetries > RESTART_MAX) {
    log.write(`[gateway] 连续 ${RESTART_MAX} 次启动失败，停止重拉\n`);
    console.warn("[pet] 网关反复启动失败，已停止重试");
    notify("网关起不来了，右下角托盘里可以手动重启。");
    return;
  }
  const wait = backoffMs(gatewayRetries);
  log.write(`[gateway] ${wait}ms 后重拉（第 ${gatewayRetries} 次）\n`);
  gatewayRestartTimer = setTimeout(async () => {
    gatewayRestartTimer = null;
    if (quitting) return;
    const ok = await ensureGateway().catch(() => false);
    if (ok) {
      gatewayRetries = 0;         // 起来了就归零
      log.write("[gateway] 重拉成功\n");
    } else if (!quitting) {
      scheduleGatewayRestart(log);
    }
  }, wait);
}

function scheduleServerRestart(log) {
  if (quitting || serverRestartTimer) return;
  serverRetries += 1;
  if (serverRetries > RESTART_MAX) {
    log.write(`[server] 连续 ${RESTART_MAX} 次启动失败，停止重拉\n`);
    console.warn("[pet] 后端反复启动失败，已停止重试");
    notify("桌宠后端起不来了，右下角托盘里可以手动重启。");
    return;
  }
  const wait = backoffMs(serverRetries);
  log.write(`[server] ${wait}ms 后重拉（第 ${serverRetries} 次）\n`);
  serverRestartTimer = setTimeout(async () => {
    serverRestartTimer = null;
    if (quitting) return;
    const ok = await ensureServer().catch(() => false);
    if (ok) {
      serverRetries = 0;
      log.write("[server] 重拉成功\n");
    } else if (!quitting) {
      scheduleServerRestart(log);
    }
  }, wait);
}

/** 周期性健康检查：进程没死但 HTTP 不通（卡死）时也能救。 */
function startWatchdog() {
  setInterval(async () => {
    if (quitting) return;
    if (!gatewayRestartTimer && !(await gatewayAlive())) {
      const log = fs.createWriteStream(GATEWAY_LOG, { flags: "a" });
      log.write("[watchdog] 网关 /health 不通\n");
      scheduleGatewayRestart(log);
      log.end();
    }
    if (!serverRestartTimer && !(await serverAlive())) {
      const log = fs.createWriteStream(SERVER_LOG, { flags: "a" });
      log.write("[watchdog] 后端 /api/status 不通\n");
      scheduleServerRestart(log);
      log.end();
    }
  }, 30000);
}

// ---------------------------------------------------------------- 调试桥（仅 DM_DEBUG_PORT 时启用）
function startDebugBridge() {
  if (!DEBUG_PORT) return;
  const http = require("http");
  const srv = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const target = url.searchParams.get("target") === "chat" ? chatWin : win;
    try {
      if (url.pathname === "/eval") {
        if (!target || target.isDestroyed()) throw new Error("no target window");
        const code = url.searchParams.get("code") || "";
        const result = await target.webContents.executeJavaScript(code, true);
        res.end(JSON.stringify({ ok: true, result }));
        return;
      }
      if (url.pathname === "/shot") {
        if (!target || target.isDestroyed()) throw new Error("no target window");
        const img = await target.webContents.capturePage();
        const out = url.searchParams.get("path") || path.join(__dirname, "capture.png");
        fs.writeFileSync(out, img.toPNG());
        res.end(JSON.stringify({ ok: true, out }));
        return;
      }
      if (url.pathname === "/blur") {
        // 测试用：让对话窗口失焦，验证「失焦自动收起」
        if (chatWin && !chatWin.isDestroyed()) chatWin.blur();
        res.end(JSON.stringify({ ok: true, has: !!(chatWin && !chatWin.isDestroyed()) }));
        return;
      }
      res.end(JSON.stringify({ ok: false, error: "unknown" }));
    } catch (err) {
      res.end(JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) }));
    }
  });
  srv.listen(DEBUG_PORT, "127.0.0.1", () => logLine(`debug bridge on ${DEBUG_PORT}`));
}

// ---------------------------------------------------------------- 窗口
function createWindow() {
  const scaleName = currentScale();
  const { width, height } = windowSize(scaleName, currentZoom());
  const pos = targetPosition(width, height);

  win = new BrowserWindow({
    width,
    height,
    x: pos.x,
    y: pos.y,
    transparent: true,
    frame: false,
    backgroundColor: "#00000000",
    hasShadow: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    show: false,
    title: "小菲鱼",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      backgroundThrottling: false,
    },
  });

  win.setAlwaysOnTop(true, "floating");
  win.setMenu(null);
  win.setIgnoreMouseEvents(false);

  win.webContents.on("did-start-loading", () => logLine(`load start ${win ? win.webContents.getURL() : ""}`));
  win.webContents.on("did-finish-load", () => {
    logLine("load finish");
    // 页面重载后前端会重新接管穿透状态；这里先复位，避免卡在“穿透”状态点不到人
    win.setIgnoreMouseEvents(false);
    pushScale();
    // 顺手把 QQ 托管状态推一次，轮盘「操作」页的文案不用等下一轮轮询
    refreshQqStatus(true).catch(() => {});
  });
  win.webContents.on("console-message", (_e, level, message, line, source) => {
    logLine(`renderer[${level}] ${message} (${source}:${line})`);
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    logLine(`render-process-gone: ${JSON.stringify(details)}`);
  });
  win.webContents.on("preload-error", (_e, p, err) => {
    logLine(`preload-error ${p}: ${err && err.message}`);
  });

  win.once("ready-to-show", () => {
    logLine("ready-to-show");
    if (!win) return;
    win.showInactive();
    win.webContents.setZoomFactor(1);
  });

  win.on("closed", () => { win = null; });
  // 窗口被隐藏/显示时同步托盘勾选（不管是从托盘、轮盘还是 IPC 触发的）
  win.on("show", refreshTrayMenu);
  win.on("hide", refreshTrayMenu);

  // 后端可能还没起来：失败就重试
  let retries = 0;
  win.webContents.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    logLine(`did-fail-load code=${code} desc=${desc} url=${url} main=${isMainFrame}`);
    if (!isMainFrame) return;
    retries += 1;
    if (retries > 40) return;
    setTimeout(() => {
      if (win && !win.isDestroyed()) win.loadURL(`${ORIGIN}/pet`).catch(() => {});
    }, 1000);
  });

  win.loadURL(`${ORIGIN}/pet`).catch((err) => logLine(`loadURL rejected: ${err && err.message}`));

  if (process.env.DM_CAPTURE) {
    setTimeout(async () => {
      try {
        const img = await win.webContents.capturePage();
        const size = img.getSize();
        fs.writeFileSync(path.join(__dirname, "capture.png"), img.toPNG());
        logLine(`capture ${size.width}x${size.height} scale=${screen.getPrimaryDisplay().scaleFactor}`);
      } catch (err) {
        logLine(`capture failed: ${err && err.message}`);
      }
    }, 6000);
  }
}

function applyScale(scaleName) {
  if (!win) return;
  const { width, height } = windowSize(scaleName, 1);
  const [x, y] = win.getPosition();
  const pos = clampPosition(x, y, width, height);
  win.setBounds({ x: pos.x, y: pos.y, width, height });
  saveState({ scale: scaleName, zoom: 1, x: pos.x, y: pos.y });
  pushScale();
}

// 把当前大小告诉渲染端（轮盘里给「大小」打勾用）
function pushScale() {
  if (!win || win.isDestroyed()) return;
  try {
    win.webContents.send("pet:scale", currentScale());
  } catch { /* ignore */ }
}

function resetZoom() {
  if (!win) return;
  const { width, height } = windowSize(currentScale(), 1);
  const [x, y] = win.getPosition();
  const pos = clampPosition(x, y, width, height);
  win.setBounds({ x: pos.x, y: pos.y, width, height });
  saveState({ zoom: 1, x: pos.x, y: pos.y });
}

function moveToCorner() {
  if (!win) return;
  const { width, height } = windowSize(currentScale(), currentZoom());
  const pos = defaultPosition(width, height);
  win.setBounds({ x: pos.x, y: pos.y, width, height });
  saveState({ x: pos.x, y: pos.y });
}

// 拖动时滚轮缩放：以窗口底边中点为锚点，人不会乱跳
function zoomBy(deltaY) {
  if (!win) return;
  const cur = currentZoom();
  const next = Math.max(0.45, Math.min(2.2, cur * Math.exp(-deltaY * 0.0007)));
  if (Math.abs(next - cur) < 0.002) return;
  const [x, y] = win.getPosition();
  const [w, h] = win.getSize();
  const size = windowSize(currentScale(), next);
  const cx = x + w / 2;
  const bottom = y + h;
  const pos = clampPosition(
    Math.round(cx - size.width / 2),
    Math.round(bottom - size.height),
    size.width,
    size.height
  );
  win.setBounds({ x: pos.x, y: pos.y, width: size.width, height: size.height });
  saveState({ zoom: next, x: pos.x, y: pos.y });
}

// ---------------------------------------------------------------- 全局鼠标（目光跟随）
// 桌宠窗口很小，只在窗口内跟鼠标会断；这里按屏幕坐标轮询，换算成窗口内坐标推给前端。
let cursorTimer = null;

function startCursorTracking() {
  if (cursorTimer) return;
  let lastX = null;
  let lastY = null;
  let sentOnce = false;
  cursorTimer = setInterval(() => {
    try {
      if (!win || win.isDestroyed() || !win.isVisible()) return;
      const p = screen.getCursorScreenPoint();
      const b = win.getContentBounds();
      const x = Math.round(p.x - b.x);
      const y = Math.round(p.y - b.y);
      if (x === lastX && y === lastY) return;
      lastX = x;
      lastY = y;
      win.webContents.send("pet:cursor", x, y);
      if (!sentOnce) {
        sentOnce = true;
        logLine(`cursor tracking ok ${x},${y} bounds=${JSON.stringify(b)}`);
      }
    } catch (err) {
      logLine(`cursor tick error: ${err && err.message ? err.message : err}`);
    }
  }, 60);
}

// ---------------------------------------------------------------- 后台控制台窗口
// 2026-11-08：后台不再用系统浏览器开，改成自己的窗口（见 openAdmin 注释）。
let adminWin = null;
const ADMIN_W = 1180;
const ADMIN_H = 860;

// ---------------------------------------------------------------- 对话输入框
let chatWin = null;
const CHATBOX_W = 430;
const CHATBOX_H = 62;

function chatboxPosition() {
  const pet = win && !win.isDestroyed()
    ? win.getBounds()
    : { x: 0, y: 0, width: 0, height: 0 };
  const display = screen.getDisplayNearestPoint({ x: pet.x + pet.width / 2, y: pet.y + pet.height / 2 });
  const area = display.workArea;

  let x = Math.round(pet.x + pet.width / 2 - CHATBOX_W / 2);
  // 优先放在人物头顶，放不下就放到脚下
  let y = Math.round(pet.y - CHATBOX_H - 10);
  if (y < area.y) y = Math.round(Math.min(pet.y + pet.height + 10, area.y + area.height - CHATBOX_H));

  x = Math.max(area.x, Math.min(x, area.x + area.width - CHATBOX_W));
  y = Math.max(area.y, Math.min(y, area.y + area.height - CHATBOX_H));
  return { x, y };
}

function openChatbox() {
  if (chatWin && !chatWin.isDestroyed()) {
    const p = chatboxPosition();
    chatWin.setPosition(p.x, p.y);
    chatWin.show();
    chatWin.focus();
    chatWin.webContents.send("chatbox:focus");
    return;
  }

  const pos = chatboxPosition();
  chatWin = new BrowserWindow({
    width: CHATBOX_W,
    height: CHATBOX_H,
    x: pos.x,
    y: pos.y,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    show: false,
    title: "和小菲鱼对话",
    webPreferences: {
      preload: path.join(__dirname, "chatbox-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      backgroundThrottling: false,
    },
  });

  // 比桌宠窗口高一级，保证压在人物上面
  chatWin.setAlwaysOnTop(true, "screen-saver");
  chatWin.setMenu(null);

  chatWin.once("ready-to-show", () => {
    if (!chatWin) return;
    chatWin.show();
    chatWin.focus();
    chatWin.webContents.send("chatbox:focus");
  });

  chatWin.on("closed", () => { chatWin = null; });

  chatWin.loadURL(`${ORIGIN}/chatbox`).catch((err) => {
    logLine(`chatbox load failed: ${err && err.message}`);
  });
}

function openAdmin() {
  // 2026-11-08（按主人要求改）：**用自己的窗口打开后台，不再丢给系统浏览器。**
  //
  // 原因有实证：宿主浏览器里出过「HTML 到了、/console.css 和 /console.js 全 403」
  // 的白屏，服务器侧怎么测都正常 —— 实际是那个浏览器给同源子资源发的
  // origin 是 `http://127.0.0.1`（不带端口），跟白名单对不上。
  // 后台本来就是这套应用的一部分，不该运行在别人的浏览器上下文里：
  // 没有第三方扩展、没有企业策略、没有历史缓存，行为是确定的。
  if (adminWin && !adminWin.isDestroyed()) {
    if (adminWin.isMinimized()) adminWin.restore();
    adminWin.show();
    adminWin.focus();
    return;
  }

  adminWin = new BrowserWindow({
    width: ADMIN_W,
    height: ADMIN_H,
    minWidth: 900,
    minHeight: 600,
    title: "小菲鱼 · 后台控制台",
    backgroundColor: "#0d1017",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });

  adminWin.setMenu(null);

  adminWin.once("ready-to-show", () => {
    if (!adminWin) return;
    adminWin.show();
    adminWin.focus();
  });

  adminWin.on("closed", () => { adminWin = null; });

  // 站内跳转留在窗口里，外部链接交给系统浏览器
  adminWin.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(ORIGIN)) shell.openExternal(url);
    return { action: "deny" };
  });

  adminWin.loadURL(`${ORIGIN}/`).catch((err) => {
    logLine(`admin load failed: ${err && err.message}`);
  });
}

// ---------------------------------------------------------------- 托盘菜单
// 「显示桌宠」的勾选状态跟着窗口实际可见性走，轮盘里点「隐藏」之后也能同步
function petVisible() {
  return !!(win && !win.isDestroyed() && win.isVisible());
}

function refreshTrayMenu() {
  if (tray) tray.setContextMenu(buildMenu());
}

function buildMenu() {
  return Menu.buildFromTemplate([
    { label: "对话", click: openChatbox },
    { label: "打开后台控制台", click: openAdmin },
    { type: "separator" },
    {
      label: "大小",
      submenu: [
        { label: "小", type: "radio", checked: currentScale() === "small", click: () => applyScale("small") },
        { label: "中", type: "radio", checked: currentScale() === "normal", click: () => applyScale("normal") },
        { label: "大", type: "radio", checked: currentScale() === "large", click: () => applyScale("large") },
      ],
    },
    { label: "回到右下角", click: moveToCorner },
    { label: "重置缩放", click: resetZoom },
    { type: "separator" },
    {
      label: "显示桌宠",
      type: "checkbox",
      checked: petVisible(),
      click: (item) => {
        if (!win) return;
        if (item.checked) win.showInactive(); else win.hide();
      },
    },
    { label: "重新载入形象", click: () => { if (win) win.loadURL(`${ORIGIN}/pet`).catch(() => {}); } },
    { type: "separator" },
    { label: "退出", click: () => { quitAll().catch(() => {}); } },
  ]);
}

function createTray() {
  const iconPath = path.join(__dirname, "icon.png");
  let icon = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : nativeImage.createEmpty();
  if (!icon.isEmpty()) icon = icon.resize({ width: 20, height: 20 });
  tray = new Tray(icon);
  tray.setToolTip("小菲鱼 · 桌宠（右键菜单）");
  tray.setContextMenu(buildMenu());
  tray.on("double-click", openAdmin);
}

// ---------------------------------------------------------------- IPC
ipcMain.on("pet:ignore-mouse", (_e, flag) => {
  if (!win) return;
  win.setIgnoreMouseEvents(!!flag, { forward: true });
});

ipcMain.on("pet:drag-start", (_e, x, y) => {
  if (!win) return;
  const [wx, wy] = win.getPosition();
  const [width, height] = win.getSize();
  dragState = { startX: x, startY: y, winX: wx, winY: wy, width, height };
});

ipcMain.on("pet:drag-move", (_e, x, y) => {
  if (!win || !dragState) return;
  const nx = Math.round(dragState.winX + (x - dragState.startX));
  const ny = Math.round(dragState.winY + (y - dragState.startY));
  // 必须带尺寸：只调 setPosition 时，Windows 会按 DPI 取整把窗口一点点撑大
  win.setBounds({ x: nx, y: ny, width: dragState.width, height: dragState.height });
});

ipcMain.on("pet:drag-end", (_e, moved) => {
  dragState = null;
  if (!win) return;
  // 单纯点一下（没拖动）就到此为止：不 setBounds。
  // setBounds 会触发 renderer 的 resize，而画布是 resizeTo: window，
  // PIXI 一重建画布就清空一帧 —— 表现就是「做动作时闪一下、中间空一拍」。
  if (!moved) return;
  const [x, y] = win.getPosition();
  // 拖完把尺寸还原成应有的大小，清掉拖动过程中可能产生的误差
  const { width, height } = windowSize(currentScale(), currentZoom());
  const [cw, ch] = win.getSize();
  // 尺寸本来就对就别再设一次，省掉一次无谓的重建
  if (cw !== width || ch !== height) {
    win.setBounds({ x, y, width, height });
  }
  saveState({ x, y });
  // 对话输入框跟着人物走
  if (chatWin && !chatWin.isDestroyed()) {
    const p = chatboxPosition();
    chatWin.setPosition(p.x, p.y);
  }
});

ipcMain.on("pet:zoom", (_e, deltaY) => zoomBy(Number(deltaY) || 0));

// ---------------------------------------------------------------- 操作：重启 / QQ 托管
// 轮盘「操作」那一页的几个按钮。
// 重启走根目录 restart.ps1（按端口 + 进程名双杀，再 start.ps1 拉起全套），
//   但语音引擎和 DSH 两边都故意不杀 —— 留给新实例复用，重启才快。
// QQ 托管只管 SnowLuma(5099/3000/3001) + AstrBot(6185/6199)，不碰桌宠和网关。
// 退出走 quitAll()，见下面的「退出：清理重资源」。
const ROOT_DIR = path.resolve(BASE, "..");                       // <repo>
const QQ_SCRIPT = path.join(ROOT_DIR, "qq", "启动-小鱼.ps1");
// 重启走 restart.bat（它再调 restart.ps1）：和用户手动双击的是同一个入口，
// 行为一致、出问题也好复现。不能直接 spawn powershell，见 spawnOutside 的注释。
const RESTART_BAT = path.join(ROOT_DIR, "restart.bat");
const QQ_PORTS = [6185, 6199, 5099, 3000, 3001];

function probePort(port, timeout = 500) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let settled = false;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(timeout);
    sock.once("connect", () => done(true));
    sock.once("timeout", () => done(false));
    sock.once("error", () => done(false));
    sock.connect(port, "127.0.0.1");
  });
}

function notify(body) {
  try {
    if (Notification.isSupported()) new Notification({ title: "小鱼", body }).show();
  } catch { /* 通知失败不影响动作 */ }
}

// Windows 环境里可能同时存在 HTTPS_PROXY 和 https_proxy（大小写两套）。
// PowerShell 5.1 的 Start-Process 枚举环境变量时会因此抛 ArgumentException，
// 而 restart.ps1 → start.ps1 正是靠 Start-Process 拉服务。这里先收敛成一份。
function dedupeProxyEnv(src) {
  const env = { ...src };
  const seen = new Set();
  for (const key of Object.keys(env)) {
    if (!/^https?_proxy$/i.test(key)) continue;
    const norm = key.toLowerCase();
    if (seen.has(norm)) delete env[key];
    else seen.add(norm);
  }
  return env;
}

// 教训（2026-11-01，用户报「点轮盘重启等了 5 分钟都没成功」）：
// 这个 Electron 进程把子进程放在一个「父进程退出就全杀」的 Job 里，而 Node 的
// detached:true 只设了 DETACHED_PROCESS，**没有** CREATE_BREAKAWAY_FROM_JOB，
// 结果 PowerShell 拿不到控制台、起来就退 —— 脚本一行都没执行。实测矩阵：
//   detached:true  + windowsHide:true  → 脚本不执行（连第一行都没跑）
//   detached:true  + windowsHide:false → 同上
//   detached:false + windowsHide:true  → 能执行，但本进程一退出就被 Job 带走
//   detached:false + windowsHide:false → 同上
// 所以要「活过本进程退出」，只能让 Job 之外的进程去启动它（explorer.exe）。
function spawnQuiet(file, args) {
  /** 本进程还活着时用的普通子进程：能正常执行，随本进程一起结束。 */
  try {
    const child = spawn(file, args, {
      stdio: "ignore",
      windowsHide: true,
      cwd: ROOT_DIR,
      env: dedupeProxyEnv(process.env),
    });
    child.unref();
    return true;
  } catch (exc) {
    logLine(`[ops] 启动失败 ${file}: ${exc}`);
    return false;
  }
}

function spawnOutside(targetPath) {
  /** 必须活过本进程退出时用：交给 explorer 启动，新进程不在我们的 Job 里。

  代价是会带出一个控制台窗口 —— .bat 本来就是要给人看进度的，可接受。
  explorer.exe 正常也会返回 1，别拿退出码判断成败。
  */
  try {
    const child = spawn("explorer.exe", [targetPath], {
      stdio: "ignore",
      windowsHide: true,
      cwd: ROOT_DIR,
      env: dedupeProxyEnv(process.env),
    });
    child.unref();
    return true;
  } catch (exc) {
    logLine(`[ops] 交给 explorer 启动失败 ${targetPath}: ${exc}`);
    return false;
  }
}

// QQ 是否在线，按 6185 判断；状态变化时推给渲染进程，菜单文案跟着变
let qqRunning = null;
async function refreshQqStatus(force = false) {
  const running = await probePort(6185);
  if (force || running !== qqRunning) {
    qqRunning = running;
    if (win && !win.isDestroyed()) win.webContents.send("pet:qq-status", running);
  }
}

setInterval(() => { refreshQqStatus().catch(() => {}); }, 3000).unref();

function restartAll() {
  if (!fs.existsSync(RESTART_BAT)) {
    logLine(`[ops] 找不到 ${RESTART_BAT}`);
    notify("找不到 restart.bat，没法重启");
    return;
  }
  logLine("[ops] 菜单触发：重启全套");
  notify("重启大概需要90秒");
  // 交给 explorer 启动 restart.bat。为什么不自己 spawn powershell：
  //   ① 本进程马上要 app.quit()，普通子进程会被 Job 一起杀掉；
  //   ② detached:true 在这台机器上会让 PowerShell 直接不执行（见 spawnOutside 上方注释）。
  // 走 .bat 还有个好处：和用户手动双击完全同一条路径，能看到的进度也一样。
  if (!spawnOutside(RESTART_BAT)) {
    notify("重启没起来，看桌面日志");
    return;
  }
  // 重启**故意**不调用 stopHeavy() / killDsh()：语音引擎（3.5GB 显存）和 DSH
  // 留着继续跑，restart.ps1 也不杀引擎，新起的 server.py 会 adopted 复用它们。
  // 只有「退出」才走完整清理（见 quitAll）。
  setTimeout(() => { quitting = true; app.quit(); }, 250);
}

// ---------------------------------------------------------------- 退出：清理重资源
// 三个「退出」入口（托盘菜单 / 轮盘 / pet:quit）都走 quitAll()：
//   ① 让 server.py 优雅停掉语音引擎（释放 3.5GB 显存）+ 停掉 QQ 托管
//   ② **DSH 保留不动**（它是主人自己的应用，2026-11-05 按规格改）
//   ③ 再 app.quit()
// will-quit 是同步的、不能 await，所以清理必须全部在 app.quit() 之前做完。
let cleanupStarted = false; // 防重入：连点两下「退出」只清理一次

function opsConfig() {
  // 现读一遍：用户可能改了 config.json 但没重启桌宠
  const cfg = readConfig();
  return (cfg && cfg.ops) || {};
}

function runPowershell(script, timeoutMs = 8000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok, info) => {
      if (settled) return;
      settled = true;
      resolve({ ok, info });
    };
    let child;
    try {
      child = spawn("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], {
        cwd: ROOT_DIR,
        windowsHide: true,
        env: dedupeProxyEnv(process.env),
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (err) {
      finish(false, String(err && err.message ? err.message : err));
      return;
    }
    let out = "";
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* ignore */ }
      finish(false, "timeout");
    }, timeoutMs);
    child.stdout.on("data", (b) => { out += b.toString("utf-8"); });
    child.stderr.on("data", (b) => { out += b.toString("utf-8"); });
    child.on("error", (err) => {
      clearTimeout(timer);
      finish(false, String(err && err.message ? err.message : err));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      finish(code === 0, out.trim() || `exit=${code}`);
    });
  });
}

/** 轮询直到端口不再可连，返回是否已关闭。 */
async function waitPortClosed(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await probePort(port, 400))) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return !(await probePort(port, 400));
}

/**
 * 兜底强杀引擎：按端口反查 PID，只有进程名像引擎才杀。
 *
 * 为什么需要它：stopHeavy 的正路是让 server.py 自己停引擎，但 server.py 可能
 * 已经崩了 / 卡住不应答（serverProc 变 null 之后没人再拉它）。那种情况下
 * 8849 还占着 3.5GB 显存，只打一行日志不满足「退出必须释放显存」。
 */
async function killEngineByPort(port) {
  const script = [
    // ⚠ 不要加 -State Listen（2026-11-05 修，和第二十/二十一轮同一类坑）：
    // 本机有些监听器的 State 字段是**空串**，带这个过滤会漏掉它们。
    // 这里漏判的后果很直接：8849 上的 3.5GB 显存释放不掉。
    `$ownerPid = (Get-NetTCPConnection -LocalPort ${Number(port)}` +
      " -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -ne 0 }" +
      " | Select-Object -First 1 -ExpandProperty OwningProcess)",
    'if (-not $ownerPid) { "no-listener"; return }',
    "$n = (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue).ProcessName",
    'if (-not $n) { "gone pid=" + $ownerPid; return }',
    'if ($n -like "*breeze*") { taskkill /PID $ownerPid /T /F 2>$null | Out-Null; "killed=" + $ownerPid }',
    'else { "skip name=" + $n }',
  ].join("; ");
  try {
    return await runPowershell(script, 8000);
  } catch (err) {
    return { ok: false, info: String(err && err.message ? err.message : err) };
  }
}

/** 优雅停掉语音引擎，等引擎端口真正释放。失败只记日志，不抛。 */
async function stopHeavy() {
  const enginePort = Number((CFG.breeze || {}).port) || 8849;
  // ① 请求 server.py 自己停引擎再退出（内部 tts.stop(force=True)，adopted 也杀）
  //    超时给 8 秒：/api/shutdown 里除了停引擎，还要顺带停掉 QQ 托管
  //    （qq_stop 一次 PowerShell 约 0.5~1s），3 秒会把它拦在半路。
  try {
    const res = await fetch(`${ORIGIN}/api/shutdown`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
    });
    logLine(`[ops] /api/shutdown -> ${res.status}`);
  } catch (err) {
    logLine(`[ops] /api/shutdown 失败（走兜底）: ${err && err.message ? err.message : err}`);
  }
  // ② 等引擎端口真的关掉：卸载 3.5GB 显存要一两秒
  let released = await waitPortClosed(enginePort, 6000);
  // ③ 兜底：server.py 没响应 / 已经挂了，引擎还占着端口 → JS 侧自己强杀
  if (!released) {
    logLine(`[ops] ${enginePort} 还在监听，server.py 可能已失效，尝试直接结束引擎`);
    const res = await killEngineByPort(enginePort);
    logLine(`[ops] 兜底结束引擎 -> ok=${res.ok} ${res.info}`);
    released = await waitPortClosed(enginePort, 3000);
  }
  if (released) {
    logLine(`[ops] 语音引擎已停，${enginePort} 已释放`);
  } else {
    logLine(`[ops] ${enginePort} 仍在监听，显存可能没释放，请手动检查`);
  }
  // ④ 兜底：server.py 还活着就照旧 kill，至少不留 server 进程
  try { stopServer(); } catch { /* ignore */ }
}

/** 结束 DSH（DeepSeek Harness）。配置缺失 / 没装 / 没在跑都静默跳过。 */
async function killDsh() {
  const ops = opsConfig();
  const exe = typeof ops.dsh_exe === "string" ? ops.dsh_exe.trim() : "";
  if (!ops.manage_dsh) { logLine("[ops] manage_dsh=false，不动 DSH"); return; }
  if (!exe) { logLine("[ops] 没配 ops.dsh_exe，不动 DSH"); return; }
  if (!fs.existsSync(exe)) { logLine(`[ops] 找不到 DSH：${exe}`); return; }
  // 必须按 ExecutablePath 精确匹配：按进程名会命中别的安装位置的同名程序。
  // /T 连带子进程；对命中的每个 PID 都杀一遍，重复杀同一个无害。
  const quoted = exe.replace(/'/g, "''");
  const ps = [
    `$exe = '${quoted}'`,
    "$hits = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |" +
      " Where-Object { $_.ExecutablePath -eq $exe })",
    "foreach ($h in $hits) { taskkill /PID $h.ProcessId /T /F 2>$null | Out-Null }",
    '"killed=" + $hits.Count',
  ].join("; ");
  try {
    const res = await runPowershell(ps);
    logLine(`[ops] 结束 DSH -> ok=${res.ok} ${res.info}`);
  } catch (err) {
    logLine(`[ops] 结束 DSH 失败: ${err && err.message ? err.message : err}`);
  }
}

/**
 * 「退出」唯一入口：把小鱼自己的东西全关掉，**保留 DSH**，然后真正退出。
 *
 * 三个按钮的边界（2026-11-05 按主人给的规格固定下来，别再凭感觉改）：
 *
 *   启动  全部拉起来；**DSH 和语音引擎已经在跑就跳过**（不重复起、不冲突）
 *   重启  除**语音引擎**和**DSH**外全关，再重开
 *   退出  除**DSH**外全关（含 QQ 机器人），DSH 留着
 *
 * 为什么退出要留 DSH：DSH（DeepSeek Harness）是**主人自己的应用**，
 * 不是小鱼的组件。小鱼没理由顺手把他正在用的东西关掉。
 * —— 这条以前是反的：quitAll 会主动 killDsh()，把人家的应用一起杀了。
 */
async function quitAll() {
  // cleanupStarted：连点两下只清理一次
  // quitting：已经在退出/重启流程里（比如 app.quit() 触发的 window-all-closed）
  //          就不要再跑一轮清理 —— 否则「重启」会顺手把引擎杀掉
  if (cleanupStarted || quitting) return;
  cleanupStarted = true;
  logLine("[ops] 退出：清理 QQ 托管 + 语音引擎（DSH 保留不动）");
  notify("正在退出…");
  // 先把窗口收起来：清理最长要几秒（等引擎卸载显存），别让人以为点了没反应
  try {
    if (chatWin && !chatWin.isDestroyed()) chatWin.hide();
    if (win && !win.isDestroyed()) win.hide();
  } catch { /* 窗口收不起来不影响清理 */ }
  try {
    await stopHeavy();
  } catch (err) {
    logLine(`[ops] stopHeavy 异常: ${err && err.message ? err.message : err}`);
  }
  // ⚠ 这里**故意不调 killDsh()**：DSH 是主人自己的应用，退出小鱼不该关它。
  //   以前会调，等于"我只是想关桌宠，结果 DeepSeek Harness 也没了"。
  //   要关 DSH 的话请用 DSH 自己的退出，或把 config.json 的 ops.manage_dsh 关掉
  //   —— 但那个开关现在只影响"启动时要不要顺手拉起 DSH"。
  const ops = opsConfig();
  if (ops.manage_dsh) {
    logLine("[ops] 退出：DSH 保留运行（按规格，退出不动 DSH）");
  }
  quitting = true;
  app.quit();
}

async function toggleQq() {
  const running = await probePort(6185);
  if (running) {
    logLine("[ops] 菜单触发：关闭 QQ 托管");
    // 只杀 QQ 那几个端口上的监听进程，别碰 8848/8898
    const ps = [
      `$ports = ${QQ_PORTS.join(",")}`,
      "$pids = @()",
      "foreach ($p in $ports) {",
      // ⚠ 不要加 -State Listen（2026-11-05 修）：某些监听器 State 是空串，
      // 带过滤会漏掉 -> 那些端口上的 QQ 进程杀不到，托管关不干净。
      "  $pids += (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -ne 0 }).OwningProcess",
      "}",
      "$pids = $pids | Where-Object { $_ } | Sort-Object -Unique",
      "foreach ($procId in $pids) { taskkill /PID $procId /T /F 2>$null | Out-Null }",
    ].join("\n");
    // 桌宠还活着，普通子进程就够（detached:true 反而会让 PowerShell 不执行）
    spawnQuiet("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]);
    notify("QQ 托管已关闭");
  } else {
    logLine("[ops] 菜单触发：启用 QQ 托管");
    if (!fs.existsSync(QQ_SCRIPT)) {
      notify("找不到 qq\\启动-小鱼.ps1");
      return;
    }
    spawnQuiet("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", QQ_SCRIPT]);
    notify("正在拉起 QQ 托管…");
  }
  // 起停都要几秒，分两次回读状态，让菜单尽快变成正确的文案
  setTimeout(() => refreshQqStatus(true).catch(() => {}), 1500);
  setTimeout(() => refreshQqStatus(true).catch(() => {}), 6000);
}

ipcMain.on("pet:open-admin", openAdmin);
ipcMain.on("pet:open-chatbox", openChatbox);
ipcMain.on("chatbox:close", () => {
  if (chatWin && !chatWin.isDestroyed()) chatWin.close();
});
ipcMain.on("pet:menu-action", (_e, action, arg) => {
  switch (action) {
    case "chat": openChatbox(); break;
    case "admin": openAdmin(); break;
    case "scale": if (SCALES[arg]) applyScale(arg); break;
    case "reset-zoom": resetZoom(); break;
    case "corner": moveToCorner(); break;
    case "hide": if (win) win.hide(); break;
    case "restart": restartAll(); break;
    case "qq-toggle": toggleQq(); break;
    case "quit": quitAll().catch(() => {}); break;
    default: break;
  }
});
ipcMain.on("pet:quit", () => { quitAll().catch(() => {}); });

// ---------------------------------------------------------------- 生命周期
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) { win.showInactive(); win.setAlwaysOnTop(true, "floating"); }
  });

  app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
  app.commandLine.appendSwitch("disable-features", "CalculateNativeWinOcclusion");

  // ---------------------------------------------------------------- GPU
  //
  // ⚠ 2026-11-09 实测发现的问题：
  // 桌宠窗口的 WebGL renderer 是
  //     ANGLE (Microsoft, Microsoft Basic Render Driver, Direct3D11)
  // 也就是 **Windows 的软件渲染兜底驱动，不是独显**。
  // 后果：名为 `gpu-process` 的那个进程其实在用 CPU 做光栅化，
  // 实测吃掉单核 21%，而同期网关（含向量库）只用了 1 秒。
  //
  // 为什么会这样：这台机器有两个显示适配器
  //     GameViewer Virtual Display Adapter（远程串流用的虚拟显示器）
  //     NVIDIA GeForce RTX 4060 Laptop GPU
  // Electron 默认拿到的是虚拟显示器那条路，它没有真 GPU 能力，
  // 于是 Chromium 回退到 Basic Render Driver。
  //
  // 这三条是标准的「请用高性能 GPU」声明，Chromium 会据此挑适配器。
  // 显式写 d3d11 是因为 ANGLE 在 Windows 上默认就可能选 D3D9/WARP。
  app.commandLine.appendSwitch("force_high_performance_gpu");
  app.commandLine.appendSwitch("use-angle", "d3d11");
  // 让 GPU 进程有独立的共享内存预算，避免小窗口被当成"不值得分配"
  app.commandLine.appendSwitch("force-gpu-mem-available-mb", "2048");

  app.whenReady().then(async () => {
    app.setAppUserModelId("com.xiaohaiyan.pet");
    logLine(`app ready, origin=${ORIGIN}`);
    const ok = await ensureServer();
    logLine(`server ready=${ok}`);
    if (!ok) {
      // T-04：启动失败必须**可见**。原来只有一行 console.warn，
      // 用户看到的是"透明空窗口 + 托盘图标"，零提示。
      console.warn("[pet] 后端没有就绪，仍尝试加载页面");
      notify("桌宠后端没起来，页面可能是空的。托盘里可以重启。");
    }

    const gwOk = await ensureGateway();
    logLine(`gateway ready=${gwOk}`);
    if (!gwOk) {
      console.warn("[pet] 网关没有就绪，对话会退回提示语");
      notify("对话网关没起来，先只能看着，说不了话。");
    }

    createWindow();
    createTray();
    startCursorTracking();
    startDebugBridge();
    // T-04：起来之后开始巡逻。只启动一次，别重复。
    startWatchdog();

    screen.on("display-metrics-changed", () => {
      if (!win) return;
      const [x, y] = win.getPosition();
      const [w, h] = win.getSize();
      const p = clampPosition(x, y, w, h);
      win.setPosition(p.x, p.y);
    });
  });

  app.on("window-all-closed", () => {
    // 窗口被关光（Alt+F4 / 任务栏关闭）也要走和「退出」一样的清理，
    // 否则会漏掉语音引擎的 3.5GB 显存和 DSH。
    // 但如果已经在退出 / 重启流程里（quitting 已置位），就别再触发一轮清理。
    if (quitting) { app.quit(); return; }
    quitAll().catch(() => {});
  });

  app.on("before-quit", () => {
    quitting = true;
  });

  app.on("will-quit", () => {
    stopServer();
    stopGateway();
  });
}
