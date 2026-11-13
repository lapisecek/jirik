// Chat v okně aplikace: stránka pozná, že běží v aplikaci (vlastní záhlaví, bez tlačítka „Spustit vše“).
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("aikoApp", { isApp: true });
window.addEventListener("DOMContentLoaded", () => document.documentElement.classList.add("electron-app"));
