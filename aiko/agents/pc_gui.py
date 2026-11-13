"""Oči a ruce GUI agenta na počítači: mss (snímek), pywinauto/UIA (prvky), pyautogui + SendInput (myš, klávesy).

Volá se z vlákna (`asyncio.to_thread`) – všechno je synchronní.
Bezpečnostní pojistka pyautogui: rychlé najetí myší do rohu obrazovky akci přeruší.
"""
from __future__ import annotations

import ctypes
import logging
import re
import sys
import time

from ..common import som

log = logging.getLogger("aiko.pc.gui")
WIN = sys.platform.startswith("win")

INTERACTIVE = {"Button", "Edit", "Hyperlink", "MenuItem", "ListItem", "TabItem", "CheckBox", "ComboBox",
               "RadioButton", "TreeItem", "SplitButton", "DataItem"}

_dpi_done = False


def dpi_aware() -> None:
    """Souřadnice mss, UIA i pyautogui musí být ve fyzických pixelech každého monitoru.

    Nastavení platí jen jednou za proces a vyhraje první volání: `import pyautogui` samo volá
    SetProcessDPIAware (jen „systémové“ DPI) a pywinauto vlastní nastavení. Kdyby se importovaly dřív,
    na druhém monitoru s jiným měřítkem (125 % vs. 100 %) by snímek a klik nesouhlasily a agent by se
    netrefoval. Proto se tohle volá hned při načtení modulu – Per-Monitor v2, jinak v1, jinak systémové."""
    global _dpi_done
    if _dpi_done or not WIN:
        return
    _dpi_done = True
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4 (Windows 10 1703+): správně i dialogy a podokna
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


dpi_aware()   # dřív než cokoli importuje pyautogui / pywinauto


def foreground_title() -> str:
    if not WIN:
        return ""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    from ctypes import wintypes

    rect = wintypes.RECT()
    if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def pick_monitor(monitors: list[dict], window: tuple[int, int, int, int] | None) -> dict:
    """Monitor, na kterém je aktivní okno (největší překryv). Bez okna hlavní monitor."""
    if not window:
        return monitors[0]

    def overlap(m: dict) -> int:
        x1, y1 = max(window[0], m["left"]), max(window[1], m["top"])
        x2, y2 = min(window[2], m["left"] + m["width"]), min(window[3], m["top"] + m["height"])
        return max(0, x2 - x1) * max(0, y2 - y1)

    best = max(monitors, key=overlap)
    return best if overlap(best) > 0 else monitors[0]


def screenshot() -> tuple[bytes, tuple[int, int, int, int]]:
    """PNG monitoru s aktivním oknem a jeho (left, top, width, height).

    Dřív se fotil vždy hlavní monitor – s oknem na druhém monitoru agent viděl úplně jinou obrazovku,
    než na kterou klikal.
    """
    import mss
    import mss.tools

    dpi_aware()
    with mss.mss() as s:
        window = _window_rect(ctypes.windll.user32.GetForegroundWindow()) if WIN else None
        mon = pick_monitor(s.monitors[1:] or s.monitors, window)
        img = s.grab(mon)
        return mss.tools.to_png(img.rgb, img.size), (mon["left"], mon["top"], mon["width"], mon["height"])


def foreground_rect(monitor: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """Okno v popředí v souřadnicích snímku monitoru."""
    if not WIN:
        return None
    r = _window_rect(ctypes.windll.user32.GetForegroundWindow())
    if not r:
        return None
    ox, oy = monitor[:2]
    return r[0] - ox, r[1] - oy, r[2] - ox, r[3] - oy


def monitor_count() -> int:
    try:
        import mss

        with mss.mss() as s:
            return max(1, len(s.monitors) - 1)
    except Exception:  # noqa: BLE001
        return 1


PRIO_POPUP, PRIO_TASKBAR, PRIO_DOCUMENT, PRIO_WINDOW = 3, 2, 1, 0

# UI Automation – vlastnosti prvků (čtou se levně, bez vzorů/patterns). Vlastnost vzoru, který prvek
# nepodporuje, vrací výchozí hodnotu (např. „nezaškrtnuto“) – proto se nejdřív ptáme, jestli vzor má.
_UIA_HAS_FOCUS, _UIA_IS_PASSWORD, _UIA_VALUE, _UIA_EXPANDED, _UIA_SELECTED, _UIA_TOGGLE = (
    30008, 30019, 30045, 30070, 30079, 30086)
_UIA_HAS_EXPAND, _UIA_HAS_SELECTION_ITEM, _UIA_HAS_TOGGLE, _UIA_HAS_VALUE = 30028, 30036, 30041, 30043
TEXT_TYPES = {"Text", "Header", "HeaderItem", "TitleBar", "StatusBar", "ToolTip"}


def _prop(info, pid: int):  # noqa: ANN001, ANN202
    try:
        return info.element.GetCurrentPropertyValue(pid)
    except Exception:  # noqa: BLE001
        return None


def _state(info, ct: str) -> tuple[str, bool]:  # noqa: ANN001
    """Stav prvku pro model: zaškrtnuto, rozbaleno, vybráno, hodnota pole, fokus. Vrací (text, heslo)."""
    bits: list[str] = []
    password = bool(_prop(info, _UIA_IS_PASSWORD)) if ct in ("Edit", "ComboBox") else False
    if ct in ("CheckBox", "RadioButton", "MenuItem", "Button", "ListItem", "TreeItem", "DataItem")             and _prop(info, _UIA_HAS_TOGGLE):
        tog = _prop(info, _UIA_TOGGLE)
        if ct == "Button":                       # přepínací tlačítka (Start, Hledat): jen když je stisknuté
            if tog == 1:
                bits.append("pressed")
        elif tog in (0, 1):
            bits.append("checked" if tog == 1 else "unchecked")
    if ct in ("ComboBox", "MenuItem", "TreeItem", "SplitButton", "Button") and _prop(info, _UIA_HAS_EXPAND):
        exp = _prop(info, _UIA_EXPANDED)
        if exp in (0, 1):
            bits.append("expanded" if exp == 1 else "collapsed")
    if ct in ("ListItem", "TabItem", "RadioButton", "DataItem", "TreeItem") and _prop(info, _UIA_HAS_SELECTION_ITEM)             and _prop(info, _UIA_SELECTED) is True:
        bits.append("selected")
    if ct in ("Edit", "ComboBox") and not password and _prop(info, _UIA_HAS_VALUE):
        val = _prop(info, _UIA_VALUE)
        if isinstance(val, str) and val.strip():
            bits.append(f"value=\"{' '.join(val.split())[:40]}\"")
    if _prop(info, _UIA_HAS_FOCUS) is True:
        bits.append("focused")
    return (", " + ", ".join(bits) if bits else ""), password


def ui_elements(monitor: tuple[int, int, int, int], budget_s: float = 4.0, limit: int = 250) -> list[som.Element]:
    return ui_snapshot(monitor, budget_s, limit)[0]


# --- OCR: nápisy ze snímku obrazovky (vestavěné OCR Windows 10/11) ------------------------------
#
# UI Automation nevidí všechno: Chrome bez čtečky obrazovky nepustí ven obsah stránky (živě: model
# viděl na snímku video, v seznamu prvků měl jen „Vyhledávání v kartách“), některé aplikace (hry, Java,
# Electron, vlastní kreslené ovládání) nemají strom prvků vůbec. OCR přečte každý viditelný nápis
# i s polohou – v jakékoli aplikaci – a nápis dostane číslo jako tlačítko. Model pak nehádá souřadnice.
# Běží lokálně (Windows.Media.Ocr), bez instalace navíc: jen pip balíčky winrt-* z extra „pc“.

OCR_MAX_SEGMENTS = 80
_ocr_engine = None
_ocr_failed = False


def _engine():  # noqa: ANN202
    """OCR engine: čeština, když ji Windows mají, jinak jazyk uživatele. None = OCR není k dispozici."""
    global _ocr_engine, _ocr_failed
    if _ocr_engine is not None or _ocr_failed:
        return _ocr_engine
    try:
        import os

        if not WIN or os.environ.get("AIKO_OCR", "1") == "0":
            raise ImportError("vypnuto")
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        eng = None
        for tag in ("cs", "cs-CZ"):
            if OcrEngine.is_language_supported(Language(tag)):
                eng = OcrEngine.try_create_from_language(Language(tag))
                break
        _ocr_engine = eng or OcrEngine.try_create_from_user_profile_languages()
        if _ocr_engine is None:
            raise RuntimeError("Windows nemají žádný jazyk pro OCR")
        log.info("OCR: %s", _ocr_engine.recognizer_language.language_tag)
    except Exception as e:  # noqa: BLE001 – bez OCR agent jede dál jen s UI Automation
        _ocr_failed = True
        log.info("OCR není k dispozici (%s) – pip install -e .[pc]", e)
    return _ocr_engine


def _run_async(coro_factory):  # noqa: ANN001, ANN202
    """WinRT vrací awaitable – ve vlákně bez smyčky stačí asyncio.run, jinak vlastní vlákno."""
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    box: dict = {}

    def run() -> None:
        try:
            box["v"] = asyncio.run(coro_factory())
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def ocr_lines(image) -> list[list[tuple[str, tuple[int, int, int, int]]]]:  # noqa: ANN001
    """Řádky textu ze snímku: [[(slovo, (x1, y1, x2, y2)), …], …] v pixelech obrázku. [] = bez OCR."""
    eng = _engine()
    if eng is None:
        return []
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    img = image.convert("RGBA")
    limit = int(getattr(type(eng), "max_image_dimension", 10000) or 10000)
    if max(img.size) > limit:
        img.thumbnail((limit, limit))
    r, g, b, a = img.split()
    writer = DataWriter()
    writer.write_bytes(_bgra(r, g, b, a))
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, img.width,
                                                 img.height, BitmapAlphaMode.PREMULTIPLIED)
    fx, fy = image.width / img.width, image.height / img.height

    async def go():  # noqa: ANN202
        return await eng.recognize_async(bmp)

    res = _run_async(go)
    out = []
    for line in res.lines if res else []:
        words = []
        for w in line.words:
            rc = w.bounding_rect
            words.append((str(w.text), (round(rc.x * fx), round(rc.y * fy), round((rc.x + rc.width) * fx),
                                        round((rc.y + rc.height) * fy))))
        if words:
            out.append(words)
    return out


def _bgra(r, g, b, a) -> bytes:  # noqa: ANN001
    from PIL import Image

    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def ocr_segments(lines: list[list[tuple[str, tuple[int, int, int, int]]]]) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Řádky OCR → klikatelné nápisy. OCR spojí do jednoho řádku i položky lišty vedle sebe
    („Soubor Domů Vložení Návrh“) – rozdělí se tam, kde je mezi slovy velká mezera."""
    segs = []
    for words in lines:
        cur: list[tuple[str, tuple[int, int, int, int]]] = []
        for w in words:
            if cur:
                h = max(6, sorted(b[3] - b[1] for _t, b in cur)[len(cur) // 2])
                if w[1][0] - cur[-1][1][2] > max(1.3 * h, 18):
                    segs.append(cur)
                    cur = []
            cur.append(w)
        if cur:
            segs.append(cur)
    out = []
    for seg in segs:
        text = " ".join(t for t, _b in seg).strip()
        letters = sum(c.isalpha() for c in text)
        if letters < 2 or sum(c.isalnum() for c in text) < 0.5 * len(text.replace(" ", "")):
            continue                                # „•“, „»>“, zbytky ikon – žádný nápis
        out.append((text, (min(b[0] for _t, b in seg), min(b[1] for _t, b in seg),
                           max(b[2] for _t, b in seg), max(b[3] for _t, b in seg))))
    return out


def merge_ocr(elements: list[som.Element], segments: list[tuple[str, tuple[int, int, int, int]]],
              limit: int = OCR_MAX_SEGMENTS, focus: tuple[int, int, int, int] | None = None) -> list[som.Element]:
    """Přidá k prvkům UI Automation nápisy z OCR, které v nich ještě nejsou (sloučení jako Microsoft UFO2).

    Nápis uvnitř malého prvku (tlačítko „Tisknout“, řádek souboru) patří tomu prvku – nepřidává se.
    Nápis jinde (obsah webové stránky, plátno aplikace bez stromu prvků) dostane vlastní číslo."""
    from ..common.safety import redact_codes

    if focus:                                       # nejdřív nápisy z okna v popředí, pak zbytek obrazovky
        fx1, fy1, fx2, fy2 = focus
        segments = sorted(segments, key=lambda s: not (fx1 <= (s[1][0] + s[1][2]) // 2 <= fx2
                                                       and fy1 <= (s[1][1] + s[1][3]) // 2 <= fy2))
    added: list[som.Element] = []
    for text, (x1, y1, x2, y2) in segments:
        cx, cy, h = (x1 + x2) // 2, (y1 + y2) // 2, max(1, y2 - y1)
        inside = [e for e in elements if e.contains(cx, cy)]
        if any((e.bounds[3] - e.bounds[1]) < 3 * h or _same_text(text, e.label) for e in inside):
            continue
        label = redact_codes(" ".join(text.split()))[:70]
        in_focus = bool(focus) and focus[0] <= cx <= focus[2] and focus[1] <= cy <= focus[3]
        # obsah okna v popředí je jako obsah stránky – limit prvků nesmí vyčerpat rám okna a lišty
        added.append(som.Element(0, f"{label} (Text, OCR)", (x1, y1, x2, y2), "text",
                                 priority=PRIO_DOCUMENT if in_focus else PRIO_WINDOW))
        if len(added) >= limit:
            break
    return elements + added


def _same_text(a: str, label: str) -> bool:
    wa = {w for w in a.lower().split() if len(w) > 1}
    wb = {w for w in label.split(" (")[0].lower().split() if len(w) > 1}
    return bool(wa) and len(wa & wb) >= max(1, len(wa) // 2)


def ui_snapshot(monitor: tuple[int, int, int, int], budget_s: float = 4.0, limit: int = 250,
                text_limit: int = 50, numbered: bool = True) -> tuple[list[som.Element], list[str]]:
    """Klikatelné prvky a viditelné texty aktivního okna přes UI Automation (časově omezené).

    Pořadí procházení: otevřená menu → hlavní panel → obsah webové stránky (Document) → zbytek okna.
    Dřív se strom procházel do šířky a limit vyčerpaly karty, záložky a postranní lišta prohlížeče –
    vyhledávací pole YouTube nebo odkazy na videa se do seznamu vůbec nedostaly a model hádal čísla.

    Texty (nadpisy, hlášky dialogů, popisky) dostane model jako seznam – malý lokální model
    z obrázku 1000×600 drobné písmo nepřečte, z textu ano.
    """
    if not WIN:
        return [], []
    try:
        from pywinauto.uia_element_info import UIAElementInfo
    except ImportError:
        return [], []
    from collections import deque

    dpi_aware()
    ox, oy, w, h = monitor
    deadline = time.time() + budget_s
    out: list[som.Element] = []
    texts: list[tuple[int, int, int, str]] = []     # (priorita, y, x, text)
    seen_text: set[str] = set()
    user32 = ctypes.windll.user32
    queues: dict[int, deque] = {p: deque() for p in (PRIO_POPUP, PRIO_TASKBAR, PRIO_DOCUMENT, PRIO_WINDOW)}
    # 1) otevřená menu a vyskakovací okna (kontextové menu, podmenu „Zobrazit“, rozbalovací seznamy)
    #    jsou samostatná okna – bez nich by agent musel hádat souřadnice položek a menu by se zavřelo
    for p in _popup_roots(UIAElementInfo):
        queues[PRIO_POPUP].append(p)
    # 2) hlavní panel (Start, hledání, síť, hlasitost), 3) aktivní okno
    for hwnd, prio in ((user32.FindWindowW("Shell_TrayWnd", None), PRIO_TASKBAR),
                       (user32.GetForegroundWindow(), PRIO_WINDOW)):
        try:
            if hwnd:
                queues[prio].append(UIAElementInfo(hwnd))
        except Exception:  # noqa: BLE001
            continue
    while time.time() < deadline and len(out) < limit:
        prio = next((p for p in (PRIO_POPUP, PRIO_TASKBAR, PRIO_DOCUMENT, PRIO_WINDOW) if queues[p]), None)
        if prio is None:
            break
        info = queues[prio].popleft()
        in_popup = prio == PRIO_POPUP
        try:
            children = info.children()
        except Exception:  # noqa: BLE001
            continue
        for c in children:
            try:
                ct = c.control_type
            except Exception:  # noqa: BLE001
                continue
            child_prio = PRIO_DOCUMENT if prio == PRIO_WINDOW and ct == "Document" else prio
            queues[child_prio].append(c)
            try:
                is_text = ct in TEXT_TYPES and prio != PRIO_TASKBAR
                if not is_text and (ct not in INTERACTIVE or not c.enabled):
                    continue
                r = c.rectangle
                x1, y1, x2, y2 = r.left - ox, r.top - oy, r.right - ox, r.bottom - oy
                x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
                if x2 - x1 < 6 or y2 - y1 < 6:
                    continue                              # mimo obrazovku (odscrollovaný obsah stránky)
                if is_text:
                    t = " ".join((c.name or "").split())[:160]
                    if len(t) >= 2 and t.lower() not in seen_text and len(texts) < text_limit * 2:
                        seen_text.add(t.lower())
                        texts.append((child_prio, y1, x1, t))
                    continue
                name = " ".join((c.name or c.automation_id or ct).split())[:60]
                kind = "input" if ct in ("Edit", "ComboBox") else "switch" if ct in ("CheckBox", "RadioButton") \
                    else "button"
                state, pw = _state(c, ct)
                pw = pw or (ct == "Edit" and any(k in name.lower() for k in ("heslo", "password", "pin")))
                label = f"{name} ({ct}{', open menu' if in_popup else ''}{state})"
                out.append(som.Element(0, label, (x1, y1, x2, y2), kind, pw, priority=child_prio))
            except Exception:  # noqa: BLE001
                continue
    # texty: nejdřív dialogy a menu, pak shora dolů jako se čte stránka; popisky tlačítek už model má
    labels = {e.label.split(" (")[0].lower() for e in out}
    texts.sort(key=lambda t: (-t[0], t[1] // 12, t[2]))
    screen_text = [t for _p, _y, _x, t in texts if t.lower() not in labels][:text_limit]
    return (som.number(out, limit=100) if numbered else out), screen_text


def _taskbar_windows(limit: int = 40) -> list[tuple[int, str, str]]:
    """(hwnd, titulek, třída) oken, která senior vidí na hlavním panelu – bez skrytých (cloaked) oken
    aplikací z Microsoft Store („Settings“, „Realtek Audio Console“ visí v systému neviditelně) a nástrojových oken."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out: list[tuple[int, str, str]] = []
    GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW = 4, -20, 0x80

    def cloaked(hwnd: int) -> bool:
        val = ctypes.c_int(0)
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
        except (AttributeError, OSError):
            return False
        return bool(val.value)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):  # noqa: ANN001, ANN202
        if (user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER)
                and not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW and not cloaked(hwnd)):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value.strip()
                if t and t != "Program Manager":
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    out.append((hwnd, t, cls.value))
        return len(out) < limit

    user32.EnumWindows(cb, 0)
    return out


def open_windows(limit: int = 12) -> list[str]:
    """Titulky oken, která senior vidí na hlavním panelu (bez skrytých a nástrojových oken)."""
    if not WIN:
        return []
    user32 = ctypes.windll.user32
    titles: list[str] = []
    for hwnd, t, _cls in _taskbar_windows():
        t = t[:90] + (" (minimized)" if user32.IsIconic(hwnd) else "")
        if t not in titles:
            titles.append(t)
        if len(titles) >= limit:
            break
    return titles


def best_window(want: str, titles: list[str]) -> int | None:
    """Index okna podle části titulku. Model často napíše název aplikace jinak, než je v titulku
    („Opera GX Browser“ vs. „… - Opera“), takže při neshodě rozhodnou společná slova."""
    from ..common.safety import normalize

    w = normalize(want)
    if not w:
        return None
    for i, t in enumerate(titles):
        if w in normalize(t):
            return i
    words = {x for x in w.split() if len(x) >= 3}
    scored = [(len(words & set(normalize(t).split())), i) for i, t in enumerate(titles)]
    best = max(scored, default=(0, -1))
    return best[1] if best[0] > 0 else None


# Průzkumník má v titulku jen název složky („Plocha“, „zolty“) – model ale chce „File Explorer“.
_EXPLORER = re.compile(r"explor|pruzkum|spravce soubor|file manager|slozk")


def focus_window(title_part: str) -> str:
    """Přepne na okno podle části titulku (obnoví minimalizované). Vrací titulek okna v popředí."""
    if not WIN:
        raise RuntimeError("jen pro Windows")
    from ..common.safety import normalize

    user32 = ctypes.windll.user32
    windows = _taskbar_windows()
    i = best_window(title_part, [t for _, t, _ in windows])
    if i is None and _EXPLORER.search(normalize(title_part)):
        i = next((k for k, (_, _, cls) in enumerate(windows) if cls == "CabinetWClass"), None)
    if i is None:
        raise ValueError(f"okno „{title_part}“ není otevřené; otevřená okna: "
                         + (" | ".join(dict.fromkeys(t[:50] for _, t, _ in windows[:10])) or "žádná"))
    hwnd = windows[i][0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
    # Windows nedovolí procesu na pozadí převzít popředí – krátký stisk Alt to odemkne
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return foreground_title()


POPUP_CLASSES = {"#32768", "Xaml_WindowedPopupClass", "PopupHost", "DropDown", "ComboLBox",
                 "Microsoft.UI.Content.PopupWindowSiteBridge", "Microsoft.UI.Content.DesktopChildSiteBridge",
                 # Windows 11: nové kontextové menu plochy a průzkumníka (XAML ostrůvek ve Windows App SDK)
                 "XamlExplorerHostIslandWindow", "XamlExplorerHostIslandWindow_WASDK"}


def _is_popup_class(cls: str) -> bool:
    return cls in POPUP_CLASSES or "Popup" in cls or cls.startswith("XamlExplorerHostIsland")


def _popup_hwnds(limit: int = 4) -> list[int]:
    """Viditelná menu/vyskakovací okna, nejvýš položená první (EnumWindows – milisekundy, ne sekundy)."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []
    buf = ctypes.create_unicode_buffer(256)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):  # noqa: ANN001, ANN202
        if user32.IsWindowVisible(hwnd):
            user32.GetClassNameW(hwnd, buf, 256)
            cls = buf.value
            if _is_popup_class(cls):
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.right - rect.left > 8 and rect.bottom - rect.top > 8:
                    found.append(hwnd)
        return len(found) < limit

    user32.EnumWindows(cb, 0)
    return found


def _popup_roots(info_cls) -> list:  # noqa: ANN001
    roots = []
    for hwnd in _popup_hwnds():
        try:
            roots.append(info_cls(hwnd))
        except Exception:  # noqa: BLE001
            continue
    return roots


# --- ruce ------------------------------------------------------------------------------

def click(x: int, y: int, double: bool = False, right: bool = False, hover_s: float = 0.12) -> None:
    import pyautogui

    dpi_aware()
    # Skok přímo na cíl: plynulý pohyb po diagonále přejížděl přes jiné položky menu
    # a zavíral otevřené podmenu („Zobrazit“ → „Zobrazit ikony na ploše“) dřív, než se kliklo.
    pyautogui.moveTo(x, y, duration=0)
    if hover_s > 0:
        time.sleep(hover_s)                        # menu zaregistruje najetí myši na položku
    if right:
        pyautogui.rightClick()
    elif double:
        pyautogui.doubleClick()
    else:
        pyautogui.click()


KEY_ALIASES = {"plus": "add", "minus": "subtract", "-": "subtract", "escape": "esc", "return": "enter",
               "windows": "win", "super": "win", "cmd": "win", "meta": "win", "control": "ctrl", "del": "delete",
               "pgdn": "pagedown", "pgup": "pageup", "page_down": "pagedown", "page_up": "pageup",
               "arrowdown": "down", "arrowup": "up", "arrowleft": "left", "arrowright": "right", "spacebar": "space",
               "backspace": "backspace", "bksp": "backspace", "ins": "insert", "option": "alt"}


def key_parts(key: str) -> list[str]:
    """„Ctrl + Plus“ → ['ctrl', 'add']. Neznámou klávesu pyautogui tiše ignoruje (zkratka pak
    zmáčkla jen Ctrl) – proto se ověří a model dostane chybu, ze které se poučí."""
    import pyautogui

    raw = key.replace(" ", "").lower()
    if raw == "+" or raw.endswith("++"):                  # „+“, „ctrl++“
        raw = raw[:-1] + "plus" if raw != "+" else "plus"
    parts = [KEY_ALIASES.get(p, p) for p in raw.split("+") if p]
    bad = [p for p in parts if p not in pyautogui.KEYBOARD_KEYS]
    if not parts or bad:
        raise ValueError(f"neznámá klávesa {', '.join(bad) or key!r} – použij např. enter, esc, tab, ctrl+l, "
                         "alt+left, f5, ctrl+plus")
    return parts


def press(key: str) -> None:
    import pyautogui

    parts = key_parts(key)
    if len(parts) > 1:
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(parts[0])


def scroll(direction: str, at: tuple[int, int] | None = None) -> None:
    import pyautogui

    if at:
        pyautogui.moveTo(*at, duration=0.2)
    d = direction.lower()
    if d in ("down", "up"):
        pyautogui.scroll(-600 if d == "down" else 600)
    else:
        pyautogui.hscroll(600 if d == "right" else -600)


def type_text(text: str) -> None:
    """Psaní včetně české diakritiky (SendInput s KEYEVENTF_UNICODE)."""
    if not WIN:
        import pyautogui

        pyautogui.write(text, interval=0.02)
        return
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x4, 0x2
    send = ctypes.windll.user32.SendInput
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        down = INPUT(1, _U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        up = INPUT(1, _U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        arr = (INPUT * 2)(down, up)
        send(2, arr, ctypes.sizeof(INPUT))
        time.sleep(0.01)
