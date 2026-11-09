/* 小菲鱼 · 桌宠前端
 * - 透明窗口里只画人物 + 气泡，没有输入框（聊天在浏览器后台）
 * - 通过 SSE /api/events 接收后端推送的 {"type":"say"} 事件
 * - 鼠标在人物身上时可交互，移到透明区域自动穿透到桌面
 * - Electron 主进程通过 window.petBridge 提供窗口操作能力；普通浏览器打开会自动降级
 */
(() => {
  "use strict";

  const bridge = window.petBridge || null;
  const canvas = document.getElementById("canvas");
  const bubble = document.getElementById("bubble");
  const bubbleText = document.getElementById("bubble-text");

  // ---------------------------------------------------------------- PIXI
  //
  // ⚠ 帧率（2026-11-09 按主人要求限到 24）
  //
  // 为什么改：实测这个窗口的 `renderer` + `gpu-process` 两个进程
  // 累计吃了 31 + 26 秒 CPU，而同期网关（含向量库检索）只用了 1 秒 ——
  // 桌宠渲染的 CPU 开销是重活儿的大头，主人反馈「电脑卡卡的」。
  //
  // 24 而不是 30：Live2D 是呼吸、眨眼这种慢动作，24fps 已经足够顺；
  // 再高只是在空转。要调回去改这一个数就行。
  const PET_FPS = 24;

  const app = new PIXI.Application({
    view: canvas,
    resizeTo: window,
    backgroundAlpha: 0,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 2),
  });

  // pixi v6 的 Ticker 有 maxFPS：设了就按这个上限走，不是每帧都提交。
  //
  // ⚠ 只设 maxFPS，**不要碰 minFPS** —— 看过 pixi 6.5.10 的实现：
  //     maxFPS 的 setter 里是 `Math.max(this.minFPS, t)`
  //     minFPS 的 setter 里是 `Math.min(this.maxFPS, t)`
  //   两者互相夹。minFPS 在 pixi 里是「低于这个帧率就当卡了、补时长」的
  //   降级阈值，不是「不许掉到这么低」。把它也设成 24 会反过来把
  //   maxFPS 顶住，等于白设。
  try {
    app.ticker.maxFPS = PET_FPS;
  } catch (_) { /* 老版本 pixi 没有这个字段，忽略 */ }

  let model = null;
  let coreModel = null;
  let originalW = 1;
  let originalH = 1;
  let modelBounds = null;   // 模型画布范围（含大头发、袖子）
  let hitRect = null;       // 真正可点的范围（头 + 身子）

  // 命中框由服务端下发（/api/touch-regions 的 hit_box），这里只是拿不到时的兜底。
  // 以前写死在这，后台编辑器拿不到同一份，画出来的形状会整体错位。
  let HIT_BOX = { x0: 0.33, x1: 0.67, y0: 0.10, y1: 1.0 };

  function refreshBounds() {
    if (!model) return;
    try { modelBounds = model.getBounds(); } catch (_) { return; }
    hitRect = {
      x: modelBounds.x + modelBounds.width * HIT_BOX.x0,
      y: modelBounds.y + modelBounds.height * HIT_BOX.y0,
      width: modelBounds.width * (HIT_BOX.x1 - HIT_BOX.x0),
      height: modelBounds.height * (HIT_BOX.y1 - HIT_BOX.y0),
    };
  }

  let refW = 0;
  let refH = 0;
  let resizeTimer = 0;

  function layout() {
    if (!model) return;
    const w = app.screen.width;
    const h = app.screen.height;
    refW = window.innerWidth;
    refH = window.innerHeight;

    // 上方约 18% 留给气泡，人物贴着底部
    const targetH = h * 0.80;
    const scale = Math.min(targetH / originalH, (w * 0.98) / originalW);
    model.scale.set(scale);
    model.position.set(0, 0);

    const b = model.getBounds();
    model.position.set(
      (w - b.width) / 2 - b.x,
      h - b.height - h * 0.01 - b.y
    );
    refreshBounds();
  }

  window.addEventListener("resize", () => {
    // 拖动时 Windows 会按 DPI 取整把窗口尺寸一点点撑大（实测拖 600px 涨 8%），
    // 结果人物“自己变大”。小于 8px 的变化直接忽略，拖完主进程会把尺寸还原。
    if (
      Math.abs(window.innerWidth - refW) < 8 &&
      Math.abs(window.innerHeight - refH) < 8
    ) {
      return;
    }
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => layout(), 60);
  });

  // ---------------------------------------------------------------- 默认隐藏部件
  // 与游戏 item.json 的处理保持一致：某些部件（例如模型的 WM 水印板）默认不显示。
  // 隐藏清单放在 config.json 的 hidden_parts 里，改配置即可，不用改代码。
  let hiddenParts = [];

  async function loadHiddenParts() {
    try {
      const cfg = await (await fetch("/api/pet-config")).json();
      const names = cfg.hidden_parts || [];
      const ids = coreModel._partIds || [];
      hiddenParts = names
        .map((n) => ids.indexOf(n))
        .filter((i) => i >= 0);
      for (const i of hiddenParts) coreModel.setPartOpacityByIndex(i, 0);
    } catch (e) {
      console.warn("hidden parts:", e && e.message ? e.message : e);
    }
  }

  function enforceHiddenParts() {
    for (let k = 0; k < hiddenParts.length; k++) {
      try { coreModel.setPartOpacityByIndex(hiddenParts[k], 0); } catch (_) {}
    }
  }

  // ---------------------------------------------------------------- 目光跟随
  // 桌宠窗口只有几百像素宽，只靠窗口内的 pointermove 会经常“断线”；
  // 所以由 Electron 主进程轮询全局鼠标位置（屏幕坐标 → 窗口坐标）推进来。
  const focus = { x: 0, y: 0, has: false };

  function bindCursor() {
    if (bridge && typeof bridge.onCursor === "function") {
      bridge.onCursor((x, y) => {
        focus.x = x;
        focus.y = y;
        focus.has = true;
      });
    } else {
      // 普通浏览器里退化成窗口内跟随
      window.addEventListener("pointermove", (e) => {
        focus.x = e.clientX;
        focus.y = e.clientY;
        focus.has = true;
      }, { passive: true });
    }
  }

  function applyFocus() {
    if (!model || !focus.has || !modelBounds) return;
    // 视线起点放在头部。模型画布的正中大概在胯部，直接用 model.focus()
    // 会导致鼠标放在肚子上时眼睛还往上看。
    const hx = modelBounds.x + modelBounds.width * 0.5;
    const hy = modelBounds.y + modelBounds.height * 0.21;
    const dx = focus.x - hx;
    const dy = focus.y - hy;
    const len = Math.max(120, Math.hypot(dx, dy));  // 离得近时不要一下甩到极限
    const nx = Math.max(-1, Math.min(1, dx / len));
    const ny = Math.max(-1, Math.min(1, -dy / len));
    try {
      model.internalModel.focusController.focus(nx, ny);
    } catch (_) { /* ignore */ }
  }

  // ---------------------------------------------------------------- 音频 + 口型
  let audioCtx = null;
  let analyser = null;
  let wave = null;
  let sourceNode = null;
  let playResolve = null;   // 当前这句播完要放行的 pump()
  let playing = false;
  let mouthValue = 0;
  let mouthFrames = 0;
  let lastRms = -1;

  function ensureAudio() {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AC();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.2;
      wave = new Uint8Array(analyser.fftSize);
      analyser.connect(audioCtx.destination);
    }
    return audioCtx;
  }

  function applyMouth() {
    if (!coreModel) return;
    mouthFrames++;
    if (hiddenParts.length) enforceHiddenParts();
    let target = 0;
    if (playing && analyser) {
      analyser.getByteTimeDomainData(wave);
      let sum = 0;
      for (let i = 0; i < wave.length; i++) {
        const x = (wave[i] - 128) / 128;
        sum += x * x;
      }
      const rms = Math.sqrt(sum / wave.length);
      lastRms = rms;
      target = Math.min(1, Math.pow(rms * 8.0, 0.7));
    } else {
      lastRms = -1;
    }
    const k = target > mouthValue ? 0.6 : 0.22;
    mouthValue += (target - mouthValue) * k;
    if (mouthValue < 0.002) mouthValue = 0;
    try {
      coreModel.setParameterValueById("ParamMouthOpenY", mouthValue);
    } catch (_) { /* ignore */ }
  }

  async function playArrayBuffer(ab) {
    const ctx = ensureAudio();
    if (ctx.state === "suspended") await ctx.resume();
    const buffer = await new Promise((res, rej) => {
      ctx.decodeAudioData(ab, res, (e) => rej(e || new Error("解码失败")));
    });
    stopAudio();
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(analyser);
    sourceNode = src;
    playing = true;
    // 必须等真正播完再 resolve：否则队列里的下一句会立刻 stopAudio()，
    // 把这一句掐断（await tts() 一秒不到就返回了）。
    return new Promise((resolve) => {
      playResolve = resolve;
      src.onended = () => {
        if (sourceNode === src) { playing = false; sourceNode = null; }
        settlePlay(resolve, buffer.duration);
      };
      src.start();
    });
  }

  // 正常播完 / 被下一句打断，都要把等待中的 pump() 放行，否则队列会卡死
  function settlePlay(resolve, duration) {
    if (playResolve === resolve) playResolve = null;
    resolve(duration);
  }

  function stopAudio() {
    const pending = playResolve;
    if (sourceNode) {
      try { sourceNode.onended = null; sourceNode.stop(); } catch (_) {}
      sourceNode = null;
    }
    playing = false;
    mouthValue = 0;
    if (pending) settlePlay(pending, 0);
  }

  async function tts(text, emotion, tone) {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, emotion: emotion || null, tone: tone || null }),
    });
    if (!res.ok) {
      let msg = "语音合成失败";
      let loading = false;
      try { const j = await res.json(); if (j && j.error) msg = j.error; if (j && j.loading) loading = true; } catch (_) {}
      if (loading) return { loading: true };
      throw new Error(msg);
    }
    await playArrayBuffer(await res.arrayBuffer());
    return { ok: true };
  }

  // ---------------------------------------------------------------- 表情
  const EMOTION_MAP = {
    happy: "Flowers",
    joy: "Stars",
    smile: "Flowers",
    love: "Hearts",
    shy: "Sweat",
    blush: "Sweat",
    sad: "Tears",
    cry: "Tears",
    angry: "Angry",
    mad: "Angry",
    confused: "Question",
    question: "Question",
    surprised: "Points",
    dark: "Dark",
    speechless: "Wuyu",
    wuyu: "Wuyu",
  };

  let exprResetTimer = 0;

  // 25 个表情也是首次使用才去拉 exp3.json。第一次摸某个部位时，
  // 「动作文件」和「表情文件」同时要现下，正好卡在起手那一拍 —— 启动时全预热。
  function preloadExpressions() {
    try {
      const em = model.internalModel.motionManager.expressionManager;
      if (!em || !Array.isArray(em.definitions)) return;
      em.definitions.forEach((_def, index) => {
        try {
          const p = em.loadExpression(index);
          if (p && typeof p.catch === "function") p.catch(() => {});
        } catch (_) { /* 单个坏了不影响别的 */ }
      });
    } catch (e) {
      console.warn("expression preload:", e && e.message ? e.message : e);
    }
  }

  function setExpression(name) {
    if (!model) return;
    try {
      if (!name) {
        const em = model.internalModel.motionManager.expressionManager;
        if (em && typeof em.resetExpression === "function") em.resetExpression();
        return;
      }
      if (typeof model.expression === "function") {
        model.expression(name);
      } else {
        const em = model.internalModel.motionManager.expressionManager;
        if (em && typeof em.setExpression === "function") em.setExpression(name);
      }
    } catch (e) {
      console.warn("expression failed:", e);
    }
  }

  // 情绪 -> 动作。工坊模型只带一个 Idle，这些动作是仓库里自带的
  // （web/motions/*.motion3.json，由 server.py 挂进 model3.json）。
  // 表情管"脸上"，动作管"身上"，两个一起用才像有反应。
  const EMOTION_MOTION = {
    happy: "Happy", joy: "Happy", smile: "Happy",
    love: "Shy",
    shy: "Shy", blush: "Shy",
    sad: "Sad", cry: "Sad",
    angry: "Angry", mad: "Angry",
    confused: "Tilt", question: "Tilt",
    surprised: "Surprise",
    calm: "", dark: "", speechless: "", wuyu: "",
  };

  function playMotion(name) {
    if (!model || !name) return;
    try {
      // motion() 返回 Promise：加载失败是异步拒绝，try/catch 抓不到，
      // 必须自己挂 catch，否则控制台一片未处理拒绝、动作还静默不播。
      const p = model.motion(name);
      if (p && typeof p.catch === "function") {
        p.catch((e) => console.warn("motion failed:", name, e && e.message ? e.message : e));
      }
    } catch (e) {
      console.warn("motion failed:", name, e && e.message ? e.message : e);
    }
  }

  function applyEmotion(tag, holdMs = 4200) {
    if (!tag) return;
    const key = String(tag).toLowerCase();
    const name = EMOTION_MAP[key];
    if (name) {
      setExpression(name);
      clearTimeout(exprResetTimer);
      exprResetTimer = setTimeout(() => setExpression(""), holdMs);
    }
    // 动作和表情分开判：有的情绪只配动作（比如 calm 不换表情）
    playMotion(EMOTION_MOTION[key]);
  }

  // ---------------------------------------------------------------- 触摸反馈
  // 轻点一下模型 = 摸她。拖动不算，所以这里靠 pointerup 时的位移和时间判断。
  // 分区：按命中框内的归一化坐标判定（服务端 /api/touch-regions 给的同一份表），
  // 摸不同部位说不同的话。
  // 节奏：前两次只放动作不出声，第三次才播一句语音，然后重新计数。
  // 语音是服务端**提前合成好**的（server.py 的 prewarm_touch_voices），点的时候
  // 直接拿 wav，不用等引擎现算 —— 现算一次要好几秒，反馈感全没了。
  const TOUCH_VOICE_EVERY = 3;
  const TOUCH_SILENT_EMOTIONS = ["shy", "happy"];
  let touchRegions = [];     // 服务端那份区域表，保证判定和语音说的是同一套
  let touchCount = 0;
  let lastTouchAt = 0;

  async function loadTouchRegions() {
    try {
      const res = await fetch("/api/touch-regions");
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.hit_box) HIT_BOX = data.hit_box;
      if (Array.isArray(data.regions)) touchRegions = data.regions;
      refreshBounds();      // 命中框可能刚变过，重算一次可点范围
    } catch (e) {
      console.warn("touch regions:", e && e.message ? e.message : e);
    }
  }

  // 图形判定：矩形直接比，椭圆用外接矩形套椭圆方程 —— 和编辑器里画的一致
  function shapeHit(shape, nx, ny) {
    if (shape.type === "ellipse") {
      const rx = shape.w / 2;
      const ry = shape.h / 2;
      if (rx <= 0 || ry <= 0) return false;
      const dx = (nx - (shape.x + rx)) / rx;
      const dy = (ny - (shape.y + ry)) / ry;
      return dx * dx + dy * dy <= 1;
    }
    return nx >= shape.x && nx <= shape.x + shape.w
      && ny >= shape.y && ny <= shape.y + shape.h;
  }

  function regionAt(nx, ny) {
    for (const r of touchRegions) {
      for (const shape of r.shapes || []) {
        if (shapeHit(shape, nx, ny)) return r;
      }
    }
    // 一个都没命中：以前退回 touchRegions[0]（左臂），点空白处会被当成摸手臂。
    // 现在返回 null，由调用方决定「只做动作、不出声」。
    return null;
  }

  async function playTouchVoice(region, index) {
    try {
      const res = await fetch(
        `/api/touch-voice?r=${encodeURIComponent(region.id)}&i=${index}`,
      );
      if (!res.ok) return false;   // 还没合成好：静默降级成只做动作
      await playArrayBuffer(await res.arrayBuffer());
      return true;
    } catch (e) {
      console.warn("touch voice:", e && e.message ? e.message : e);
      return false;
    }
  }

  function flashExpression(emotion, holdMs = 1600) {
    setExpression(EMOTION_MAP[emotion] || "");
    clearTimeout(exprResetTimer);
    exprResetTimer = setTimeout(() => setExpression(""), holdMs);
  }

  function reactToTouch(clientX, clientY) {
    const now = Date.now();
    if (now - lastTouchAt < 900) return;   // 连点不叠动作
    lastTouchAt = now;

    // 换算成命中框内的 0~1 坐标；框外（理论上进不来）按中心算
    let nx = 0.5;
    let ny = 0.5;
    if (hitRect && hitRect.width > 0 && hitRect.height > 0) {
      nx = Math.min(1, Math.max(0, (clientX - hitRect.x) / hitRect.width));
      ny = Math.min(1, Math.max(0, (clientY - hitRect.y) / hitRect.height));
    }
    const region = regionAt(nx, ny);
    if (!region) {
      // 空白处：给一个「被碰到」的动作，但不出声，也不占第三次说话的名额
      playMotion("Touch");
      flashExpression("shy", 1200);
      return;
    }
    touchCount += 1;

    if (touchCount < TOUCH_VOICE_EVERY) {
      // 前两次：只动，不出声
      playMotion("Touch");
      flashExpression(TOUCH_SILENT_EMOTIONS[(touchCount - 1) % TOUCH_SILENT_EMOTIONS.length]);
      return;
    }

    // 第三次：动作 + 表情 + 一句预合成的语音，然后重新数
    touchCount = 0;
    if (!region.lines || !region.lines.length) {
      playMotion("Touch");
      flashExpression("shy");
      return;
    }
    const index = Math.floor(Math.random() * region.lines.length);
    const line = region.lines[index];
    playMotion(region.motion || "Touch");
    flashExpression(line.emotion, 2200);
    if (line.text) showBubble(line.text);
    // 调区域用：摸错地方时把这一行发出来就知道该改哪个区间了
    console.log(`[touch] ${region.id} x=${nx.toFixed(2)} y=${ny.toFixed(2)}`);
    playTouchVoice(region, index);
  }

  // ---------------------------------------------------------------- 气泡
  let bubbleTimer = 0;
  function showBubble(text, sticky = false) {
    bubbleText.textContent = text;
    bubble.classList.remove("hidden");
    requestAnimationFrame(() => bubble.classList.add("show"));
    clearTimeout(bubbleTimer);
    if (!sticky) {
      const ms = Math.max(4200, Math.min(15000, text.length * 230));
      bubbleTimer = setTimeout(hideBubble, ms);
    }
  }
  function hideBubble() {
    bubble.classList.remove("show");
    bubbleTimer = setTimeout(() => bubble.classList.add("hidden"), 220);
  }

  // ---------------------------------------------------------------- 说话队列
  const sayQueue = [];
  let sayBusy = false;

  function say(text, emotion, interrupt, tone) {
    const clean = String(text || "").trim();
    if (!clean) return;
    if (interrupt) {
      sayQueue.length = 0;
      stopAudio();
    }
    sayQueue.push({ text: clean, emotion, tone });
    if (!sayBusy) pump();
  }

  async function pump() {
    if (sayBusy) return;
    sayBusy = true;
    while (sayQueue.length) {
      const item = sayQueue.shift();
      showBubble(item.text);
      applyEmotion(item.emotion);
      try {
        const r = await tts(item.text, item.emotion, item.tone);
        if (r && r.loading) {
          // 语音引擎还没就绪：气泡里给一次可见提示，别让主人以为她哑巴了
          showBubble(item.text + "（声音引擎还在加载）");
        }
      } catch (e) {
        console.warn("tts:", e && e.message ? e.message : e);
      }
    }
    sayBusy = false;
  }

  // ---------------------------------------------------------------- SSE
  let es = null;
  function connectEvents() {
    if (es) es.close();
    es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let evt;
      try { evt = JSON.parse(e.data); } catch (_) { return; }
      if (!evt) return;
      // 后台改完触摸区域：桌宠窗口常驻，不主动刷新就会一直是旧判定
      if (evt.type === "touch-regions") { loadTouchRegions(); return; }
      if (evt.type !== "say") return;
      say(evt.text, evt.emotion, true, evt.tone);
    };
    es.onerror = () => {
      // EventSource 自己会重连；这里只在彻底关闭时兜底
    };
  }

  // ---------------------------------------------------------------- 鼠标穿透
  let ignoring = null;   // null = 还没和主进程同步过，第一次必须发出去
  let dragging = false;
  // 本次按下的起点与累计位移，用来区分「摸一下」和「拖走」
  let pressInfo = null;
  let menuOpen = false;
  let hitTimer = 0;
  let moveCount = 0;
  let lastMove = [0, 0];

  function insidePet(x, y) {
    if (!hitRect) return false;
    const pad = 2;
    return (
      x >= hitRect.x - pad &&
      x <= hitRect.x + hitRect.width + pad &&
      y >= hitRect.y - pad &&
      y <= hitRect.y + hitRect.height + pad
    );
  }

  function setIgnore(flag) {
    if (!bridge) return;
    if (ignoring === flag) return;
    ignoring = flag;
    bridge.setIgnoreMouse(flag);
  }

  if (bridge) {
    // 初始先不穿透，避免首帧还没算好包围盒就点不到
    setIgnore(false);

    window.addEventListener("pointermove", (e) => {
      if (dragging) return;
      moveCount++;
      lastMove = [Math.round(e.clientX), Math.round(e.clientY)];
      // 轮盘开着的时候整块窗口都要能点，不能穿透
      if (menuOpen) {
        setIgnore(false);
        return;
      }
      setIgnore(!insidePet(e.clientX, e.clientY));
    }, { passive: true });

    window.addEventListener("pointerdown", (e) => {
      if (menuOpen) {
        // 点在菜单以外（窗口里的空白）就收起，这一下不当成拖动
        const el = e.target instanceof Element ? e.target : null;
        if (!el || (!el.closest(".radial-item") && !el.closest(".radial-hub"))) {
          closeRadial();
        }
        return;
      }
      ensureAudio().resume().catch(() => {});
      if (e.button === 0 && insidePet(e.clientX, e.clientY)) {
        dragging = true;
        setIgnore(false);
        // 记下按下时的屏幕坐标：松开时位移很小才算「摸了一下」
        pressInfo = { x: e.screenX, y: e.screenY, t: Date.now(), moved: 0 };
        try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
        bridge.dragStart(e.screenX, e.screenY);
      }
    });

    window.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      if (pressInfo) {
        pressInfo.moved = Math.max(
          pressInfo.moved,
          Math.hypot(e.screenX - pressInfo.x, e.screenY - pressInfo.y),
        );
      }
      bridge.dragMove(e.screenX, e.screenY);
    }, { passive: true });

    window.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      const tap = pressInfo && pressInfo.moved < 8 && Date.now() - pressInfo.t < 700;
      // 只有真拖动过才让主进程去「还原窗口尺寸」。
      // 点一下也走还原的话会触发 resize → PIXI 的 resizeTo 重建画布 →
      // 画布被清空一帧，看起来就是「闪一下、中间空一拍」。
      bridge.dragEnd(!tap);
      pressInfo = null;
      if (tap) reactToTouch(e.clientX, e.clientY);
    });

    window.addEventListener("pointercancel", () => {
      if (!dragging) return;
      dragging = false;
      // 真的挪过才需要主进程还原窗口尺寸（理由同 pointerup）
      const moved = !!(pressInfo && pressInfo.moved >= 8);
      pressInfo = null;
      bridge.dragEnd(moved);
    });

    // 按住拖动模型时，滚轮缩放（上下滑都可以，方向按系统滚轮走）
    window.addEventListener("wheel", (e) => {
      if (!dragging) return;
      e.preventDefault();
      bridge.zoom(e.deltaY);
    }, { passive: false });

    // 双击不再打开后台：轮盘菜单「操作」里有「后台」入口，双击只用来摸她。
    // 触摸反馈在 pointerup 里判（轻点 = 摸，拖动不算），见 reactToTouch()。

    // 右键：轮盘菜单（再右键一次收起）
    window.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      if (menuOpen) {
        closeRadial();
        return;
      }
      openRadial(e.clientX, e.clientY);
    });
  }

  // ---------------------------------------------------------------- 轮盘菜单
  // 右键在光标处弹出；点了带箭头的项，一级先收起来，再弹二级。
  const RADIAL_ITEM = 56;      // 按钮直径
  let petScale = "normal";     // 主进程推过来的当前大小，用来给「大小」打勾
  let qqRunning = false;       // 主进程推过来的 QQ 托管状态（决定「关托管 / 开托管」）

  //: 渲染是否暂停（2026-11-09）。
  //:
  //: 真源在主进程 —— 因为暂停可以由托盘菜单触发，那时页面里没有任何点击。
  //: 这里只是镜像：主进程推 pet:paused 过来，页面跟着停/起 ticker。
  //:
  //: 暂停的实现就是 `app.ticker.stop()`：pixi 不再回调，Live2D 的
  //: model.update（物理/眨眼/呼吸，实测 3.1% 单核）和 renderer.render
  //: （5.0%）一起停。画布保留最后一帧，所以看起来是「定住了」而不是空白。
  let petPaused = false;

  function applyPaused(paused) {
    petPaused = !!paused;
    try {
      if (petPaused) {
        app.ticker.stop();
      } else {
        // 恢复时先把 ticker 的上一帧时间重置，避免停了几分钟后第一帧
        // dt 巨大、Live2D 物理一步跳很远（表现是「猛地抖一下」）。
        try {
          app.ticker.lastTime = -1;
          app.ticker._lastFrame = -1;
        } catch (_) {}
        app.ticker.start();
      }
    } catch (e) {
      console.warn("applyPaused failed:", e);
    }
  }


  const MENU = {
    root: {
      title: "菜单",
      items: [
        { label: "对话", action: "chat" },
        { label: "大小", sub: "size" },
        { label: "操作", sub: "ops" },
        // 2026-11-09 主人要求：新增一级「模型」，里面是暂停 / 隐藏。
        // 这两个都跟"占不占资源"有关，和其他页（对话/大小/操作）分开更清楚。
        { label: "模型", sub: "model" },
        { label: "后台", action: "admin" },
      ],
    },
    // 模型渲染控制。两项的区别要让人一眼看出：
    //   暂停 —— 窗口在、人还看得见，只是定住不动（渲染 CPU 归零）
    //   隐藏 —— 整个窗口消失（从托盘「显示桌宠」叫回来）
    model: {
      title: "模型",
      items: [
        {
          label: () => (petPaused ? "继续" : "暂停"),
          action: "pause",
          hint: () => (petPaused
            ? "继续渲染，她会重新呼吸、眨眼"
            : "暂停渲染：她定住不动，渲染 CPU 归零；窗口还在"),
          active: () => petPaused,
        },
        {
          label: "隐藏",
          action: "hide",
          hint: "整个窗口消失（不是暂停）。从托盘「显示桌宠」叫回来",
        },
        { label: "返回", sub: "root" },
      ],
    },
    ops: {
      title: "操作",
      items: [
        // 2026-11-05 修文案：原来写「大概 90 秒」，实测是 **~50 秒**（90 秒是
        // status.ps1 的等待上限，不是真实耗时）。差一倍会让人觉得「怎么这么快
        // 就好了，是不是没重启」。同时把「保留什么」说清 —— 那是重启快的唯一原因。
        { label: "重启", action: "restart", hint: "重启桌宠 + 网关 + QQ，约 50 秒；语音引擎（3.5GB 显存）和 DSH 不动，直接复用" },
        {
          label: () => (qqRunning ? "关托管" : "开托管"),
          action: "qq-toggle",
          hint: () => (qqRunning
            ? "关闭 QQ 托管（停掉 SnowLuma + AstrBot）"
            : "启用 QQ 托管（拉起 SnowLuma + AstrBot）"),
          active: () => qqRunning,
        },
        { label: "归位", action: "corner", hint: "把桌宠挪回屏幕右下角" },
        { label: "隐藏", action: "hide", hint: "把桌宠藏起来，从托盘或后台再叫出来" },
        // 2026-11-05 修文案：原来只提「语音引擎 + DSH」，漏了 **QQ 托管**。
        // 实际 quitAll -> stopHeavy -> /api/shutdown -> qq_stop，QQ 那套会被停掉。
        // 对用户的差别很大：他只是想关桌宠，结果 QQ 机器人也下线了 —— 得让他知道。
        { label: "退出", sub: "quit", hint: "退出：停语音引擎释放 3.5GB 显存 + 停 QQ 托管；DSH 保留不动" },
        { label: "返回", sub: "root" },
      ],
    },
    size: {
      title: "大小",
      items: [
        { label: "小", action: "scale", arg: "small", active: () => petScale === "small" },
        { label: "中", action: "scale", arg: "normal", active: () => petScale === "normal" },
        { label: "大", action: "scale", arg: "large", active: () => petScale === "large" },
        { label: "复位", action: "reset-zoom" },
        { label: "返回", sub: "root" },
      ],
    },
    quit: {
      // 二次确认页。hub 只有 30~44px，写不下长句，后果放在按钮的 hint 里
      // （和「重启」「开/关托管」一致，悬停即见）
      title: "退出？",
      items: [
        { label: "确定", action: "quit", danger: true, hint: "会停语音引擎释放 3.5GB 显存、停掉 QQ 托管；DSH 保留" },
        { label: "取消", action: "close" },
      ],
    },
  };

  let radialEl = null;
  let radialLevelEl = null;
  let radialCenter = { x: 0, y: 0 };
  let radialLevelName = "root";
  let radialLeaveTimer = 0;

  if (bridge && typeof bridge.onScale === "function") {
    bridge.onScale((name) => { if (name) petScale = name; });
  }
  if (bridge && typeof bridge.onQqStatus === "function") {
    bridge.onQqStatus((running) => { qqRunning = !!running; });
  }
  // 暂停状态：先主动问一次（页面重载时主进程可能已经处于暂停态），
  // 之后跟着主进程的推送走。
  if (bridge && typeof bridge.getPaused === "function") {
    try { applyPaused(!!bridge.getPaused()); } catch (_) {}
  }
  if (bridge && typeof bridge.onPaused === "function") {
    bridge.onPaused((paused) => applyPaused(paused));
  }

  function radialRadius() {
    // 留出按钮半径和边距；窗口被缩得很小时轮盘跟着收
    const room = Math.min(window.innerWidth, window.innerHeight) / 2 - RADIAL_ITEM / 2 - 8;
    return Math.max(44, Math.min(96, room));
  }

  function radialRoot() {
    if (!radialEl) radialEl = document.getElementById("radial");
    return radialEl;
  }

  function openRadial(cx, cy) {
    const root = radialRoot();
    if (!root) return;

    const R = radialRadius();
    const pad = R + RADIAL_ITEM / 2 + 8;
    cx = Math.max(pad, Math.min(window.innerWidth - pad, cx));
    cy = Math.max(pad, Math.min(window.innerHeight - pad, cy));

    radialCenter = { x: cx, y: cy };
    radialLevelEl = null;
    radialLevelName = "root";
    menuOpen = true;
    setIgnore(false);

    const discR = R + RADIAL_ITEM / 2 + 22;
    const hubR = Math.max(15, Math.min(22, R * 0.28));

    root.innerHTML = "";
    root.classList.remove("hidden", "open");

    const disc = document.createElement("div");
    disc.className = "radial-disc";
    disc.style.cssText = `left:${cx - discR}px;top:${cy - discR}px;width:${discR * 2}px;height:${discR * 2}px`;
    root.appendChild(disc);

    const ring = document.createElement("div");
    ring.className = "radial-ring";
    ring.style.cssText = `left:${cx - R}px;top:${cy - R}px;width:${R * 2}px;height:${R * 2}px`;
    root.appendChild(ring);

    const hub = document.createElement("div");
    hub.className = "radial-hub";
    hub.id = "radial-hub";
    hub.style.cssText = `left:${cx - hubR}px;top:${cy - hubR}px;width:${hubR * 2}px;height:${hubR * 2}px`;
    hub.textContent = MENU.root.title;
    root.appendChild(hub);

    renderLevel("root", 0);
    requestAnimationFrame(() => root.classList.add("open"));
  }

  function renderLevel(name, delay) {
    const level = MENU[name];
    if (!level) return;
    radialLevelName = name;

    const hub = document.getElementById("radial-hub");
    if (hub) hub.textContent = level.title;

    if (radialLevelEl) {
      const old = radialLevelEl;
      old.classList.add("out");
      setTimeout(() => old.remove(), 130);
    }
    radialLevelEl = null;

    const build = () => {
      if (!menuOpen) return;
      const root = radialRoot();
      if (!root) return;

      const R = radialRadius();
      const box = document.createElement("div");
      box.className = "radial-level";

      const n = level.items.length;
      const start = -Math.PI / 2;   // 从正上方开始顺时针排

      level.items.forEach((item, i) => {
        const a = start + (i * 2 * Math.PI) / n;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "radial-item";
        if (item.sub) btn.classList.add("has-sub");
        if (item.danger) btn.classList.add("danger");
        if (item.active && item.active()) btn.classList.add("active");
        btn.style.left = `${radialCenter.x + Math.cos(a) * R}px`;
        btn.style.top = `${radialCenter.y + Math.sin(a) * R}px`;
        btn.style.animationDelay = `${i * 16}ms`;
        // 文案可以是函数（比如「关托管 / 开托管」随状态变）
        const text = typeof item.label === "function" ? item.label() : item.label;
        btn.textContent = text;
        if (text.length >= 3) btn.classList.add("compact");
        const hint = typeof item.hint === "function" ? item.hint() : item.hint;
        if (hint) btn.title = hint;

        if (item.sub) {
          const mark = document.createElement("i");
          mark.className = "sub-mark";
          btn.appendChild(mark);
        }

        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          if (item.sub) {
            // 一级收掉 → 二级弹出
            renderLevel(item.sub, 120);
            return;
          }
          closeRadial();
          if (bridge && item.action !== "close") bridge.menuAction(item.action, item.arg);
        });

        box.appendChild(btn);
      });

      root.appendChild(box);
      radialLevelEl = box;
    };

    if (delay > 0) setTimeout(build, delay);
    else build();
  }

  function closeRadial() {
    if (!menuOpen) return;
    menuOpen = false;
    clearTimeout(radialLeaveTimer);

    const root = radialRoot();
    if (root) {
      root.classList.remove("open");
      setTimeout(() => {
        if (!menuOpen && root) {
          root.classList.add("hidden");
          root.innerHTML = "";
        }
      }, 150);
    }
    radialLevelEl = null;
    // 收起后按鼠标现在的位置决定还穿不穿透
    setIgnore(!insidePet(lastMove[0], lastMove[1]));
  }

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menuOpen) {
      e.preventDefault();
      closeRadial();
    }
  });

  // 鼠标跑出窗口一段时间就自动收起，免得菜单一直挂在那儿
  document.addEventListener("pointerleave", () => {
    if (!menuOpen) return;
    clearTimeout(radialLeaveTimer);
    radialLeaveTimer = setTimeout(() => { if (menuOpen) closeRadial(); }, 700);
  });
  document.addEventListener("pointerenter", () => clearTimeout(radialLeaveTimer));

  // ---------------------------------------------------------------- 初始化
  async function init() {
    try {
      model = await PIXI.live2d.Live2DModel.from("/model/settings.json", {
        autoInteract: false,
        // 默认只预加载 Idle（MotionPreloadStrategy 的 default 分支）——
        // 第一次摸她时才开始下载 Touch 动作，那一拍模型先回到默认姿势，
        // 看起来就是「闪一下、中间空一段」；后面再点已经在缓存里，就不闪了。
        // ALL = 启动时把 9 个动作全下好，首点没有这段空窗。
        motionPreload: "ALL",
      });
      app.stage.addChild(model);

      originalW = model.internalModel.originalWidth || 1;
      originalH = model.internalModel.originalHeight || 1;
      coreModel = model.internalModel.coreModel;

      model.internalModel.on("beforeModelUpdate", () => {
        applyFocus();
        applyMouth();
      });
      await loadHiddenParts();
      preloadExpressions();
      bindCursor();
      // 触摸分区和文案都从服务端拿（和预合成用的是同一份，才不会对不上）
      loadTouchRegions();

      layout();
      setTimeout(layout, 200);

      // 音频上下文在启动时就建好（此时是 suspended，不出声）。
      // 放到第一次点击再建的话，创建设备要几十上百毫秒，那一拍正好卡在
      // 动作起手上，看起来就是闪一下。首点只做 resume()，很便宜。
      try { ensureAudio(); } catch (_) {}

      try { model.motion("Idle"); } catch (_) {}
      setInterval(() => {
        try {
          const mm = model.internalModel.motionManager;
          if (mm && mm.isFinished()) model.motion("Idle");
        } catch (_) {}
      }, 6000);

      // 命中框随动作变化，低频刷新即可
      hitTimer = setInterval(() => {
        if (!model || dragging) return;
        refreshBounds();
      }, 250);

      connectEvents();
      showBubble("我在这里～");
      setTimeout(hideBubble, 4200);

      window.__pet = {
        app, model, say, stopAudio,
        get hitRect() { return hitRect; },
        get modelBounds() { return modelBounds; },
        focusState: () => focus,
        audioState: () => (audioCtx ? audioCtx.state : "none"),
        menu: () => ({ open: menuOpen, level: radialLevelName, center: radialCenter }),
        openMenu: (x, y) => openRadial(x, y),
        closeMenu: () => closeRadial(),
        debug: () => ({ playing, mouthValue, mouthFrames, lastRms, moveCount, lastMove, ignoring, dragging }),
      };
    } catch (e) {
      console.error(e);
      showBubble("模型加载失败：" + (e && e.message ? e.message : e), true);
    }
  }

  init();
})();
