// Stavová lišta „Aiko pracuje“: tlačítka posílají povely hlavnímu procesu aplikace.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aiko", {
  open: () => ipcRenderer.send("pill:open"),
  stop: () => ipcRenderer.send("pill:stop"),
});
