/* 桌宠对话输入框
 * - 打开即聚焦，直接打字（不用再点一下）
 * - Enter 发送 → POST /api/chat（服务端会把回复通过 SSE 广播给桌宠：气泡 + 表情 + 语音）
 * - Esc 关闭；失焦也关闭，避免挡住桌面
 * - 中文输入法组字时按 Enter 不会误发（isComposing / keyCode 229）
 */
(() => {
  "use strict";

  const bridge = window.petChat || null;
  const input = document.getElementById("text");
  const HINT = "和小菲鱼说点什么…    Enter 发送 · Esc 关闭";

  let busy = false;
  const openedAt = Date.now();

  function setState(msg) {
    input.placeholder = msg || HINT;
  }

  function focusInput() {
    try {
      input.focus({ preventScroll: true });
      const n = input.value.length;
      input.setSelectionRange(n, n);
    } catch (_) { /* ignore */ }
  }

  function close() {
    if (bridge) bridge.close();
    else window.close();
  }

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    input.value = "";
    setState("小菲鱼正在想…");
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, client: "chatbox" }),
      });
      if (!res.ok) {
        let msg = "发送失败";
        try {
          const j = await res.json();
          if (j && j.error) msg = j.error;
        } catch (_) { /* ignore */ }
        setState(msg);
        setTimeout(() => setState(""), 2600);
      } else {
        setState("");
      }
    } catch (_) {
      setState("连不上本地服务");
      setTimeout(() => setState(""), 2600);
    }
    busy = false;
    focusInput();
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if ((e.key === "Enter" || e.keyCode === 13) && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      send();
    }
  });

  // 打开就聚焦，并且再兜底补一次（窗口刚显示时偶尔抢不到焦点）
  window.addEventListener("focus", focusInput);
  focusInput();
  requestAnimationFrame(focusInput);
  setTimeout(focusInput, 60);
  setTimeout(focusInput, 220);

  if (bridge && typeof bridge.onFocus === "function") {
    bridge.onFocus(focusInput);
  }

  // 点到别处就收起来（刚打开的 400ms 内不响应，避免被自己的显示流程误触发）
  // 已经打了字的时候不关，免得写了一半被点没了
  window.addEventListener("blur", () => {
    if (Date.now() - openedAt < 400) return;
    setTimeout(() => {
      if (document.hasFocus()) return;
      if (input.value.trim()) return;
      close();
    }, 120);
  });
})();
