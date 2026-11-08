/* 小菲鱼 · 后台控制台
 * 这里不渲染 Live2D（人物在桌面窗口里），只做对话、状态和推送。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const logEl = $("log");
  const input = $("input");
  const btnSend = $("btn-send");
  const pillTts = $("pill-tts");
  const pillAgent = $("pill-agent");
  const pillEvents = $("pill-events");
  const mainEl = $("main");
  const btnLog = $("btn-log");
  const logBadge = $("log-badge");

  const MY_ID = "console-" + Math.random().toString(36).slice(2, 10);

  // ---------------------------------------------------------------- 对话记录（二级标签）
  let logOpen = false;
  let unread = 0;

  function updateBadge() {
    if (unread > 0 && !logOpen) {
      logBadge.textContent = unread > 99 ? "99+" : String(unread);
      logBadge.classList.remove("hidden");
    } else {
      logBadge.classList.add("hidden");
    }
  }

  function setLogOpen(open) {
    logOpen = !!open;
    mainEl.classList.toggle("collapsed", !logOpen);
    btnLog.classList.toggle("on", logOpen);
    btnLog.setAttribute("aria-expanded", String(logOpen));
    btnLog.title = logOpen ? "收起对话记录" : "展开对话记录";
    btnLog.querySelector("span").textContent = logOpen ? "收起记录" : "对话记录";
    if (logOpen) {
      unread = 0;
      logEl.scrollTop = logEl.scrollHeight;
      input.focus();
    }
    updateBadge();
    try { localStorage.setItem("dmconsole.logOpen", logOpen ? "1" : "0"); } catch (_) {}
  }

  // ---------------------------------------------------------------- 消息列表
  function addMsg(kind, text, meta) {
    const row = document.createElement("div");
    row.className = "msg " + kind;

    const who = document.createElement("div");
    who.className = "who";
    who.textContent = kind === "me" ? "我" : kind === "ai" ? "盐" : "·";

    const body = document.createElement("div");
    body.className = "body";
    body.textContent = text;
    if (meta) {
      const m = document.createElement("span");
      m.className = "meta";
      m.textContent = meta;
      body.appendChild(m);
    }

    row.appendChild(who);
    row.appendChild(body);
    logEl.appendChild(row);

    if (logOpen) {
      logEl.scrollTop = logEl.scrollHeight;
    } else if (kind !== "sys") {
      // 收起状态下新消息只在标签上提示，不占版面
      unread++;
      updateBadge();
    }
    return row;
  }

  function pill(el, kind, text) {
    el.className = "pill" + (kind ? " " + kind : "");
    el.querySelector("span").textContent = text;
  }

  // ---------------------------------------------------------------- 状态
  let lastTtsReady = false;

  async function refreshStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      lastTtsReady = !!s.tts_ready;
      if (s.tts_ready) pill(pillTts, "ok", "语音就绪");
      else if (s.tts_error) pill(pillTts, "err", "语音不可用");
      else pill(pillTts, "", "语音加载中…");

      if (s.agent_ready) pill(pillAgent, "ok", "智能体就绪");
      else if (!s.agent_enabled) pill(pillAgent, "", "智能体未启用");
      else if (s.agent_error) pill(pillAgent, "err", "智能体异常");
      else pill(pillAgent, "", "智能体待命");

      pill(pillEvents, s.events > 0 ? "ok" : "", "桌宠 " + (s.events || 0));

      $("st-tts").textContent = s.tts_ready ? "就绪" : (s.tts_error ? "不可用" : "加载中");
      $("st-agent").textContent = s.agent_ready ? "就绪" : (s.agent_enabled ? "待命" : "未启用");
      $("st-events").textContent = (s.events || 0) + " 个订阅";
      $("st-model").textContent = s.model || "—";
      refreshQq();
    } catch (_) {
      pill(pillTts, "err", "服务未连接");
      pill(pillAgent, "err", "服务未连接");
      pill(pillEvents, "err", "服务未连接");
    }
  }

  // ---------------------------------------------------------------- 发送
  let busy = false;

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    btnSend.disabled = true;
    input.value = "";
    addMsg("me", text);

    const t0 = performance.now();
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, client: MY_ID }),
      });
      if (!res.ok) throw new Error("聊天接口返回 " + res.status);
      const data = await res.json();
      const reply = String(data.reply || "").trim() || "…";
      const dt = ((performance.now() - t0) / 1000).toFixed(1);
      const tags = [data.emotion ? "情绪 " + data.emotion : "", data.tone ? "语气 " + data.tone : ""]
        .filter(Boolean).join(" · ");
      addMsg("ai", reply, `用时 ${dt}s${tags ? " · " + tags : ""}`);
    } catch (e) {
      addMsg("sys", "出错了：" + (e && e.message ? e.message : e));
    } finally {
      busy = false;
      btnSend.disabled = false;
      input.focus();
      refreshStatus();
    }
  }

  // ---------------------------------------------------------------- 推送说话
  async function sayToPet(text, emotion, tone) {
    if (!text) return;
    try {
      const res = await fetch("/api/say", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, emotion, tone: tone || null, client: MY_ID }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "推送失败");
      if (!data.subscribers) addMsg("sys", "已推送，但桌宠还没连上（先启动桌面窗口）");
      else addMsg("sys", "已推送到桌宠");
    } catch (e) {
      addMsg("sys", "推送失败：" + (e && e.message ? e.message : e));
    }
  }

  // ---------------------------------------------------------------- SSE
  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let evt;
      try { evt = JSON.parse(e.data); } catch (_) { return; }
      if (!evt || evt.type !== "say") return;
      if (evt.client === MY_ID) return; // 自己发的已经渲染过
      addMsg("ai", String(evt.text || ""), "来自" + (evt.source === "say" ? "推送" : "其它窗口"));
    };
    es.onopen = () => refreshStatus();
  }

  // ---------------------------------------------------------------- 事件绑定
  btnLog.addEventListener("click", () => setLogOpen(!logOpen));

  btnSend.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) send();
  });

  $("btn-say").addEventListener("click", () => {
    sayToPet(
      $("say-text").value.trim(),
      $("say-emotion").value || null,
      $("say-tone").value.trim()
    );
  });

  // 打开语音缓存文件夹：服务端会调资源管理器，所以只允许本机。
  // 状态写在按钮下面：这个动作的成败必须看得见 —— 以前只往「对话记录」里丢一行，
  // 而那个面板默认是收起的，失败了看起来就是「点了没反应」。
  $("btn-tts-cache").addEventListener("click", async () => {
    const btn = $("btn-tts-cache");
    const state = $("tts-cache-state");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "打开中…";
    state.textContent = "";
    try {
      const res = await fetch("/api/open-tts-cache", { method: "POST", headers: { "Content-Type": "application/json" } });
      const d = await res.json();
      if (!res.ok || !d.ok) {
        // 404 基本只有一个原因：桌宠还没重启，新接口没挂上
        const hint = res.status === 404 ? "（服务还是旧版本，重启桌宠后才有这个接口）" : "";
        throw new Error((d.error || "打不开") + hint);
      }
      state.textContent = d.count
        ? `已打开，里面 ${d.count} 个语音文件`
        : "已打开，但文件夹是空的 —— 先说一句让她出声，再来点。";
      addMsg("sys", `已打开语音缓存文件夹（里面 ${d.count} 个文件）`);
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      state.textContent = "打不开：" + msg;
      setLogOpen(true);  // 错误不能只往折叠着的面板里丢
      addMsg("sys", "打不开：" + msg);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });

  $("btn-reset").addEventListener("click", async () => {
    try {
      await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" } });
      addMsg("sys", "对话记忆已清空");
    } catch (e) {
      addMsg("sys", "清空失败");
    }
  });

  $("btn-reload").addEventListener("click", refreshStatus);

  // ---------------------------------------------------------------- QQ 托管
  // 原来在右键轮盘的「操作」里。后台是系统浏览器打开的，拿不到 Electron 的 IPC，
  // 所以走 8848 的 /api/qq-hosting：停 = 杀 QQ 那几个端口，起 = 跑 qq\启动-小鱼.ps1。
  let qqOnline = null;
  let qqBusy = false;

  async function refreshQq() {
    try {
      const d = await (await fetch("/api/qq-hosting")).json();
      qqOnline = !!d.online;
      const btn = $("btn-qq-toggle");
      $("st-qq").textContent = d.script_exists === false
        ? "启动脚本缺失"
        : (qqOnline ? "运行中" : "未启动");
      if (!qqBusy) {
        btn.textContent = qqOnline ? "关闭托管" : "开启托管";
        btn.disabled = d.script_exists === false;
      }
    } catch (_) {
      $("st-qq").textContent = "读取失败";
    }
  }

  async function toggleQq() {
    if (qqBusy) return;
    qqBusy = true;
    const btn = $("btn-qq-toggle");
    btn.disabled = true;
    btn.textContent = qqOnline ? "正在关闭…" : "正在启动…";
    try {
      const res = await fetch("/api/qq-hosting", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "toggle" }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "操作失败");
      addMsg("sys", d.message || "已发出指令");
      setTimeout(refreshQq, 2000);
      setTimeout(refreshQq, 8000);
    } catch (e) {
      addMsg("sys", "QQ 托管操作失败：" + (e && e.message ? e.message : e));
    } finally {
      // 起停要十几秒，这段时间别让人连点
      setTimeout(() => { qqBusy = false; refreshQq(); }, 10000);
    }
  }

  $("btn-qq-toggle").addEventListener("click", toggleQq);

  // ---------------------------------------------------------------- 对话模型
  // 网关在 8898，页面在 8848，跨端口但同机
  const GATEWAY = location.protocol + "//" + location.hostname + ":8898";
  const lmMode = $("lm-mode");
  const lmProvider = $("lm-provider");
  const lmBase = $("lm-base");
  const lmModel = $("lm-model");
  let lmPresets = {};

  function applyModeState() {
    const cloud = lmMode.value === "cloud";
    lmProvider.disabled = !cloud;
    lmBase.disabled = !cloud;
    lmModel.disabled = !cloud;
  }

  function fillProviders(providers) {
    lmProvider.innerHTML = "";
    Object.entries(providers || {}).forEach(([value, label]) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      lmProvider.appendChild(opt);
    });
  }

  async function loadModelConfig() {
    try {
      const res = await fetch(GATEWAY + "/admin/model");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      lmPresets = d.presets || {};
      fillProviders(d.providers);
      lmMode.value = d.mode === "cloud" ? "cloud" : "local";
      lmProvider.value = d.provider || "local";
      lmBase.value = d.base || "";
      lmModel.value = d.model || "";
      $("lm-current").textContent =
        (d.mode === "cloud" ? "云端 · " : "本地 · ") + (d.model || "—");
      $("lm-key-state").textContent = d.key_set ? "已配置" : "未配置";
      applyModeState();
    } catch (e) {
      $("lm-current").textContent = "网关未连接";
      $("lm-key-state").textContent = "—";
    }
  }

  async function saveModelConfig() {
    const btn = $("btn-lm-save");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "试连中…";
    const body = {
      mode: lmMode.value,
      provider: lmProvider.value,
      base: lmBase.value.trim(),
      model: lmModel.value.trim(),
      // key 由下面的「API 密钥」卡片管，这里不动它
      key: "",
    };
    try {
      const res = await fetch(GATEWAY + "/admin/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.message || d.error || "切换失败");
      addMsg("sys", d.message);
      await loadModelConfig();
      await loadKeys();
      refreshStatus();
    } catch (e) {
      addMsg("sys", "切换失败：" + (e && e.message ? e.message : e));
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  lmMode.addEventListener("change", applyModeState);
  lmProvider.addEventListener("change", () => {
    if (lmMode.value !== "cloud") return;
    const preset = lmPresets[lmProvider.value];
    if (preset) lmBase.value = preset;
  });
  $("btn-lm-save").addEventListener("click", saveModelConfig);

  // ---------------------------------------------------------------- 系统模型
  // 系统层（心跳/巩固/复盘/体检）用哪个模型。和上面「对话模型」是两张独立的卡：
  // 两层可以各用各的服务商和 key，401 时才能定位是哪层死。
  //
  // ⚠ 走**桌宠服务器代理**（8848 `/api/system-model`），不是直接打网关：
  // 网关那个接口要 `X-Gateway-Token`，而 token 只在服务端读得到
  // （`gateway/data/gateway.token`），浏览器里拿不到也不该拿到。
  // 和「她现在的状态」那张卡走 `/api/system` 是同一个路子。
  const smProvider = $("sm-provider");
  const smBase = $("sm-base");
  const smModel = $("sm-model");
  let smPresets = {};

  function fillSystemProviders(providers) {
    smProvider.innerHTML = "";
    Object.entries(providers || {}).forEach(([value, label]) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      smProvider.appendChild(opt);
    });
  }

  async function loadSystemModel() {
    try {
      const res = await fetch("/api/system-model");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      smPresets = d.presets || {};
      fillSystemProviders(d.providers);
      smProvider.value = d.provider || "custom";
      smBase.value = d.base || "";
      smModel.value = d.model || "";
      $("sm-current").textContent = d.model || "—";
      $("sm-key-state").textContent = d.key_set
        ? (d.key_masked ? "已配置 " + d.key_masked : "已配置")
        : "未配置";
    } catch (e) {
      $("sm-current").textContent = "网关未连接";
      $("sm-key-state").textContent = "—";
    }
  }

  async function saveSystemModel() {
    const btn = $("btn-sm-save");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "试连中…";
    const body = {
      provider: smProvider.value,
      base: smBase.value.trim(),
      model: smModel.value.trim(),
      // 留空 = 不改现有 key（前端不重复传明文）
      key: $("sm-key").value.trim(),
    };
    try {
      const res = await fetch("/api/system-model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.message || d.error || "切换失败");
      addMsg("sys", d.message);
      $("sm-key").value = "";   // 成功了就把明文清掉，别留在 DOM 里
      await loadSystemModel();
      refreshStatus();
    } catch (e) {
      addMsg("sys", "系统模型切换失败：" + (e && e.message ? e.message : e));
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  smProvider.addEventListener("change", () => {
    const preset = smPresets[smProvider.value];
    if (preset) smBase.value = preset;
  });
  $("btn-sm-save").addEventListener("click", saveSystemModel);
  loadSystemModel();

  // ---------------------------------------------------------------- 每日梳理期
  // 系统层的**第二层节律**（P0-4）。心跳那层完全不动，这一层每天一次。
  //
  // ⚠ 为什么卡片要显示「现在会跑吗 / blocked_by」：
  // 梳理期不跑的表现是「什么都没发生」，和「主人在忙，本来就不该跑」
  // 长得一模一样。不把闸门状态亮出来，线上永远查不出卡在哪一条。
  const DR_GATE_TEXT = {
    disabled: "未启用（配置里 enabled=false）",
    too_early: "还没到点（21:00 之后才回头看）",
    not_idle: "他还在说话（要安静满 2 小时）",
    already_ran_today: "今天已经梳过一次",
    no_material: "今天没有素材",
    ok: "条件齐了，下一轮会跑",
  };
  const DR_ACT_TEXT = {
    ruminate: "反刍",
    prospect: "预演",
    theory_of_mind: "心智理论",
    self_narrative: "自我叙事",
  };

  function fmtAgo(hours) {
    if (hours == null) return "—";
    if (hours < 1) return "刚刚";
    if (hours < 24) return Math.round(hours) + " 小时前";
    return Math.round(hours / 24) + " 天前";
  }

  function renderDailyReview(d) {
    $("dr-enabled").textContent = d.enabled ? "已启用" : "未启用";
    const gate = d.should_run_now
      ? "会跑"
      : (DR_GATE_TEXT[d.blocked_by] || d.blocked_by || "—");
    $("dr-gate").textContent = gate;
    $("dr-last").textContent = d.last_ts
      ? fmtAgo(d.last_ago_hours) + "（" + (d.last_day || "") + "）"
      : "还没跑过";
    $("dr-today").textContent = (d.today_turns || 0) + " 条";
    $("dr-absence").textContent = d.absence_text || "—";
    const el = (d.eligible_activities || []).map((a) => DR_ACT_TEXT[a] || a);
    $("dr-eligible").textContent = el.length ? el.join(" / ") : "无（没素材）";

    const box = $("dr-recent");
    box.innerHTML = "";
    const rows = d.recent || [];
    if (!rows.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      p.textContent = "还没有梳理记录。";
      box.appendChild(p);
      return;
    }
    for (const r of rows) {
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      const when = r.ts ? new Date(r.ts * 1000).toLocaleString() : "—";
      const acts = (r.activities || []).map((a) => DR_ACT_TEXT[a] || a).join("/");
      const bits = [];
      if (r.thoughts) bits.push(r.thoughts + " 条念头");
      if (r.profile_hints) bits.push(r.profile_hints + " 处画像更新");
      if (r.memory_written) bits.push(r.memory_written + " 条进长期记忆");
      p.textContent = when + " · " + (acts || "—") +
        (bits.length ? " · " + bits.join("，") : " · 没产出");
      box.appendChild(p);
    }
  }

  async function loadDailyReview() {
    try {
      const res = await fetch("/api/daily-review");
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderDailyReview(await res.json());
    } catch (e) {
      $("dr-enabled").textContent = "网关未连接";
      $("dr-gate").textContent = "—";
      $("dr-last").textContent = "—";
    }
  }

  async function runDailyReview() {
    const btn = $("btn-dr-run");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "梳理中…";
    try {
      const res = await fetch("/api/daily-review/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.message || d.error || "触发失败");
      addMsg("sys", d.message);
      // 它是后台线程，给它点时间再刷新
      setTimeout(loadDailyReview, 8000);
    } catch (e) {
      addMsg("sys", "梳理期触发失败：" + (e && e.message ? e.message : e));
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  $("btn-dr-run").addEventListener("click", runDailyReview);
  loadDailyReview();

  // ---------------------------------------------------------------- 主动说话
  //
  // 总开关默认关（网关 config.json → system.proactive_speak.enabled）。
  //
  // ⚠ 为什么卡片要显示「现在能说吗 / 卡在哪」：
  // 主人打开开关后发现她还是不说话，第一反应会以为坏了。
  // 实际上有三道闸：总开关 / 每日额度 / 冷却。直接把卡在哪说出来，
  // 比让主人猜"是不是没生效"好得多。
  const PS_BLOCK_TEXT = {
    disabled: "总开关关着",
    quota: "今天的额度用完了",
    cooldown: "冷却中",
  };

  function renderProactiveSpeak(d) {
    $("ps-enabled").textContent = d.enabled ? "已打开" : "已关闭";
    $("ps-gate").textContent = d.can_speak_now
      ? "可以"
      : (PS_BLOCK_TEXT[d.blocked_by] || d.blocked_by || "—");
    $("ps-quota").textContent =
      (d.speak_count_today || 0) + " / " + (d.max_per_day || 6) + " 句";
    $("ps-last").textContent = d.last_ago_hours != null
      ? fmtAgo(d.last_ago_hours)
      : "还没开过口";
    $("ps-desktop").textContent = d.desktop_reachable
      ? "在线"
      : "连不上（说了也没人听见）";
    $("ps-cooldown").textContent = Math.round((d.cooldown_s || 1800) / 60);
    $("ps-max").textContent = d.max_per_day || 6;

    const btn = $("btn-ps-toggle");
    btn.textContent = d.enabled ? "关掉" : "打开";
    // 记下当前状态，`toggleProactiveSpeak` 靠它取反。
    // 不能靠按钮文字判断 —— 文字是给人看的，中途会被改成 "…"。
    btn.dataset.enabled = d.enabled ? "1" : "0";
    btn.classList.toggle("primary", !d.enabled);
  }

  async function loadProactiveSpeak() {
    try {
      const res = await fetch("/api/proactive-speak");
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderProactiveSpeak(await res.json());
    } catch (e) {
      $("ps-enabled").textContent = "网关未连接";
      $("ps-gate").textContent = "—";
      $("ps-desktop").textContent = "—";
    }
  }

  async function toggleProactiveSpeak() {
    const btn = $("btn-ps-toggle");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      // 当前状态取反：按钮上写的是"打开"就说明现在是关的
      const wantOn = btn.dataset.enabled !== "1";
      const res = await fetch("/api/proactive-speak/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: wantOn }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.message || d.error || "切换失败");
      addMsg("sys", d.message);
      renderProactiveSpeak(d);
    } catch (e) {
      addMsg("sys", "主动说话开关失败：" + (e && e.message ? e.message : e));
      btn.textContent = label;
    } finally {
      btn.disabled = false;
    }
  }

  $("btn-ps-toggle").addEventListener("click", toggleProactiveSpeak);
  loadProactiveSpeak();

  // ---------------------------------------------------------------- API 密钥（卡片式）
  // 每把 key 一张卡片：点卡片切过去，右下角删除。明文只在网关本机，
  // 这里显示的一律是打码后的样子。
  const keysGrid = $("keys-grid");
  let keyBusy = false;
  // 测试结果按 key id 存，重画卡片时带出来；不落盘，刷新页面就没了
  const keyTestResult = {};

  function renderKeys(d) {
    const keys = (d && d.keys) || [];
    keysGrid.innerHTML = "";
    if (!keys.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.style.margin = "0";
      empty.textContent = "还没有保存任何 key，在下面粘一把。";
      keysGrid.appendChild(empty);
      return;
    }
    for (const k of keys) {
      const card = document.createElement("div");
      card.className = "key-card" + (k.active ? " active" : "");
      if (!k.active) {
        card.title = "点一下切到这把";
        card.addEventListener("click", () => keyAction("activate", k.id));
      }

      const label = document.createElement("div");
      label.className = "key-label";
      label.textContent = k.label || "未命名";
      card.appendChild(label);

      const mask = document.createElement("div");
      mask.className = "key-mask";
      mask.textContent = k.masked || "";
      card.appendChild(mask);

      const tested = keyTestResult[k.id];
      if (tested) {
        const line = document.createElement("div");
        line.className = "key-mask";
        line.style.color = tested.ok ? "#7bd88f" : "#ff8080";
        line.textContent = tested.text;
        card.appendChild(line);
      }

      const foot = document.createElement("div");
      foot.className = "key-foot";
      if (k.active) {
        const badge = document.createElement("span");
        badge.className = "key-badge";
        badge.textContent = "使用中";
        foot.appendChild(badge);
      }
      const test = document.createElement("button");
      test.className = "key-del";
      test.style.color = "#7fb3ff";
      test.textContent = "测试";
      test.title = "用当前的模型和服务商试连一次，不会切换";
      test.addEventListener("click", (ev) => {
        ev.stopPropagation();  // 别顺带触发「切过去」
        testKey(k);
      });
      foot.appendChild(test);
      const del = document.createElement("button");
      del.className = "key-del";
      del.textContent = "删除";
      del.addEventListener("click", (ev) => {
        ev.stopPropagation();  // 别顺带触发「切过去」
        keyAction("delete", k.id);
      });
      foot.appendChild(del);
      card.appendChild(foot);

      keysGrid.appendChild(card);
    }
  }

  async function loadKeys() {
    try {
      const res = await fetch(GATEWAY + "/admin/keys");
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderKeys(await res.json());
    } catch (e) {
      keysGrid.innerHTML = "";
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      p.textContent = "网关没连上，读不到 key 列表。";
      keysGrid.appendChild(p);
    }
  }

  async function keyAction(action, id, extra) {
    if (keyBusy) return;
    if (action === "delete" && !window.confirm("删掉这把 key？删了就没了。")) return;
    keyBusy = true;
    const btn = $("btn-key-add");
    const btnLabel = btn.textContent;
    if (action === "add") {
      btn.disabled = true;
      btn.textContent = "添加中…";
    }
    try {
      const res = await fetch(GATEWAY + "/admin/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ action, id }, extra || {})),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.message || d.error || "操作失败");
      addMsg("sys", d.message || "好了");
      renderKeys(d);
      await loadModelConfig();
      if (action === "add") {
        $("key-new-label").value = "";
        $("key-new-value").value = "";
      }
    } catch (e) {
      addMsg("sys", "操作失败：" + (e && e.message ? e.message : e));
    } finally {
      keyBusy = false;
      btn.disabled = false;
      btn.textContent = btnLabel;
    }
  }

  $("btn-key-add").addEventListener("click", () => {
    const key = $("key-new-value").value.trim();
    if (!key) {
      addMsg("sys", "先粘一把 key 再点添加。");
      $("key-new-value").focus();
      return;
    }
    keyAction("add", "", { key, label: $("key-new-label").value.trim() });
  });

  // 只测不改：拿当前服务商和模型试连一次，不切换、不动 active
  async function testKey(k) {
    if (keyBusy) return;
    keyBusy = true;
    const label = k.label || "未命名";
    addMsg("sys", `正在测试「${label}」…（用的是当前模型和服务商）`);
    try {
      const res = await fetch(GATEWAY + "/admin/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "test", id: k.id }),
      });
      const d = await res.json();
      const ok = !!(d && d.ok);
      keyTestResult[k.id] = { ok, text: (d && d.message) || (ok ? "可用" : "不可用") };
      addMsg("sys", keyTestResult[k.id].text);
    } catch (e) {
      keyTestResult[k.id] = { ok: false, text: "测试失败：" + (e && e.message ? e.message : e) };
      addMsg("sys", keyTestResult[k.id].text);
    } finally {
      keyBusy = false;
      await loadKeys();
    }
  }

  // 单独试一把还没存的 key：只连不存，测通了再决定加不加
  let probedKey = "";

  async function probeKey() {
    const input = $("key-probe-value");
    const key = input.value.trim();
    const result = $("key-probe-result");
    if (!key) {
      input.focus();
      return;
    }
    const btn = $("btn-key-probe");
    btn.disabled = true;
    btn.textContent = "测试中…";
    result.style.color = "";
    result.textContent = "正在试连…（用的是当前模型和服务商）";
    $("key-probe-actions").classList.add("hidden");
    try {
      const res = await fetch(GATEWAY + "/admin/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "probe", key }),
      });
      const d = await res.json();
      const ok = !!(d && d.ok);
      result.textContent = (d && d.message) || (ok ? "可用" : "不可用");
      result.style.color = ok ? "#7bd88f" : "#ff8080";
      if (ok) {
        probedKey = key;
        $("key-probe-actions").classList.remove("hidden");
      }
    } catch (e) {
      result.style.color = "#ff8080";
      result.textContent = "测试失败：" + (e && e.message ? e.message : e);
    } finally {
      btn.disabled = false;
      btn.textContent = "测试这把";
    }
  }

  $("btn-key-probe").addEventListener("click", probeKey);
  $("key-probe-value").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      probeKey();
    }
  });
  $("btn-key-probe-add").addEventListener("click", () => {
    if (!probedKey) return;
    keyAction("add", "", { key: probedKey, label: $("key-new-label").value.trim() });
    probedKey = "";
    $("key-probe-value").value = "";
    $("key-probe-actions").classList.add("hidden");
    $("key-probe-result").style.color = "";
    $("key-probe-result").textContent = "已提交添加；卡片上出现就说明进钥匙串了。";
  });

  // ---------------------------------------------------------------- 群聊唤醒关键词
  // 和「对话模型」不一样：这个走同源（8848），不用跨到网关。
  // 服务端把词写进 AstrBot 那边的 plugin_data，插件按 mtime 现读，所以改完即时生效。
  const wkGrid = $("wk-grid");
  const wkCurrent = $("wk-current");
  let wkWords = [];       // 本地一份，增删之后整体提交（后端就是收整个列表）
  let wkBusy = false;

  function renderKeywords() {
    wkGrid.innerHTML = "";
    if (!wkWords.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.style.margin = "0";
      empty.textContent = "还没有关键词，现在只有 @ 她才会醒。";
      wkGrid.appendChild(empty);
    }
    for (const word of wkWords) {
      const card = document.createElement("div");
      card.className = "key-card kw-card";

      const label = document.createElement("div");
      label.className = "key-label";
      label.textContent = word;
      card.appendChild(label);

      const foot = document.createElement("div");
      foot.className = "key-foot";
      const del = document.createElement("button");
      del.className = "key-del";
      del.textContent = "删除";
      del.addEventListener("click", () => removeKeyword(word));
      foot.appendChild(del);
      card.appendChild(foot);

      wkGrid.appendChild(card);
    }
    wkCurrent.textContent = wkWords.length ? wkWords.join(" / ") : "（空，只靠 @ 唤醒）";
  }

  async function loadWakeKeywords() {
    try {
      const res = await fetch("/api/wake-keywords");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      wkWords = d.keywords || [];
      renderKeywords();
    } catch (e) {
      wkCurrent.textContent = "读取失败";
    }
  }

  // 增删都走这里：提交整个列表，成功就用服务端回的值重画（它会把超长/重复的清掉）
  async function saveKeywords(next, okMsg) {
    if (wkBusy) return;
    wkBusy = true;
    try {
      const res = await fetch("/api/wake-keywords", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keywords: next }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "保存失败");
      wkWords = d.keywords || [];
      renderKeywords();
      addMsg("sys", okMsg || d.message || "已保存");
    } catch (e) {
      addMsg("sys", "保存失败：" + (e && e.message ? e.message : e));
      await loadWakeKeywords();   // 失败就回到服务端的真实状态，别让界面骗人
    } finally {
      wkBusy = false;
    }
  }

  function removeKeyword(word) {
    saveKeywords(wkWords.filter((w) => w !== word), `已删除「${word}」`);
  }

  function addKeyword() {
    const input = $("wk-new");
    const word = input.value.trim();
    if (!word) {
      input.focus();
      return;
    }
    if (wkWords.includes(word)) {
      addMsg("sys", `「${word}」已经在列表里了。`);
      input.value = "";
      return;
    }
    input.value = "";
    saveKeywords(wkWords.concat(word), `已添加「${word}」`);
  }

  $("btn-wk-add").addEventListener("click", addKeyword);
  $("wk-new").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addKeyword();
    }
  });

  // ---------------------------------------------------------------- 身份
  // 两块内容：主人 QQ 号（可多个）+ 她的名字。
  // 走的仍然是「整列表提交」那套：本地先存一份，增删之后整体发给服务端，
  // 成功就用服务端回的值重画 —— 它会顺手把重复/非法项清掉。
  const idGrid = $("id-owner-grid");
  const idCurrent = $("id-current");
  const idResult = $("id-result");
  let idOwnerIds = [];
  let idBusy = false;

  function renderIdentity(d) {
    if (d) {
      idOwnerIds = d.owner_ids || [];
      if (d.pet_name && !$("id-name").value) $("id-name").value = d.pet_name;
    }
    idGrid.innerHTML = "";
    if (!idOwnerIds.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.style.margin = "0";
      empty.textContent = "还没配主人。现在谁发消息都算路人。";
      idGrid.appendChild(empty);
    }
    for (const uid of idOwnerIds) {
      const card = document.createElement("div");
      card.className = "key-card kw-card";

      const label = document.createElement("div");
      label.className = "key-label";
      label.textContent = uid;
      card.appendChild(label);

      const foot = document.createElement("div");
      foot.className = "key-foot";
      const del = document.createElement("button");
      del.className = "key-del";
      del.textContent = "删除";
      del.addEventListener("click", () => removeOwnerId(uid));
      foot.appendChild(del);
      card.appendChild(foot);

      idGrid.appendChild(card);
    }
    const name = $("id-name").value.trim() || "（未命名）";
    idCurrent.textContent = name + (idOwnerIds.length
      ? " · 主人 " + idOwnerIds.length + " 个"
      : " · 还没配主人");
  }

  async function loadIdentity() {
    try {
      const res = await fetch("/api/identity");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      renderIdentity(d);
    } catch (e) {
      idCurrent.textContent = "读取失败";
    }
  }

  async function saveIdentity(payload, okMsg) {
    if (idBusy) return;
    idBusy = true;
    try {
      const res = await fetch("/api/identity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.message || d.error || "保存失败");
      $("id-name").value = d.pet_name || "";
      renderIdentity(d);
      idResult.textContent = d.message || okMsg || "已保存";
      addMsg("sys", d.message || okMsg || "身份已更新");
    } catch (e) {
      idResult.textContent = "保存失败：" + (e && e.message ? e.message : e);
      addMsg("sys", "身份保存失败：" + (e && e.message ? e.message : e));
      await loadIdentity();       // 失败就回到服务端的真实状态
    } finally {
      idBusy = false;
    }
  }

  function removeOwnerId(uid) {
    const name = $("id-name").value.trim() || "小菲鱼";
    saveIdentity({ pet_name: name, owner_ids: idOwnerIds.filter((x) => x !== uid) },
      `已移除 ${uid}`);
  }

  function addOwnerId() {
    const input = $("id-owner-new");
    const raw = input.value.trim();
    if (!raw) {
      input.focus();
      return;
    }
    if (!/^\d+$/.test(raw)) {
      idResult.textContent = "主人号只能是数字（QQ 号）";
      input.focus();
      return;
    }
    if (idOwnerIds.includes(raw)) {
      idResult.textContent = "这个号已经在列表里了";
      input.value = "";
      return;
    }
    input.value = "";
    const name = $("id-name").value.trim() || "小菲鱼";
    saveIdentity({ pet_name: name, owner_ids: idOwnerIds.concat(raw) },
      `已添加主人 ${raw}`);
  }

  $("btn-id-owner-add").addEventListener("click", addOwnerId);
  $("id-owner-new").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addOwnerId();
    }
  });
  $("btn-id-save").addEventListener("click", () => {
    const name = $("id-name").value.trim();
    if (!name) {
      idResult.textContent = "名字不能为空";
      $("id-name").focus();
      return;
    }
    saveIdentity({ pet_name: name, owner_ids: idOwnerIds }, "已保存");
  });

  // ---------------------------------------------------------------- 自定义触摸区域
  // 一个区域一张卡，点「自定义」在真模型上画矩形/圆形。
  // 坐标存服务端（desktop/data/touch_regions.json），保存后走 SSE 推给桌宠 ——
  // 桌宠窗口是常驻的，不会自己重新拉这份表，不推就「点了没反应」。
  const touchGrid = $("touch-grid");
  let touchRegions = [];
  let touchHitBox = null;
  let touchEmotions = [];
  let teRegion = null;
  let teShapes = [];
  let teSelected = -1;
  let teMode = "rect";
  let teReady = false;
  let teInitTimer = 0;
  let teOriginal = "";

  function renderTouch() {
    touchGrid.innerHTML = "";
    if (!touchRegions.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      p.textContent = "读不到区域表，桌宠可能没起来。";
      touchGrid.appendChild(p);
      return;
    }
    for (const region of touchRegions) {
      const card = document.createElement("div");
      card.className = "key-card touch-card";

      const label = document.createElement("div");
      label.className = "key-label";
      label.textContent = region.label;
      card.appendChild(label);

      const count = (region.shapes || []).length;
      const voice = region.voice || {};
      const voiceText = voice.total
        ? (voice.state === "running"
            ? `语音合成中 ${voice.ready}/${voice.total}`
            : `语音 ${voice.ready}/${voice.total}`)
        : "没台词";
      const sub = document.createElement("div");
      sub.className = "key-mask" + (count ? "" : " empty");
      sub.textContent = `${count ? count + " 个图形" : "还没画图形"} · ${region.lines.length} 句台词 · ${voiceText}`;
      card.appendChild(sub);

      const foot = document.createElement("div");
      foot.className = "key-foot";
      const drawBtn = document.createElement("button");
      drawBtn.className = "mini-btn";
      drawBtn.textContent = "画区域";
      drawBtn.addEventListener("click", () => openTouchEditor(region.id));
      const voiceBtn = document.createElement("button");
      voiceBtn.className = "mini-btn";
      voiceBtn.textContent = "改台词";
      voiceBtn.addEventListener("click", () => openVoiceEditor(region.id));
      foot.append(drawBtn, voiceBtn);
      if (voice.state === "error") {
        const rebuildBtn = document.createElement("button");
        rebuildBtn.className = "mini-btn";
        rebuildBtn.textContent = "重生成语音";
        rebuildBtn.addEventListener("click", () => rebuildVoice(region.id));
        foot.appendChild(rebuildBtn);
      }
      card.appendChild(foot);

      touchGrid.appendChild(card);
    }
  }

  async function loadTouchRegions() {
    try {
      const res = await fetch("/api/touch-regions");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      touchRegions = d.regions || [];
      touchHitBox = d.hit_box || null;
      if (Array.isArray(d.emotions) && d.emotions.length) touchEmotions = d.emotions;
    } catch (e) {
      touchRegions = [];
    }
    renderTouch();
  }

  function postToFrame(msg) {
    const frame = $("te-frame");
    if (frame && frame.contentWindow) frame.contentWindow.postMessage(msg, "*");
  }

  function setTeMode(mode) {
    teMode = mode === "ellipse" ? "ellipse" : "rect";
    $("te-rect").classList.toggle("active", teMode === "rect");
    $("te-ellipse").classList.toggle("active", teMode === "ellipse");
    postToFrame({ type: "touch-edit:mode", mode: teMode });
  }

  function sendInit() {
    if (teReady) return;
    teReady = true;
    clearTimeout(teInitTimer);
    postToFrame({
      type: "touch-edit:init",
      hit_box: touchHitBox,
      shapes: teShapes,
      mode: teMode,
    });
  }

  function openTouchEditor(id) {
    const region = touchRegions.find((r) => r.id === id);
    if (!region) return;
    teRegion = region;
    teShapes = (region.shapes || []).map((s) => ({ ...s }));
    teOriginal = JSON.stringify(teShapes);
    teSelected = -1;
    teReady = false;
    $("te-title").textContent = `自定义触摸区域 · ${region.label}`;
    $("te-count").textContent = `${teShapes.length} 个图形`;
    $("te-tip").textContent = "在模型身上拖一下就画出一个；点已有的图形可以选中。";
    $("touch-editor").classList.remove("hidden");
    setTeMode("rect");
    const frame = $("te-frame");
    frame.src = "/stage?edit=1";
    // 模型加载要几秒；iframe ready 了会主动说一声，这里只是兜底
    clearTimeout(teInitTimer);
    teInitTimer = setTimeout(sendInit, 4000);
  }

  function closeTouchEditor() {
    clearTimeout(teInitTimer);
    const frame = $("te-frame");
    if (frame) frame.src = "about:blank";   // 释放 Live2D，别让后台挂着一个模型
    $("touch-editor").classList.add("hidden");
    teRegion = null;
    teShapes = [];
    teSelected = -1;
    teReady = false;
  }

  function cancelTouchEditor() {
    if (JSON.stringify(teShapes) !== teOriginal && !confirm("画的还没保存，确定放弃吗？")) {
      return;
    }
    closeTouchEditor();
  }

  async function saveTouch() {
    if (!teRegion) return;
    const label = teRegion.label;
    const next = touchRegions.map((r) =>
      r.id === teRegion.id ? { ...r, shapes: teShapes } : r
    );
    try {
      const res = await fetch("/api/touch-regions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regions: next }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "保存失败");
      touchRegions = d.regions || next;
      touchHitBox = d.hit_box || touchHitBox;
      closeTouchEditor();
      renderTouch();
      addMsg("sys", `「${label}」的触摸区域已保存，桌宠那边立刻生效。`);
    } catch (e) {
      $("te-tip").textContent = "保存失败：" + (e && e.message ? e.message : e);
    }
  }

  async function resetTouch() {
    if (!confirm("恢复成默认的 7 个区域？你自己画的图形都会没掉。")) return;
    try {
      const res = await fetch("/api/touch-regions/reset", { method: "POST", headers: { "Content-Type": "application/json" } });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "恢复失败");
      touchRegions = d.regions || [];
      touchHitBox = d.hit_box || touchHitBox;
      renderTouch();
      addMsg("sys", "已恢复默认触摸区域。");
    } catch (e) {
      addMsg("sys", "恢复失败：" + (e && e.message ? e.message : e));
    }
  }

  // iframe 回传画好的形状（只认自己那个 iframe 的消息）
  window.addEventListener("message", (e) => {
    const frame = $("te-frame");
    if (!frame || e.source !== frame.contentWindow) return;
    const d = e.data || {};
    if (d.type === "touch-edit:ready") {
      sendInit();
      return;
    }
    if (d.type === "touch-edit:state") {
      teShapes = Array.isArray(d.shapes) ? d.shapes : [];
      teSelected = typeof d.selected === "number" ? d.selected : -1;
      $("te-count").textContent = `${teShapes.length} 个图形`;
    }
  });

  $("te-rect").addEventListener("click", () => setTeMode("rect"));
  $("te-ellipse").addEventListener("click", () => setTeMode("ellipse"));
  $("te-undo").addEventListener("click", () => postToFrame({ type: "touch-edit:undo" }));
  $("te-del").addEventListener("click", () => postToFrame({ type: "touch-edit:delete" }));
  $("te-clear").addEventListener("click", () => {
    if (confirm("把这个区域画的图形全部清掉？")) {
      postToFrame({ type: "touch-edit:clear" });
    }
  });
  $("te-save").addEventListener("click", saveTouch);
  $("te-cancel").addEventListener("click", cancelTouchEditor);
  $("te-close").addEventListener("click", cancelTouchEditor);
  $("btn-touch-reset").addEventListener("click", resetTouch);

  // ---------------------------------------------------------------- 触摸台词
  // 一个区域几句话，每句带一个情绪（只改语气，不换音色）。
  // 保存后服务端会**先删旧语音再重合成**，所以这里要轮询进度。
  const veList = $("ve-list");
  let veRegion = null;
  let veLines = [];
  let vePollTimer = 0;

  function renderVoiceRows() {
    veList.innerHTML = "";
    veLines.forEach((line, index) => {
      const row = document.createElement("div");
      row.className = "ve-row";

      const input = document.createElement("input");
      input.className = "field";
      input.maxLength = 30;
      input.value = line.text;
      input.placeholder = "她说的那句，比如「别戳啦」";
      input.addEventListener("input", () => { line.text = input.value; });
      row.appendChild(input);

      const sel = document.createElement("select");
      sel.className = "field";
      for (const item of touchEmotions) {
        const opt = document.createElement("option");
        opt.value = item.key;
        opt.textContent = item.label;
        sel.appendChild(opt);
      }
      sel.value = line.emotion;
      sel.addEventListener("change", () => { line.emotion = sel.value; });
      row.appendChild(sel);

      const del = document.createElement("button");
      del.className = "key-del";
      del.textContent = "删除";
      del.addEventListener("click", () => {
        veLines.splice(index, 1);
        renderVoiceRows();
      });
      row.appendChild(del);

      veList.appendChild(row);
    });
    if (!veLines.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      p.textContent = "还没有台词。加一句吧，不然摸到这块只会动、不出声。";
      veList.appendChild(p);
    }
  }

  function openVoiceEditor(id) {
    const region = touchRegions.find((r) => r.id === id);
    if (!region) return;
    veRegion = region;
    veLines = (region.lines || []).map((l) => ({ text: l.text, emotion: l.emotion }));
    $("ve-title").textContent = `触摸台词 · ${region.label}`;
    $("ve-tip").textContent = "确定后会删掉旧语音，并按新台词重新合成，需要几秒。";
    $("voice-editor").classList.remove("hidden");
    renderVoiceRows();
  }

  function closeVoiceEditor() {
    clearTimeout(vePollTimer);
    $("voice-editor").classList.add("hidden");
    veRegion = null;
    veLines = [];
  }

  async function saveVoice() {
    if (!veRegion) return;
    const clean = veLines
      .map((l) => ({ text: (l.text || "").trim(), emotion: l.emotion || "calm" }))
      .filter((l) => l.text);
    const label = veRegion.label;
    const next = touchRegions.map((r) =>
      r.id === veRegion.id ? { ...r, lines: clean } : r
    );
    const btn = $("ve-save");
    btn.disabled = true;
    $("ve-tip").textContent = "正在保存…";
    try {
      const res = await fetch("/api/touch-regions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regions: next }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "保存失败");
      touchRegions = d.regions || next;
      if (Array.isArray(d.emotions) && d.emotions.length) touchEmotions = d.emotions;
      closeVoiceEditor();
      renderTouch();
      if (d.voice_errors && d.voice_errors.length) {
        addMsg("sys", `「${label}」台词已保存，但语音没合成：${d.voice_errors[0].error}`);
      } else if (d.rebuilding && d.rebuilding.length) {
        addMsg("sys", `「${label}」台词已保存，正在重新合成语音…`);
        pollVoice();
      } else {
        addMsg("sys", `「${label}」台词已保存。`);
      }
    } catch (e) {
      $("ve-tip").textContent = "保存失败：" + (e && e.message ? e.message : e);
    } finally {
      btn.disabled = false;
    }
  }

  // 轮询合成进度，顺便把卡片上的「语音 2/2」刷新掉
  function pollVoice(rounds = 60) {
    clearTimeout(vePollTimer);
    const tick = async () => {
      let map = null;
      try {
        const res = await fetch("/api/touch-voice-status");
        const d = await res.json();
        map = d.regions || null;
      } catch (_) { /* 网络抖一下就下一轮再试 */ }
      if (map) {
        for (const region of touchRegions) {
          if (map[region.id]) region.voice = map[region.id];
        }
        renderTouch();
        const busy = Object.values(map).some((v) => v.state === "running");
        if (!busy) {
          const bad = Object.entries(map).find(([, v]) => v.state === "error");
          addMsg("sys", bad ? `语音合成没成功：${bad[1].error}` : "语音合成完成。");
          return;
        }
      }
      if (rounds-- > 0) vePollTimer = setTimeout(tick, 1000);
    };
    vePollTimer = setTimeout(tick, 800);
  }

  async function rebuildVoice(id) {
    try {
      const res = await fetch("/api/touch-voices/rebuild", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "重生成失败");
      addMsg("sys", "正在重新合成语音…");
      pollVoice();
    } catch (e) {
      addMsg("sys", "重生成失败：" + (e && e.message ? e.message : e));
    }
  }

  $("ve-add").addEventListener("click", () => {
    veLines.push({ text: "", emotion: "calm" });
    renderVoiceRows();
    const inputs = veList.querySelectorAll("input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  });
  $("ve-save").addEventListener("click", saveVoice);
  $("ve-cancel").addEventListener("click", closeVoiceEditor);
  $("ve-close").addEventListener("click", closeVoiceEditor);

  // ---------------------------------------------------------------- 系统层（DMN）
  // 后台是浏览器页面，不该持有网关 token；统一走 8848 的 /api/system 代理。
  const DMN_EMOTION = {
    happy: "开心", sad: "难过", shy: "害羞", calm: "平静",
    surprised: "惊讶", confused: "困惑", love: "喜欢", angry: "生气",
  };
  let dmnBusy = false;

  function ago(ts) {
    if (!ts) return "还没有";
    const seconds = Math.max(0, Date.now() / 1000 - Number(ts));
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return Math.round(seconds / 60) + " 分钟前";
    if (seconds < 86400) return (seconds / 3600).toFixed(1).replace(".0", "") + " 小时前";
    return Math.round(seconds / 86400) + " 天前";
  }

  function renderInnerOs(items) {
    const box = $("inner-list");
    box.innerHTML = "";
    if (!items || !items.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      p.textContent = "队列是空的——她这会儿没有憋着的话。";
      box.appendChild(p);
      return;
    }
    for (const item of items.slice().reverse()) {
      const card = document.createElement("div");
      card.className = "key-card inner-card";
      const text = document.createElement("div");
      text.className = "inner-text";
      text.textContent = item.text || "";
      const ts = document.createElement("div");
      ts.className = "inner-ts";
      ts.textContent = ago(item.ts);
      card.append(text, ts);
      box.appendChild(card);
    }
  }

  async function loadSystem() {
    try {
      const res = await fetch("/api/system");
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || ("HTTP " + res.status));
      $("dm-mood").textContent = DMN_EMOTION[d.emotion] || d.emotion || "—";
      $("dm-focus").textContent = d.focus || "—";
      $("dm-inner").textContent = (d.inner_os_count || 0) + " 条";
      $("dm-tick").textContent = ago(d.last_tick_ts);
      $("dm-speak").textContent = ago(d.last_speak_ts);
      $("dm-count").textContent = (d.speak_count_today || 0) + " / " + (d.max_speak_per_day || 6);
      const every = Math.round((d.heartbeat_s || 300) / 60);
      $("dm-tip").textContent = `心跳每 ${every} 分钟查一次；没变化就跳过，所以「上次冒念头」可能比这更久。`;
      $("inner-tip").textContent = d.last_consolidate_ts
        ? `上次巩固 ${ago(d.last_consolidate_ts)}；巩固一天一次。`
        : "还没巩固过；巩固一天一次，把最近的对话蒸馏成观点。";
      renderInnerOs(d.inner_os || []);
    } catch (_) {
      $("dm-mood").textContent = "读不到";
      renderInnerOs([]);
      const box = $("inner-list");
      box.innerHTML = "";
      const p = document.createElement("p");
      p.className = "hint";
      p.style.margin = "0";
      p.textContent = "网关没连上，读不到她的状态。";
      box.appendChild(p);
    }
  }

  async function dmnPost(path, label) {
    if (dmnBusy) return;
    dmnBusy = true;
    try {
      const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" } });
      const d = await res.json();
      if (!res.ok || !d.ok) throw new Error(d.error || "操作失败");
      addMsg("sys", d.message || label);
      // 心跳/巩固都要调模型，给它几秒再刷新
      setTimeout(loadSystem, 3000);
      setTimeout(loadSystem, 9000);
    } catch (e) {
      addMsg("sys", label + "失败：" + (e && e.message ? e.message : e));
    } finally {
      setTimeout(() => { dmnBusy = false; }, 3000);
    }
  }

  $("btn-dm-tick").addEventListener("click", () => dmnPost("/api/system/tick", "让她想一次"));
  $("btn-dm-refresh").addEventListener("click", loadSystem);
  $("btn-inner-consolidate").addEventListener("click", () => dmnPost("/api/system/consolidate", "巩固记忆"));
  $("btn-inner-clear").addEventListener("click", () => {
    if (!confirm("把内心OS 队列清空？清掉的想法不会再被说出来。")) return;
    dmnPost("/api/system/inner-os/clear", "清空内心OS");
  });

  // 给 index.html 的顶栏自检一个信号：这份脚本确实执行到了这里。
  // 页面白屏且这行没跑到时，自检会显示「JS 没跑起来」。
  window.__consoleJsLoaded = true;

  // ---------------------------------------------------------------- 启动
  let savedLog = null;
  try { savedLog = localStorage.getItem("dmconsole.logOpen"); } catch (_) {}
  setLogOpen(savedLog === "1");

  addMsg("sys", "后台已就绪。桌宠窗口如果没出现，双击桌面人物或从托盘菜单打开。");
  refreshStatus();
  connectEvents();
  loadIdentity();
  loadModelConfig();
  loadKeys();
  loadWakeKeywords();
  loadTouchRegions();
  loadSystem();
  setInterval(loadSystem, 8000);
  setInterval(refreshStatus, 4000);
  if (logOpen) input.focus();
})();
