"""Skilly: znalosti „jak na to“ jako textové soubory, ne kód.

Každý skill je složka se souborem SKILL.md:

    ---
    name: youtube
    title: YouTube – najít a pustit video
    description: Pustit video, písničku nebo pořad na YouTube …
    keywords: youtube, video, pisnick, klip, kanal
    devices: pc, phone
    tools: web_youtube_search, open_and_fix_browser
    first_tool: web_youtube_search    # volitelné: nástroj, kterým má agent začít
    answer_tool: web_weather          # volitelné: skill umí rovnou odpovědět na otázku (bez klikání)
    ---
    ## Postup pro Aiko
    1. … (pro agenta, anglicky nebo česky)
    ## Pro seniora
    1. … (česky, krok za krokem – když to Aiko nedokončí)

Do promptu jde jen skill, který se k větě hodí (lokální model má malý kontext). Vestavěné skilly jsou
v aiko/skills, vlastní (rodina) v data/skills – stejný formát, vlastní má přednost před vestavěným
se stejným jménem. Skill je RADA, ne oprávnění: bezpečnostní pravidla v kódu platí dál a skilly se
čtou jen z místních složek, nikdy z webu ani z obrazovky.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("aiko.skills")

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "skills"
MAX_AGENT_CHARS = 1800


@dataclass
class Skill:
    name: str
    title: str
    description: str
    keywords: list[str]
    devices: list[str]
    tools: list[str]
    agent: str                          # postup pro agenta
    senior: list[str] = field(default_factory=list)   # očíslované kroky pro seniora
    answer_tool: str = ""
    first_tool: str = ""                # malý model skill rád přehlédne – první krok se mu připomene výslovně
    exclude: list[str] = field(default_factory=list)  # slova, se kterými se skill NEpoužije („internet“)
    path: str = ""

    def brief(self) -> str:
        return f"{self.name}: {self.description}"


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _plain(text))


def parse(text: str, path: str = "") -> Skill:
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n(.*)", text, re.S)
    if not m:
        raise ValueError("chybí hlavička --- … ---")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.split(" #", 1)[0].rstrip()
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip('"')
    body = m.group(2)
    sections: dict[str, str] = {}
    cur = ""
    for line in body.splitlines():
        h = re.match(r"^##\s+(.*)$", line)
        if h:
            cur = _plain(h.group(1)).strip()
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"

    def lst(k: str) -> list[str]:
        return [x.strip() for x in re.split(r"[,;]", meta.get(k, "")) if x.strip()]

    name = meta.get("name", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", name):
        raise ValueError(f"neplatné name {name!r} (malá písmena, číslice, pomlčka)")
    if not meta.get("description"):
        raise ValueError("chybí description")
    agent = next((v for k, v in sections.items() if k.startswith(("postup pro aiko", "for aiko", "agent"))), "")
    if not agent.strip():
        raise ValueError("chybí sekce „## Postup pro Aiko“")
    return Skill(name=name, title=meta.get("title", name), description=meta["description"],
                 keywords=[_plain(k) for k in lst("keywords")], devices=[d.lower() for d in lst("devices")] or
                 ["pc", "phone"], tools=lst("tools"), agent=agent.strip()[:MAX_AGENT_CHARS],
                 answer_tool=meta.get("answer_tool", ""), first_tool=meta.get("first_tool", ""),
                 exclude=[_plain(k) for k in lst("exclude")], path=path)


class SkillBook:
    def __init__(self, dirs: list[Path] | None = None):
        self.skills: dict[str, Skill] = {}
        for d in dirs if dirs is not None else [BUILTIN_DIR]:
            self.load_dir(Path(d))

    def load_dir(self, d: Path) -> None:
        if not d.is_dir():
            return
        for f in sorted(d.glob("*/SKILL.md")):
            try:
                s = parse(f.read_text(encoding="utf-8"), str(f))
            except (OSError, ValueError) as e:
                log.warning("Skill %s nejde načíst: %s", f.parent.name, e)
                continue
            if s.name in self.skills:
                log.info("Skill %s z %s nahrazuje vestavěný", s.name, d)
            self.skills[s.name] = s
        if self.skills:
            log.info("Skilly: %s", ", ".join(sorted(self.skills)))

    def check_tools(self, known: set[str]) -> None:
        for s in self.skills.values():
            missing = [t for t in s.tools if t not in known]
            if missing:
                log.info("Skill %s zmiňuje nástroje, které teď nejsou k dispozici: %s", s.name, ", ".join(missing))

    def score(self, s: Skill, words: list[str]) -> float:
        """Kolik klíčových slov skillu je ve větě. Klíčové slovo je kmen: „pisnick“ najde „písničku“."""
        hits = 0.0
        for k in s.keywords:
            kw = k.split()
            if len(kw) > 1:
                hits += 1.5 if " ".join(kw) in " ".join(words) else 0
            elif any(w.startswith(k) for w in words if len(k) >= 3 or w == k):
                hits += 1
        return hits

    def select(self, text: str, device: str = "", k: int = 2, min_score: float = 1.0) -> list[Skill]:
        words = _words(text)
        ranked = []
        for s in self.skills.values():
            if device and device not in s.devices:
                continue
            if any(w.startswith(x) for x in s.exclude for w in words):
                continue                      # „najdi na internetu“ není hledání souboru v počítači
            sc = self.score(s, words)
            if sc >= min_score:
                ranked.append((sc, s))
        ranked.sort(key=lambda t: t[0], reverse=True)
        # další skill jen když sedí skoro stejně dobře („kde mám dopis“ ≠ tisk jen kvůli slovu „dopis“)
        ranked = [(sc, s) for sc, s in ranked if sc >= 0.6 * ranked[0][0]] if ranked else []
        if ranked:
            log.info("Skilly pro „%s“: %s", text[:60], ", ".join(f"{s.name} ({sc:g})" for sc, s in ranked[:k]))
        return [s for _sc, s in ranked[:k]]

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)


def as_prompt(skills: list[Skill], limit: int = MAX_AGENT_CHARS, used_tools: set[str] | None = None) -> str:
    """Skilly do promptu agenta – důvěryhodný návod z místních souborů (na rozdíl od textu z obrazovky).

    Dokud agent nepoužil první nástroj skillu, dostane ho výslovně jako další krok. Živě: qwen3-vl:2b
    se skillem YouTube v promptu přesto „přepínal okno“; přímá věta „NEXT ACTION: tool …“ zabírá líp."""
    parts = []
    for s in skills:
        parts.append(f"How-to from skill '{s.title}' (trusted local guide, follow it when it fits the screen):\n"
                     + s.agent[:limit])
    first = next((s.first_tool for s in skills if s.first_tool), "")
    if first and used_tools is not None and first not in used_tools:
        parts.append(f'NEXT ACTION (from the skill): action "tool", tool "{first}" with the right tool_args_json. '
                     "Do not click and do not switch windows first.")
    return "\n".join(parts)


# --- argumenty pro odpověď bez klikání (počasí, Wikipedie) ------------------------------------

_PLACE = re.compile(r"\b(?:v|ve|na|pro|u)\s+([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž-]+"
                    r"(?:\s+(?:nad|pod)?\s*[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž-]+)?)")
_QWORDS = re.compile(r"^(?:prosim\w*\s+)?(?:a\s+)?(?:rekni|reknete|povez|povezte|zjisti|zjistete|vyhledej|najdi)?"
                     r"\s*(?:mi|nam)?\s*(?:neco\s+)?(?:o\s+|kdo\s+(?:je|byl|byla|jsou)\s+|co\s+(?:je|jsou|znamena)\s+|"
                     r"jak\w*\s+(?:je|byl)\s+)?", re.I)


def answer_args(skill: Skill, question: str, home_city: str = "") -> dict[str, str]:
    """Z otázky vytáhne argument pro answer_tool: místo pro počasí, téma pro Wikipedii."""
    if skill.answer_tool == "web_weather":
        m = _PLACE.search(question)
        return {"place": (m.group(1) if m else "") or home_city or "Praha"}
    if skill.answer_tool == "web_wikipedia":
        q = re.sub(r"[?!.]+$", "", question.strip())
        plain = _plain(q)
        cut = _QWORDS.match(plain)
        topic = q[cut.end():] if cut and cut.end() else q
        return {"topic": topic.strip() or q}
    return {}
