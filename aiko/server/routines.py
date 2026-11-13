"""Pevné postupy pro nejčastější úkoly – provede je KÓD nástroji, bez modelu a bez klikání.

Proč: lokální model na slabé grafice je na klikání primitivní. Živě: na YouTube klikl na hledání Windows
vedle tlačítka Start místo hledání na stránce; přesouvání souborů a hledání ve složkách bloudilo.
Tyhle úkoly přitom mají vždy stejný postup, takže ho zná kód:

  YouTube (PC i telefon)   web_youtube_search → vybrat video (kanál, nejsledovanější) → otevřít jeho adresu
  stažené soubory → složka create_folder → list_files(dnes) → move_files → open_item
  nová složka              create_folder → open_item
  najít soubor             find_files → open_item (nejnovější shoda)
  spustit aplikaci         start_app (česky i anglicky, „kalkulačka“ = Calculator)
  pomalý počítač           pc_health → srozumitelné shrnutí
  tiskárna netiskne        list_printers → stav výchozí tiskárny

Model (GUI agent) přijde na řadu jen když postup nesedí nebo selže – a dostane, co už kód udělal.
Bezpečnost platí stejně: nástroje démonů mají vlastní pojistky (jen uživatelské složky, žádné programy…).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..common.safety import Level, normalize
from . import webtools
from .planner import RX_NEWEST, RX_POPULAR, _nominative, search_query, settings_plan

log = logging.getLogger("aiko.routines")

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
Say = Callable[[str], Awaitable[None]]


@dataclass
class RoutineResult:
    ok: bool
    say: str
    done: list[str] = field(default_factory=list)     # co kód opravdu udělal (pro agenta / návod / rodinu)
    fallback: bool = True                              # při neúspěchu smí pokračovat GUI agent


@dataclass
class Routine:
    name: str
    confirm: str                                       # česky: co Aiko udělá (bez „Mám začít?“)
    level: Level                                       # GREEN = rovnou, ORANGE = nejdřív „ano“
    needs: tuple[str, ...]                             # nástroje zařízení, bez kterých to nejde
    run: Callable[[Call, Say], Awaitable[RoutineResult]]
    skill: str = ""                                    # skill s postupem pro seniora, když to selže
    task: str = ""                                     # úkol pro GUI agenta, kdyby postup selhal


WHERE_CZ = {"desktop": "na ploše", "documents": "v Dokumentech", "downloads": "ve Stažených souborech",
            "pictures": "v Obrázcích", "music": "v Hudbě", "videos": "ve Videích"}


def _err(e: Exception) -> str:
    return str(e)[:160] or type(e).__name__


# --- YouTube -------------------------------------------------------------------------------------

_MEDIA = re.compile(r"\b(video\w*|videjk\w*|pisnick\w*|pisn\w*|klip\w*|pohadk\w*|koncert\w*|porad\w*|serial\w*)\b")
_PLAY = re.compile(r"\b(pust\w*|prehr\w*|zahr\w*|chci (videt|slyset|poslouchat|koukat)|ukaz\w*)\b")
_STOPWORDS_MEDIA = re.compile(r"(?i)\b(písničk\w*|pisnick\w*|písn\w*|pisn\w*|klip\w*|nějak\w*|nejak\w*|"
                              r"prosím|prosim|hned|teď|ted|celé|cele)\b")


def youtube_args(text: str) -> dict[str, str] | None:
    t = normalize(text)
    if re.search(r"\b(zastav|vypni|stop|pauza|ztlum)\w*", t):
        return None
    if not (re.search(r"\b(youtub\w*|jutub\w*)\b", t) or (_PLAY.search(t) and _MEDIA.search(t))):
        return None
    channel = ""
    by = re.search(r"\b(?:od|ode|kanal\w*)\s+([^\W\d_]+(?:\s+[^\W\d_]+)?)", text, re.IGNORECASE)
    if by:
        words = [w for w in by.group(1).split() if normalize(w) not in ("na", "v", "ve", "youtube", "youtubu")]
        channel = " ".join(_nominative(w) for w in words[:2])
    query = " ".join(_STOPWORDS_MEDIA.sub(" ", search_query(text)).split())
    if channel and normalize(query) in ("", normalize(channel)):
        query = channel
    query = query or channel
    if not query:
        return None
    order = "views" if RX_POPULAR.search(t) else "date" if RX_NEWEST.search(t) else "relevance"
    return {"query": query, "order": order, "channel": channel, "said_by": by.group(0) if by else ""}


def youtube(text: str, kind: str) -> Routine | None:
    args = youtube_args(text)
    if not args:
        return None
    opener = "open_and_fix_browser" if kind == "pc" else "open_url"
    said_by = args.pop("said_by", "")
    what = {"views": "nejsledovanější video", "date": "nejnovější video"}.get(args["order"], "video")
    who = f" od {args['channel']}" if args["channel"] else ""
    # do věty seniorova slova („od Karla Gotta“), ne hrubý 1. pád z kódu („Karl Gott“)
    subject = said_by if said_by and normalize(args["query"]) == normalize(args["channel"]) else         f"„{args['query']}“" + (f" {said_by}" if said_by else "")
    confirm = f"Najdu na YouTube {what} {subject} a pustím ho."

    async def run(call: Call, say: Say) -> RoutineResult:
        try:
            res = await webtools.call("web_youtube_search", args)
        except webtools.WebToolError as e:
            return RoutineResult(False, f"YouTube teď neodpovídá ({_err(e)}).")
        vids = res.get("videos") or []
        if not vids:
            return RoutineResult(False, f"Na YouTube jsem k „{args['query']}“ nic nenašla.")
        v = vids[0]
        await say(f"Pouštím „{v['title'][:70]}“.")
        try:
            r = await call(opener, {"url": v["url"]})
        except Exception as e:  # noqa: BLE001 – DeviceError i odmítnutí
            return RoutineResult(False, "Video se nepodařilo otevřít.", [f"found video {v['url']}"])
        window = str((r or {}).get("window", "")) if isinstance(r, dict) else ""
        log.info("YouTube: %s (%s) → okno %r", v["title"], v.get("channel"), window[:80])
        by = f" od {v['channel']}" if v.get("channel") else ""
        return RoutineResult(True, f"Pouštím „{v['title'][:80]}“{by}. Kdyby se video samo nespustilo, "
                                   "zmáčkněte mezerník.", [f"opened {v['url']}"])

    return Routine("youtube", confirm, Level.GREEN, (opener,), run, skill="youtube",
                   task=f"Play on YouTube: {args['query']}{who}")


# --- složky a soubory (PC) --------------------------------------------------------------------------

_NAME_AFTER = re.compile(r"(?i)\bslo[žz]k\w*\s+(?:s\s+n[áa]zvem\s+|jm[ée]nem\s+|pojmenovanou\s+)?[„\"']?"
                         r"([^\s,.„\"'“”]+)")
_NOT_NAME = {"a", "na", "v", "ve", "do", "kam", "kde", "ktera", "ktere", "se", "s", "pro", "mi", "plose", "ploche",
             "dokumentech", "dokumentu", "tam"}


def folder_name(text: str) -> str:
    m = _NAME_AFTER.search(text)
    if not m:
        return ""
    name = m.group(1).strip()
    return "" if normalize(name) in _NOT_NAME else name[:60]


def _where(text: str) -> str:
    return "documents" if re.search(r"\bdokument\w*\b", normalize(text)) else "desktop"


def _when(text: str) -> tuple[float, str, str]:
    """(days pro list_files, filtr podle „modified“, česky) – „dnes“ = jen dnešní, ne posledních 24 h."""
    t = normalize(text)
    if re.search(r"\bvcer\w*", t):
        return 2, "včera", "včera"
    if re.search(r"\b(tyden|tento tyden|poslednich|nedavno)\b", t):
        return 7, "", "za poslední týden"
    return 1, "dnes", "dnes"


def downloads_to_folder(text: str, kind: str, female: bool = True) -> Routine | None:
    t = normalize(text)
    if kind != "pc" or not re.search(r"\b(stazen\w*|stahl\w*|stahnul\w*|stahla|download\w*)", t) \
            or not re.search(r"\bslozk\w*", t):
        return None
    name, where = folder_name(text), _where(text)
    if not name:
        return None
    days, flt, when_cz = _when(text)
    images = bool(re.search(r"\b(fotk\w*|fotograf\w*|obrazk\w*)\b", t))
    what = "fotky" if images else "soubory"
    stahl = "stáhla" if female else "stáhl"
    confirm = (f"Vytvořím {WHERE_CZ[where]} složku „{name}“ a přesunu do ní {what}, které jste {stahl} "
               f"{when_cz}. Nic nesmažu.")
    target = f"{where}/{name}"

    async def run(call: Call, say: Say) -> RoutineResult:
        done = []
        try:
            r = await call("create_folder", {"name": name, "where": where})
            done.append(f"create_folder({name}) -> {'existed' if r.get('existed') else 'created'}")
            await say("Hledám stažené soubory.")
            lst = await call("list_files", {"where": "downloads", "days": days, "limit": 100,
                                            "kind": "images" if images else ""})
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Se složkou to nešlo ({_err(e)}).", done)
        files = [f for f in lst.get("files", []) if f.get("kind") not in ("složka",)
                 and (not flt or str(f.get("modified", "")).startswith(flt))]
        programs = [f["name"] for f in files if f.get("kind") == "program"]
        names = [f["name"] for f in files if f.get("kind") != "program"]
        if not names:
            extra = " Jen instalační programy, ty jsem nechala ve Stažených souborech." if programs else ""
            return RoutineResult(True, f"Složku „{name}“ {WHERE_CZ[where]} máte. {when_cz.capitalize()} ale "
                                       f"žádné {what} stažené nevidím.{extra}", done, fallback=False)
        await say(f"Přesouvám {len(names)} {'soubor' if len(names) == 1 else 'soubory' if len(names) < 5 else 'souborů'}.")
        try:
            mv = await call("move_files", {"names_json": json.dumps(names, ensure_ascii=False),
                                           "source": "downloads", "target": target})
            done.append(f"move_files -> {len(mv.get('done', []))} moved")
            await call("open_item", {"where": target})
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Soubory se nepodařilo přesunout ({_err(e)}).", done)
        moved = [d["to"] for d in mv.get("done", [])]
        listing = ", ".join(moved[:3]) + (f" a {len(moved) - 3} dalších" if len(moved) > 3 else "")
        extra = " Instalační programy jsem nechala ve Stažených souborech." if programs else ""
        return RoutineResult(True, f"Hotovo. Ve složce „{name}“ {WHERE_CZ[where]} je teď {len(moved)} "
                                   f"{'soubor' if len(moved) == 1 else 'souborů' if len(moved) >= 5 else 'soubory'}: "
                                   f"{listing}. Složku jsem vám otevřela.{extra}", done)

    return Routine("downloads_to_folder", confirm, Level.ORANGE, ("create_folder", "list_files", "move_files"),
                   run, skill="stazene-soubory", task=text)


def new_folder(text: str, kind: str) -> Routine | None:
    t = normalize(text)
    if kind != "pc" or not re.search(r"\b(vytvor\w*|udelej\w*|zaloz\w*|nov\w*)\b", t) \
            or not re.search(r"\bslozk\w*", t) or re.search(r"\b(stazen\w*|stahl\w*|presun\w*|soubor\w*)", t):
        return None
    name, where = folder_name(text), _where(text)
    if not name:
        return None

    async def run(call: Call, say: Say) -> RoutineResult:
        try:
            r = await call("create_folder", {"name": name, "where": where})
            if "open_item" in _TOOLS_SEEN:
                await call("open_item", {"where": f"{where}/{name}"})
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Složku se nepodařilo vytvořit ({_err(e)}).")
        if r.get("existed"):
            return RoutineResult(True, f"Složku „{name}“ {WHERE_CZ[where]} už máte, otevřela jsem ji.")
        return RoutineResult(True, f"Hotovo. {WHERE_CZ[where].capitalize()} je nová složka „{name}“.")

    return Routine("new_folder", f"Vytvořím {WHERE_CZ[where]} složku „{name}“.", Level.ORANGE, ("create_folder",),
                   run, skill="slozky", task=text)


_FIND = re.compile(r"\b(najdi\w*|najit|najdete|kde (mam|je|jsou|byl\w*)|hledam|nemuzu najit|ztratil\w*|"
                   r"zmizel\w* mi (soubor|dopis|fotk))\b")
_FILEISH = re.compile(r"\b(soubor\w*|dopis\w*|fotk\w*|fotograf\w*|dokument\w*|faktur\w*|pdf|obrazk\w*|smlouv\w*|"
                      r"tabulk\w*|prezentac\w*|recept\w*|pozvank\w*|vypis\w*|slozk\w*)\b")
_FIND_STOP = set("""najdi najdete najit kde mam je jsou byl byla bylo hledam nemuzu ztratil ztratila ten ta to tu
ty tam ten mi me muj moje moji prosim soubor souboru soubory pocitaci pocitac na v ve ktery ktera ktere co jsem si
nejaky nejakou nejake ulozeny ulozenou ulozene zmizel zmizela pro od do za ke ze se jak nekde ktery""".split())


def find_file(text: str, kind: str) -> Routine | None:
    t = normalize(text)
    if kind != "pc" or not _FIND.search(t) or not _FILEISH.search(t) \
            or re.search(r"\b(internet\w*|web\w*|youtub\w*|google|seznam\w*|map\w*)\b", t):
        return None
    words = [w for w in t.split() if w not in _FIND_STOP and len(w) >= 3][:3]
    if not words:
        return None
    query = " ".join(words)

    async def run(call: Call, say: Say) -> RoutineResult:
        try:
            r = await call("find_files", {"name": query})
            hits = r.get("files", [])
            if not hits and len(words) > 1:           # „dopis Janu babicka“ → zkusit jen nejvýraznější slovo
                r = await call("find_files", {"name": max(words, key=len)})
                hits = r.get("files", [])
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Hledání nešlo ({_err(e)}).")
        hits = [h for h in hits if h.get("kind") != "program"]
        if not hits:
            return RoutineResult(False, f"Soubor „{query}“ jsem v počítači nenašla.", [f"find_files({query}) -> 0"])
        h = hits[0]
        where = str(h.get("where", ""))
        where_cz = WHERE_CZ.get(where.split("/")[0], "") + (f" ve složce {where.split('/', 1)[1]}" if "/" in where else "")
        try:
            if h.get("kind") == "složka":
                await call("open_item", {"where": f"{where}/{h['name']}"})
            else:
                await call("open_item", {"name": h["name"], "where": where})
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Našla jsem „{h['name']}“ {where_cz}, ale nejde otevřít ({_err(e)}).")
        more = f" Podobných souborů je víc ({len(hits)}), otevřela jsem nejnovější." if len(hits) > 1 else ""
        return RoutineResult(True, f"Našla jsem „{h['name']}“ {where_cz}, teď ho máte otevřený.{more}",
                             [f"opened {h['name']}"])

    return Routine("find_file", f"Najdu v počítači „{query}“ a otevřu to.", Level.GREEN, ("find_files", "open_item"),
                   run, skill="hledani-souboru", task=text)


_OPEN_APP = re.compile(r"^(?:prosim\s+)?(?:otevri|otevrete|spust|spustte|zapni|zapnete)\s+(?:mi\s+)?"
                       r"(?:aplikaci\s+|program\s+|apku\s+)?(.+?)(?:\s+na\s+pocitaci|\s+prosim)?$")
_NOT_APP = re.compile(r"\b(www|\.cz|\.com|youtub\w*|seznam\w*|google\w*|strank\w*|internet\w*|web\w*|mail\w*|"
                      r"post\w*|zpravy|novinky|idnes|mapy|slozk\w*|soubor\w*|dokument\w*|fotk\w*|video\w*)\b")


def open_app(text: str, kind: str) -> Routine | None:
    t = re.sub(r"\s+na (pocitaci|notebooku|telefonu|mobilu)\b", "", normalize(text))
    m = _OPEN_APP.match(t)
    if kind != "pc" or not m or _NOT_APP.search(m.group(1)):
        return None
    if settings_plan(text, kind) or re.search(r"\b(rezim\w*|jas\w*|wi ?fi\w*|bluetooth|zvuk\w*|hlasitost\w*|"
                                             r"pismo|pisma|tapet\w*|internet\w*)\b", t):
        return None                           # „zapni tmavý režim / wifi / zvuk“ je nastavení, ne aplikace
    name = m.group(1).strip()
    if not name or len(name) > 40:
        return None
    # do věty původní slova s diakritikou („kalkulačku“), pro hledání stačí bez ní
    orig = re.sub(r"(?i)\s+(na počítači|na pocitaci|prosím|prosim)\s*$", "", text.strip().rstrip(".!?"))
    shown = " ".join(orig.split()[-len(name.split()):]) or name

    async def run(call: Call, say: Say) -> RoutineResult:
        try:
            r = await call("start_app", {"name": name})
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Program „{name}“ nejde spustit ({_err(e)}).", fallback=False)
        if r.get("started"):
            return RoutineResult(True, f"Otevírám {r.get('app', name)}.", [f"start_app({name})"])
        cands = r.get("candidates") or []
        if cands:
            return RoutineResult(False, f"Program „{name}“ přesně nemám. Podobné jsou: {', '.join(cands[:3])}.",
                                 fallback=False)
        return RoutineResult(False, f"Program „{name}“ v počítači nevidím. Asi není nainstalovaný.", fallback=False)

    return Routine("open_app", f"Otevřu {shown}.", Level.GREEN, ("start_app",), run, skill="aplikace", task=text)


def slow_pc(text: str, kind: str) -> Routine | None:
    t = normalize(text)
    if kind != "pc" or not re.search(r"\b(pomal\w*|zasek\w*|nereaguj\w*|zamrz\w*|seka se|trva to|plny disk|"
                                     r"baterie|nabit\w*)\b", t):
        return None

    async def run(call: Call, say: Say) -> RoutineResult:
        try:
            h = await call("pc_health", {})
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Stav počítače nejde zjistit ({_err(e)}).")
        bits = []
        if h.get("disk_free_percent", 100) < 10:
            bits.append(f"disk je skoro plný (volných jen {h['disk_free_percent']} %)")
        if h.get("memory_used_percent", 0) > 90:
            bits.append(f"paměť je plná, máte otevřených {h.get('open_windows', 'hodně')} oken")
        if h.get("running_days", 0) > 7:
            bits.append(f"počítač běží už {round(h['running_days'])} dní bez restartu")
        if "battery_percent" in h and h["battery_percent"] < 20 and not h.get("on_charger"):
            bits.append(f"baterie má jen {h['battery_percent']} %")
        advice = []
        if any("restartu" in b or "paměť" in b for b in bits):
            advice.append("Pomůže zavřít okna, která nepotřebujete, a počítač restartovat: Start, vypínač, "
                          "Restartovat.")
        if any("baterie" in b for b in bits):
            advice.append("Připojte prosím nabíječku.")
        if any("disk" in b for b in bits):
            advice.append("Na uvolnění místa se zeptejte rodiny, sama nic nemažu.")
        if not bits:
            return RoutineResult(True, f"Počítač vypadá v pořádku: na disku je {h.get('disk_free_percent', '?')} % "
                                       f"volného místa a paměť je zaplněná na {h.get('memory_used_percent', '?')} %. "
                                       "Když je pomalý jen jeden program, zkuste ho zavřít a znovu otevřít.",
                                 fallback=False)
        return RoutineResult(True, "Podívala jsem se: " + ", ".join(bits) + ". " + " ".join(advice),
                             fallback=False)

    return Routine("slow_pc", "Podívám se, proč je počítač pomalý.", Level.GREEN, ("pc_health",), run,
                   skill="pomaly-pocitac", task=text)


def printer(text: str, kind: str) -> Routine | None:
    t = normalize(text)
    if kind != "pc" or not re.search(r"\b(netiskn\w*|nejde tisk\w*|tiskarn\w* (nejde|nefunguje|nereaguje|netiskne)|"
                                     r"nefunguje tiskarn\w*)\b", t):
        return None

    async def run(call: Call, say: Say) -> RoutineResult:
        try:
            ps = (await call("list_printers", {})).get("printers", [])
        except Exception as e:  # noqa: BLE001
            return RoutineResult(False, f"Tiskárny nejde zjistit ({_err(e)}).")
        real = [p for p in ps if not p.get("virtual")]
        default = next((p for p in ps if p.get("default")), None)
        if not real:
            return RoutineResult(True, "V počítači nevidím žádnou skutečnou tiskárnu, jen tisk do souboru. "
                                       "Tiskárnu je potřeba připojit, zeptejte se prosím rodiny.", fallback=False)
        if default and default.get("virtual"):
            return RoutineResult(True, f"Jako hlavní je nastavený tisk do souboru („{default['name']}“), ne "
                                       f"tiskárna „{real[0]['name']}“. V okně tisku nahoře vyberte „{real[0]['name']}“.",
                                 fallback=False)
        p = default or real[0]
        if p.get("state") == "offline":
            return RoutineResult(True, f"Tiskárna „{p['name']}“ je offline. Zkontrolujte, že je zapnutá a svítí. "
                                       "Vypněte ji a zapněte tlačítkem na tiskárně a zkuste tisk znovu.",
                                 fallback=False)
        return RoutineResult(True, f"Tiskárna „{p['name']}“ je {p.get('state', 'připravená')}. Zkontrolujte papír "
                                   "a inkoust; když bliká kontrolka, podívejte se na displej tiskárny.",
                             fallback=False)

    return Routine("printer", "Podívám se, co je s tiskárnou.", Level.GREEN, ("list_printers",), run,
                   skill="tisk", task=text)


_TOOLS_SEEN: set[str] = set()      # nástroje zařízení, pro které se postup zrovna vybírá (open_item volitelně)

MATCHERS = (youtube, downloads_to_folder, new_folder, find_file, open_app, slow_pc, printer)


def match(text: str, kind: str, tools: dict[str, Any] | set[str], female: bool = True) -> Routine | None:
    """První pevný postup, který k větě sedí a jehož nástroje zařízení má. None = nechat to modelu."""
    if not text:
        return None
    _TOOLS_SEEN.clear()
    _TOOLS_SEEN.update(tools)
    for m in MATCHERS:
        try:
            r = m(text, kind, female) if m is downloads_to_folder else m(text, kind)
        except Exception as e:  # noqa: BLE001 – vadné pravidlo nesmí shodit porozumění
            log.warning("Pravidlo %s selhalo: %s", m.__name__, e)
            continue
        if r and all(t in tools for t in r.needs):
            log.info("Pevný postup: %s (%s)", r.name, r.confirm)
            return r
    return None
