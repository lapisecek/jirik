"""Simulovaná zařízení pro demo a testy (bez telefonu a bez zásahu do PC).

    python -m aiko.agents.mock                 # telefon i počítač
    python -m aiko.agents.mock --kind phone --in-call

Mají stejné nástroje jako skutečné démony (android.py, pc.py) a výchozí
stav s typickými potížemi seniorů: vypnutý WhatsApp, ztlumený zvuk,
malé písmo, rozbité DNS na počítači.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from ..common.agent_base import DeviceAgent, setup_logging
from ..common.protocol import ToolRefused, ToolSpec, tool
from ..common.safety import is_blocked_package, is_blocked_url

log = logging.getLogger("aiko.mock")

# Simulované obrazovky pro GUI agenta: název -> [(popisek, bounds, druh, kam vede klik)]
SCREENS: dict[str, dict[str, list[tuple[str, tuple[int, int, int, int], str, str]]]] = {
    "phone": {
        "home": [("Fotky", (60, 300, 260, 500), "button", "photos"),
                 ("Nastavení", (300, 300, 500, 500), "button", "settings"),
                 ("Obchod Play", (540, 300, 740, 500), "button", "store")],
        "popup": [("Gratulujeme! Vyhrál jste iPhone", (80, 600, 1000, 900), "button", "popup"),
                  ("Zaplatit poštovné 99 Kč", (200, 1000, 880, 1120), "button", "popup"),
                  ("Zavřít ✕", (880, 520, 1000, 600), "button", "home")],
        "settings": [("Displej", (0, 300, 1080, 420), "button", "display"),
                     ("Zvuk a vibrace", (0, 420, 1080, 540), "button", "settings"),
                     ("Aplikace", (0, 540, 1080, 660), "button", "settings")],
        "display": [("Velikost písma", (0, 300, 1080, 420), "button", "display"),
                    ("Tmavý motiv (vypnuto)", (0, 420, 1080, 540), "switch", "display")],
        "photos": [("Hledat ve fotkách", (60, 150, 1020, 260), "input", "photos")],
        "store": [("Hledat aplikace", (60, 150, 1020, 260), "input", "store")],
    },
    "pc": {
        "desktop": [("Google Chrome", (40, 40, 140, 140), "button", "browser"),
                    ("Start (Button)", (900, 1040, 960, 1080), "button", "desktop")],
        "browser": [("Adresní řádek (Edit)", (200, 60, 1700, 100), "input", "browser"),
                    ("Zavřít (Button)", (1860, 0, 1920, 40), "button", "desktop")],
    },
}


# Viditelné texty mimo tlačítka (nadpisy, obsah zpráv) – jako screen_text ze skutečných démonů
SCREEN_TEXT: dict[str, list[str]] = {
    "popup": ["Váš telefon je napaden virem! Okamžitě zaplaťte."],
    "display": ["Displej"],
}


class MockScreen:
    """Simulovaná obrazovka se Set-of-Marks pro testy GUI agenta."""

    screen_kind = "phone"
    size = (1080, 2400)

    def _init_screen(self, start: str) -> None:
        self.screen = start
        self.typed: list[str] = []
        self.gui_log: list[str] = []
        self._elements: list = []

    def _current(self) -> list:
        from ..common import som

        return [som.Element(i + 1, lbl, b, kind) for i, (lbl, b, kind, _) in
                enumerate(SCREENS[self.screen_kind][self.screen])]

    @tool("Vyfotí obrazovku, očísluje klikatelné prvky (Set-of-Marks) a vrátí WebP snímek a seznam prvků.")
    async def observe_screen(self) -> dict[str, Any]:
        from PIL import Image, ImageDraw

        from ..common import som

        els = self._current()
        img = Image.new("RGB", self.size, (240, 240, 240))
        d = ImageDraw.Draw(img)
        for e in els:
            d.rectangle(e.bounds, outline=(80, 80, 80), width=3)
            d.text((e.bounds[0] + 10, e.bounds[1] + 10), e.label, fill=(0, 0, 0))
        webp, scale, size = som.render(img, els, max_side=800)
        self._elements, self._scale = els, scale
        return {"type": "SCREEN_OBSERVATION", "device_id": self.device_id, "active_app": self.screen,
                "screen_resolution": list(self.size), "image_size": list(size), "scale": scale,
                "screenshot_webp_base64": webp, "interactive_elements": [e.public(scale) for e in els],
                "screen_text": list(SCREEN_TEXT.get(self.screen, [])), "ui_tree_ok": True}

    @tool("Popis zařízení pro orientaci. Nic nemění.")
    async def system_info(self) -> dict[str, Any]:
        if self.screen_kind == "phone":
            return {"android": "14 (simulace)", "maker": "Mock", "language": "cs-CZ", "screen": "1080x2400"}
        return {"os": "Windows 11 (simulace)", "language": "cs_CZ", "screen": "1920x1080",
                "default_browser": "Google Chrome"}

    @tool("Provede jednu akci na obrazovce.")
    async def execute_action(self, action_type: str, target_element_id: int = 0, x: int = 0, y: int = 0,
                             text_to_type: str = "", direction: str = "", key: str = "",
                             package: str = "") -> dict[str, Any]:
        from ..common import som
        from ..common.safety import is_risky_label, looks_like_code

        a = action_type.upper()
        el = next((e for e in self._elements if e.id == int(target_element_id or 0)), None)
        if el is None and (x or y):
            el = som.element_at(self._elements, round(x / self._scale), round(y / self._scale))
        if a in ("CLICK_ELEMENT", "CLICK_XY", "LONG_PRESS", "DOUBLE_CLICK"):
            if el is None:
                raise ValueError("na tomto místě nic není")
            if is_risky_label(el.label):
                raise ToolRefused(f"Na „{el.label}“ Aiko nikdy neklikne.")
            self.screen = SCREENS[self.screen_kind][self.screen][el.id - 1][3]
        elif a == "TYPE_TEXT":
            if looks_like_code(text_to_type):
                raise ToolRefused("Kódy a PINy Aiko nikdy nepíše.")
            self.typed.append(text_to_type)
        elif a == "KEY_PRESS" and key.lower() in ("back", "home", "esc"):
            self.screen = "home" if self.screen_kind == "phone" else "desktop"
        self.gui_log.append(f"{a} {el.label if el else key or text_to_type}")
        return {"done": a, "label": el.label if el else ""}

    @tool("Makro-transakce: seznam akcí najednou.")
    async def run_macro(self, actions_json: str) -> dict[str, Any]:
        import json

        res = []
        for act in json.loads(actions_json):
            if "action_type" in act:
                res.append(await self.execute_action(**act))
        return {"results": res}

    @tool("Jen čtecí systémový příkaz pro diagnostiku.")
    async def shell_read(self, command: str) -> dict[str, Any]:
        return {"output": f"(simulace) {command}: OK"}


class MockPhone(MockScreen, DeviceAgent):
    kind = "phone"
    platform = "android (simulace)"

    def __init__(self, *a: Any, in_call: bool = False, delay: float = 0.3, **kw: Any):
        kw.setdefault("device_id", "phone-mock")
        super().__init__(*a, **kw)
        self.delay = delay
        self.in_call = in_call
        self.apps: dict[str, str] = {           # balíček -> stav
            "com.whatsapp": "disabled",
            "com.google.android.apps.photos": "ok",
            "com.google.android.youtube": "archived",
        }
        self.ring, self.music, self.ringer_mode, self.dnd = 0, 2, "silent", True
        self.font_scale = 1.0
        self.battery = 34
        self.airplane, self.wifi = True, False
        self.foreground = "com.sec.android.app.launcher"
        self.actions: list[str] = []
        self._init_screen("popup")      # na displeji visí podvodná reklama

    async def _work(self) -> None:
        await asyncio.sleep(self.delay)

    async def status(self) -> dict[str, Any]:
        return {"reachable": True, "battery": self.battery, "charging": False, "in_call": self.in_call}

    async def guard(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        pkg = args.get("package_name") or args.get("apk_or_id") or ""
        if is_blocked_package(pkg):
            raise ToolRefused("Do chráněných aplikací Aiko nesahá.")
        if spec.name in ("get_ui_tree", "take_screenshot") and is_blocked_package(self.foreground):
            raise ToolRefused("Na obrazovce je chráněná aplikace, nic nečtu.")
        await super().guard(spec, args)

    @tool("Zjistí stav aplikace. Nic nemění.")
    async def check_app_state(self, package_name: str) -> dict[str, Any]:
        await self._work()
        state = self.apps.get(package_name, "missing")
        return {"package": package_name, "installed": state not in ("missing", "hidden"),
                "enabled": state == "ok", "archived": state == "archived", "hidden": state == "hidden",
                "launchable": state == "ok", "state": state}

    @tool("Otevře aplikaci.")
    async def launch_app(self, package_name: str) -> dict[str, Any]:
        await self._work()
        if self.apps.get(package_name) != "ok":
            raise RuntimeError("aplikaci se nepodařilo otevřít")
        self.foreground = package_name
        self.actions.append(f"launch {package_name}")
        return {"launched": package_name}

    @tool("Otevře webovou adresu.")
    async def open_url(self, url: str) -> dict[str, Any]:
        if is_blocked_url(url):
            raise ToolRefused("Tuhle stránku Aiko neotevírá.")
        self.actions.append(f"open_url {url}")
        return {"opened": url}

    @tool("Seznam aplikací, které jdou otevřít z plochy. Nic nemění.")
    async def list_apps(self) -> dict[str, Any]:
        return {"apps": sorted(p for p, st in self.apps.items() if st != "missing" and not is_blocked_package(p))}

    @tool("Obnoví aplikaci a otevře ji.")
    async def restore_and_launch_app(self, package_name: str) -> dict[str, Any]:
        await self._work()
        before = self.apps.get(package_name, "missing")
        if before == "missing":
            raise RuntimeError("aplikace není nainstalovaná")
        self.apps[package_name] = "ok"
        self.actions.append(f"restore {package_name}")
        await self.launch_app(package_name)
        return {"state_before": before, "steps": ["restore", "launch"]}

    @tool("Doinstaluje aplikaci nebo otevře Obchod Play.")
    async def install_package_silent(self, apk_or_id: str) -> dict[str, Any]:
        await self._work()
        self.actions.append(f"store {apk_or_id}")
        return {"mode": "store"}

    @tool("Stav připojení. Nic nemění.")
    async def get_connectivity_state(self) -> dict[str, Any]:
        await self._work()
        return {"airplane": self.airplane, "wifi_enabled": self.wifi, "data_enabled": False,
                "internet_ok": self.wifi and not self.airplane}

    @tool("Zapne internet.")
    async def reset_connectivity(self, keep_wifi: bool = True) -> dict[str, Any]:
        await self._work()
        self.airplane, self.wifi = False, True
        self.actions.append("reset_connectivity")
        return {"internet_ok": True}

    @tool("Stav zvuku. Nic nemění.")
    async def get_audio_state(self) -> dict[str, Any]:
        await self._work()
        return {"ring": self.ring, "ring_max": 7, "music": self.music, "music_max": 15,
                "ringer_mode": self.ringer_mode, "dnd": self.dnd}

    @tool("Zapne a zesílí zvuk.")
    async def set_volume_normal(self) -> dict[str, Any]:
        await self._work()
        self.ring, self.music, self.ringer_mode, self.dnd = 6, 10, "normal", False
        self.actions.append("set_volume_normal")
        return {"set": {"2": 6, "3": 10}}

    @tool("Otevře stránku Nastavení.")
    async def open_settings(self, page: str) -> dict[str, Any]:
        self.actions.append(f"settings {page}")
        self.screen = "display" if page in ("dark_mode", "display") else "settings"
        return {"opened": page}

    @tool("Velikost písma. Nic nemění.")
    async def get_font_scale(self) -> dict[str, Any]:
        return {"scale": self.font_scale}

    @tool("Nastaví velikost písma.")
    async def set_font_scale(self, scale: float) -> dict[str, Any]:
        self.font_scale = min(1.5, max(0.85, float(scale)))
        self.actions.append(f"font {self.font_scale}")
        return {"scale": self.font_scale}

    @tool("Stav baterie. Nic nemění.")
    async def get_battery(self) -> dict[str, Any]:
        return {"level": self.battery, "charging": False, "power_save": False}

    @tool("Otevře chat ve WhatsAppu s textem.")
    async def open_whatsapp_chat(self, phone: str, text: str) -> dict[str, Any]:
        await self._work()
        self.actions.append(f"whatsapp {phone}")
        return {"opened": True}

    PHONEBOOK = [
        {"name": "Lenka Nováková", "phone": "+420 777 100 200", "type": "2"},
        {"name": "Lenka Svobodová", "phone": "+420 777 300 400", "type": "2"},
        {"name": "Polyfráček Mi", "phone": "+420 722 000 111", "type": "2"},
        {"name": "Adam Dvořák", "phone": "+420 603 555 666", "type": "2"},
        {"name": "Adam Dvořák", "phone": "+420 2 2222 3333", "type": "3"},
        {"name": "Adam Kučera", "phone": "+420 604 777 888", "type": "2"},
        {"name": "Zubař Novotný", "phone": "+420 2 4444 5555", "type": "3"},
    ]

    @tool("Najde v kontaktech telefonu lidi podle jména. Nic nemění.")
    async def find_contacts(self, query: str) -> dict[str, Any]:
        from ..common import contacts

        matches = contacts.match(query, self.PHONEBOOK)
        out: dict[str, Any] = {"matches": matches, "searched": len(self.PHONEBOOK)}
        if not matches:
            out["suggestions"] = contacts.suggest(query, self.PHONEBOOK)
        return out

    @tool("Otevře Zprávy s předvyplněnou SMS.")
    async def open_sms_draft(self, phone: str, text: str) -> dict[str, Any]:
        await self._work()
        self.actions.append(f"sms {phone}")
        return {"opened": True}

    @tool("Zavolá na číslo.")
    async def call_number(self, phone: str) -> dict[str, Any]:
        await self._work()
        self.actions.append(f"call {phone}")
        return {"calling": True}

    @tool("XML strom obrazovky.")
    async def get_ui_tree(self) -> dict[str, Any]:
        return {"xml": "<hierarchy/>"}

    @tool("Snímek obrazovky.")
    async def take_screenshot(self) -> dict[str, Any]:
        return {"png_base64": ""}


class MockPc(MockScreen, DeviceAgent):
    kind = "pc"
    platform = "Windows (simulace)"
    screen_kind = "pc"
    size = (1920, 1080)

    def __init__(self, *a: Any, delay: float = 0.3, **kw: Any):
        kw.setdefault("device_id", "pc-mock")
        super().__init__(*a, **kw)
        self.delay = delay
        self.dns_broken = True
        self.muted = True
        self.opened: list[str] = []
        self.actions: list[str] = []
        self._init_screen("desktop")

    @tool("Diagnostika sítě. Nic nemění.")
    async def diagnose_network(self, url: str = "") -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        return {"adapters_up": 1, "has_ip": True, "gateway": "192.168.1.1", "gateway_ok": True,
                "internet_ok": True, "dns_ok": not self.dns_broken, "url": url,
                "url_ok": None if self.dns_broken else True, "verdict": "dns" if self.dns_broken else "ok"}

    @tool("Atomická oprava sítě.")
    async def atomic_network_repair(self, set_fallback_dns: bool = True) -> dict[str, Any]:
        await asyncio.sleep(self.delay * 3)
        self.dns_broken = False
        self.actions.append("atomic_network_repair")
        return {"ok": True, "steps": ["flushdns", "fallback_dns", "adapter_off", "adapter_on"]}

    @tool("Otevře stránku v prohlížeči.")
    async def open_and_fix_browser(self, url: str, task: str = "open") -> dict[str, Any]:
        if is_blocked_url(url):
            raise ToolRefused("Tuhle stránku Aiko neotevírá.")
        self.opened.append(url)
        return {"opened": url}

    @tool("Hledání min: otevře hru Google Minesweeper a hraje ji (simulace – hraje se skutečná hra v paměti).")
    async def play_minesweeper(self, max_seconds: int = 60, open_game: bool = True,
                               allow_guess: bool = True) -> dict[str, Any]:
        from ..common import minesweeper as ms

        self.minesweeper = getattr(self, "minesweeper", None) or ms.SimGame(10, 8, seed=7)
        if open_game:
            self.opened.append(ms.GAME_URL)
        game = self.minesweeper
        res = await asyncio.to_thread(ms.play, game.render, game.click, max_seconds=float(max_seconds),
                                      allow_guess=allow_guess, settle=0, sleep=lambda _s: None)
        self.actions.append(f"minesweeper {res['status']}")
        return {**res, "opened": open_game, "url": ms.GAME_URL, "say": ms.summary_cs(res)}

    @tool("Stav zvuku. Nic nemění.")
    async def get_audio_state(self) -> dict[str, Any]:
        return {"muted": self.muted, "volume": 10 if self.muted else 70}

    @tool("Zobrazí nebo skryje ikony na ploše.")
    async def set_desktop_icons(self, visible: bool = True) -> dict[str, Any]:
        changed = getattr(self, "icons_visible", True) != bool(visible)
        self.icons_visible = bool(visible)
        self.actions.append(f"desktop_icons {visible}")
        return {"visible": self.icons_visible, "changed": changed}

    @tool("Přepne na otevřené okno podle části titulku.")
    async def focus_window(self, title: str) -> dict[str, Any]:
        self.actions.append(f"focus {title}")
        if "chrome" in title.lower() or "prohlížeč" in title.lower():
            self.screen = "browser"
        return {"window": self.screen}

    @tool("Vytvoří novou složku na ploše nebo v Dokumentech.")
    async def create_folder(self, name: str, where: str = "desktop") -> dict[str, Any]:
        self.actions.append(f"create_folder {where}/{name}")
        return {"path": f"C:/Users/senior/{where}/{name}", "created": True}

    @tool("Otevře stránku Nastavení Windows.")
    async def open_settings(self, page: str) -> dict[str, Any]:
        self.actions.append(f"settings {page}")
        return {"opened": page}

    # soubory, aplikace, tiskárny – simulace (skutečné chování testuje tests/test_skills.py na dočasných složkách)
    @tool("Vypíše soubory ve složce. Nic nemění.")
    async def list_files(self, where: str = "downloads", days: float = 0, kind: str = "",
                         limit: int = 30) -> dict[str, Any]:
        self.actions.append(f"list_files {where}")
        files = [{"name": "faktura.pdf", "kind": "dokument", "modified": "dnes 09:12"},
                 {"name": "pejsek.jpg", "kind": "obrázek", "modified": "dnes 08:40"}] if where == "downloads" else []
        return {"folder": where, "count": len(files), "files": files}

    @tool("Najde soubory podle jména. Nic nemění.")
    async def find_files(self, name: str, where: str = "", days: float = 0) -> dict[str, Any]:
        self.actions.append(f"find_files {name}")
        return {"query": name, "count": 0, "files": []}

    @tool("Přesune soubory mezi uživatelskými složkami.")
    async def move_files(self, names_json: str, source: str = "downloads", target: str = "desktop",
                         copy: bool = False) -> dict[str, Any]:
        self.actions.append(f"move_files {source}->{target} {names_json}")
        return {"action": "moved", "done": [{"name": n, "to": n} for n in json.loads(names_json)], "skipped": []}

    @tool("Otevře složku nebo soubor.")
    async def open_item(self, name: str = "", where: str = "desktop") -> dict[str, Any]:
        self.actions.append(f"open_item {where}/{name}")
        return {"opened": name or where}

    @tool("Vytiskne soubor.")
    async def print_file(self, name: str, where: str = "documents") -> dict[str, Any]:
        self.actions.append(f"print_file {where}/{name}")
        return {"printing": name}

    @tool("Spustí aplikaci podle jména.")
    async def start_app(self, name: str) -> dict[str, Any]:
        self.actions.append(f"start_app {name}")
        return {"started": True, "app": name}

    @tool("Seznam tiskáren. Nic nemění.")
    async def list_printers(self) -> dict[str, Any]:
        return {"printers": [{"name": "HP DeskJet", "default": True, "state": "připravená", "virtual": False}]}

    @tool("Stav počítače. Nic nemění.")
    async def pc_health(self) -> dict[str, Any]:
        return {"disk_free_percent": 40, "hints": []}

    @tool("Zapne zvuk.")
    async def fix_sound(self) -> dict[str, Any]:
        self.muted = False
        self.actions.append("fix_sound")
        return {"volume": 70, "muted": False}


def main() -> None:
    p = argparse.ArgumentParser(description="Aiko – simulovaná zařízení")
    p.add_argument("--server", help="ws://host:8765/ws/device (výchozí mDNS)")
    p.add_argument("--token", default="")
    p.add_argument("--kind", choices=["phone", "pc", "both"], default="both")
    p.add_argument("--in-call", action="store_true", help="telefon má probíhající hovor")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    setup_logging(a.verbose)
    agents = []
    if a.kind in ("phone", "both"):
        agents.append(MockPhone("Telefon babičky (simulace)", server_url=a.server, token=a.token, in_call=a.in_call))
    if a.kind in ("pc", "both"):
        agents.append(MockPc("Počítač v obýváku (simulace)", server_url=a.server, token=a.token))

    async def run_all() -> None:
        await asyncio.gather(*(ag.run() for ag in agents))

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print("Simulace ukončena.")


if __name__ == "__main__":
    main()
