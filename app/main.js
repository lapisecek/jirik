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
const ICON = path.join(__dirname, "assets", "jirik.ico");
const LOGS = path.join(ROOT, "data", "logs");

let main = null;
let pill = null;
let overlays = [];
const children = {};
let quitting = false;

app.setAppUserModelId("cz.jirik.local");
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) app.quit();
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
  return nativeTheme.shouldUseDarkColors ? { bg: "#131210", fg: "#EEEBE4" } : { bg: "#F4F2EE", fg: "#1B1A17" };
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
    if (name === "server") status("Server Jiříka se zastavil. Podrobnosti jsou v data/logs/server.log.", true);
  });
  return p;
}
function startPc() {
  if (quitting || children.pc) return;
  startPython("pc", ["-m", "aiko.agents.pc", "--server", `ws://127.0.0.1:${PORT}/ws/device`, "--name", "Tento počítač"]);
}

async function runFirstSetup() {
  await new Promise(resolve => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    const timer = setTimeout(finish, 3600 * 1000);
    try {
      const p = spawn("cmd.exe", [
        "/c", "start", "Jiřík – instalace", "/wait", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", path.join(ROOT, "setup.ps1"),
      ], { cwd: ROOT, stdio: "ignore", windowsHide: true });
      p.once("exit", () => { clearTimeout(timer); finish(); });
      p.once("error", e => { log("setup", e); clearTimeout(timer); finish(); });
    } catch (e) { log("setup", e); clearTimeout(timer); finish(); }
  });
}

async function startEverything() {
  if (!fs.existsSync(PY)) {
    // první spuštění: instalace se otevře sama v okně a aplikace počká, až doběhne
    status("První spuštění – instaluji vše potřebné (otevřelo se okno instalace)…");
    await runFirstSetup();
    if (!fs.existsSync(PY)) { status("Instalace se nedokončila. Spusťte Setup.bat ve složce Jiříka.", true); return false; }
    status("Instalace hotová – spouštím…");
  }
  status("Spouštím model…");
  if (!(await ok("http://localhost:11434/api/tags"))) {
    try {
      const env = { ...process.env, OLLAMA_FLASH_ATTENTION: "1", OLLAMA_KV_CACHE_TYPE: "q8_0" };
      const app = path.join(process.env.LOCALAPPDATA || "", "Programs", "Ollama", "ollama app.exe");
      if (fs.existsSync(app)) spawn(app, [], { detached: true, stdio: "ignore", env }).unref();
      else spawn("ollama", ["serve"], { detached: true, stdio: "ignore", windowsHide: true, env }).unref();
    } catch (e) { log("ollama", e); }
    for (let i = 0; i < 40 && !(await ok("http://localhost:11434/api/tags")); i++) await sleep(500);
  }
  if (!(await ok(`${URL}/api/hello`))) {
    status("Spouštím Jiříka…");
    startPython("server", ["-m", "aiko.server", "--config", "aiko.toml", "--no-mdns"]);
    for (let i = 0; i < 360 && !(await ok(`${URL}/api/hello`)); i++) {
      if (!children.server) return false;
      if (i === 8) status("Načítám hlas a model…");
      if (i === 60) status("Ještě chvilku – první start trvá déle…");
      await sleep(500);
    }
    if (!(await ok(`${URL}/api/hello`))) { status("Server Jiříka neodpovídá.", true); return false; }
  }
  const st = await json(`${URL}/api/state`);
  if (!(st && (st.devices || []).some(d => !d.remote && d.online))) startPc();
  return true;
}

// ---------- okno s chatem ----------
function createMain() {
  const t = theme();
  main = new BrowserWindow({
    width: 1080, height: 800, minWidth: 440, minHeight: 560, show: false, title: "Jiřík", icon: ICON,
    backgroundColor: t.bg, titleBarStyle: "hidden",
    titleBarOverlay: { color: t.bg, symbolColor: t.fg, height: 52 },
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  main.setMenuBarVisibility(false);
  main.once("ready-to-show", () => main.show());
  main.webContents.setWindowOpenHandler(({ url }) => {
    try { const u = new URL(url); if (u.protocol === "http:" || u.protocol === "https:") shell.openExternal(url); }
    catch { /* neplatná adresa */ }
    return { action: "deny" };
  });
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

// ---------- „Aiko pracuje“: rámeček kolem obrazovky + stavová lišta ----------
// Okna se vytvoří až při první práci (nestojí paměť, dokud nejsou potřeba) a pak se jen ukazují.
function ensureOverlays() {
  if (pill && !pill.isDestroyed()) return;
  overlays = [];
  for (const d of screen.getAllDisplays()) {
    const w = new BrowserWindow({
      ...d.bounds, transparent: true, frame: false, resizable: false, movable: false, focusable: false,
      skipTaskbar: true, alwaysOnTop: true, hasShadow: false, fullscreenable: false, show: false, title: "",
      webPreferences: { backgroundThrottling: false },
    });
    w.setAlwaysOnTop(true, "screen-saver");
    w.setIgnoreMouseEvents(true);          // kliky jdou skrz
    w.setContentProtection(true);          // na snímcích obrazovky pro model rámeček není
    w.setBounds(d.bounds);
    w.loadFile(path.join(__dirname, "overlay.html"));
    overlays.push(w);
  }
  const wa = screen.getPrimaryDisplay().workArea;
  const W = 460, H = 60;
  pill = new BrowserWindow({
    x: Math.round(wa.x + (wa.width - W) / 2), y: wa.y + 12, width: W, height: H, transparent: true, frame: false,
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
function whenLoaded(win, fn) {
  if (!win || win.isDestroyed()) return;
  if (win.webContents.isLoading()) win.webContents.once("did-finish-load", fn); else fn();
}

const work = { on: false, screen: false, minimized: false, last: 0, hideTimer: null };
function workStart(text, isScreen) {
  ensureOverlays();
  work.last = Date.now();
  clearTimeout(work.hideTimer);
  if (!work.on) {
    work.on = true;
    work.minimized = false;
    overlays.forEach(w => { w.showInactive(); whenLoaded(w, () => call(w, "setOn(true)")); });
    pill.showInactive();
  }
  // Při klikání po obrazovce se okno s chatem uhne (jinak by Aiko klikala do sebe)
  if (isScreen && !work.screen) {
    work.screen = true;
    if (main && !main.isMinimized() && main.isVisible()) setTimeout(() => {
      if (work.on && main && !main.isMinimized()) { main.minimize(); work.minimized = true; }
    }, 200);
  }
  whenLoaded(pill, () => call(pill, `setPill("work", ${JSON.stringify(text || "Pracuji…")})`));
}
function workEnd() {
  if (!work.on) return;
  work.on = false;
  overlays.forEach(w => call(w, "setOn(false)"));
  call(pill, `setPill("done", "Hotovo")`);
  work.hideTimer = setTimeout(() => {
    overlays.forEach(w => !w.isDestroyed() && w.hide());
    call(pill, "hidePill()");
    setTimeout(() => { if (!work.on && pill && !pill.isDestroyed()) pill.hide(); }, 400);
  }, 2200);
  if (main && work.minimized) {
    if (main.isMinimized()) main.restore();
    main.showInactive();
  }
  work.screen = false;
  work.minimized = false;
}
setInterval(() => { if (work.on && Date.now() - work.last > 300000) workEnd(); }, 5000);

ipcMain.on("pill:open", () => showMain());
ipcMain.on("pill:stop", async () => {
  try { await fetch(`${URL}/api/stop`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { log("stop", e); }
});

// Události ze serveru (stejné, jaké dostávají okna s chatem)
function listen() {
  let ws;
  try { ws = new WebSocket(`ws://127.0.0.1:${PORT}/ws/ui`); } catch (e) { log("ws", e); return setTimeout(listen, 3000); }
  ws.onmessage = ev => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "work") { if (m.on) workStart(m.text, m.screen); else workEnd(); }
    else if (m.type === "chat.end" && work.on) workEnd();
  };
  ws.onclose = () => { if (!quitting) setTimeout(listen, 2000); };
  ws.onerror = () => {};
}

// ---------- start / konec ----------
if (gotLock) {
  app.whenReady().then(async () => {
    createMain();
    const ready = await startEverything();
    if (!ready || !main) return;
    status("Hotovo");
    listen();
    main.loadURL(URL);
  });
}
app.on("before-quit", () => {
  quitting = true;
  for (const p of Object.values(children)) { try { p.kill(); } catch { /* už neběží */ } }
});
app.on("window-all-closed", () => app.quit());
