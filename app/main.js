// Aiko – desktopová aplikace (Electron).
// Spustí vše potřebné (Ollama, server Aiko, ovládání počítače), ukáže chat v okně aplikace
// a když Aiko pracuje na počítači, rozsvítí kolem obrazovky barevný rámeček se stavovou lištou.
const { app, BrowserWindow, screen, ipcMain, nativeTheme, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { spawn } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");
const PORT = 8770;
const URL = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const ICON = path.join(__dirname, "assets", "aiko.ico");
const LOGS = path.join(ROOT, "data", "logs");

let main = null;
let pill = null;
let overlays = [];
const children = {};
let quitting = false;

app.setAppUserModelId("cz.aiko.local");
if (!app.requestSingleInstanceLock()) app.quit();
app.on("second-instance", () => showMain());

// ---------- pomocné ----------
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function ok(url, ms = 1500) {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(ms) });
    return r.ok;
  } catch { return false; }
}
async function json(url) {
  try { const r = await fetch(url, { signal: AbortSignal.timeout(3000) }); return r.ok ? r.json() : null; }
  catch { return null; }
}
function log(...a) {
  try {
    fs.mkdirSync(LOGS, { recursive: true });
    fs.appendFileSync(path.join(LOGS, "app.log"), `${new Date().toISOString()} ${a.join(" ")}\n`);
  } catch { /* nevadí */ }
}
function theme() {
  return nativeTheme.shouldUseDarkColors ? { bg: "#0f1115", fg: "#e9ebef" } : { bg: "#f6f7f9", fg: "#15171a" };
}
function status(text, error = false) {
  log("status:", text);
  if (main && !main.isDestroyed() && main.webContents.getURL().startsWith("file:"))
    main.webContents.executeJavaScript(`window.setStatus && setStatus(${JSON.stringify(text)}, ${error})`).catch(() => {});
}

// ---------- procesy na pozadí ----------
function startPython(name, args) {
  fs.mkdirSync(LOGS, { recursive: true });
  const out = fs.openSync(path.join(LOGS, `${name}.log`), "a");
  const env = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", AIKO_CONFIG: path.join(ROOT, "aiko.toml") };
  delete env.AIKO_TOKEN;
  const p = spawn(PY, args, { cwd: ROOT, env, windowsHide: true, stdio: ["ignore", out, out] });
  children[name] = p;
  log("start", name, p.pid);
  p.on("exit", code => {
    log("exit", name, code);
    delete children[name];
    if (quitting) return;
    if (name === "pc") setTimeout(startPc, 3000);              // ovládání počítače se samo vrátí
    if (name === "server") status("Server Aiko se zastavil. Podrobnosti jsou v data/logs/server.log.", true);
  });
  return p;
}
function startPc() {
  if (quitting || children.pc) return;
  startPython("pc", ["-m", "aiko.agents.pc", "--server", `ws://127.0.0.1:${PORT}/ws/device`, "--name", "Můj počítač"]);
}

async function startEverything() {
  if (!fs.existsSync(PY)) {
    status("Aiko ještě není nainstalovaná. Spusťte ve složce Aiko-Local soubor Setup.bat.", true);
    return false;
  }
  status("Spouštím model…");
  if (!(await ok("http://localhost:11434/api/tags"))) {
    try {
      spawn("ollama", ["serve"], { detached: true, stdio: "ignore", windowsHide: true }).unref();
    } catch (e) { log("ollama", e); }
  }
  if (!(await ok(`${URL}/api/hello`))) {
    status("Spouštím Aiko…");
    startPython("server", ["-m", "aiko.server", "--config", "aiko.toml", "--no-mdns"]);
    for (let i = 0; i < 360 && !(await ok(`${URL}/api/hello`)); i++) {
      if (!children.server) return false;
      if (i === 8) status("Načítám hlas Jirka a model…");
      if (i === 60) status("Ještě chvilku – první start trvá déle…");
      await sleep(500);
    }
  }
  const st = await json(`${URL}/api/state`);
  if (!(st && (st.devices || []).some(d => d.kind === "pc" && d.online))) startPc();
  return true;
}

// ---------- okno s chatem ----------
function createMain() {
  const t = theme();
  main = new BrowserWindow({
    width: 1040, height: 780, minWidth: 420, minHeight: 540, show: false, title: "Aiko", icon: ICON,
    backgroundColor: t.bg, titleBarStyle: "hidden",
    titleBarOverlay: { color: t.bg, symbolColor: t.fg, height: 56 },
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  main.setMenuBarVisibility(false);
  main.once("ready-to-show", () => main.show());
  main.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  main.on("closed", () => { main = null; app.quit(); });
  main.loadFile(path.join(__dirname, "splash.html"));
  nativeTheme.on("updated", () => {
    const c = theme();
    if (main) { main.setBackgroundColor(c.bg); main.setTitleBarOverlay({ color: c.bg, symbolColor: c.fg }); }
  });
}
function showMain() {
  if (!main) return;
  if (main.isMinimized()) main.restore();
  main.show();
  main.focus();
  main.flashFrame(false);
}

// ---------- „Aiko pracuje“: zářící rámeček + stavová lišta ----------
function createOverlays() {
  for (const d of screen.getAllDisplays()) {
    const w = new BrowserWindow({
      ...d.bounds, transparent: true, frame: false, resizable: false, movable: false, focusable: false,
      skipTaskbar: true, alwaysOnTop: true, hasShadow: false, fullscreenable: false, show: false, title: "",
      webPreferences: { backgroundThrottling: false },
    });
    w.setAlwaysOnTop(true, "screen-saver");
    w.setIgnoreMouseEvents(true);          // kliky jdou skrz – Aiko i člověk ovládají okna pod ním
    w.setContentProtection(true);          // na snímcích obrazovky pro Aiko rámeček není
    w.setBounds(d.bounds);
    w.loadFile(path.join(__dirname, "overlay.html"));
    overlays.push(w);
  }
  const wa = screen.getPrimaryDisplay().workArea;
  const W = 480, H = 64;
  pill = new BrowserWindow({
    x: Math.round(wa.x + (wa.width - W) / 2), y: wa.y + 14, width: W, height: H, transparent: true, frame: false,
    resizable: false, movable: false, focusable: false, skipTaskbar: true, alwaysOnTop: true, hasShadow: false,
    fullscreenable: false, show: false, title: "",
    webPreferences: { preload: path.join(__dirname, "preload-ui.js"), backgroundThrottling: false },
  });
  pill.setAlwaysOnTop(true, "screen-saver");
  pill.setContentProtection(true);
  pill.loadFile(path.join(__dirname, "pill.html"));
}
function call(win, js) {
  if (win && !win.isDestroyed()) win.webContents.executeJavaScript(js).catch(() => {});
}

const work = { on: false, steps: 0, minimized: false, last: 0, hideTimer: null };
function workStart(text) {
  work.last = Date.now();
  if (!work.on) {
    log("work start:", text || "");
    work.on = true;
    work.steps = 0;
    work.minimized = false;
    clearTimeout(work.hideTimer);
    overlays.forEach(w => { w.showInactive(); call(w, "setOn(true)"); });
    pill.showInactive();
    // Okno s chatem nesmí Aiko překážet (klikala by do sebe) – na dobu práce se schová dolů
    if (main && main.isFocused()) setTimeout(() => {
      if (work.on && main && !main.isMinimized()) { main.minimize(); work.minimized = true; }
    }, 250);
  }
  call(pill, `setPill("work", ${JSON.stringify(text || "Pracuji na počítači…")})`);
}
function workEnd(text) {
  if (!work.on) return;
  log("work end:", text || "", "steps", work.steps);
  work.on = false;
  overlays.forEach(w => call(w, "setOn(false)"));
  call(pill, `setPill("done", ${JSON.stringify(text || "Hotovo.")})`);
  clearTimeout(work.hideTimer);
  work.hideTimer = setTimeout(() => {
    overlays.forEach(w => w.hide());
    if (!work.on) pill.hide();
  }, 7000);
  setTimeout(() => { if (!work.on) overlays.forEach(w => w.hide()); }, 900);
  if (main && work.minimized) {
    if (work.steps === 0) main.showInactive();      // krátká práce (recept) – chat zase ukázat
    else main.flashFrame(true);                     // Aiko klikala – její výsledek nepřekrývat, jen bliknout
  }
}
setInterval(() => { if (work.on && Date.now() - work.last > 180000) workEnd("Hotovo."); }, 5000);

ipcMain.on("pill:open", () => showMain());
ipcMain.on("pill:stop", async () => {
  try { await fetch(`${URL}/api/stop`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { log("stop", e); }
});

// Události ze serveru (stejné, jaké dostává chat): z nich se pozná, kdy Aiko na počítači pracuje
function listen() {
  let ws;
  try { ws = new WebSocket(`ws://127.0.0.1:${PORT}/ws/ui`); } catch (e) { log("ws", e); return setTimeout(listen, 3000); }
  ws.onmessage = ev => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "reply" && m.reply) {
      if (m.reply.screen === "pracuje") workStart(m.reply.say);
      else if (work.on) workEnd(m.reply.say);
    } else if (m.type === "say" && m.working) workStart(m.text);
    else if (m.type === "agent") { work.steps++; workStart(`Krok ${m.step}: ${m.action}`); }
  };
  ws.onclose = () => { if (!quitting) setTimeout(listen, 2000); };
  ws.onerror = () => {};
}

// ---------- start / konec ----------
app.whenReady().then(async () => {
  createMain();
  createOverlays();
  const ready = await startEverything();
  if (!ready || !main) return;
  status("Hotovo");
  listen();
  main.loadURL(URL);
});
app.on("before-quit", () => {
  quitting = true;
  for (const p of Object.values(children)) { try { p.kill(); } catch { /* už neběží */ } }
});
app.on("window-all-closed", () => app.quit());
