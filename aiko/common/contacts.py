"""Hledání lidí v kontaktech podle jména, jak ho senior řekl – i v jiném pádě, zdrobnělinou nebo s překlepem.

„Lence“ najde Lenku, „Petrovi“ Petra, „babičce“ Babičku, „Jirkovi“ Jiřího, „Honzovi“ Jana,
„sestře Lence“ Lenku (i když v telefonu není slovo „sestra“), „Novákova“ Nováková.
Porovnává se bez diakritiky: společný začátek (české koncovky se liší jen na konci), přezdívky z pevné
tabulky a podobnost slov (překlepy). Když nic nesedí dost, démon vrátí aspoň NÁVRHY – senior pak
uslyší „Myslíte Lenu Novákovou?“ místo ticha.
Démon vrací serveru jen shody, ne celý adresář.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .safety import normalize

# Slova, která ve větě o zprávě nejsou jméno.
FILLER = {
    "zpravu", "zprava", "zpravy", "sms", "smsku", "esemesku", "esemes", "textovku", "vzkaz", "whatsapp",
    "whatsappem", "prosim", "mi", "mu", "ji", "jim", "nam", "hned", "ted", "rychle", "pak", "pres", "na", "do",
    "v", "ve", "a", "taky", "tez", "moje", "mojemu", "moji", "me", "pani", "panu", "panovi", "pana", "pan",
    "zavolej", "zavolejte", "napis", "napiste", "posli", "poslete", "kontakt", "kontaktu", "cislo", "cislu",
}

# Vztahy: když jsou v kontaktu („Sestra Lenka“, „Máma“), sedí; když ne, jméno vedle nich stačí.
RELATIONS = {"sestra", "sestre", "sestry", "sestru", "bratr", "bratrovi", "bratra", "dcera", "dceri", "dcery",
             "dceru", "syn", "synovi", "syna", "vnuk", "vnukovi", "vnuka", "vnucka", "vnucce", "vnucku", "teta",
             "tete", "tetu", "stryc", "strejda", "strejdovi", "sousedka", "sousedce", "soused", "sousedovi",
             "kamaradka", "kamaradce", "kamarad", "kamaradovi", "doktor", "doktorovi", "doktorka", "doktorce",
             "neter", "synovec", "zet", "snacha", "svagr", "svagrova"}

# Zdrobněliny a přezdívky → občanské jméno (bez diakritiky). Senior řekne „Honzovi“, v telefonu je „Jan“.
_NICK_GROUPS = [
    ("jan", "honza honzik jenda jendа jeník jenik jani"), ("josef", "pepa pepik pepicek joza"),
    ("jiri", "jirka jirik jiricek jura"), ("jakub", "kuba kubik kubicek"), ("michal", "misa misak"),
    ("michaela", "misa misa misacka michalka"), ("miroslav", "mirek mira"), ("miroslava", "mirka mira"),
    ("jaroslav", "jarda jara"), ("jaroslava", "jarka jara"), ("stanislav", "standa stana"),
    ("antonin", "tonda tonik"), ("vaclav", "venca vasek vena"), ("frantisek", "franta franticek fanda"),
    ("ladislav", "lada ladik"), ("tomas", "tom tomik tomasek"), ("ondrej", "ondra ondrasek"),
    ("matej", "matej matejicek"), ("lukas", "luky lukasek"), ("pavel", "pavlik pavlicek"),
    ("petr", "peta petrik petricek"), ("petra", "peta petruska petulka"), ("karel", "karlik kaja"),
    ("zdenek", "zdenda"), ("zdenka", "zdena"), ("bohumil", "bohous"), ("vojtech", "vojta"),
    ("david", "davidek"), ("martin", "marta martinek"), ("radek", "radecek"), ("vladimir", "vlada"),
    ("anna", "anicka anca ani andula"), ("marie", "mana maruska marenka marka majka"),
    ("barbora", "bara barca barbi"), ("veronika", "verca veru"), ("katerina", "katka kata katka"),
    ("lenka", "lenicka lenca"), ("lucie", "lucka lucinka"), ("jana", "janicka janca"), ("eva", "evicka evca"),
    ("hana", "hanka hanicka"), ("zuzana", "zuzka zuzanka"), ("dagmar", "dasa dagmarka"),
    ("alena", "alenka"), ("jitka", "jitule"), ("ludmila", "lida lidka"), ("ruzena", "ruza ruzenka"),
    ("vera", "verka veruska"), ("bozena", "bozka"), ("vlasta", "vlastik"), ("helena", "helca helenka"),
    ("tereza", "terka terezka"), ("kristyna", "kiki tyna kristynka"), ("nikola", "niki nikolka"),
    ("mama", "mamka maminka mamina mami mamca"), ("tata", "tatka taticek tati taťka"),
    ("babicka", "baba babi babca"), ("dedecek", "deda dedecek dedous dedu"),
]
NICKNAMES: dict[str, set[str]] = {}
for _full, _nicks in _NICK_GROUPS:
    group = {_full, *(normalize(n) for n in _nicks.split())}
    for n in group:
        NICKNAMES.setdefault(n, set()).update(group)


def _tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t and t not in FILLER]


def _prefix(q: str, n: str) -> int:
    p = 0
    for a, b in zip(q, n):
        if a != b:
            break
        p += 1
    return p


def _stem(w: str) -> str:
    """Hrubý kmen: „lence“ → „len“, „jirkovi“ → „jirk“, „honzovi“ → „honz“ (pády mění konec slova)."""
    for end in ("ovi", "ove", "ou", "ce", "ci", "ku", "ky", "ko", "ka", "u", "y", "e", "i", "a", "o"):
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[: -len(end)]
    return w


def _nick_match(q: str, n: str) -> bool:
    """„honzovi“ ~ „jan“: kmen slova dotazu je kmenem některé přezdívky ze skupiny jména."""
    group = NICKNAMES.get(n) or NICKNAMES.get(_stem(n)) or set()
    if not group:
        return False
    qs = _stem(q)
    return any(g == q or (len(qs) >= 3 and (g.startswith(qs) or _stem(g) == qs)) for g in group)


def _token_score(q: str, n: str) -> float:
    """Shoda dvou slov 0–1: přesně, společný začátek (pády), přezdívka, nebo podobnost (překlep)."""
    if q == n:
        return 1.0
    p = _prefix(q, n)
    short = min(len(q), len(n))
    if p >= 3 and p >= 0.6 * short:
        return 0.5 + 0.5 * p / max(len(q), len(n))
    if p >= 3 and _stem(q) == _stem(n):
        return 0.7
    if _nick_match(q, n) or _nick_match(n, q):
        return 0.85
    if min(len(q), len(n)) >= 4:
        r = SequenceMatcher(None, q, n).ratio()          # „novakova“ ~ „nowakowa“, „lusie“ ~ „lucie“
        if r >= 0.75:
            return 0.4 + 0.4 * r
    return 0.0


def score(query: str, name: str) -> float:
    """0 = nic, 1 = přesně. Slova dotazu, která nesedí (vztah „sestře“, příjmení navíc), snižují skóre,
    ale nezahodí kontakt, když jiné slovo sedí dobře. Dřív muselo sedět každé slovo – „sestře Lence“
    nenašlo „Lenka“ a senior neslyšel nic."""
    qt, nt = _tokens(query), normalize(name).split()
    if not qt or not nt:
        return 0.0
    def tok(q: str, n: str) -> float:
        s = _token_score(q, n)
        # „Petrovi“, „Pepovi“ je mužský rod – Petr má přednost před Petrou (a naopak „Petře“ nic nemění)
        if s and q.endswith("ovi") and n.endswith("a") and not q.startswith(n):
            s -= 0.1
        return s

    scores = [max(tok(q, n) for n in nt) for q in qt]
    named = [s for q, s in zip(qt, scores) if q not in RELATIONS]      # vztah navíc nevadí
    rel_hit = any(s > 0 for q, s in zip(qt, scores) if q in RELATIONS)
    if not named:                                                      # jen „mamce“, „sestře“
        return max(scores)
    best = max(named)
    if best < 0.55:
        return 0.0
    matched = [s for s in named if s > 0]
    avg = sum(named) / len(named)
    if len(matched) < len(named):                                      # něco navíc nesedí
        avg = 0.75 * sum(matched) / len(matched) * (len(matched) / len(named)) ** 0.5
    return min(1.0, avg + (0.05 if rel_hit else 0.0))


def match(query: str, rows: list[dict[str, Any]], limit: int = 5, min_score: float = 0.5) -> list[dict[str, Any]]:
    """rows: [{"name", "phone", "type"}] → nejlepší shody (jedno číslo na člověka, mobil má přednost)."""
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = score(query, r.get("name", ""))
        if s < min_score:
            continue
        key = normalize(r["name"])
        mobile = str(r.get("type", "")) == "2"
        cur = best.get(key)
        if cur is None or (mobile and not cur["mobile"]):
            best[key] = {"name": r["name"], "phone": r.get("phone", ""), "score": round(s, 3), "mobile": mobile}
    out = sorted(best.values(), key=lambda m: -m["score"])[:limit]
    # jasný vítěz → ostatní slabší nepotřebujeme (přesná shoda, nebo o hodně lepší než druhý)
    if len(out) > 1 and (out[0]["score"] >= 0.99 > out[1]["score"] or out[0]["score"] - out[1]["score"] >= 0.3):
        out = out[:1]
    return out


def suggest(query: str, rows: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Když match nic nenašel: kontakty, které jsou si aspoň trochu podobné (pro otázku „Myslíte …?“)."""
    qt = [t for t in _tokens(query) if t not in RELATIONS] or _tokens(query)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        nt = normalize(r.get("name", "")).split()
        if not nt or not qt:
            continue
        s = max(max(SequenceMatcher(None, q, n).ratio(), 0.7 if _prefix(q, n) >= 2 and len(q) >= 3 else 0)
                for q in qt for n in nt)
        if s >= 0.6:
            key = normalize(r["name"])
            if key not in out or s > out[key]["score"]:
                out[key] = {"name": r["name"], "phone": r.get("phone", ""), "score": round(s, 3),
                            "mobile": str(r.get("type", "")) == "2"}
    return sorted(out.values(), key=lambda m: -m["score"])[:limit]
