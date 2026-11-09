const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petBridge", {
  setIgnoreMouse: (flag) => ipcRenderer.send("pet:ignore-mouse", !!flag),
  onCursor: (cb) => {
    const handler = (_e, x, y) => cb(x, y);
    ipcRenderer.on("pet:cursor", handler);
    return () => ipcRenderer.removeListener("pet:cursor", handler);
  },
  dragStart: (x, y) => ipcRenderer.send("pet:drag-start", x, y),
  dragMove: (x, y) => ipcRenderer.send("pet:drag-move", x, y),
  dragEnd: (moved) => ipcRenderer.send("pet:drag-end", !!moved),
  zoom: (deltaY) => ipcRenderer.send("pet:zoom", deltaY),
  openAdmin: () => ipcRenderer.send("pet:open-admin"),
  openChatbox: () => ipcRenderer.send("pet:open-chatbox"),
  menuAction: (action, arg) => ipcRenderer.send("pet:menu-action", action, arg),
  onScale: (cb) => ipcRenderer.on("pet:scale", (_e, name) => cb(name)),
  onQqStatus: (cb) => {
    const handler = (_e, running) => cb(!!running);
    ipcRenderer.on("pet:qq-status", handler);
    return () => ipcRenderer.removeListener("pet:qq-status", handler);
  },
  // 渲染暂停状态（2026-11-09）：主进程是唯一真源，因为暂停可以由
  // 托盘菜单触发（那时页面里没有任何操作），页面必须被动跟随。
  onPaused: (cb) => {
    const handler = (_e, paused) => cb(!!paused);
    ipcRenderer.on("pet:paused", handler);
    return () => ipcRenderer.removeListener("pet:paused", handler);
  },
  // 页面启动后主动问一次当前状态，避免"主进程已暂停、页面却以为在跑"
  getPaused: () => ipcRenderer.sendSync("pet:get-paused"),
  quit: () => ipcRenderer.send("pet:quit"),
});
