"""Hledání min (Minesweeper) na internetu: čtení hrací plochy ze snímku obrazovky, řešitel a herní smyčka.

Cílová hra: Google Minesweeper (https://www.google.com/fbx?fbx=minesweeper) – zelená šachovnice neodkrytých
políček, béžová odkrytá políčka a barevná čísla. Barvy jsou ploché (bez stínů), takže plochu jde spolehlivě
přečíst bez vision modelu a bez numpy (čistý Python + Pillow, běží v démonovi PC).

Proč kódem, a ne modelem: malý vision model by se v mřížce 18×14 netrefil a logiku min by nezvládl. Kód přečte
všechna políčka najednou, odvodí jistě bezpečná políčka (pravidla + výčet možností na hranici) a hádá jen,
když to jinak nejde – políčko s nejmenší pravděpodobností miny.

Bezpečnost: smyčka kliká výhradně na středy políček přečtené plochy (nikdy mimo ni) a skončí, jakmile plochu
nevidí, hra skončí, vyprší čas nebo uživatel pohne myší (`should_stop`).
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

GAME_URL = "https://www.google.com/fbx?fbx=minesweeper"

UNKNOWN = -1      # neodkryté políčko
FLAG = -2         # vlajka (položil ji člověk) – bereme jako minu a neklikáme
MINE = -3         # odkrytá mina / cokoli, co není plocha (konec hry, překryv)

# Počty min podle velikosti plochy (sloupce, řádky) u Google Minesweeper: lehká, střední, těžká
KNOWN_MINES = {(10, 8): 10, (18, 14): 40, (24, 20): 99}

GREEN_SHADES = ((170, 215, 81), (162, 209, 73))     # neodkryté: světlá, tmavá
TAN_SHADES = ((229, 194, 159), (215, 184, 153))     # odkryté: světlá, tmavá
NUMBER_COLORS = {1: (25, 118, 210), 2: (56, 142, 60), 3: (211, 47, 47), 4: (123, 31, 162),
                 5: (255, 143, 0), 6: (0, 151, 167), 7: (66, 66, 66), 8: (158, 158, 158)}
FLAG_COLOR = (242, 54, 7)

Pixel = tuple[int, int, int]
Cell = tuple[int, int]          # (řádek, sloupec)


def _is_green(p: Pixel) -> bool:
    r, g, b = p
    return g >= 165 and g > r + 15 and g > b + 60


def _is_tan(p: Pixel) -> bool:
    r, g, b = p
    return r >= 190 and g >= 150 and r > g > b and r - b >= 40


def _is_board(p: Pixel) -> bool:
    return _is_green(p) or _is_tan(p)


def _dist(a: Pixel, b: Pixel) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _rgb_rows(img: Any) -> tuple[list[bytes], int, int]:
    """Obrázek jako řádky bajtů RGB (rychlý přístup bez numpy)."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    raw = rgb.tobytes()
    return [raw[y * w * 3:(y + 1) * w * 3] for y in range(h)], w, h


def _px(rows: list[bytes], x: int, y: int) -> Pixel:
    row = rows[y]
    i = x * 3
    return row[i], row[i + 1], row[i + 2]


@dataclass
class Board:
    left: int
    top: int
    right: int
    bottom: int
    cols: int
    rows: int
    cells: list[list[int]] = field(default_factory=list)

    @property
    def cw(self) -> float:
        return (self.right - self.left) / self.cols

    @property
    def ch(self) -> float:
        return (self.bottom - self.top) / self.rows

    @property
    def mines_total(self) -> int | None:
        return KNOWN_MINES.get((self.cols, self.rows))

    def center(self, r: int, c: int) -> tuple[int, int]:
        return round(self.left + (c + 0.5) * self.cw), round(self.top + (r + 0.5) * self.ch)

    def count(self, value: int) -> int:
        return sum(row.count(value) for row in self.cells)

    def text(self) -> str:
        sym = {UNKNOWN: "#", FLAG: "F", MINE: "*", 0: "."}
        return "\n".join("".join(sym.get(v, str(v)) for v in row) for row in self.cells)


# --- čtení plochy ze snímku -----------------------------------------------------------------------

def _board_bbox(img: Any) -> tuple[int, int, int, int] | None:
    """Hrubý obdélník plochy na 4× zmenšeném snímku, zpřesněný na plném rozlišení."""
    from PIL import Image

    w, h = img.size
    k = 4 if min(w, h) >= 400 else 1
    small = img.convert("RGB").resize((max(1, w // k), max(1, h // k)), Image.BOX) if k > 1 else img.convert("RGB")
    rows, sw, sh = _rgb_rows(small)
    mask = [[_is_board(_px(rows, x, y)) for x in range(sw)] for y in range(sh)]
    row_counts = [sum(r) for r in mask]
    if not row_counts or max(row_counts) < 12:
        return None

    def longest_run(values: list[int], limit: float, gap: int = 3) -> tuple[int, int] | None:
        """Nejdelší úsek nad prahem; krátké mezery (sloupec plný číslic) úsek nepřeruší."""
        runs: list[list[int]] = []
        for i, v in enumerate(values):
            if v >= limit:
                if runs and i - runs[-1][1] <= gap:
                    runs[-1][1] = i + 1
                else:
                    runs.append([i, i + 1])
        if not runs:
            return None
        a, b = max(runs, key=lambda r: r[1] - r[0])
        return a, b

    ry = longest_run(row_counts, 0.35 * max(row_counts))
    if not ry:
        return None
    col_counts = [sum(mask[y][x] for y in range(ry[0], ry[1])) for x in range(sw)]
    rx = longest_run(col_counts, 0.35 * max(col_counts))
    if not rx or (rx[1] - rx[0]) * k < 120 or (ry[1] - ry[0]) * k < 90:
        return None
    left, top, right, bottom = rx[0] * k, ry[0] * k, rx[1] * k, ry[1] * k
    if k == 1:
        return left, top, right, bottom
    # zpřesnění okrajů na plném rozlišení (na pár řádcích / sloupcích uvnitř plochy, medián)
    full, fw, fh = _rgb_rows(img)

    def edge(fixed: list[int], lo: int, hi: int, horizontal: bool, first: bool) -> int:
        found = []
        for f in fixed:
            rng = range(max(0, lo), min(fw if horizontal else fh, hi))
            seq = rng if first else reversed(rng)
            for v in seq:
                p = _px(full, v, f) if horizontal else _px(full, f, v)
                if _is_board(p):
                    found.append(v if first else v + 1)
                    break
        found.sort()
        return found[len(found) // 2] if found else (lo + hi) // 2

    ys = [top + (bottom - top) * i // 6 for i in range(1, 6)]
    xs = [left + (right - left) * i // 6 for i in range(1, 6)]
    left2 = edge(ys, left - 2 * k, left + 2 * k, True, True)
    right2 = edge(ys, right - 2 * k, right + 2 * k, True, False)
    top2 = edge(xs, top - 2 * k, top + 2 * k, False, True)
    bottom2 = edge(xs, bottom - 2 * k, bottom + 2 * k, False, False)
    return left2, top2, right2, bottom2


def _shade(p: Pixel) -> tuple[str, int] | None:
    """Druh plochy a odstín šachovnice (0 světlý, 1 tmavý), nebo None (číslo, vlajka, kurzor…)."""
    for kind, shades in (("g", GREEN_SHADES), ("t", TAN_SHADES)):
        d = [_dist(p, s) for s in shades]
        if min(d) <= 30:
            return kind, 0 if d[0] <= d[1] else 1
    return None


def _grid(rows: list[bytes], box: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Počet sloupců a řádků: velikost políčka, při které odstíny šachovnice nejlépe střídají."""
    left, top, right, bottom = box
    w, h = right - left, bottom - top
    best: tuple[float, int, int] | None = None
    for cols in range(4, 41):
        cw = w / cols
        nrows = round(h / cw)
        if nrows < 4 or nrows > 40 or abs(h / nrows - cw) / cw > 0.07:
            continue
        ch = h / nrows
        votes = [0, 0]
        seen = 0
        for r in range(nrows):
            for c in range(cols):
                x = int(left + c * cw + 0.2 * cw)
                y = int(top + r * ch + 0.2 * ch)
                s = _shade(_px(rows, x, y))
                if s is None:
                    continue
                seen += 1
                votes[(s[1] + r + c) % 2] += 1
        if seen < 0.4 * cols * nrows:
            continue
        score = max(votes) / seen
        if (cols, nrows) in KNOWN_MINES:
            score += 0.02                         # známá velikost Google hry má při shodě přednost
        if best is None or score > best[0]:
            best = (score, cols, nrows)
    if best is None or best[0] < 0.85:
        return None
    return best[1], best[2]


def _classify(rows: list[bytes], x0: float, y0: float, x1: float, y1: float) -> int:
    w, h = x1 - x0, y1 - y0
    step = max(1, int(min(w, h) / 14))
    px = [_px(rows, x, y) for y in range(int(y0 + 0.18 * h), int(y1 - 0.18 * h), step)
          for x in range(int(x0 + 0.18 * w), int(x1 - 0.18 * w), step)]
    if not px:
        return MINE
    n = len(px)
    green = [p for p in px if _is_green(p)]
    tan = [p for p in px if _is_tan(p)]
    if len(green) >= 0.35 * n and len(green) >= len(tan):
        red = sum(1 for p in px if p[0] > 190 and p[1] < 120 and p[2] < 100)
        return FLAG if red >= 0.04 * n else UNKNOWN
    if len(tan) >= 0.35 * n:
        bg = tuple(sorted(c[i] for c in tan)[len(tan) // 2] for i in range(3))
        ink = [p for p in px if not _is_tan(p) and _dist(p, bg) > 60]
        if len(ink) < 0.03 * n:
            return 0
        med = tuple(sorted(p[i] for p in ink)[len(ink) // 2] for i in range(3))
        num, d = min(((k, _dist(med, col)) for k, col in NUMBER_COLORS.items()), key=lambda t: t[1])
        return num if d <= 95 else MINE
    return MINE


def read_board(img: Any) -> Board | None:
    """Najde na snímku hrací plochu Google Minesweeper a přečte všechna políčka. None = plocha není vidět."""
    box = _board_bbox(img)
    if box is None:
        return None
    rows, _, _ = _rgb_rows(img)
    grid = _grid(rows, box)
    if grid is None:
        return None
    cols, nrows = grid
    b = Board(*box, cols=cols, rows=nrows)
    b.cells = [[_classify(rows, b.left + c * b.cw, b.top + r * b.ch, b.left + (c + 1) * b.cw,
                          b.top + (r + 1) * b.ch) for c in range(cols)] for r in range(nrows)]
    return b


# --- řešitel ------------------------------------------------------------------------------------

def neighbors(r: int, c: int, rows: int, cols: int) -> list[Cell]:
    return [(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            if (dr or dc) and 0 <= r + dr < rows and 0 <= c + dc < cols]


@dataclass
class Solution:
    safe: set[Cell] = field(default_factory=set)
    mines: set[Cell] = field(default_factory=set)
    probabilities: dict[Cell, float] = field(default_factory=dict)
    guess: Cell | None = None
    guess_p: float = 1.0


MAX_COMPONENT = 22          # větší hranice se nevyčísluje (odhad) – výpočet zůstane pod sekundou


def solve(cells: list[list[int]], total_mines: int | None = None) -> Solution:
    """Jistě bezpečná políčka a jisté miny; když žádné nejsou, nejméně riskantní tip."""
    rows, cols = len(cells), len(cells[0]) if cells else 0
    sol = Solution()
    unknown = {(r, c) for r in range(rows) for c in range(cols) if cells[r][c] == UNKNOWN}
    flags = {(r, c) for r in range(rows) for c in range(cols) if cells[r][c] == FLAG}
    if not unknown:
        return sol
    if len(unknown) == rows * cols:                 # první tah: Google ho dělá vždy bezpečný
        sol.guess, sol.guess_p = (rows // 2, cols // 2), 0.0
        return sol

    constraints: list[tuple[frozenset[Cell], int]] = []
    for r in range(rows):
        for c in range(cols):
            v = cells[r][c]
            if v < 0:                               # neodkryté, vlajka, mina – žádná informace
                continue
            nb = neighbors(r, c, rows, cols)
            unk = frozenset(n for n in nb if n in unknown)
            if unk:
                constraints.append((unk, v - sum(1 for n in nb if n in flags)))

    mines, safe = set(), set()
    changed = True
    while changed:
        changed = False
        reduced = []
        for vars_, need in constraints:
            rest = vars_ - safe - mines
            need -= len(vars_ & mines)
            if not rest:
                continue
            if need <= 0:
                safe |= rest
                changed = True
            elif need >= len(rest):
                mines |= rest
                changed = True
            else:
                reduced.append((rest, need))
        if changed:
            continue
        uniq = list(dict.fromkeys(reduced))
        for a, na in uniq:          # pravidlo podmnožin: A ⊂ B → B∖A má nb−na min
            for b, nb_ in uniq:
                if not a < b:
                    continue
                diff, nd = b - a, nb_ - na
                if nd == 0 and not diff <= safe:
                    safe |= diff
                    changed = True
                elif nd == len(diff) and not diff <= mines:
                    mines |= diff
                    changed = True
        if not changed:
            constraints = uniq
    sol.safe, sol.mines = safe, mines
    if safe:
        return sol

    # Výčet všech rozložení min na hranici (po nezávislých částech) → pravděpodobnost miny pro každé políčko
    reduced = [(v - mines, n - len(v & mines)) for v, n in constraints if v - mines]
    frontier = set().union(*(v for v, _ in reduced)) if reduced else set()
    others = unknown - frontier - mines
    comps: list[tuple[list[Cell], list[tuple[frozenset[Cell], int]]]] = []
    todo = set(frontier)
    while todo:
        seed = todo.pop()
        comp, stack = {seed}, [seed]
        while stack:
            x = stack.pop()
            for v, _ in reduced:
                if x in v:
                    for y in v - comp:
                        comp.add(y)
                        stack.append(y)
        todo -= comp
        comps.append((sorted(comp), [(v, n) for v, n in reduced if v & comp]))

    dists: list[tuple[list[Cell], dict[int, tuple[float, dict[Cell, float]]]]] = []
    approx: dict[Cell, float] = {}
    for vars_, cons in comps:
        if len(vars_) > MAX_COMPONENT:
            for x in vars_:
                ps = [n / len(v) for v, n in cons if x in v]
                approx[x] = sum(ps) / len(ps)
            continue
        dists.append((vars_, _enumerate(vars_, cons)))

    remaining = None if total_mines is None else total_mines - len(flags) - len(mines)
    probs = _combine(dists, len(others), remaining)
    probs.update(approx)
    if others:
        exp_front = sum(probs.get(x, 0.0) for x in frontier)
        if remaining is not None:
            p_other = max(0.0, min(1.0, (remaining - exp_front) / len(others)))
        else:
            p_other = 0.16
        for x in others:
            probs[x] = p_other
    for x, p in probs.items():
        if p <= 1e-9:
            sol.safe.add(x)
        elif p >= 1 - 1e-9:
            sol.mines.add(x)
    sol.probabilities = probs
    if sol.safe:
        return sol
    candidates = [x for x in unknown if x not in sol.mines]
    if candidates:
        def risk(x: Cell) -> tuple[float, int]:
            # stejné riziko: raději roh/okraj (víc šance na odkrytí prázdné oblasti)
            return probs.get(x, 0.5), len(neighbors(x[0], x[1], rows, cols))
        sol.guess = min(candidates, key=risk)
        sol.guess_p = probs.get(sol.guess, 0.5)
    return sol


def _enumerate(vars_: list[Cell], cons: list[tuple[frozenset[Cell], int]]
               ) -> dict[int, tuple[float, dict[Cell, float]]]:
    """Pro každý počet min k: (počet řešení, kolikrát je které políčko minou)."""
    index = {x: i for i, x in enumerate(vars_)}
    cons_idx = [([index[x] for x in v], n) for v, n in cons]
    by_var: list[list[int]] = [[] for _ in vars_]
    for ci, (vs, _) in enumerate(cons_idx):
        for i in vs:
            by_var[i].append(ci)
    assign = [0] * len(vars_)
    out: dict[int, tuple[float, dict[Cell, float]]] = {}

    def ok(ci: int, upto: int) -> bool:
        vs, n = cons_idx[ci]
        s = sum(assign[i] for i in vs if i <= upto)
        left = sum(1 for i in vs if i > upto)
        return s <= n <= s + left

    def rec(i: int) -> None:
        if i == len(vars_):
            k = sum(assign)
            cnt, per = out.get(k, (0.0, {}))
            per = dict(per)
            for j, a in enumerate(assign):
                if a:
                    per[vars_[j]] = per.get(vars_[j], 0.0) + 1
            out[k] = (cnt + 1, per)
            return
        for a in (0, 1):
            assign[i] = a
            if all(ok(ci, i) for ci in by_var[i]):
                rec(i + 1)
        assign[i] = 0

    rec(0)
    return out


def _combine(dists: list[tuple[list[Cell], dict[int, tuple[float, dict[Cell, float]]]]], n_other: int,
             remaining: int | None) -> dict[Cell, float]:
    """Spojí nezávislé části hranice; s celkovým počtem min váží řešení počtem rozložení zbylých min."""
    def conv(parts: list[dict[int, float]]) -> dict[int, float]:
        acc = {0: 1.0}
        for d in parts:
            nxt: dict[int, float] = {}
            for a, wa in acc.items():
                for b, wb in d.items():
                    nxt[a + b] = nxt.get(a + b, 0.0) + wa * wb
            acc = nxt
        return acc

    def weight(k_total: int) -> float:
        if remaining is None:
            return 1.0
        rest = remaining - k_total
        if rest < 0 or rest > n_other:
            return 0.0
        return float(math.comb(n_other, rest))

    counts = [{k: v[0] for k, v in d.items()} for _, d in dists]
    probs: dict[Cell, float] = {}
    for i, (vars_, d) in enumerate(dists):
        others = conv(counts[:i] + counts[i + 1:])
        total = 0.0
        mine_w: dict[Cell, float] = {}
        for k, (cnt, per) in d.items():
            w_k = sum(wo * weight(k + ko) for ko, wo in others.items())
            total += cnt * w_k
            for x, m in per.items():
                mine_w[x] = mine_w.get(x, 0.0) + m * w_k
        for x in vars_:
            probs[x] = mine_w.get(x, 0.0) / total if total else 0.5
    return probs


# --- herní smyčka --------------------------------------------------------------------------------

def play(capture: Callable[[], Any], click: Callable[[int, int], None], *, max_seconds: float = 45,
         allow_guess: bool = True, settle: float = 0.45, should_stop: Callable[[], bool] = lambda: False,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         max_clicks_per_round: int = 40) -> dict[str, Any]:
    """Hraje, dokud hra neskončí, nevyprší čas, nebo uživatel nepřevezme myš. Vrací stav a statistiku.

    `capture()` vrací snímek obrazovky (PIL Image), `click(x, y)` klikne na bod snímku."""
    start = clock()
    moves = guesses = rounds = misses = 0
    board: Board | None = None
    seen_board = False
    final_batch = False
    last_guess = False
    status = "time_up"
    while clock() - start < max_seconds:
        if should_stop():
            status = "stopped_by_user"
            break
        current = read_board(capture())
        # Google odkrývá políčka s animací (odletující kousky) – jednají se jen podle dvou stejných snímků
        for _ in range(3):
            if current is None or settle <= 0:
                break
            sleep(min(0.25, settle))
            again = read_board(capture())
            if again is not None and again.cells == current.cells:
                break
            current = again
        if current is None:
            misses += 1
            if seen_board and final_batch:
                status = "won"                     # poslední bezpečná políčka odkryta, hra ukázala výsledek
                break
            if misses >= 3:
                status = ("lost" if last_guess else "finished") if seen_board else "no_board"
                break
            sleep(settle)
            continue
        misses, board, seen_board = 0, current, True
        if board.count(MINE):
            status = "won" if final_batch else "lost"
            break
        unknown, flags, total = board.count(UNKNOWN), board.count(FLAG), board.mines_total
        if unknown == 0 or (total is not None and moves and unknown + flags == total):
            status = "won"
            break
        sol = solve(board.cells, total)
        targets = sorted(sol.safe)[:max_clicks_per_round]
        last_guess = False
        if not targets:
            if not allow_guess or sol.guess is None:
                status = "needs_guess"
                break
            targets = [sol.guess]
            if moves:                               # první tah na Google je vždy bezpečný – není to tip
                guesses += 1
                last_guess = True
        final_batch = (total is not None and not last_guess and len(sol.safe) <= max_clicks_per_round
                       and unknown - flags - len(sol.mines) - len(sol.safe) == 0
                       and unknown + flags - len(sol.safe) == total)
        for r, c in targets:
            if should_stop():
                break
            x, y = board.center(r, c)
            click(x, y)
            moves += 1
            sleep(0.03)
        rounds += 1
        sleep(settle)
    out: dict[str, Any] = {"status": status, "moves": moves, "guesses": guesses, "rounds": rounds}
    if board is not None:
        out.update(board=f"{board.cols}x{board.rows}", mines=board.mines_total,
                   unopened=board.count(UNKNOWN))
    return out


def summary_cs(res: dict[str, Any]) -> str:
    """Česká věta pro seniora podle výsledku hry."""
    st = res.get("status")
    if st == "won":
        return "Vyhrála jsem! Všechny miny jsou objevené."
    if st == "lost":
        return ("Tentokrát jsem šlápla na minu. Musela jsem hádat – někdy se to nedá vyřešit jistě. "
                "Můžeme to zkusit znovu.")
    if st == "stopped_by_user":
        return "Přestala jsem hrát, protože jste pohnuli myší. Hra je vaše."
    if st == "no_board":
        return "Hru Hledání min jsem na obrazovce nenašla. Zkuste prosím zavřít okna, která ji zakrývají."
    if st == "needs_guess":
        return "Dál už se nedá jistě pokračovat, musela bych hádat."
    if st == "time_up":
        return "Hrála jsem, ale hra ještě není dohraná. Můžu pokračovat, když řeknete."
    return "Hra skončila."


# --- simulace hry (testy, simulovaný počítač) -------------------------------------------------------

class SimGame:
    """Skutečná hra Hledání min v paměti, vykreslená jako Google Minesweeper (pro testy a ukázku)."""

    def __init__(self, cols: int = 10, rows: int = 8, mines: int | None = None, seed: int = 0,
                 cell: int = 30, origin: tuple[int, int] = (360, 140), screen: tuple[int, int] = (1280, 720)):
        self.cols, self.rows, self.cell = cols, rows, cell
        self.mines_n = mines if mines is not None else KNOWN_MINES.get((cols, rows), max(1, cols * rows // 6))
        self.origin, self.screen = origin, screen
        self.rng = random.Random(seed)
        self.mines: set[Cell] = set()
        self.opened: set[Cell] = set()
        self.exploded: Cell | None = None
        self.clicks: list[tuple[int, int]] = []
        self.outside_clicks = 0

    @property
    def over(self) -> bool:
        return self.exploded is not None or self.won

    @property
    def won(self) -> bool:
        return bool(self.mines) and len(self.opened) == self.cols * self.rows - self.mines_n

    def _place(self, first: Cell) -> None:
        keep = {first, *neighbors(*first, self.rows, self.cols)}
        free = [(r, c) for r in range(self.rows) for c in range(self.cols) if (r, c) not in keep]
        self.mines = set(self.rng.sample(free, self.mines_n))

    def number(self, r: int, c: int) -> int:
        return sum(1 for n in neighbors(r, c, self.rows, self.cols) if n in self.mines)

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))
        ox, oy = self.origin
        c, r = (x - ox) // self.cell, (y - oy) // self.cell
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            self.outside_clicks += 1
            return
        if self.over:
            return
        if not self.mines:
            self._place((r, c))
        if (r, c) in self.mines:
            self.exploded = (r, c)
            return
        stack = [(r, c)]
        while stack:
            cell = stack.pop()
            if cell in self.opened:
                continue
            self.opened.add(cell)
            if self.number(*cell) == 0:
                stack += [n for n in neighbors(*cell, self.rows, self.cols) if n not in self.opened]

    def render(self) -> Any:
        from PIL import Image, ImageDraw

        from .som import _font

        img = Image.new("RGB", self.screen, (32, 33, 36))
        d = ImageDraw.Draw(img)
        ox, oy = self.origin
        d.rectangle((ox, oy - 60, ox + self.cols * self.cell - 1, oy - 1), fill=(74, 117, 44))   # lišta hry
        font = _font(int(self.cell * 0.7))
        for r in range(self.rows):
            for c in range(self.cols):
                x0, y0 = ox + c * self.cell, oy + r * self.cell
                box = (x0, y0, x0 + self.cell - 1, y0 + self.cell - 1)
                shade = (r + c) % 2
                if (r, c) in self.opened:
                    d.rectangle(box, fill=TAN_SHADES[shade])
                    n = self.number(r, c)
                    if n:
                        d.text((x0 + self.cell * 0.3, y0 + self.cell * 0.1), str(n), fill=NUMBER_COLORS[n],
                               font=font)
                elif self.exploded and (r, c) in self.mines:
                    d.rectangle(box, fill=(219, 50, 54))
                    d.ellipse((x0 + 8, y0 + 8, x0 + self.cell - 9, y0 + self.cell - 9), fill=(142, 32, 35))
                else:
                    d.rectangle(box, fill=GREEN_SHADES[shade])
        return img
