"""Plán úkolu pro GUI agenta: seznam kroků ještě před prvním kliknutím.

Malý lokální model bez plánu „bloudí“: otevře YouTube a neví, co dál. Proto:
  1. Známé weby (YouTube, Google, Seznam, Mapy) – plán sestaví kód: rovnou otevře stránku
     s výsledky hledání (u YouTube i seřazené podle oblíbenosti) a agentovi zbývá kliknout.
  2. Ostatní úkoly – plán napíše jazykový model (Brain.plan), kód ho jen zkontroluje.
Plán agent dostává v každém kroku a senior ho slyší v otázce „Mám začít?“.
Plán je jen nápověda – všechny pojistky agenta platí dál a start_url projde blocklistem.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from ..common.safety import is_blocked_url, normalize

log = logging.getLogger("aiko.planner")


@dataclass
class TaskPlan:
    steps: list[str] = field(default_factory=list)   # anglicky pro model
    say: str = ""                                    # česky pro seniora („Otevřu YouTube, vyhledám…“)
    start_url: str = ""                              # stránku otevře kód sám, ještě před modelem
    source: str = "rules"
    start_settings: str = ""                         # nebo stránku Nastavení (klíč ze settings_pages)
    start_tool: str = ""                             # nebo obecně jeden nástroj zařízení (create_folder…)
    start_args: dict = field(default_factory=dict)

    def as_notes(self) -> str:
        return " | ".join(f"{i} {s}" for i, s in enumerate(self.steps, 1))


# Slova, která do hledání nepatří (normalizovaná). Co zbyde, je hledaný výraz.
_STOP = set("""otevri otevrete otevrit pust pustte pustit spust spustit zapni najdi najdete najit najdes vyhledej
vyhledejte vyhledat hledej hledat dohledej chci chtel chtela bych bys mi me mne prosim prosimte na v ve do z ze se si
a i to tu tam ten ta ty tom tim ktery ktera ktere kterou je jsou byl bylo pocitac pocitaci notebook telefon telefonu
mobil mobilu web webu stranku stranka internet internetu youtube youtubu youtubove youtubovy jutub google googlu
googlem seznam seznamu mapy mapach mape mapu video videa videi videem videjko klip nejake neco nejaky nejakou nejvic
nejvice nejlepsi nejoblibenejsi oblibene oblibeny oblibenou nejsledovanejsi sledovane nejpopularnejsi popularni
nejznamejsi znamy zname nejnovejsi nove novy posledni zhlednute zhlednuti videne nejvetsi uplne prvni pro mne muj moje
od ode s se""".split())
RX_POPULAR = re.compile(r"nejoblibenejsi|nejvic(e)? oblib|nejsledovanejsi|nejvic(e)? (sledovan|zhlednut|viden)|"
                        r"nejpopularnejsi|nejznamejsi|nejlepsi|nejvic(e)? popular|nejvetsi hit")
RX_NEWEST = re.compile(r"nejnovejsi|posledni|nove|cerstve")
# Na čistém prohlížeči YouTube/Google/Seznam nejdřív ukážou souhlas s cookies („Before you continue“) –
# ověřeno živě. Odmítnutí je pro seniora šetrnější k soukromí a stránka funguje stejně.
CONSENT = ("If a cookie / consent window covers the page ('Before you continue', 'Než budete pokračovat', "
           "'Souhlas'), click 'Odmítnout vše' / 'Reject all' (never 'Accept'), then continue.")
YT_SORT_VIEWS, YT_SORT_DATE = "CAM%253D", "CAI%253D"     # YouTube: seřadit podle zhlédnutí / data


def _nominative(word: str) -> str:
    """„od Vidraila“ → Vidrail, „od Michala Davida“ → Michal David (2. pád mužských jmen, hrubě)."""
    if len(word) > 3 and word[-1] in "au" and word[-2].lower() not in "aeiouyáéíóúůý":
        return word[:-1]
    return word


def search_query(task: str) -> str:
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", task)
    out: list[str] = []
    after_od = False
    for w in words:
        n = normalize(w)
        if n in ("od", "ode"):
            after_od = True
            continue
        if n in _STOP or len(n) < 2:
            continue
        out.append(_nominative(w) if after_od else w)
    return " ".join(out[:8])


# Nastavení: co senior řekne → stránka Nastavení, kterou kód otevře sám (agent pak jen přepne)
SETTINGS_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("dark_mode", re.compile(r"tmav\w* (rezim|motiv|vzhled|barv\w*)|dark ?mode|nocni rezim|svetl\w* (rezim|motiv)"),
     "Turn the dark mode / dark theme on (Windows: 'Choose your mode'/'Zvolit režim' -> 'Dark'/'Tmavý'; "
     "Android: switch 'Tmavý motiv'/'Dark theme'). If the senior wants light mode, choose light instead."),
    ("display", re.compile(r"\bjas\w*|\bztmav\w* obrazovk|\bzesvetl\w*"), "Change the brightness slider as asked."),
    ("bluetooth", re.compile(r"bluetooth|bluetut|blutut|bezdrat\w* sluchat\w*"), "Turn Bluetooth on or pair as asked."),
    ("wifi", re.compile(r"\bwi ?fi\w*|\bwifin\w*"), "Turn Wi-Fi on or pick the home network as asked."),
    ("background", re.compile(r"tapet\w*|pozadi (plochy|obrazovky)|obrazek na plose"), "Change the desktop background as asked."),
    ("mouse", re.compile(r"\bmys\w*|\bkurzor\w*|sipk\w* mysi"), "Change the mouse setting as asked."),
    ("printers", re.compile(r"tiskar\w*"), "Open or add the printer as asked (never remove a printer)."),
    ("date_time", re.compile(r"\b(datum\w*|spatn\w* cas|hodiny jdou|nastav\w* cas)\b"), "Fix the date or time as asked."),
    ("notifications", re.compile(r"oznamen\w*|notifikac\w*|upozorn\w* (na|od) aplikac"), "Change the notifications as asked."),
    ("language", re.compile(r"\bjazyk\w*|\bcestin\w*|\banglictin\w*"), "Change the language setting as asked."),
]


def settings_plan(task: str, kind: str) -> TaskPlan | None:
    from ..common.settings_pages import SETTINGS_PAGES

    t = normalize(task)
    for page, rx, hint in SETTINGS_RULES:
        pc_uri, phone_action, desc = SETTINGS_PAGES[page]
        if rx.search(t) and (pc_uri if kind == "pc" else phone_action):
            return TaskPlan([f"The Settings page '{desc}' is already open (Aiko opened it) - DONE",
                             f"{hint} Scroll down if the option is not visible.",
                             "Check on the screenshot that the setting really changed.",
                             "done - tell the senior in Czech what you changed."],
                            f"Otevřu Nastavení, stránku {desc}, a změním to tam.", start_settings=page)
    return None


def rule_plan(task: str, kind: str) -> TaskPlan | None:
    """Plán z pravidel pro časté weby a nastavení. None = pravidla úkol neznají (zkusí se model)."""
    t = normalize(task)
    sp = settings_plan(task, kind)
    if sp:
        return sp
    if re.search(r"\b(youtub\w*|jutub\w*)\b", t):
        q = search_query(task)
        if not q:
            return None
        popular, newest = bool(RX_POPULAR.search(t)), bool(RX_NEWEST.search(t)) and not RX_POPULAR.search(t)
        sp = f"&sp={YT_SORT_VIEWS}" if popular else f"&sp={YT_SORT_DATE}" if newest else ""
        url = f"https://www.youtube.com/results?search_query={quote_plus(q)}{sp}"
        order = "sorted by view count (most popular first)" if popular else \
            "sorted by upload date (newest first)" if newest else "best match first"
        by = re.search(r"\bod[e]? ([^\W\d_]+(?: [^\W\d_]+)?)", task, re.IGNORECASE)
        who = f" Prefer a video whose channel name matches '{search_query('od ' + by.group(1))}'." if by else ""
        steps = [f"YouTube search results for '{q}' {order} are already open (Aiko opened them) - DONE",
                 CONSENT,
                 "Click the TITLE of the first real video in the results (skip ads, Shorts, channels and "
                 f"playlists).{who}",
                 "Check the video page opened and plays; if an ad plays, wait or click 'Skip'/'Přeskočit'.",
                 "done - tell the senior in Czech which video is playing (its title)."]
        what = "nejoblíbenější" if popular else "nejnovější" if newest else "první vhodné"
        say = (f"Otevřu YouTube, vyhledám „{q}“" + (" seřazené podle počtu zhlédnutí" if popular else "")
               + f" a pustím {what} video.")
        return TaskPlan(steps, say, url)
    m = re.search(r"\b(na |v )?(mapach|mape|mapy\.cz|mapu)\b", t)
    if m and re.search(r"\b(najdi|najit|vyhledej|cest\w*|kudy|kde je|jak se dostanu|trasa|trasu)\b", t):
        q = search_query(re.sub(r"(?i)\b(cest\w*|kudy|trasa|trasu|kde je|jak se dostanu)\b", " ", task))
        if q and kind == "pc":
            return TaskPlan([f"Mapy.cz search for '{q}' is already open - DONE", CONSENT,
                             "Click the first matching place in the results list on the left.",
                             "done - tell the senior in Czech what the map shows."],
                            f"Otevřu Mapy a najdu „{q}“.", f"https://mapy.cz/?q={quote_plus(q)}")
    if re.search(r"\b(najdi|najit|vyhledej|vyhledat|dohledej|hledej|zjisti)\b", t) and kind == "pc":
        q = search_query(task)
        if q and len(q) >= 3:
            engine = "Google" if "googl" in t else "Seznam"
            url = (f"https://www.google.com/search?q={quote_plus(q)}" if engine == "Google"
                   else f"https://search.seznam.cz/?q={quote_plus(q)}")
            return TaskPlan([f"{engine} search results for '{q}' are already open - DONE", CONSENT,
                             "Click the first result that clearly matches (skip ads marked 'Reklama'/'Sponzorováno').",
                             "Check the page shows what the senior wanted; scroll if needed.",
                             "done - tell the senior in Czech in one sentence what is on the screen."],
                            f"Vyhledám „{q}“ na {'Googlu' if engine == 'Google' else 'Seznamu'} a otevřu nejlepší výsledek.",
                            url)
    return None


def _tool_say(tool: str, args: dict) -> str:
    """Česká věta pro seniora, co Aiko jako první udělá – sestavená kódem z nástroje a argumentů."""
    from ..common.settings_pages import SETTINGS_PAGES

    if tool == "create_folder":
        where = "v Dokumentech" if str(args.get("where", "")).lower() in ("documents", "dokumenty") else "na ploše"
        return f"Vytvořím {where} složku „{args.get('name', '')}“."
    if tool == "open_settings":
        return f"Otevřu nastavení – {SETTINGS_PAGES.get(str(args.get('page')), ('', '', 'nastavení'))[2]}."
    if tool in ("open_and_fix_browser", "open_url"):
        from urllib.parse import parse_qs, urlsplit

        u = urlsplit(str(args.get("url", "")))
        q = (parse_qs(u.query).get("search_query") or parse_qs(u.query).get("q") or [""])[0]
        site = u.netloc.removeprefix("www.")
        return f"Otevřu {site} a vyhledám „{q}“." if q else f"Otevřu stránku {site}."
    if tool == "focus_window":
        return f"Přepnu na okno {args.get('title', '')}."
    return {"set_desktop_icons": "Zobrazím ikony na ploše.", "fix_sound": "Zapnu a zesílím zvuk.",
            "diagnose_network": "Zkontroluji připojení k internetu.", "list_apps": "Podívám se, jaké máte aplikace.",
            "set_font_scale": "Změním velikost písma."}.get(tool, "")


def tool_plan(raw: dict, tools: dict, kind: str = "pc") -> TaskPlan | None:
    """První krok od malého modelu – kód ho jen ověří: nástroj zařízení skutečně má, agent ho smí použít,
    argumenty sedí na parametry, adresa je https mimo blocklist. Větu pro seniora sestaví kód."""
    from ..common.safety import AGENT_FORBIDDEN_TOOLS, Level, tool_level

    name = str(raw.get("tool") or "").strip()
    args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
    spec = tools.get(name)
    if not name or spec is None or name in AGENT_FORBIDDEN_TOOLS or name in ("shell_read", "system_info") \
            or tool_level(name, spec.get("level")) >= Level.RED:
        return None
    params = set((spec.get("inputSchema") or {}).get("properties", {}))
    args = {k: v for k, v in args.items() if k in params}
    if set((spec.get("inputSchema") or {}).get("required", [])) - set(args):
        return None
    url = str(args.get("url", ""))
    if url and (not re.fullmatch(r"https://[\w.-]+\.[a-z]{2,}(/[^\s\"'<>]*)?", url) or is_blocked_url(url)):
        return None
    say = _tool_say(name, args)
    if not say:
        return None
    steps = [f"Aiko already did step 1 for you: tool {name}({args}) - see its result below - DONE",
             "Look at the screen: if step 1 already did the whole task, finish with done.",
             "Otherwise continue the task with the normal windows and menus (never a command line).",
             "done - tell the senior the result in Czech."]
    return TaskPlan(steps, say, source="model:first_step", start_tool=name, start_args=args)


_SAY_OK =re.compile(r"^(Otevřu|Najdu|Vyhledám|Pustím|Zapnu|Vypnu|Zkusím|Přepnu|Nastavím|Podívám|Změním)\b")


_GENERIC = set("""telefon telefonu mobil mobilu pocitac pocitaci prosim proc kamo kamarade nejde nefunguje musis
muzes udelej chci potrebuji nevim""".split())


_COMMAND_LINE = re.compile(r"powershell|\bpwsh\b|\bcmd\b|command (line|prompt)|terminal|prikazov\w* (radek|radk\w*)|"
                           r"\bprikaz\w*|regedit|registr\w*|new-item|mkdir|\bwin ?\+ ?r\b|spustit \(win")


def _relevant(task: str, plan_text: str) -> bool:
    """Týká se plán úkolu? Aspoň jedno obsahové slovo úkolu (kmen 5 znaků) musí v plánu být.
    Skutečný případ: „telefon mi nevyzvání“ → 2B model navrhl „Otevřu aplikaci Mapy.cz“."""
    words = {w[:5] for w in normalize(task).split() if len(w) >= 4 and w not in _GENERIC and w not in _STOP}
    have = normalize(plan_text)
    return not words or any(w in have for w in words)


def checked_plan(raw: dict, task: str, kind: str = "pc") -> TaskPlan | None:
    """Plán od modelu – kód ho jen kontroluje, význam neurčuje: kroky max. 6 a musí se týkat úkolu,
    adresa jen https mimo blocklist, stránka Nastavení jen ze seznamu, věta pro seniora krátká v 1. osobě."""
    from ..common.settings_pages import SETTINGS_PAGES

    steps = [str(s).strip()[:160] for s in (raw.get("steps") or []) if str(s).strip()][:6]
    if not steps or not _relevant(task, " ".join(steps) + " " + str(raw.get("say") or "")):
        return None
    if _COMMAND_LINE.search(normalize(" ".join(steps) + " " + str(raw.get("say") or ""))):
        # Bezpečnost (pevně): plán přes příkazovou řádku agent stejně nesmí provést – a senior by slyšel
        # „Otevřu PowerShell…“ (živě u „vytvoř na ploše složku“). Bez plánu agent použije nástroj nebo okna.
        log.warning("Plán od modelu zahozen – chtěl příkazovou řádku: %s", " | ".join(steps)[:200])
        return None
    url = str(raw.get("start_url") or "").strip()
    if not re.fullmatch(r"https://[\w.-]+\.[a-z]{2,}(/[^\s\"'<>]*)?", url) or is_blocked_url(url):
        url = ""
    page = str(raw.get("start_settings") or "").strip()
    if page not in SETTINGS_PAGES or not SETTINGS_PAGES[page][0 if kind == "pc" else 1]:
        page = ""
    if page:
        url = ""                                   # jen jedna zkratka
    say = str(raw.get("say") or "").strip()
    say = say if _SAY_OK.match(say) and len(say) <= 160 else ""
    return TaskPlan(steps, say, url, source="model", start_settings=page)
