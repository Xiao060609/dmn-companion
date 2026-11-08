const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petChat", {
  onFocus: (cb) => ipcRenderer.on("chatbox:focus", () => cb()),
  close: () => ipcRenderer.send("chatbox:close"),
});
