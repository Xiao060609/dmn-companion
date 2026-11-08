/* 小菲鱼 · 浏览器版前端
 * - PIXI 6 + pixi-live2d-display 0.4.0 (cubism4)
 * - 口型同步：Web Audio Analyser → ParamMouthOpenY（模型 LipSync 组原本是空的，自己驱动）
 * - 表情：读取 model settings 里的 Expressions，按需切换
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  // ?edit=1：后台「自定义触摸区域」用 iframe 打开这一页，在真模型上画形状。
  // 复用同一个渲染和布局，画出来的位置就是桌宠判定用的位置（换成静态贴图会错位）。
  const EDIT = new URLSearchParams(location.search).get("edit") === "1";
  const canvas = $("canvas");
  const input = $("input");
  const btnSend = $("btn-send");
  const btnTest = $("btn-test");
  const exprSel = $("expr");
  const bubble = $("bubble");
  const bubbleText = $("bubble-text");
  const statusBox = $("status");
  const statusText = $("status-text");

  // ---------------------------------------------------------------- 提示条
  const toastEl = document.createElement("div");
  toastEl.id = "toast";
  document.body.appendChild(toastEl);
  let toastTimer = 0;
  function toast(msg, ms = 3200) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), ms);
  }

  // ---------------------------------------------------------------- 状态
  function setStatus(kind, text) {
    statusBox.className = kind ? kind : "";
    statusText.textContent = text;
  }

  // ---------------------------------------------------------------- PIXI
  const app = new PIXI.Application({
    view: canvas,
    resizeTo: window,
    backgroundAlpha: 0,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 2),
  });

  let model = null;
  let coreModel = null;
  let originalW = 1;
  let originalH = 1;

  function layout() {
    if (!model) return;
    const w = app.screen.width;
    const h = app.screen.height;
    if (EDIT) {
      // 编辑器里必须看到整个模型：命中框底就是模型底，脚不能被裁掉。
      // 所以不按 1.02 撑满高度，改成等比缩到画布 92% 再居中。
      const scale = Math.min((h * 0.92) / originalH, (w * 0.92) / originalW);
      model.scale.set(scale);
      model.position.set(0, 0);
      const eb = model.getBounds();
      model.position.set((w - eb.width) / 2 - eb.x, (h - eb.height) / 2 - eb.y);
      return;
    }
    // 先按画布高度缩放，再用实际包围盒居中（Live2DModel 的局部原点是左上角，不是中心）
    model.scale.set((h * 1.02) / originalH);
    model.position.set(0, 0);
    const b = model.getBounds();
    model.position.set((w - b.width) / 2 - b.x, (h - b.height) / 2 - b.y + h * 0.02);
  }
  window.addEventListener("resize", () => setTimeout(layout, 60));

  // ---------------------------------------------------------------- 音频 + 口型
  let audioCtx = null;
  let analyser = null;
  let wave = null;
  let sourceNode = null;
  let playing = false;
  let mouthValue = 0;
  let mouthUpdates = 0;

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
    mouthUpdates++;
    let target = 0;
    if (playing && analyser) {
      analyser.getByteTimeDomainData(wave);
      let sum = 0;
      for (let i = 0; i < wave.length; i++) {
        const x = (wave[i] - 128) / 128;
        sum += x * x;
      }
      const rms = Math.sqrt(sum / wave.length);
      target = Math.min(1, Math.pow(rms * 8.0, 0.7));
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
    if (sourceNode) {
      try { sourceNode.onended = null; sourceNode.stop(); } catch (_) {}
    }
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(analyser);
    src.onended = () => { playing = false; sourceNode = null; };
    playing = true;
    src.start();
    sourceNode = src;
    return buffer.duration;
  }

  async function speak(text, emotion, tone) {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, emotion: emotion || null, tone: tone || null }),
    });
    if (!res.ok) {
      let msg = "语音合成失败";
      try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (_) {}
      throw new Error(msg);
    }
    return playArrayBuffer(await res.arrayBuffer());
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

  function applyEmotion(tag, holdMs = 4200) {
    if (!tag) return;
    const name = EMOTION_MAP[String(tag).toLowerCase()];
    if (!name) return;
    setExpression(name);
    clearTimeout(exprResetTimer);
    exprResetTimer = setTimeout(() => setExpression(""), holdMs);
  }

  // ---------------------------------------------------------------- 气泡
  let bubbleTimer = 0;
  function showBubble(text, sticky = false) {
    bubbleText.textContent = text;
    bubble.classList.remove("hidden");
    requestAnimationFrame(() => bubble.classList.add("show"));
    clearTimeout(bubbleTimer);
    if (!sticky) {
      // 话长一点就多留一会儿
      const ms = Math.max(4000, Math.min(14000, text.length * 220));
      bubbleTimer = setTimeout(hideBubble, ms);
    }
  }
  function hideBubble() {
    bubble.classList.remove("show");
    bubbleTimer = setTimeout(() => bubble.classList.add("hidden"), 260);
  }

  // ---------------------------------------------------------------- 服务状态
  async function pollStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      if (s.tts_ready) { setStatus("ok", "语音就绪"); return true; }
      if (s.tts_error) { setStatus("err", "语音不可用"); return true; }
      setStatus("loading", "语音加载中…");
      return false;
    } catch (_) {
      setStatus("err", "服务未连接");
      return true;
    }
  }

  async function waitTtsReady() {
    for (let i = 0; i < 200; i++) {
      if (await pollStatus()) return;
      await new Promise((r) => setTimeout(r, 1500));
    }
  }

  // ---------------------------------------------------------------- 初始化
  async function init() {
    try {
      const settings = await (await fetch("/model/settings.json")).json();
      const exps = (settings.FileReferences.Expressions || []).map((e) => e.Name);
      for (const name of exps) {
        if (name === "WaterMark") continue;
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        exprSel.appendChild(opt);
      }

      model = await PIXI.live2d.Live2DModel.from("/model/settings.json", {
        autoInteract: true,
        // 理由同 pet.js：默认只预加载 Idle，第一次播自定义动作会有空窗
        motionPreload: "ALL",
      });
      app.stage.addChild(model);

      originalW = model.internalModel.originalWidth || 1;
      originalH = model.internalModel.originalHeight || 1;
      coreModel = model.internalModel.coreModel;

      // 在物理演算之后、coreModel.update() 之前注入口型
      model.internalModel.on("beforeModelUpdate", applyMouth);

      layout();
      setTimeout(layout, 200);
      if (EDIT) setupEditor();

      try { model.motion("Idle"); } catch (_) {}
      // 调试出口（排查用，不影响功能）
      window.__dm = {
        app, model,
        audioState: () => (audioCtx ? audioCtx.state : "none"),
        isPlaying: () => playing,
        mouthValue: () => mouthValue,
        mouthUpdates: () => mouthUpdates,
        speak,
      };
      // 待机动作循环
      setInterval(() => {
        try {
          const mm = model.internalModel.motionManager;
          if (mm && !mm.isFinished && mm.isFinished()) model.motion("Idle");
        } catch (_) {}
      }, 6000);

      waitTtsReady();
      showBubble("我回来啦～现在是浏览器版的小菲鱼。", true);
      setTimeout(hideBubble, 5200);
    } catch (e) {
      console.error(e);
      setStatus("err", "模型加载失败");
      toast("模型加载失败：" + (e && e.message ? e.message : e));
    }
  }

  // ---------------------------------------------------------------- 触摸区域编辑器（?edit=1）
  // 父窗口（后台控制台）负责工具栏和保存，这里只负责「在模型上画」。
  // 坐标空间：命中框内归一化 0~1，和桌宠 pet.js 的 regionAt 完全一致。
  function setupEditor() {
    document.body.classList.add("edit-mode");

    const layer = document.createElement("canvas");
    layer.id = "edit-layer";
    document.body.appendChild(layer);
    const ctx = layer.getContext("2d");

    const state = {
      hitBox: { x0: 0.33, x1: 0.67, y0: 0.10, y1: 1.0 },
      shapes: [],
      mode: "rect",
      selected: -1,
    };
    let drag = null;

    function sizeLayer() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      layer.width = Math.round(window.innerWidth * dpr);
      layer.height = Math.round(window.innerHeight * dpr);
      layer.style.width = window.innerWidth + "px";
      layer.style.height = window.innerHeight + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // 命中框在屏幕上的位置：模型包围盒 × 服务端下发的 hit_box
    function hitRectNow() {
      let b = null;
      try { b = model.getBounds(); } catch (_) { b = null; }
      if (!b || !b.width || !b.height) {
        return { x: 0, y: 0, w: window.innerWidth, h: window.innerHeight };
      }
      const hb = state.hitBox;
      return {
        x: b.x + b.width * hb.x0,
        y: b.y + b.height * hb.y0,
        w: b.width * (hb.x1 - hb.x0),
        h: b.height * (hb.y1 - hb.y0),
      };
    }

    function toHit(clientX, clientY) {
      const r = hitRectNow();
      return {
        x: Math.min(1, Math.max(0, (clientX - r.x) / r.w)),
        y: Math.min(1, Math.max(0, (clientY - r.y) / r.h)),
      };
    }

    function hitShape(shape, nx, ny) {
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

    function drawShape(shape, selected, r) {
      const x = r.x + shape.x * r.w;
      const y = r.y + shape.y * r.h;
      const w = shape.w * r.w;
      const h = shape.h * r.h;
      ctx.beginPath();
      if (shape.type === "ellipse") {
        ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, Math.PI * 2);
      } else {
        ctx.rect(x, y, w, h);
      }
      ctx.fillStyle = selected ? "rgba(255,170,80,0.26)" : "rgba(127,179,255,0.16)";
      ctx.fill();
      ctx.lineWidth = selected ? 2 : 1.5;
      ctx.strokeStyle = selected ? "#ffaa50" : "#7fb3ff";
      ctx.stroke();
    }

    function rectFromDrag() {
      if (!drag) return null;
      return {
        x: Math.min(drag.x0, drag.x1),
        y: Math.min(drag.y0, drag.y1),
        w: Math.abs(drag.x1 - drag.x0),
        h: Math.abs(drag.y1 - drag.y0),
      };
    }

    function draw() {
      ctx.clearRect(0, 0, layer.width, layer.height);
      const r = hitRectNow();
      // 命中框：虚线画出来，告诉用户「只有这块里的坐标才算数」
      ctx.save();
      ctx.strokeStyle = "rgba(127,179,255,0.45)";
      ctx.setLineDash([6, 4]);
      ctx.lineWidth = 1;
      ctx.strokeRect(r.x, r.y, r.w, r.h);
      ctx.restore();
      state.shapes.forEach((s, i) => drawShape(s, i === state.selected, r));
      const preview = rectFromDrag();
      if (preview) drawShape({ type: state.mode, ...preview }, true, r);
    }

    function emit() {
      parent.postMessage({
        type: "touch-edit:state",
        shapes: state.shapes.map((s) => ({ ...s })),
        selected: state.selected,
      }, "*");
    }

    layer.addEventListener("pointerdown", (e) => {
      layer.setPointerCapture(e.pointerId);
      const p = toHit(e.clientX, e.clientY);
      const found = state.shapes.findIndex((s) => hitShape(s, p.x, p.y));
      if (found >= 0) {
        // 点到已有形状 = 选中它（不做拖拽移动，第一版只求能改对）
        state.selected = found;
        draw();
        emit();
        return;
      }
      state.selected = -1;
      drag = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      draw();
    });

    layer.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const p = toHit(e.clientX, e.clientY);
      drag.x1 = p.x;
      drag.y1 = p.y;
      draw();
    });

    layer.addEventListener("pointerup", () => {
      if (!drag) return;
      const rect = rectFromDrag();
      drag = null;
      // 太小的当成误触，不生成形状（最小 2% × 2%）
      if (rect && rect.w >= 0.02 && rect.h >= 0.02) {
        state.shapes.push({ type: state.mode, ...rect });
        state.selected = state.shapes.length - 1;
      }
      draw();
      emit();
    });

    window.addEventListener("message", (e) => {
      const d = e.data || {};
      if (d.type === "touch-edit:init") {
        if (d.hit_box) state.hitBox = d.hit_box;
        state.shapes = (d.shapes || []).map((s) => ({ ...s }));
        state.mode = d.mode === "ellipse" ? "ellipse" : "rect";
        state.selected = -1;
        sizeLayer();
        draw();
        emit();
      } else if (d.type === "touch-edit:mode") {
        state.mode = d.mode === "ellipse" ? "ellipse" : "rect";
      } else if (d.type === "touch-edit:undo") {
        state.shapes.pop();
        state.selected = -1;
        draw();
        emit();
      } else if (d.type === "touch-edit:delete") {
        if (state.selected >= 0) {
          state.shapes.splice(state.selected, 1);
          state.selected = -1;
          draw();
          emit();
        }
      } else if (d.type === "touch-edit:clear") {
        state.shapes = [];
        state.selected = -1;
        draw();
        emit();
      }
    });

    window.addEventListener("resize", () => { sizeLayer(); draw(); });
    sizeLayer();
    draw();
    // 告诉父窗口：模型好了，可以发 shapes 了
    parent.postMessage({ type: "touch-edit:ready" }, "*");
  }

  // ---------------------------------------------------------------- 交互
  let busy = false;

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    btnSend.disabled = true;
    input.value = "";
    showBubble("……", true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error("聊天接口返回 " + res.status);
      const data = await res.json();
      const reply = String(data.reply || "").trim() || "…";
      showBubble(reply);
      applyEmotion(data.emotion);
      try {
        await speak(reply, data.emotion, data.tone);
      } catch (e) {
        toast(e.message || "语音失败");
      }
    } catch (e) {
      showBubble("出错了：" + (e.message || e), true);
    } finally {
      busy = false;
      btnSend.disabled = false;
      input.focus();
    }
  }

  btnSend.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) send();
  });

  btnTest.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    btnTest.disabled = true;
    showBubble("主人，我是小菲鱼，现在可以独立说话啦。", true);
    try {
      await speak("主人，我是小菲鱼，现在可以独立说话啦。");
      setTimeout(hideBubble, 3000);
    } catch (e) {
      toast(e.message || "语音失败");
    } finally {
      busy = false;
      btnTest.disabled = false;
    }
  });

  exprSel.addEventListener("change", () => {
    clearTimeout(exprResetTimer);
    setExpression(exprSel.value);
  });

  init();
})();
