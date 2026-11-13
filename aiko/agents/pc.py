"""Lehký MCP démon pro počítač (Windows / Linux).

    python -m aiko.agents.pc --name "Počítač babičky" [--dry-run]

Žádné AI na počítači. Démon vystaví pevné nástroje a server je volá:
  diagnose_network()        – adaptér, IP, router, internet, DNS, stránka (+ osquery, pokud je)
  atomic_network_repair()   – uzavřená lokální transakce: flush DNS, záložní DNS, restart karty, čekání na IP
  open_and_fix_browser()    – otevře stránku v prohlížeči
  get_audio_state(), fix_sound()

Restart síťové karty a nastavení DNS na Windows vyžaduje spuštění jako správce.
`--dry-run` jen vypíše, co by se dělalo (bezpečné pro vývoj a demo).
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any
from urllib.parse import urlparse

from ..common import som
from ..common.agent_base import DeviceAgent, base_argparser, setup_logging
from ..common.protocol import ToolRefused, tool
from ..common.safety import (is_blocked_url, is_dangerous_text, is_forbidden_key, is_risky_label, is_system_window,
                             looks_like_code)
from . import pc_files, pc_gui

log = logging.getLogger("aiko.pc")
WIN = sys.platform.startswith("win")

# Hledání min: okno s hrou (titulek stránky Google Minesweeper v prohlížeči) a tlačítka na její stránce,
# která smí nástroj zmáčknout sám – odmítnutí nepovinných cookies (nikdy „Přijmout“) a „Hrát“.
_GAME_WINDOW = re.compile(r"minesweeper|hledání min|miny|google", re.I)
_GAME_PROMPT = re.compile(r"^\s*(odmítnout vše|reject all|hrát|play)(?:\s|$)", re.I)


def run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode, out.decode("utf-8", errors="ignore").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def ps(script: str, timeout: float = 60) -> tuple[int, str]:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
               timeout)


def ping(host: str) -> bool:
    cmd = ["ping", "-n", "1", "-w", "1500", host] if WIN else ["ping", "-c", "1", "-W", "2", host]
    return run(cmd, 5)[0] == 0


def tcp_ok(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dns_ok(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def http_ok(url: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Aiko diagnostika)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


WIN_REPAIR = r"""
$ErrorActionPreference = 'Continue'
$r = [ordered]@{ steps = @(); ok = $false }
ipconfig /flushdns | Out-Null; $r.steps += 'flushdns'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$r.admin = $admin
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
if ($route) { $a = Get-NetAdapter -InterfaceIndex $route.ifIndex -ErrorAction SilentlyContinue }
if (-not $a) { $a = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Sort-Object { $_.Status -ne 'Up' } | Select-Object -First 1 }
if ($a) { $r.adapter = $a.Name }
if ($admin -and $a) {
  if (__DNS__) { Set-DnsClientServerAddress -InterfaceIndex $a.ifIndex -ServerAddresses ('1.1.1.1','8.8.8.8'); $r.steps += 'fallback_dns' }
  Disable-NetAdapter -Name $a.Name -Confirm:$false; $r.steps += 'adapter_off'
  Start-Sleep -Seconds 5
  Enable-NetAdapter -Name $a.Name -Confirm:$false; $r.steps += 'adapter_on'
  for ($i = 0; $i -lt 30; $i++) {
    $ip = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254*' }
    if ($ip) { $r.ok = $true; $r.ip = $ip[0].IPAddress; break }
    Start-Sleep -Seconds 1
  }
} else {
  $r.ok = $true; $r.needs_admin = $true
}
ipconfig /flushdns | Out-Null
$r | ConvertTo-Json -Compress
"""


class PcAgent(DeviceAgent):
    kind = "pc"
    platform = f"{platform.system()} {platform.release()}"

    def __init__(self, *a: Any, dry_run: bool = False, **kw: Any):
        super().__init__(*a, **kw)
        self.dry_run = dry_run

    async def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {"dry_run": self.dry_run}
        try:
            # co je na počítači otevřené – porozumění pak ví, že „zprávy“ znamenají novinky v prohlížeči
            title = pc_gui.foreground_title()
            st["foreground"] = "" if is_blocked_window(title) else title[:100]
        except Exception:  # noqa: BLE001
            pass
        try:
            import psutil  # volitelné

            b = psutil.sensors_battery()
            if b:
                st["battery"] = int(b.percent)
                st["charging"] = bool(b.power_plugged)
        except Exception:  # noqa: BLE001
            pass
        return st

    # --- síť ------------------------------------------------------------------
    @tool("Zjistí stav sítě: adaptér, IP adresu, router, internet, překlad DNS a dostupnost stránky. Nic nemění.")
    def diagnose_network(self, url: str = "") -> dict[str, Any]:
        d: dict[str, Any] = {}
        if WIN:
            code, out = ps("(Get-NetAdapter | Where-Object Status -eq 'Up' | Measure-Object).Count; "
                           "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
                           "Sort-Object RouteMetric | Select-Object -First 1).NextHop", 20)
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            d["adapters_up"] = int(lines[0]) if lines and lines[0].isdigit() else None
            d["gateway"] = lines[1] if len(lines) > 1 else ""
        else:
            _, out = run(["ip", "-o", "link", "show", "up"])
            d["adapters_up"] = sum(1 for l in out.splitlines() if " lo:" not in l)
            _, out = run(["ip", "route", "show", "default"])
            m = re.search(r"via (\S+)", out)
            d["gateway"] = m.group(1) if m else ""
        d["ip"] = _local_ip()
        d["has_ip"] = bool(d["gateway"]) and not d["ip"].startswith(("127.", "169.254"))
        d["gateway_ok"] = ping(d["gateway"]) if d["gateway"] else False
        d["internet_ok"] = tcp_ok("1.1.1.1", 443) or tcp_ok("8.8.8.8", 53) or ping("1.1.1.1")
        host = urlparse(url).hostname if url else None
        d["dns_ok"] = dns_ok(host or "seznam.cz") and dns_ok("google.com")
        d["url"] = url
        d["url_ok"] = http_ok(url) if url and d["dns_ok"] and d["internet_ok"] else None
        if shutil.which("osqueryi"):
            code, out = run(["osqueryi", "--json", "select interface, address from interface_addresses "
                                                   "where address not like '%:%' and interface != 'lo';"], 15)
            if code == 0:
                try:
                    d["osquery_interfaces"] = json.loads(out)
                except ValueError:
                    pass
        if d["adapters_up"] == 0:
            v = "no_adapter"
        elif not d["has_ip"]:
            v = "no_ip"
        elif not d["internet_ok"]:
            v = "router" if not d["gateway_ok"] else "isp"
        elif not d["dns_ok"]:
            v = "dns"
        elif url and not d["url_ok"]:
            v = "site"
        else:
            v = "ok"
        d["verdict"] = v
        return d

    @tool("Atomická oprava sítě jako uzavřená lokální transakce: vyčistí DNS cache, volitelně nastaví záložní DNS, "
          "vypne síťovou kartu, po 5 s ji zapne a počká na IP adresu.")
    def atomic_network_repair(self, set_fallback_dns: bool = True) -> dict[str, Any]:
        if self.dry_run:
            log.warning("[dry-run] atomic_network_repair(set_fallback_dns=%s)", set_fallback_dns)
            time.sleep(2)
            return {"ok": True, "dry_run": True, "steps": ["flushdns", "fallback_dns", "adapter_off", "adapter_on"]}
        if WIN:
            code, out = ps(WIN_REPAIR.replace("__DNS__", "$true" if set_fallback_dns else "$false"), timeout=90)
            try:
                return json.loads(out.splitlines()[-1])
            except (ValueError, IndexError):
                raise RuntimeError(f"oprava sítě selhala: {out[-300:]}") from None
        steps = []
        if shutil.which("resolvectl"):
            run(["resolvectl", "flush-caches"])
            steps.append("flushdns")
        if shutil.which("nmcli"):
            if set_fallback_dns:
                _, con = run(["nmcli", "-t", "-f", "NAME", "con", "show", "--active"])
                name = con.splitlines()[0] if con else ""
                if name:
                    run(["nmcli", "con", "mod", name, "ipv4.dns", "1.1.1.1 8.8.8.8"])
                    steps.append("fallback_dns")
            run(["nmcli", "networking", "off"])
            steps.append("adapter_off")
            time.sleep(5)
            run(["nmcli", "networking", "on"])
            steps.append("adapter_on")
        ok = False
        for _ in range(30):
            _, out = run(["ip", "route", "show", "default"])
            if "via" in out:
                ok = True
                break
            time.sleep(1)
        return {"ok": ok, "steps": steps}

    @tool("Otevře webovou stránku v prohlížeči na obrazovce. Stránky pro vzdálený přístup a bankovnictví odmítne.")
    def open_and_fix_browser(self, url: str, task: str = "open") -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if is_blocked_url(url):
            raise ToolRefused("Tuhle stránku Aiko neotevírá.")
        if self.dry_run:
            log.warning("[dry-run] open %s (%s)", url, task)
            return {"opened": url, "dry_run": True}
        webbrowser.open(url, new=2)
        # Počkat, než se stránka načte a prohlížeč skočí do popředí – jinak agent na dalším snímku
        # vidí ještě starou stránku (v logu „Browser is open on Flaticon“ hned po otevření YouTube).
        deadline = time.time() + 8
        time.sleep(2.0)
        title = pc_gui.foreground_title()
        while time.time() < deadline and (not title or any(
                k in title.lower() for k in ("new tab", "nová karta", "loading", "načítání"))):
            time.sleep(0.5)
            title = pc_gui.foreground_title()
        return {"opened": url, "task": task, "window": title}

    # --- hra Hledání min ----------------------------------------------------------
    @tool("Hledání min (Minesweeper): otevře oficiální hru Google Minesweeper v prohlížeči (když ještě není "
          "vidět) a sama ji hraje – plochu čte ze snímku, kliká jen na jistě bezpečná políčka uvnitř hrací plochy "
          "a hádá jen, když to jinak nejde. Pohyb myší nebo přepnutí okna hru hned zastaví. Vrací status: won | "
          "lost | time_up | stopped_by_user | no_board | needs_guess.")
    def play_minesweeper(self, max_seconds: int = 60, open_game: bool = True,
                         allow_guess: bool = True) -> dict[str, Any]:
        import io

        from PIL import Image

        from ..common import minesweeper as ms

        if is_blocked_window(pc_gui.foreground_title()):
            raise ToolRefused("Na obrazovce je chráněná aplikace, hrát teď nebudu.")
        max_seconds = max(5, min(100, int(max_seconds)))
        state: dict[str, Any] = {"mon": (0, 0, 0, 0), "last": None, "title": ""}

        def capture() -> Any:
            png, mon = pc_gui.screenshot()
            state["mon"] = mon
            return Image.open(io.BytesIO(png))

        opened = False
        board = ms.read_board(capture())
        if board is None and open_game:
            self.open_and_fix_browser(ms.GAME_URL, task="minesweeper")
            opened = True
            deadline = time.time() + 15
            while board is None and time.time() < deadline:
                time.sleep(1.0)
                board = ms.read_board(capture())
                if board is None:
                    self._minesweeper_prompts(state["mon"])
        if board is None:
            return {"status": "no_board", "opened": opened, "url": ms.GAME_URL,
                    "say": ms.summary_cs({"status": "no_board"})}
        title = pc_gui.foreground_title()
        if is_system_window(title) or is_blocked_window(title) or not _GAME_WINDOW.search(title or ""):
            return {"status": "no_board", "opened": opened, "window": title[:80],
                    "error": "hrací plocha není v okně prohlížeče s hrou – neklikám"}
        if self.dry_run:
            return {"status": "dry_run", "board": f"{board.cols}x{board.rows}", "window": title[:80]}
        state["title"] = title

        def click(x: int, y: int) -> None:
            ox, oy = state["mon"][:2]
            X, Y = x + ox, y + oy
            pc_gui.click(X, Y, hover_s=0.0)
            state["last"] = (X, Y)

        def should_stop() -> bool:
            if pc_gui.foreground_title() != state["title"]:
                return True                          # senior přepnul okno – nehrát „naslepo“ jinam
            if state["last"] is None:
                return False
            import pyautogui

            px, py = pyautogui.position()
            lx, ly = state["last"]
            return abs(px - lx) > 40 or abs(py - ly) > 40   # myš převzal člověk

        res = ms.play(capture, click, max_seconds=max_seconds, allow_guess=allow_guess, should_stop=should_stop)
        res.update(opened=opened, url=ms.GAME_URL, say=ms.summary_cs(res))
        return res

    def _minesweeper_prompts(self, mon: tuple[int, int, int, int]) -> None:
        """Na stránce hry: odmítne nepovinné cookies (nikdy nepřijme) a zmáčkne „Hrát“, když tam je."""
        try:
            elements, _ = pc_gui.ui_snapshot(mon, budget_s=2.0, numbered=False)
        except Exception as e:  # noqa: BLE001
            log.info("Hledání min: prvky stránky nejde přečíst (%s)", e)
            return
        for e in elements:
            if _GAME_PROMPT.match(str(e.label)) and not is_risky_label(e.label):
                x, y = e.center
                log.info("Hledání min: klikám na „%s“", e.label)
                pc_gui.click(x + mon[0], y + mon[1])
                return

    # --- zvuk -------------------------------------------------------------------
    @tool("Otevře stránku Nastavení Windows (page: dark_mode, display, text_size, sound, bluetooth, wifi, "
          "background, mouse, printers, date_time, battery, notifications, language, accessibility). "
          "Jen otevře, nic nemění.")
    def open_settings(self, page: str) -> dict[str, Any]:
        from ..common.settings_pages import SETTINGS_PAGES

        uri = SETTINGS_PAGES.get(page, ("", "", ""))[0]
        if not uri:
            raise ToolRefused(f"Stránku nastavení „{page}“ Aiko neotevírá.")
        if self.dry_run:
            return {"opened": uri, "dry_run": True}
        if not WIN:
            raise RuntimeError("jen pro Windows")
        import os

        os.startfile(uri)                                   # noqa: S606 – pevný seznam ms-settings: adres
        time.sleep(2.0)
        return {"opened": uri, "window": pc_gui.foreground_title()}

    @tool("Vytvoří novou složku na ploše (where=desktop) nebo v Dokumentech (where=documents). Nic nemaže a "
          "nepřepisuje – když složka existuje, jen to řekne.")
    def create_folder(self, name: str, where: str = "desktop") -> dict[str, Any]:
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip().strip(".")[:80]
        if not clean or clean.upper().split(".")[0] in WIN_RESERVED:
            raise ToolRefused("Takový název složky nejde použít.")
        base = _known_folder(where)
        if base is None:
            raise ToolRefused("Složku Aiko vytváří jen na ploše nebo v Dokumentech.")
        path = base / clean
        if self.dry_run:
            return {"path": str(path), "dry_run": True}
        existed = path.exists()
        path.mkdir(parents=False, exist_ok=True)
        return {"path": str(path), "created": not existed, "existed": existed}

    # --- soubory (skill „soubory“) – pevné nástroje místo psaní příkazů -----------------------------
    def _folders(self) -> dict[str, Any]:
        return pc_files.known_folders_windows(
            (lambda name: _reg(r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", name))
            if WIN else None)

    @tool("Vypíše soubory ve složce, nejnovější nahoře. where: desktop | documents | downloads | pictures | "
          "music | videos, nebo podsložka (např. desktop/pejsek). days: jen změněné za posledních N dní "
          "(1 = dnes a včera, 0 = vše). kind: images | documents | videos | music. Nic nemění.")
    def list_files(self, where: str = "downloads", days: float = 0, kind: str = "", limit: int = 30) -> dict[str, Any]:
        try:
            return pc_files.list_files(pc_files.resolve(where, self._folders()), days, kind, limit)
        except pc_files.FileToolError as e:
            raise ToolRefused(str(e)) from e

    @tool("Najde soubory a složky podle části jména (bez diakritiky) ve všech uživatelských složkách, nebo jen "
          "v where. Vrátí jméno, kde je (where) a kdy se změnil. Nic nemění.")
    def find_files(self, name: str, where: str = "", days: float = 0) -> dict[str, Any]:
        try:
            return pc_files.find_files(name, self._folders(), where, days)
        except pc_files.FileToolError as e:
            raise ToolRefused(str(e)) from e

    @tool("Přesune (nebo zkopíruje, copy=true) soubory mezi uživatelskými složkami. names_json = JSON seznam "
          "přesných jmen z list_files, source a target jako where u list_files. Nic nepřepíše ani nesmaže; "
          "cílová složka musí existovat (create_folder).")
    def move_files(self, names_json: str, source: str = "downloads", target: str = "desktop",
                   copy: bool = False) -> dict[str, Any]:
        try:
            names = json.loads(names_json) if isinstance(names_json, str) else names_json
            folders = self._folders()
            src, dst = pc_files.resolve(source, folders), pc_files.resolve(target, folders)
            if self.dry_run:
                return {"dry_run": True, "names": names, "source": str(src), "target": str(dst)}
            return pc_files.move_files(names, src, dst, folders, copy=bool(copy))
        except (pc_files.FileToolError, ValueError) as e:
            raise ToolRefused(str(e)) from e

    @tool("Otevře složku v Průzkumníku, nebo soubor v jeho běžné aplikaci (fotka, PDF, dokument). where = složka, "
          "name = soubor v ní (prázdné = otevřít jen složku). Programy (.exe…) nikdy nespouští.")
    def open_item(self, name: str = "", where: str = "desktop") -> dict[str, Any]:
        try:
            folders = self._folders()
            path = pc_files.openable(pc_files.resolve(where, folders) / name if name else
                                     pc_files.resolve(where, folders), folders)
        except pc_files.FileToolError as e:
            raise ToolRefused(str(e)) from e
        if self.dry_run:
            return {"dry_run": True, "path": str(path)}
        if not WIN:
            raise RuntimeError("jen pro Windows")
        import os

        os.startfile(str(path))                            # noqa: S606 – jen ověřená cesta, žádný program
        time.sleep(2.0)
        return {"opened": path.name, "window": pc_gui.foreground_title()}

    @tool("Pošle dokument nebo fotku na výchozí tiskárnu (where + name jako u open_item). Před tiskem zjisti "
          "list_printers, jestli tiskárna není offline.")
    def print_file(self, name: str, where: str = "documents") -> dict[str, Any]:
        try:
            folders = self._folders()
            path = pc_files.printable(pc_files.resolve(where, folders) / name, folders)
        except pc_files.FileToolError as e:
            raise ToolRefused(str(e)) from e
        if self.dry_run:
            return {"dry_run": True, "path": str(path)}
        if not WIN:
            raise RuntimeError("jen pro Windows")
        import os

        os.startfile(str(path), "print")                   # noqa: S606
        return {"printing": path.name}

    # --- aplikace, tiskárny, stav počítače (pevné PowerShell skripty, model žádný příkaz nepíše) ------
    @tool("Spustí nainstalovanou aplikaci podle jména (např. „Word“, „HP Smart“, „Fotky“, „Kalkulačka“, "
          "„Chrome“). Když jméno nesedí, vrátí podobné aplikace. Konzole, registry a vzdálený přístup nespouští.")
    def start_app(self, name: str) -> dict[str, Any]:
        apps = self._start_apps()
        best, candidates = pick_app(name, apps)
        if not best:
            return {"started": False, "candidates": candidates}
        if forbidden_app(best["Name"]):
            raise ToolRefused(f"Aplikaci „{best['Name']}“ Aiko nespouští.")
        if self.dry_run:
            return {"dry_run": True, "app": best["Name"]}
        app_id = str(best["AppID"])
        if re.search(r'["&|<>^%\r\n]', app_id):
            raise ToolRefused("neplatná aplikace")
        run(["explorer.exe", f"shell:AppsFolder\\{app_id}"], timeout=15)
        time.sleep(3.0)
        return {"started": True, "app": best["Name"], "window": pc_gui.foreground_title()}

    def _start_apps(self) -> list[dict[str, Any]]:
        cached = getattr(self, "_apps_cache", None)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        if not WIN:
            return []
        code, out = ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                       "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=30)
        try:
            apps = json.loads(out) if code == 0 and out else []
        except ValueError:
            apps = []
        apps = [apps] if isinstance(apps, dict) else [a for a in apps if isinstance(a, dict)]
        self._apps_cache = (time.time(), apps)
        return apps

    @tool("Seznam tiskáren: jméno, jestli je výchozí a jestli je připravená / offline. Nic nemění.")
    def list_printers(self) -> dict[str, Any]:
        if not WIN:
            return {"printers": []}
        code, out = ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Printer | "
                       "Select-Object Name, Default, WorkOffline, PrinterStatus | ConvertTo-Json -Compress", timeout=30)
        try:
            rows = json.loads(out) if code == 0 and out else []
        except ValueError:
            rows = []
        rows = [rows] if isinstance(rows, dict) else rows
        state = {3: "připravená", 4: "tiskne", 5: "zahřívá se", 7: "offline"}
        return {"printers": [{"name": r.get("Name"), "default": bool(r.get("Default")),
                              "state": "offline" if r.get("WorkOffline") else state.get(r.get("PrinterStatus"),
                                                                                         "neznámý stav"),
                              "virtual": bool(re.search(r"pdf|xps|onenote|fax", str(r.get("Name")), re.I))}
                             for r in rows if isinstance(r, dict)]}

    @tool("Stav počítače pro „počítač je pomalý / plný“: volné místo na disku, paměť, baterie, jak dlouho běží "
          "bez restartu, kolik oken je otevřených. Nic nemění.")
    def pc_health(self) -> dict[str, Any]:
        import os

        info: dict[str, Any] = {}
        home = os.path.splitdrive(str(__import__("pathlib").Path.home()))[0] + "\\" if WIN else "/"
        du = shutil.disk_usage(home)
        info["disk_free_gb"] = round(du.free / 2**30, 1)
        info["disk_free_percent"] = round(100 * du.free / du.total)
        if WIN:
            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in ("t", "a", "tp", "ap", "tv", "av", "ae")]

            m = MEM()
            m.dwLength = ctypes.sizeof(MEM)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                info["memory_used_percent"] = int(m.dwMemoryLoad)

            class PWR(ctypes.Structure):
                _fields_ = [("ac", ctypes.c_byte), ("flag", ctypes.c_byte), ("percent", ctypes.c_byte),
                            ("saver", ctypes.c_byte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]

            p = PWR()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)) and p.flag != -128 and p.percent != -1:
                info["battery_percent"], info["on_charger"] = int(p.percent) & 0xFF, p.ac == 1
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            info["running_days"] = round(ctypes.windll.kernel32.GetTickCount64() / 86_400_000, 1)
            info["open_windows"] = len(pc_gui.open_windows())
        hints = []
        if info["disk_free_percent"] < 10:
            hints.append("disk je skoro plný")
        if info.get("memory_used_percent", 0) > 90:
            hints.append("paměť je plná – pomůže zavřít okna")
        if info.get("running_days", 0) > 7:
            hints.append("počítač běží přes týden bez restartu – doporučit seniorovi restart (Aiko ho nedělá)")
        info["hints"] = hints
        return info

    @tool("Zjistí hlasitost a ztlumení zvuku. Nic nemění.")
    def get_audio_state(self) -> dict[str, Any]:
        if WIN:
            vol = _pycaw()
            if vol is not None:
                return {"muted": bool(vol.GetMute()), "volume": round(vol.GetMasterVolumeLevelScalar() * 100)}
            return {"muted": None, "volume": None, "unknown": True}
        if shutil.which("pactl"):
            _, m = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
            _, v = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            pct = re.search(r"(\d+)%", v)
            return {"muted": "yes" in m.lower(), "volume": int(pct.group(1)) if pct else None}
        return {"muted": None, "volume": None, "unknown": True}

    @tool("Zruší ztlumení zvuku a nastaví hlasitost na 70 %.")
    def fix_sound(self) -> dict[str, Any]:
        if self.dry_run:
            return {"volume": 70, "dry_run": True}
        if WIN:
            vol = _pycaw()
            if vol is not None:
                vol.SetMute(0, None)
                vol.SetMasterVolumeLevelScalar(0.7, None)
            else:  # bez knihoven: multimediální klávesy (každý stisk = 2 %)
                import ctypes

                kb = ctypes.windll.user32.keybd_event
                for key, n in ((0xAE, 50), (0xAF, 35)):   # VOLUME_DOWN ×50, VOLUME_UP ×35
                    for _ in range(n):
                        kb(key, 0, 0, 0)
                        kb(key, 0, 2, 0)
            return {"volume": 70, "muted": False}
        run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
        run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "70%"])
        return {"volume": 70, "muted": False}

    # --- plocha -------------------------------------------------------------------------------
    @tool("Zobrazí (visible=true) nebo skryje ikony na ploše – totéž co Plocha → pravé tlačítko → "
          "Zobrazit → Zobrazit ikony na ploše, ale bez klikání. Vrátí, jestli jsou ikony vidět.")
    def set_desktop_icons(self, visible: bool = True) -> dict[str, Any]:
        if not WIN:
            raise RuntimeError("jen pro Windows")
        before = _desktop_icons_visible()
        if before == bool(visible):
            return {"visible": before, "changed": False}
        if self.dry_run:
            return {"visible": before, "changed": False, "dry_run": True}
        defview = _desktop_defview()
        if not defview:
            raise RuntimeError("plochu Windows se nepodařilo najít")
        ctypes.windll.user32.SendMessageW(defview, 0x0111, 0x7402, 0)   # WM_COMMAND „Zobrazit ikony na ploše“
        time.sleep(0.6)
        after = _desktop_icons_visible()
        return {"visible": after, "changed": after != before}

    # --- GUI agent (TDD v2): Observe → Act -------------------------------------------------
    @tool("Vyfotí obrazovku, očísluje klikatelné prvky aktivního okna (Set-of-Marks) a vrátí WebP, seznam prvků "
          "(i se stavem: zaškrtnuto, hodnota pole), viditelné texty a otevřená okna.")
    def observe_screen(self) -> dict[str, Any]:
        title = pc_gui.foreground_title()
        if is_blocked_window(title):
            return {"type": "SCREEN_OBSERVATION", "blocked": True, "active_app": title}
        png, mon = pc_gui.screenshot()
        elements, texts = pc_gui.ui_snapshot(mon, numbered=False)
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png))
        ocr = 0
        try:
            segments = pc_gui.ocr_segments(pc_gui.ocr_lines(image))
            merged = pc_gui.merge_ocr(elements, segments, focus=pc_gui.foreground_rect(mon))
            ocr, elements = len(merged) - len(elements), merged
        except Exception as e:  # noqa: BLE001 – OCR je pomoc navíc, snímek musí přijít vždy
            log.warning("OCR selhalo: %s", e)
        elements = som.number(elements, limit=120)
        webp, scale, size = som.render(image, elements)
        self._elements, self._scale, self._monitor = elements, scale, mon
        windows = [w for w in pc_gui.open_windows() if not is_blocked_window(w)]
        return {"type": "SCREEN_OBSERVATION", "device_id": self.device_id, "active_app": title,
                "screen_resolution": [mon[2], mon[3]], "image_size": list(size), "scale": scale,
                "screenshot_webp_base64": webp, "interactive_elements": [e.public(scale) for e in elements],
                "screen_text": texts, "open_windows": windows,
                "system_window": is_system_window(title), "ui_tree_ok": bool(elements), "ocr_elements": ocr}

    @tool("Popis počítače pro orientaci: verze Windows, jazyk, rozlišení a měřítko obrazovky, počet monitorů, "
          "výchozí prohlížeč. Nic nemění.")
    def system_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"os": f"{platform.system()} {platform.release()}", "version": platform.version(),
                                "monitors": pc_gui.monitor_count()}
        if WIN:
            info["ui_language"], info["keyboard"] = _ui_language(), _keyboard_layout()
            info["os"] = _windows_name() or info["os"]
            pc_gui.dpi_aware()
            user32 = ctypes.windll.user32
            info["screen"] = f"{user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}"
            try:
                info["display_scale_percent"] = round(ctypes.windll.shcore.GetScaleFactorForDevice(0))
            except (AttributeError, OSError):
                pass
            info["default_browser"] = _default_browser()
            info["dark_mode"] = _dark_mode()
        return info

    @tool("Přepne na otevřené okno podle části jeho titulku (např. „YouTube“, „Průzkumník“) a obnoví ho, "
          "je-li minimalizované. Spolehlivější než klikání na hlavní panel.")
    def focus_window(self, title: str) -> dict[str, Any]:
        if is_blocked_window(title) or is_system_window(title):
            raise ToolRefused("Na tohle okno Aiko nepřepíná.")
        if self.dry_run:
            return {"dry_run": True, "window": title}
        now = pc_gui.focus_window(title)
        if is_blocked_window(now):
            raise ToolRefused("V popředí je chráněné okno.")
        return {"window": now}

    @tool("Provede jednu akci: CLICK_ELEMENT, CLICK_XY, DOUBLE_CLICK, RIGHT_CLICK, TYPE_TEXT, SWIPE (scroll), "
          "KEY_PRESS (např. enter, esc, ctrl+l, alt+f4) nebo WAIT. Souřadnice jsou v pixelech posledního snímku.")
    def execute_action(self, action_type: str, target_element_id: int = 0, x: int = 0, y: int = 0,
                       text_to_type: str = "", direction: str = "", key: str = "",
                       package: str = "") -> dict[str, Any]:
        title = pc_gui.foreground_title()
        if is_blocked_window(title):
            raise ToolRefused("V tomhle okně Aiko nic nedělá.")
        a = action_type.upper()
        combo = re.sub(r"\s+", "", (key or "").lower())
        if a == "KEY_PRESS" and is_forbidden_key(key):
            raise ToolRefused("Tuhle klávesovou zkratku Aiko nepoužívá.")
        if a == "KEY_PRESS" and combo == "alt+f4" and (not title.strip() or title in ("Program Manager",)):
            # Alt+F4 na ploše otevře „Vypnout Windows“ – Enter by pak počítač vypnul
            raise ToolRefused("Alt+F4 na ploše by otevřelo vypnutí počítače. Nejdřív vyberte okno.")
        if is_system_window(title) and not (a == "KEY_PRESS" and combo in ("win+d", "alt+tab", "esc")) \
                and a != "WAIT":
            # konzole, registry, Správce úloh… – psaním by šlo spustit příkaz. Ani zavřít ne: v konzoli
            # často běží sám server Aiko. Smí jen přepnout jinam.
            raise ToolRefused(f"V systémovém okně „{title[:60]}“ Aiko nic nedělá. Přepni jinam: "
                              "key win+d, nebo tool focus_window.")
        if a == "TYPE_TEXT" and is_dangerous_text(text_to_type):
            raise ToolRefused("Spouštění systémových příkazů Aiko nepíše.")
        elements = getattr(self, "_elements", [])
        scale = getattr(self, "_scale", 1.0) or 1.0
        ox, oy = getattr(self, "_monitor", (0, 0, 0, 0))[:2]
        el = None
        if target_element_id:
            el = next((e for e in elements if e.id == int(target_element_id)), None)
            if el is None:
                raise ValueError(f"prvek [{target_element_id}] na posledním snímku není – udělej nový snímek")
        if el:
            px, py = el.center
        else:
            px, py = round(int(x) / scale), round(int(y) / scale)
            el = som.element_at(elements, px, py)
        if a in ("CLICK_ELEMENT", "CLICK_XY", "DOUBLE_CLICK", "RIGHT_CLICK", "LONG_PRESS") and el \
                and is_risky_label(el.label):
            raise ToolRefused(f"Na „{el.label}“ Aiko nikdy neklikne.")
        if a == "TYPE_TEXT" and ((el and el.password) or looks_like_code(text_to_type)):
            raise ToolRefused("Hesla, PINy a kódy Aiko nikdy nepíše.")
        if a == "KEY_PRESS":
            pc_gui.key_parts(key)                 # neznámá klávesa → chyba pro model, ne tichý „nic“
        if self.dry_run:
            log.warning("[dry-run] %s el=%s xy=%s text=%r key=%r", a, target_element_id, (px, py), text_to_type, key)
            return {"dry_run": True, "action": a}
        X, Y = px + ox, py + oy
        if a in ("CLICK_ELEMENT", "CLICK_XY", "LONG_PRESS"):
            pc_gui.click(X, Y)
        elif a == "DOUBLE_CLICK":
            pc_gui.click(X, Y, double=True)
        elif a == "RIGHT_CLICK":
            pc_gui.click(X, Y, right=True)
        elif a == "TYPE_TEXT":
            if el or x or y:
                pc_gui.click(X, Y)
                time.sleep(0.3)
            pc_gui.type_text(text_to_type)
        elif a == "SWIPE":
            pc_gui.scroll(direction or "down", (X, Y) if (el or x or y) else None)
        elif a == "KEY_PRESS":
            pc_gui.press(key)
        elif a == "WAIT":
            time.sleep(2)
        else:
            raise ValueError(f"akci {action_type} počítač neumí")
        # Verify až po dokreslení: kontextové menu a podmenu mají animaci ~0,3–0,5 s
        time.sleep(1.0 if a == "RIGHT_CLICK" or (el and "MenuItem" in el.label) else 0.6)
        return {"done": a, "at": [px, py], "label": el.label if el else ""}

    @tool("Makro-transakce: provede seznam akcí najednou lokálně (např. odpojit a připojit síť), "
          "aby se počítač neodpojil uprostřed. actions_json = JSON pole akcí jako u execute_action.")
    def run_macro(self, actions_json: str) -> dict[str, Any]:
        actions = json.loads(actions_json)
        if not isinstance(actions, list) or len(actions) > 10:
            raise ValueError("makro musí být seznam nejvýš 10 akcí")
        keys = {"action_type", "target_element_id", "x", "y", "text_to_type", "direction", "key", "package"}
        results = []
        for act in actions:
            if "wait_seconds" in act:
                time.sleep(min(15, float(act["wait_seconds"])))
                continue
            results.append(self.execute_action(**{k: v for k, v in act.items() if k in keys}))
        return {"results": results}

    @tool("Jen čtecí systémový příkaz pro diagnostiku: ipconfig, ping, nslookup, tracert, "
          "netsh wlan show interfaces, systeminfo, tasklist, getmac.")
    def shell_read(self, command: str) -> dict[str, Any]:
        cmd = command.strip()
        allow = ("ipconfig", "ping ", "nslookup ", "tracert ", "netsh wlan show", "systeminfo", "tasklist",
                 "getmac", "ip addr", "ip route", "nmcli dev", "df", "free")
        low = cmd.lower()
        # ipconfig /release|/renew|/registerdns a netsh … set/add/delete už něco mění
        if re.search(r"[;&|`$<>\n^%]", cmd) or not low.startswith(allow) or re.search(
                r"/(release|renew|registerdns|setclassid)|\b(set|add|delete|reset|connect|disconnect)\b", low):
            raise ToolRefused("Tenhle příkaz není mezi povolenými čtecími příkazy.")
        args = cmd.split()
        if args[0] == "ping" and not any(a in ("-n", "-c") for a in args):
            args[1:1] = ["-n" if WIN else "-c", "2"]
        code, out = run(args, timeout=30)
        return {"code": code, "output": out[-8000:]}


def _desktop_icons_visible() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced") as k:
            return int(winreg.QueryValueEx(k, "HideIcons")[0]) == 0
    except OSError:
        return True                                   # hodnota chybí = výchozí stav = ikony vidět


def _desktop_defview() -> int:
    """Okno SHELLDLL_DefView plochy – je pod Progman, nebo (s tapetou) pod jedním z oken WorkerW."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.FindWindowExW.restype = wintypes.HWND
    progman = user32.FindWindowW("Progman", None)
    dv = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None) if progman else None
    worker = None
    while not dv:
        worker = user32.FindWindowExW(None, worker, "WorkerW", None)
        if not worker:
            break
        dv = user32.FindWindowExW(worker, None, "SHELLDLL_DefView", None)
    return dv or 0


def _reg(path: str, name: str, root: str = "HKCU") -> Any:
    import winreg

    try:
        with winreg.OpenKey(getattr(winreg, f"HKEY_{'CURRENT_USER' if root == 'HKCU' else 'LOCAL_MACHINE'}"),
                            path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _windows_name() -> str:
    cur = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    name, build = _reg(cur, "ProductName", "HKLM"), _reg(cur, "CurrentBuildNumber", "HKLM")
    if not name:
        return ""
    if build and str(build).isdigit() and int(build) >= 22000:
        name = str(name).replace("Windows 10", "Windows 11")    # Windows 11 se v registru hlásí jako 10
    return f"{name} (build {build}, {_reg(cur, 'DisplayVersion', 'HKLM') or ''})".replace(", )", ")")


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _known_folder(where: str):  # noqa: ANN202
    """Skutečná cesta k ploše / Dokumentům (i když je přesměrovaná do OneDrive)."""
    import os
    from pathlib import Path

    key = {"desktop": "Desktop", "plocha": "Desktop", "documents": "Personal", "dokumenty": "Personal"}.get(
        (where or "desktop").lower())
    if not key:
        return None
    if WIN:
        raw = _reg(r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", key)
        if raw:
            return Path(os.path.expandvars(str(raw)))
    return Path.home() / ("Desktop" if key == "Desktop" else "Documents")


def _lcid_name(lcid: int) -> str:
    buf = ctypes.create_unicode_buffer(85)
    try:
        return buf.value if ctypes.windll.kernel32.LCIDToLocaleName(lcid, buf, 85, 0) else ""
    except (AttributeError, OSError):
        return ""


def _ui_language() -> str:
    """Jazyk menu a tlačítek Windows (např. cs-CZ) – podle něj model hledá „Nastavení“, nebo „Settings“."""
    try:
        return _lcid_name(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except (AttributeError, OSError):
        return ""


def _keyboard_layout() -> str:
    try:
        user32 = ctypes.windll.user32
        thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        return _lcid_name(user32.GetKeyboardLayout(thread) & 0xFFFF)
    except (AttributeError, OSError):
        return ""


def _default_browser() -> str:
    prog = str(_reg(r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
                    "ProgId") or "")
    for key, name in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge"), ("firefox", "Mozilla Firefox"),
                      ("opera", "Opera"), ("brave", "Brave"), ("seznam", "Seznam prohlížeč")):
        if key in prog.lower():
            return name
    return prog


def _dark_mode() -> bool | None:
    v = _reg(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme")
    return None if v is None else not bool(v)


_FORBIDDEN_APP = re.compile(
    r"powershell|\bpwsh\b|\bcmd\b|command prompt|příkazový|prikazovy|terminal|regedit|registr|registry|"
    r"správce úloh|spravce uloh|task manager|nástroje systému|windows tools|správa počítače|computer management|"
    r"\bspustit\b|^run$|zabezpečení windows|windows security|bitlocker|vývojář|developer|"
    r"visual studio|wsl\b|ubuntu|git bash|hyper-v|virtualbox|vmware", re.I)


def forbidden_app(name: str) -> bool:
    """Aplikace, které agent nespouští: konzole a systémové nástroje (jako u oken) + banky a vzdálený přístup."""
    return bool(_FORBIDDEN_APP.search(name or "")) or is_blocked_window(name)


def pick_app(name: str, apps: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Nejlepší shoda jména aplikace z nabídky Start (bez diakritiky; „word“ → „Word“, ne „WordPad“ dřív)."""
    import unicodedata

    def plain(s: str) -> str:
        t = unicodedata.normalize("NFKD", str(s).lower())
        return "".join(c for c in t if not unicodedata.combining(c)).strip()

    want = plain(name)
    words = set(re.findall(r"\w+", want))
    if not want:
        return None, []
    scored = []
    for a in apps:
        n = plain(a.get("Name", ""))
        aw = set(re.findall(r"\w+", n))
        if n == want:
            s = 100
        elif words and words <= aw:
            s = 80 - len(aw - words)                  # „word“ ⊂ {word} před {wordpad}… a před delšími názvy
        elif n.startswith(want):
            s = 60 - len(n)
        elif want in n:
            s = 40 - len(n) / 10
        else:
            s = 10 * len(words & aw)
        if s > 0:
            scored.append((s, a))
    scored.sort(key=lambda t: t[0], reverse=True)
    names = [a.get("Name", "") for _s, a in scored[:6] if not forbidden_app(a.get("Name", ""))]
    best = scored[0][1] if scored and scored[0][0] >= 40 else None
    if best is None and want in APP_SYNONYMS:
        # Windows v jiném jazyce: senior řekne „kalkulačka“, v nabídce Start je „Calculator“ (a naopak)
        for alt in APP_SYNONYMS[want]:
            alt_best, alt_names = pick_app(alt, apps)
            if alt_best:
                return alt_best, alt_names
    return best, names


# Vestavěné aplikace Windows česky ↔ anglicky (bez diakritiky, oba směry)
_APP_PAIRS = [("kalkulacka", "calculator"), ("nastaveni", "settings"), ("fotky", "photos"),
              ("poznamkovy blok", "notepad"), ("malovani", "paint"), ("pocasi", "weather"),
              ("kalendar", "calendar"), ("posta", "mail"), ("hodiny", "clock"), ("budik", "clock"),
              ("mapy", "maps"), ("pruzkumnik souboru", "file explorer"), ("pruzkumnik", "file explorer"),
              ("kamera", "camera"), ("microsoft store", "obchod"), ("prehravac medii", "media player"),
              ("nastroj pro vystrizky", "snipping tool"), ("vystrizky", "snipping tool"), ("lupa", "magnifier"),
              ("klavesnice na obrazovce", "on-screen keyboard"), ("zaznam zvuku", "sound recorder")]
APP_SYNONYMS: dict[str, list[str]] = {}
for _cz, _en in _APP_PAIRS:
    APP_SYNONYMS.setdefault(_cz, []).append(_en)
    APP_SYNONYMS.setdefault(_en, []).append(_cz)


def is_blocked_window(title: str) -> bool:
    return bool(re.search(r"bank|banka|anydesk|teamviewer|rustdesk|bitwarden|1password|keepass|lastpass|"
                          r"správce hesel|password manager", (title or "").lower()))


def _local_ip() -> str:
    from ..common.discovery import local_ip

    return local_ip()


def _pycaw():  # noqa: ANN202
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        dev = AudioUtilities.GetSpeakers()
        return dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    p = base_argparser("Aiko – MCP démon pro počítač")
    p.add_argument("--dry-run", action="store_true", help="nic neměnit, jen vypsat")
    a = p.parse_args()
    setup_logging(a.verbose)
    agent = PcAgent(a.name or f"Počítač {socket.gethostname()}", device_id=a.device_id,
                    server_url=a.server, token=a.token, dry_run=a.dry_run)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("Démon počítače ukončen.")


if __name__ == "__main__":
    main()
