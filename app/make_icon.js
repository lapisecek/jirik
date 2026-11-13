// Vykreslí ikonu Jiříka (aiko/server/static/jirik.svg) do app/assets/jirik-1024.png.
// Spuštění: node_modules\electron\dist\electron.exe app\make_icon.js   (pak python app\make_icon.py udělá .ico)
const { app, BrowserWindow } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const SVG = fs.readFileSync(path.join(__dirname, "..", "aiko", "server", "static", "jirik.svg"), "utf8");
const S = 1024;

app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const w = new BrowserWindow({ width: S, height: S, show: false, transparent: true, frame: false,
                                useContentSize: true, webPreferences: { offscreen: true } });
  const html = `<html><body style="margin:0;background:transparent;overflow:hidden">
    <div style="width:${S}px;height:${S}px">${SVG.replace("<svg ", `<svg width="${S}" height="${S}" `)}</div></body></html>`;
  await w.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
  await new Promise(r => setTimeout(r, 400));
  const img = await w.webContents.capturePage({ x: 0, y: 0, width: S, height: S });
  fs.mkdirSync(path.join(__dirname, "assets"), { recursive: true });
  fs.writeFileSync(path.join(__dirname, "assets", "jirik-1024.png"), img.toPNG());
  console.log("png ok", img.getSize());
  app.quit();
});
