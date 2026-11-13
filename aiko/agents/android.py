"""MCP démon pro Android telefon přes (bezdrátové) ADB.

    python -m aiko.agents.android --serial 192.168.1.50:5555 --name "Telefon babičky"
    python -m aiko.agents.android --all          # pro každé zařízení z `adb devices`

Fáze 1–2 z TDD: telefon se spáruje přes Bezdrátové ladění (Android 11+),
démon běží na serveru (nebo jiném PC v LAN) a ovládá telefon přes ADB.
Žádné AI ani těžký proces na telefonu – baterie ho nezabije.

Vrstva 1 – atomické systémové příkazy (pm, settings, cmd, am): 90 % případů.
Vrstva 2 – strukturální UI strom (uiautomator dump): jen když je potřeba.
Vrstva 3 – screenshot: poslední záchrana; nic se neukládá.

Hardcoded pojistky: blocklist bank, hesel a vzdáleného přístupu, žádné
změny během hovoru, žádné snímky z chráněných aplikací.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..common import contacts, som
from ..common.agent_base import DeviceAgent, base_argparser, setup_logging
from ..common.protocol import ToolRefused, ToolSpec, tool
from ..common.safety import is_blocked_package, is_blocked_url, is_risky_label, looks_like_code, valid_package

log = logging.getLogger("aiko.android")


class AdbError(RuntimeError):
    pass


ANDROID_KEYS = {"back": 4, "home": 3, "enter": 66, "recent": 187, "delete": 67, "tab": 61,
                "volume_up": 24, "volume_down": 25, "menu": 82}
MACRO_KEYS = {"action_type", "target_element_id", "x", "y", "text_to_type", "direction", "key", "package"}
# Jen čtení. „cmd connectivity“ tu dřív bylo, ale umí i airplane-mode enable (a odpojit tak bezdrátové ADB).
SHELL_ALLOW = ("dumpsys ", "getprop", "settings get ", "settings list ", "pm list ", "logcat -d", "ping -c",
               "ip addr", "df", "wm size", "wm density")
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_CONTACT_ROW = re.compile(r"display_name=(.*?), data1=(.*?), data2=(\S*)\s*$", re.MULTILINE)


def _adb_text(text: str) -> str:
    """`input text` umí jen ASCII – diakritika se odstraní, mezery jsou %s."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c) and ord(c) < 128 and c not in "'\"\\`")
    return t.replace(" ", "%s")


def parse_ui(xml: str) -> list[som.Element]:
    """Z `uiautomator dump` vybere klikatelné prvky a pole pro psaní."""
    return parse_screen(xml)[0]


def parse_screen(xml: str, text_limit: int = 40) -> tuple[list[som.Element], list[str], bool]:
    """(klikatelné prvky, viditelné texty mimo prvky, jde obrazovka posouvat?).

    Texty (nadpisy, obsah zpráv, hlášky) dostane model jako seznam – z malého zmenšeného snímku
    je lokální model špatně přečte. Posouvání říká, že pod okrajem je další obsah.
    """
    start = xml.find("<?xml")
    if start < 0 or "<hierarchy" not in xml:
        return [], [], False
    try:
        root = ET.fromstring(xml[start:xml.rfind(">") + 1])
    except ET.ParseError:
        return [], [], False

    def texts(node: ET.Element) -> list[str]:
        out = []
        for n in node.iter("node"):
            t = (n.get("text") or n.get("content-desc") or "").strip()
            if t and t not in out:
                out.append(t)
            if len(out) >= 2:
                break
        return out

    elements = []
    plain: list[tuple[int, int, str]] = []
    scrollable = False
    for n in root.iter("node"):
        cls = n.get("class", "")
        m = _BOUNDS.match(n.get("bounds", ""))
        if n.get("scrollable") == "true":
            scrollable = True
        interactive = (n.get("clickable") == "true" or n.get("long-clickable") == "true"
                       or n.get("checkable") == "true" or cls.endswith(("EditText", "Switch")))
        if not interactive or n.get("enabled") == "false":
            t = " ".join((n.get("text") or n.get("content-desc") or "").split())
            if m and len(t) >= 2:
                x1, y1, x2, y2 = map(int, m.groups())
                if x2 - x1 > 4 and y2 - y1 > 4:
                    plain.append((y1, x1, t[:160]))
            continue
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        label = " · ".join(texts(n)) or (n.get("resource-id") or "").split("/")[-1] or cls.split(".")[-1]
        kind = "input" if cls.endswith("EditText") else "switch" if n.get("checkable") == "true" else "button"
        if kind == "switch":
            label += " (zapnuto)" if n.get("checked") == "true" else " (vypnuto)"
        if n.get("selected") == "true":
            label += " (vybráno)"
        if n.get("focused") == "true" and kind == "input":
            label += " (kurzor)"
        elements.append(som.Element(0, label, (x1, y1, x2, y2), kind, n.get("password") == "true"))
    numbered = som.number(elements)
    labels = " ".join(e.label.lower() for e in numbered)
    seen: set[str] = set()
    screen_text = []
    for _y, _x, t in sorted(plain):                            # shora dolů, jak se čte
        if t.lower() in seen or t.lower() in labels:
            continue
        seen.add(t.lower())
        screen_text.append(t)
        if len(screen_text) >= text_limit:
            break
    return numbered, screen_text, scrollable


def _digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) < 9:
        raise ValueError("neplatné telefonní číslo")
    return d


class AndroidAgent(DeviceAgent):
    kind = "phone"
    platform = "android"

    def __init__(self, serial: str, *a: Any, **kw: Any):
        self.serial = serial
        kw.setdefault("device_id", "phone-" + re.sub(r"[^A-Za-z0-9]+", "-", serial).strip("-").lower())
        super().__init__(*a, **kw)
        self._reachable = False

    # --- ADB ------------------------------------------------------------------
    async def adb_raw(self, *args: str, timeout: float = 20) -> bytes:
        proc = await asyncio.create_subprocess_exec("adb", "-s", self.serial, *args,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise AdbError(f"adb {' '.join(args[:2])}: timeout") from None
        if proc.returncode != 0:
            msg = (err or out).decode(errors="ignore").strip()
            if "not found" in msg or "offline" in msg:
                self._reachable = False
                await self._reconnect()
            raise AdbError(msg or f"adb skončilo s kódem {proc.returncode}")
        self._reachable = True
        return out

    async def sh(self, cmd: str, timeout: float = 20) -> str:
        return (await self.adb_raw("shell", cmd, timeout=timeout)).decode(errors="ignore").strip()

    async def _reconnect(self) -> None:
        if ":" in self.serial:
            proc = await asyncio.create_subprocess_exec("adb", "connect", self.serial,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()

    async def foreground_package(self) -> str:
        out = await self.sh("dumpsys activity activities | grep -E 'ResumedActivity' | head -n 1")
        m = re.search(r"\s([A-Za-z0-9_.]+)/", out)
        return m.group(1) if m else ""

    async def wake_and_check_lock(self) -> dict[str, bool]:
        """Rozsvítí displej a zkusí odsunout zámek bez PINu. PIN/heslo Aiko nikdy nezadává."""
        power = await self.sh("dumpsys power | grep -E 'mWakefulness='")
        if "Awake" not in power:
            await self.sh("input keyevent 224")          # KEYCODE_WAKEUP
            await asyncio.sleep(0.8)
        lock = await self.sh("dumpsys window | grep -E 'isKeyguardShowing|mDreamingLockscreen'")
        if "isKeyguardShowing=true" in lock or "mDreamingLockscreen=true" in lock:
            await self.sh("wm dismiss-keyguard")          # projde jen u zámku bez zabezpečení
            await asyncio.sleep(0.8)
            lock = await self.sh("dumpsys window | grep -E 'isKeyguardShowing'")
        return {"woke": "Awake" not in power, "locked": "isKeyguardShowing=true" in lock}

    async def in_call(self) -> bool:
        out = await self.sh("dumpsys telephony.registry | grep mCallState")
        return any(v != "0" for v in re.findall(r"mCallState=(\d+)", out))

    # --- pojistky ---------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        try:
            out = await self.sh("dumpsys battery", timeout=8)
            level = re.search(r"level: (\d+)", out)
            fg = await self.foreground_package()           # co je v telefonu otevřené (kontext pro porozumění)
            return {"reachable": True, "battery": int(level.group(1)) if level else None,
                    "charging": bool(re.search(r"(AC|USB|Wireless) powered: true", out)),
                    "in_call": await self.in_call(), "serial": self.serial,
                    "foreground": "" if is_blocked_package(fg) else fg}
        except (AdbError, OSError) as e:
            return {"reachable": False, "error": str(e)[:120], "serial": self.serial}

    async def guard(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        pkg = args.get("package_name") or args.get("apk_or_id") or ""
        if pkg and not pkg.endswith(".apk") and not valid_package(pkg):
            raise ToolRefused("neplatný název balíčku")
        if is_blocked_package(pkg):
            raise ToolRefused("Do chráněných aplikací (banky, hesla, vzdálený přístup) Aiko nesahá.")
        if spec.name in ("get_ui_tree", "take_screenshot"):
            if is_blocked_package(await self.foreground_package()):
                raise ToolRefused("Na obrazovce je chráněná aplikace, nic nečtu.")
        if spec.level >= 1 and await self.in_call():
            raise ToolRefused("Během hovoru nic neměním.")

    # --- aplikace -----------------------------------------------------------------
    @tool("Během 0,1 s zjistí, zda je aplikace nainstalovaná, vypnutá, archivovaná nebo skrytá. Nic nemění.")
    async def check_app_state(self, package_name: str) -> dict[str, Any]:
        pkg = package_name

        async def listed(flags: str) -> bool:
            out = await self.sh(f"pm list packages {flags} {pkg}")
            return f"package:{pkg}" in out.split()

        installed = await listed("")
        disabled = installed and await listed("-d")
        known = await listed("-u")
        hidden = known and not installed
        archived = False
        if installed:
            dump = await self.sh(f"dumpsys package {pkg} | grep -i -E 'archiv' | head -n 5")
            archived = "true" in dump.lower()
        launch = ""
        if installed and not disabled:
            launch = await self.sh(f"cmd package resolve-activity --brief "
                                   f"-a android.intent.action.MAIN -c android.intent.category.LAUNCHER {pkg}")
        state = ("hidden" if hidden else "missing" if not installed else "archived" if archived
                 else "disabled" if disabled else "ok")
        return {"package": pkg, "installed": installed, "enabled": installed and not disabled,
                "archived": archived, "hidden": hidden, "launchable": f"{pkg}/" in launch, "state": state}

    @tool("Otevře aplikaci.")
    async def launch_app(self, package_name: str) -> dict[str, Any]:
        out = await self.sh(f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1")
        if "Events injected: 1" not in out:
            raise AdbError("aplikaci se nepodařilo otevřít")
        return {"launched": package_name}

    @tool("Obnoví aplikaci (zapne vypnutou, vrátí skrytou, probudí archivovanou) a rovnou ji otevře.")
    async def restore_and_launch_app(self, package_name: str) -> dict[str, Any]:
        pkg = package_name
        st = await self.check_app_state(pkg)
        steps = []
        if st["state"] == "missing":
            raise AdbError("aplikace není nainstalovaná")
        if st["hidden"]:
            await self.sh(f"cmd package install-existing {pkg}")
            steps.append("install-existing")
        if st["state"] == "disabled":
            await self.sh(f"pm enable {pkg}")
            steps.append("enable")
        if st["archived"]:
            out = await self.sh(f"pm request-unarchive {pkg}")
            steps.append("request-unarchive")
            if "Unknown" in out or "Error" in out:
                raise AdbError("archivovanou aplikaci se nepodařilo probudit")
            await asyncio.sleep(8)
        await self.launch_app(pkg)
        steps.append("launch")
        return {"state_before": st["state"], "steps": steps}

    @tool("Doinstaluje aplikaci. S cestou k APK nainstaluje přímo, jinak otevře její stránku v Obchodě Play.")
    async def install_package_silent(self, apk_or_id: str) -> dict[str, Any]:
        if apk_or_id.endswith(".apk"):
            if not Path(apk_or_id).is_file():
                raise AdbError("APK soubor neexistuje")
            out = (await self.adb_raw("install", "-r", apk_or_id, timeout=180)).decode(errors="ignore")
            if "Success" not in out:
                raise AdbError(out[-200:])
            return {"mode": "installed"}
        await self.sh(f"am start -a android.intent.action.VIEW -d 'market://details?id={apk_or_id}'")
        return {"mode": "store"}

    # --- připojení ------------------------------------------------------------------
    @tool("Zjistí stav připojení: režim letadlo, Wi-Fi, mobilní data a jestli funguje internet. Nic nemění.")
    async def get_connectivity_state(self) -> dict[str, Any]:
        return {
            "airplane": (await self.sh("settings get global airplane_mode_on")) == "1",
            "wifi_enabled": (await self.sh("settings get global wifi_on")) not in ("0", ""),
            "data_enabled": (await self.sh("settings get global mobile_data")) == "1",
            "internet_ok": await self._internet(),
        }

    async def _internet(self) -> bool:
        out = await self.sh("ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 && echo OK || echo FAIL")
        return out.endswith("OK")

    @tool("Atomicky vypne režim letadlo, zapne Wi-Fi a data a otestuje spojení.")
    async def reset_connectivity(self, keep_wifi: bool = True) -> dict[str, Any]:
        await self.sh("cmd connectivity airplane-mode disable || settings put global airplane_mode_on 0")
        await self.sh("svc wifi enable")
        await self.sh("svc data enable")
        for _ in range(10):
            if await self._internet():
                return {"internet_ok": True}
            await asyncio.sleep(2)
        return {"internet_ok": False}

    # --- zvuk -------------------------------------------------------------------------
    async def _volume(self, stream: int) -> tuple[int, int]:
        out = await self.sh(f"cmd media_session volume --stream {stream} --get 2>/dev/null || "
                            f"media volume --stream {stream} --get")
        m = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", out)
        return (int(m.group(1)), int(m.group(3))) if m else (0, 0)

    @tool("Zjistí hlasitost vyzvánění a médií, režim vyzvánění a Nerušit. Nic nemění.")
    async def get_audio_state(self) -> dict[str, Any]:
        ring, ring_max = await self._volume(2)
        music, music_max = await self._volume(3)
        mode = await self.sh("settings get global mode_ringer")
        zen = await self.sh("settings get global zen_mode")
        return {"ring": ring, "ring_max": ring_max, "music": music, "music_max": music_max,
                "ringer_mode": {"0": "silent", "1": "vibrate"}.get(mode, "normal"),
                "dnd": zen not in ("0", "", "null")}

    @tool("Vypne Nerušit, přepne vyzvánění na normální a nastaví hlasitost nahlas.")
    async def set_volume_normal(self) -> dict[str, Any]:
        await self.sh("cmd notification set_dnd off")
        await self.sh("cmd audio set-ringer-mode NORMAL 2>/dev/null || true")
        result = {}
        for stream, share in ((2, 0.85), (5, 0.85), (3, 0.7)):
            _, mx = await self._volume(stream)
            if mx:
                val = max(1, round(mx * share))
                await self.sh(f"cmd media_session volume --stream {stream} --set {val} 2>/dev/null || "
                              f"media volume --stream {stream} --set {val}")
                result[str(stream)] = val
        return {"set": result}

    # --- displej, baterie --------------------------------------------------------------
    @tool("Otevře stránku Nastavení (page: dark_mode, display, text_size, sound, bluetooth, wifi, date_time, "
          "battery, notifications, language, accessibility). Jen otevře, nic nemění.")
    async def open_settings(self, page: str) -> dict[str, Any]:
        from ..common.settings_pages import SETTINGS_PAGES

        action = SETTINGS_PAGES.get(page, ("", "", ""))[1]
        if not action:
            raise ToolRefused(f"Stránku nastavení „{page}“ Aiko neotevírá.")
        await self.sh(f"am start -a {action}")
        await asyncio.sleep(1.5)
        return {"opened": action}

    @tool("Zjistí velikost písma. Nic nemění.")
    async def get_font_scale(self) -> dict[str, Any]:
        out = await self.sh("settings get system font_scale")
        try:
            return {"scale": float(out)}
        except ValueError:
            return {"scale": 1.0}

    @tool("Nastaví velikost písma (0,85 až 1,5).")
    async def set_font_scale(self, scale: float) -> dict[str, Any]:
        s = min(1.5, max(0.85, float(scale)))
        await self.sh(f"settings put system font_scale {s}")
        return {"scale": s}

    @tool("Zjistí stav baterie a úsporného režimu. Nic nemění.")
    async def get_battery(self) -> dict[str, Any]:
        out = await self.sh("dumpsys battery")
        level = re.search(r"level: (\d+)", out)
        return {"level": int(level.group(1)) if level else 0,
                "charging": bool(re.search(r"(AC|USB|Wireless) powered: true", out)),
                "power_save": (await self.sh("settings get global low_power")) == "1"}

    # --- komunikace (jen důvěryhodné kontakty, vybírá server) ---------------------------
    @tool("Otevře chat ve WhatsAppu s předvyplněným textem. Odeslat musí senior sám.")
    async def open_whatsapp_chat(self, phone: str, text: str) -> dict[str, Any]:
        url = f"https://wa.me/{_digits(phone)}?text={quote(text[:500], safe='')}"
        await self.sh(f"am start -a android.intent.action.VIEW -d '{url}'")
        return {"opened": True}

    @tool("Otevře webovou adresu (https) – YouTube odkaz otevře v aplikaci YouTube, ostatní v prohlížeči. "
          "Stránky pro vzdálený přístup a bankovnictví odmítne.")
    async def open_url(self, url: str) -> dict[str, Any]:
        if not url.startswith("https://") or not re.fullmatch(r"https://[\w.-]+(/[\w\-./?=&%+:~#@]*)?", url):
            raise ToolRefused("Tuhle adresu Aiko neotevře.")
        if is_blocked_url(url):
            raise ToolRefused("Tuhle stránku Aiko neotevírá.")
        await self.sh(f"am start -a android.intent.action.VIEW -d '{url}'")
        await asyncio.sleep(2)
        return {"opened": url, "app": await self.foreground_package()}

    @tool("Najde v kontaktech telefonu lidi podle jména (i v jiném pádě, např. Lence → Lenka). "
          "Vrátí jen shody, ne celý adresář. Nic nemění.")
    async def find_contacts(self, query: str) -> dict[str, Any]:
        out = await self.sh("content query --uri content://com.android.contacts/data/phones "
                            "--projection display_name:data1:data2", timeout=30)
        rows = [{"name": m.group(1).strip(), "phone": m.group(2).strip(), "type": m.group(3)}
                for m in _CONTACT_ROW.finditer(out)]
        matches = contacts.match(query, rows)
        out: dict[str, Any] = {"matches": matches, "searched": len(rows)}
        if not matches:                       # aspoň podobná jména – senior uslyší „Myslíte …?“, ne ticho
            out["suggestions"] = contacts.suggest(query, rows)
        return out

    @tool("Otevře aplikaci Zprávy s předvyplněnou SMS. Odeslat musí senior sám.")
    async def open_sms_draft(self, phone: str, text: str) -> dict[str, Any]:
        num = ("+" if phone.strip().startswith("+") else "") + _digits(phone)
        body = re.sub(r"['\"\\`$]", "", text)[:500]
        await self.sh(f"am start -a android.intent.action.SENDTO -d sms:{num} --es sms_body '{body}'")
        return {"opened": True}

    @tool("Zavolá na telefonní číslo.")
    async def call_number(self, phone: str) -> dict[str, Any]:
        num = ("+" if phone.strip().startswith("+") else "") + _digits(phone)
        await self.sh(f"am start -a android.intent.action.CALL -d tel:{num}")
        return {"calling": True}

    # --- vrstva 2 a 3 ---------------------------------------------------------------------
    @tool("Vrátí XML strom prvků na obrazovce (pár kB). Neplatí pro chráněné aplikace.")
    async def get_ui_tree(self) -> dict[str, Any]:
        xml = await self.sh("uiautomator dump /sdcard/aiko_ui.xml >/dev/null && cat /sdcard/aiko_ui.xml; "
                            "rm -f /sdcard/aiko_ui.xml", timeout=30)
        return {"xml": xml[:200_000]}

    @tool("Snímek obrazovky jako PNG (base64). Nikam se neukládá. Neplatí pro chráněné aplikace.")
    async def take_screenshot(self) -> dict[str, Any]:
        png = await self.adb_raw("exec-out", "screencap", "-p", timeout=30)
        return {"png_base64": base64.b64encode(png).decode()}

    # --- GUI agent (TDD v2): Observe → Act ----------------------------------------------
    @tool("Vyfotí obrazovku, očísluje klikatelné prvky (Set-of-Marks) a vrátí WebP snímek a seznam prvků.")
    async def observe_screen(self) -> dict[str, Any]:
        lock = await self.wake_and_check_lock()
        if lock["locked"]:
            return {"type": "SCREEN_OBSERVATION", "locked": True,
                    "note": "Telefon je zamčený PINem nebo gestem – odemknout musí senior."}
        fg = await self.foreground_package()
        if is_blocked_package(fg):
            return {"type": "SCREEN_OBSERVATION", "blocked": True, "active_app": fg}
        png, xml = await asyncio.gather(self.adb_raw("exec-out", "screencap", "-p", timeout=30), self._dump_ui())
        elements, texts, scrollable = parse_screen(xml)
        webp, scale, size = som.render(png, elements)
        self._elements, self._scale = elements, scale
        from PIL import Image
        import io
        w, h = Image.open(io.BytesIO(png)).size
        return {"type": "SCREEN_OBSERVATION", "device_id": self.device_id, "active_app": fg,
                "screen_resolution": [w, h], "image_size": list(size), "scale": scale,
                "screenshot_webp_base64": webp,
                "interactive_elements": [e.public(scale) for e in elements],
                "screen_text": texts, "scrollable": scrollable,
                "ui_tree_ok": bool(elements)}

    async def _dump_ui(self) -> str:
        """uiautomator při animaci hlásí „could not get idle state“ a vrátí prázdno – zkusí to ještě jednou."""
        cmd = ("uiautomator dump /sdcard/aiko_ui.xml >/dev/null 2>&1; cat /sdcard/aiko_ui.xml; "
               "rm -f /sdcard/aiko_ui.xml")
        xml = ""
        for attempt in range(2):
            try:
                xml = await self.sh(cmd, timeout=30)
            except AdbError as e:
                log.debug("uiautomator dump: %s", e)
            if "<hierarchy" in xml:
                return xml
            await asyncio.sleep(0.6)
        return xml

    @tool("Seznam aplikací, které jdou otevřít z plochy (názvy balíčků pro open_app). Chráněné aplikace "
          "(banky, hesla) v seznamu nejsou. Nic nemění.")
    async def list_apps(self) -> dict[str, Any]:
        out = await self.sh("cmd package query-activities --brief -a android.intent.action.MAIN "
                            "-c android.intent.category.LAUNCHER", timeout=20)
        pkgs = sorted({m.group(1) for m in re.finditer(r"^\s*([A-Za-z][\w.]+)/", out, re.MULTILINE)})
        if not pkgs:                                              # starší Android: jen balíčky třetích stran
            out = await self.sh("pm list packages -3")
            pkgs = sorted(l.split(":", 1)[1] for l in out.splitlines() if l.startswith("package:"))
        return {"apps": [p for p in pkgs if not is_blocked_package(p)][:150]}

    @tool("Popis telefonu pro orientaci: verze Androidu, výrobce a model, jazyk, velikost obrazovky, "
          "velikost písma, tmavý režim. Nic nemění.")
    async def system_info(self) -> dict[str, Any]:
        props = await self.sh("getprop ro.build.version.release; getprop ro.product.manufacturer; "
                              "getprop ro.product.model; getprop persist.sys.locale; getprop ro.product.locale")
        p = (props.splitlines() + [""] * 5)[:5]
        w, h = await self._screen_size()
        night = await self.sh("cmd uimode night 2>/dev/null")
        font = await self.get_font_scale()
        return {"android": p[0], "maker": p[1], "model": p[2], "language": p[3] or p[4],
                "screen": f"{w}x{h}", "font_scale": font["scale"],
                "dark_mode": True if "yes" in night.lower() else False if "no" in night.lower() else None}

    @tool("Provede jednu akci na obrazovce: CLICK_ELEMENT, CLICK_XY, LONG_PRESS, TYPE_TEXT, SWIPE, "
          "KEY_PRESS, OPEN_APP nebo WAIT. Souřadnice x, y jsou v pixelech posledního snímku.")
    async def execute_action(self, action_type: str, target_element_id: int = 0, x: int = 0, y: int = 0,
                             text_to_type: str = "", direction: str = "", key: str = "",
                             package: str = "") -> dict[str, Any]:
        fg = await self.foreground_package()
        if is_blocked_package(fg):
            raise ToolRefused("Na obrazovce je chráněná aplikace, nic nedělám.")
        a = action_type.upper()
        elements = getattr(self, "_elements", [])
        scale = getattr(self, "_scale", 1.0) or 1.0
        el = None
        if target_element_id:
            el = next((e for e in elements if e.id == int(target_element_id)), None)
            if el is None:
                raise ValueError(f"prvek [{target_element_id}] na posledním snímku není – udělej nový snímek")
        if a == "RIGHT_CLICK":                    # telefon nemá pravé tlačítko – nejbližší je dlouhý stisk
            a = "LONG_PRESS"
        if a in ("CLICK_ELEMENT", "CLICK_XY", "LONG_PRESS", "DOUBLE_CLICK"):
            if el is None:
                px, py = round(int(x) / scale), round(int(y) / scale)
                el = som.element_at(elements, px, py)
            else:
                px, py = el.center
            if el and is_risky_label(el.label):
                raise ToolRefused(f"Na „{el.label}“ Aiko nikdy neklikne.")
            if a == "LONG_PRESS":
                await self.sh(f"input swipe {px} {py} {px} {py} 800")
            elif a == "DOUBLE_CLICK":
                await self.sh(f"input tap {px} {py} && input tap {px} {py}")
            else:
                await self.sh(f"input tap {px} {py}")
            result: dict[str, Any] = {"tapped": [px, py], "label": el.label if el else ""}
        elif a == "TYPE_TEXT":
            if el and el.password:
                raise ToolRefused("Do pole pro heslo Aiko nic nepíše.")
            if looks_like_code(text_to_type):
                raise ToolRefused("Kódy a PINy Aiko nikdy nepíše.")
            if el:
                await self.sh(f"input tap {el.center[0]} {el.center[1]}")
                await asyncio.sleep(0.4)
            await self.sh(f"input text '{_adb_text(text_to_type)}'")
            result = {"typed": True, "ascii_only": _adb_text(text_to_type) != text_to_type.replace(" ", "%s")}
        elif a == "SWIPE":
            w, h = await self._screen_size()
            cx, cy = w // 2, h // 2
            d = (direction or "down").lower()
            vec = {"down": (cx, int(h * .7), cx, int(h * .3)), "up": (cx, int(h * .3), cx, int(h * .7)),
                   "left": (int(w * .8), cy, int(w * .2), cy), "right": (int(w * .2), cy, int(w * .8), cy)}
            x1, y1, x2, y2 = vec.get(d, vec["down"])
            await self.sh(f"input swipe {x1} {y1} {x2} {y2} 350")
            result = {"swiped": d}
        elif a == "KEY_PRESS":
            code = ANDROID_KEYS.get((key or "").lower())
            if code is None:
                raise ValueError(f"neznámá klávesa {key}, použij: {', '.join(ANDROID_KEYS)}")
            await self.sh(f"input keyevent {code}")
            result = {"key": key}
        elif a == "OPEN_APP":
            if not valid_package(package) or is_blocked_package(package):
                raise ToolRefused("Tuhle aplikaci Aiko neotevře.")
            await self.launch_app(package)
            result = {"opened": package}
        elif a == "WAIT":
            await asyncio.sleep(2)
            result = {"waited": 2}
        else:
            raise ValueError(f"neznámá akce {action_type}")
        await asyncio.sleep(0.7)   # Verify: krátce počkat, než se obrazovka překreslí
        return result

    @tool("Makro-transakce: provede seznam akcí najednou lokálně (např. vypnout a zapnout Wi-Fi), "
          "aby se zařízení neodpojilo uprostřed. actions_json = JSON pole akcí jako u execute_action.")
    async def run_macro(self, actions_json: str) -> dict[str, Any]:
        actions = json.loads(actions_json)
        if not isinstance(actions, list) or len(actions) > 10:
            raise ValueError("makro musí být seznam nejvýš 10 akcí")
        results = []
        for act in actions:
            if "wait_seconds" in act:
                await asyncio.sleep(min(15, float(act["wait_seconds"])))
                continue
            args = {k: v for k, v in act.items() if k in MACRO_KEYS}
            results.append(await self.execute_action(**args))
        return {"results": results}

    @tool("Jen čtecí systémový příkaz pro diagnostiku (dumpsys, getprop, settings get, pm list, "
          "logcat -d, ping -c, ip addr, df).")
    async def shell_read(self, command: str) -> dict[str, Any]:
        cmd = command.strip()
        # --noredact by vypsal plný obsah notifikací (i ověřovací kódy z SMS)
        if re.search(r"[;&|`$<>\n\\]", cmd) or not cmd.startswith(SHELL_ALLOW) or "noredact" in cmd:
            raise ToolRefused("Tenhle příkaz není mezi povolenými čtecími příkazy.")
        if cmd.startswith("logcat") and " -t " not in f" {cmd} ":
            cmd += " -t 200"
        out = await self.sh(cmd, timeout=20)
        return {"output": out[-8000:]}

    async def _screen_size(self) -> tuple[int, int]:
        out = await self.sh("wm size")
        m = re.findall(r"(\d+)x(\d+)", out)
        return (int(m[-1][0]), int(m[-1][1])) if m else (1080, 2400)


async def _list_devices() -> list[str]:
    proc = await asyncio.create_subprocess_exec("adb", "devices", stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return [l.split()[0] for l in out.decode().splitlines()[1:] if l.strip().endswith("device")]


async def _main(a) -> None:  # noqa: ANN001
    if a.all:
        serials = await _unique_phones(await _list_devices())
    else:
        await AndroidAgent(a.serial, "probe")._reconnect()
        available = await _list_devices()
        if a.serial not in available:
            seen = "\n  ".join(available) or "(žádné – připojte telefon, viz docs/NAVOD.md)"
            raise SystemExit(f"ADB nezná zařízení '{a.serial}'. Zařízení z `adb devices`:\n  {seen}\n"
                             f"Použijte jedno z nich v --serial, nebo --all.")
        serials = [a.serial]
    if not serials:
        raise SystemExit("Žádný telefon. Připojte ho (viz `adb devices`) a použijte --serial nebo --all.")
    agents = []
    for i, s in enumerate(serials):
        name = a.name if a.name and len(serials) == 1 else (a.name or "Telefon") + (f" {i + 1}" if len(serials) > 1 else "")
        # stabilní ID podle hardwaru – port bezdrátového ladění se po restartu mění
        did = (a.device_id if len(serials) == 1 else None) or f"phone-{(await _hw_serial(s)).lower()}"
        ag = AndroidAgent(s, name, server_url=a.server, token=a.token, device_id=did)
        agents.append(ag.run())
    log.info("Ovládám: %s", ", ".join(serials))
    await asyncio.gather(*agents)


async def _unique_phones(serials: list[str]) -> list[str]:
    """Jeden telefon bývá v `adb devices` dvakrát (IP:PORT i jméno z mDNS) – vezmeme ho jednou."""
    seen: dict[str, str] = {}
    for s in sorted(serials, key=lambda x: "._adb" in x):   # IP:PORT má přednost
        seen.setdefault(await _hw_serial(s), s)
    return list(seen.values())


async def _hw_serial(serial: str) -> str:
    proc = await asyncio.create_subprocess_exec("adb", "-s", serial, "shell", "getprop", "ro.serialno",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    hw = re.sub(r"[^A-Za-z0-9]+", "", out.decode(errors="ignore"))
    return hw or re.sub(r"[^A-Za-z0-9]+", "-", serial).strip("-")


def main() -> None:
    p = base_argparser("Aiko – MCP démon pro Android přes ADB")
    p.add_argument("--serial", help="ADB serial nebo IP:PORT (bezdrátové ladění)")
    p.add_argument("--all", action="store_true", help="všechna zařízení z `adb devices`")
    a = p.parse_args()
    setup_logging(a.verbose)
    if not shutil.which("adb"):
        raise SystemExit("Chybí adb (Android platform-tools) v PATH.")
    if not a.serial and not a.all:
        p.error("zadejte --serial nebo --all")
    try:
        asyncio.run(_main(a))
    except KeyboardInterrupt:
        print("Démon telefonu ukončen.")


if __name__ == "__main__":
    main()
