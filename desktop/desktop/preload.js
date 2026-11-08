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
  quit: () => ipcRenderer.send("pet:quit"),
});
