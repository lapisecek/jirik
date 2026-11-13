"""Set-of-Marks: do snímku obrazovky dokreslí očíslované štítky u klikatelných prvků.

AI pak nehádá pixely, ale řekne „klikni na [3]“. Souřadnice prvků drží klient
v původním rozlišení, model dostává zmenšený obrázek (max. 1600 px), takže
souřadnice z modelu se přepočítávají přes `scale`.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any

PALETTE = [(230, 25, 75), (60, 140, 60), (0, 92, 230), (245, 130, 48), (145, 30, 180),
           (0, 150, 150), (200, 30, 160), (120, 90, 0)]


@dataclass
class Element:
    id: int
    label: str
    bounds: tuple[int, int, int, int]      # x1, y1, x2, y2 v pixelech obrazovky
    kind: str = "button"                   # button | input | switch | text
    password: bool = False
    priority: int = 0                      # 3 menu, 2 hlavní panel, 1 obsah stránky – nevypadnou kvůli limitu

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def contains(self, x: int, y: int) -> bool:
        x1, y1, x2, y2 = self.bounds
        return x1 <= x <= x2 and y1 <= y <= y2

    def public(self, scale: float) -> dict[str, Any]:
        d = {"id": self.id, "label": self.label[:80], "kind": self.kind,
             "bounds": [round(v * scale) for v in self.bounds]}
        if self.password:
            d["password"] = True
        return d


def number(elements: list[Element], limit: int = 80) -> list[Element]:
    """Odstraní duplicity, vybere nejvýš `limit` prvků (položky otevřeného menu vždy) a očísluje
    je 1..N shora dolů."""
    seen: set[tuple[int, int, int, int]] = set()
    out = []
    for e in sorted(elements, key=lambda e: (-int(e.priority), e.bounds[1] // 12, e.bounds[0])):
        x1, y1, x2, y2 = e.bounds
        if x2 - x1 < 6 or y2 - y1 < 6 or e.bounds in seen:
            continue
        seen.add(e.bounds)
        out.append(e)
        if len(out) >= limit:
            break
    out.sort(key=lambda e: (e.bounds[1] // 12, e.bounds[0]))
    for i, e in enumerate(out, 1):
        e.id = i
    return out


def element_at(elements: list[Element], x: int, y: int) -> Element | None:
    hits = [e for e in elements if e.contains(x, y)]
    return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1])) if hits else None


def _font(size: int):  # noqa: ANN202
    from PIL import ImageFont

    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render(image: bytes | Any, elements: list[Element], max_side: int = 1600,
           quality: int = 75) -> tuple[str, float, tuple[int, int]]:
    """Vrátí (WebP v base64 se štítky, měřítko, velikost obrázku)."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(image)) if isinstance(image, (bytes, bytearray)) else image
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    draw = ImageDraw.Draw(img, "RGBA")
    # Malé poloprůhledné štítky: velké zakrývaly text tlačítek a model pak hůř četl, co kde je.
    size = max(11, round(min(img.size) / 70))
    font = _font(size)
    for e in elements:
        color = PALETTE[e.id % len(PALETTE)]
        x1, y1, x2, y2 = (round(v * scale) for v in e.bounds)
        draw.rectangle((x1, y1, x2, y2), outline=color + (200,), width=1)
        tag = str(e.id)
        tw = draw.textlength(tag, font=font)
        th = size + 2
        # štítek nad prvek, pokud je místo a prvek je nízký (položky menu, ikony) – jinak do rohu
        tx, ty = max(0, x1), (y1 - th if y1 - th >= 0 and (y2 - y1) < 3 * th else max(0, y1))
        draw.rectangle((tx, ty, tx + tw + 4, ty + th), fill=color + (190,))
        draw.text((tx + 2, ty), tag, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality, method=4)
    return base64.b64encode(buf.getvalue()).decode(), scale, img.size
