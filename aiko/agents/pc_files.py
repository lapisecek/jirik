"""Soubory seniora bez klikání: výpis, hledání, přesun, otevření, tisk.

Jen uvnitř uživatelských složek (Plocha, Dokumenty, Stažené soubory, Obrázky, Hudba, Videa).
Nic se nemaže a nepřepisuje (stejné jméno → „(2)“), programy (.exe, .bat, .ps1…) se nespouštějí –
stažený „Faktura.pdf.exe“ je klasický podvod. Funkce jsou čisté (složky dostanou jako parametr),
takže jdou testovat i bez Windows.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any

ALIASES = {
    "desktop": "desktop", "plocha": "desktop", "plose": "desktop",
    "documents": "documents", "dokumenty": "documents", "dokumentech": "documents", "dokumentu": "documents",
    "downloads": "downloads", "stazene": "downloads", "stazene soubory": "downloads", "stazenych": "downloads",
    "pictures": "pictures", "obrazky": "pictures", "fotky": "pictures", "fotografie": "pictures",
    "music": "music", "hudba": "music",
    "videos": "videos", "videa": "videos",
}
KINDS = {
    "images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".webp", ".tif", ".tiff"},
    "documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".csv"},
    "videos": {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v"},
    "music": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".wma"},
    "archives": {".zip", ".rar", ".7z"},
}
KIND_CZ = {"images": "obrázek", "documents": "dokument", "videos": "video", "music": "hudba", "archives": "archiv",
           "program": "program", "folder": "složka", "other": "soubor"}
# Spustitelné a „zástupce“ – stažený program nebo odkaz je nejčastější cesta podvodu
EXECUTABLE = {".exe", ".msi", ".msix", ".appx", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js",
              ".jse", ".wsf", ".wsh", ".hta", ".scr", ".pif", ".lnk", ".url", ".reg", ".jar", ".cpl", ".msc",
              ".appref-ms", ".iso", ".img", ".vhd", ".vhdx", ".dll", ".sys", ".inf"}
BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')


class FileToolError(ValueError):
    pass


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in t if not unicodedata.combining(c)).strip()


def kind_of(p: Path) -> str:
    if p.is_dir():
        return "folder"
    ext = p.suffix.lower()
    if ext in EXECUTABLE:
        return "program"
    return next((k for k, exts in KINDS.items() if ext in exts), "other")


def resolve(where: str, folders: dict[str, Path]) -> Path:
    """„downloads“, „plocha/pejsek“, „Dokumenty\\Dopisy“ → cesta; nikdy mimo uživatelské složky."""
    raw = str(where or "").replace("\\", "/").strip().strip("/")
    if not raw:
        raise FileToolError("chybí složka (desktop, documents, downloads, pictures, music, videos)")
    parts = [p for p in raw.split("/") if p]
    head = ALIASES.get(_plain(parts[0]))
    rest = parts[1:]
    if head is None and len(parts) >= 2 and ALIASES.get(_plain(" ".join(parts[:2]))):
        head, rest = ALIASES[_plain(" ".join(parts[:2]))], parts[2:]
    if head is None or head not in folders:
        raise FileToolError(f"složku „{parts[0]}“ neznám – jen plocha, dokumenty, stažené soubory, obrázky, hudba, "
                            "videa a složky v nich")
    if any(p in ("..", ".") or BAD_NAME.search(p) for p in rest):
        raise FileToolError("neplatná cesta")
    base = folders[head].resolve()
    path = (base / Path(*rest)).resolve() if rest else base
    if path != base and base not in path.parents:
        raise FileToolError("mimo uživatelské složky Aiko nesahá")
    return path


def _inside(path: Path, folders: dict[str, Path]) -> bool:
    path = path.resolve()
    return any(path == b.resolve() or b.resolve() in path.parents for b in folders.values())


def _when(ts: float, now: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    today = dt.datetime.fromtimestamp(now).date()
    if d.date() == today:
        return f"dnes {d:%H:%M}"
    if d.date() == today - dt.timedelta(days=1):
        return f"včera {d:%H:%M}"
    return f"{d:%d.%m.%Y %H:%M}"


def _entry(p: Path, now: float, folders: dict[str, Path] | None = None) -> dict[str, Any]:
    st = p.stat()
    k = kind_of(p)
    e: dict[str, Any] = {"name": p.name, "kind": KIND_CZ[k], "modified": _when(st.st_mtime, now)}
    if k != "folder":
        e["size_kb"] = max(1, st.st_size // 1024)
    if k == "program":
        e["warning"] = "program – Aiko ho nespouští"
    if folders:
        for key, base in folders.items():
            try:
                rel = p.resolve().parent.relative_to(base.resolve())
            except ValueError:
                continue
            e["where"] = "/".join([key, *rel.parts])
            break
    return e


def list_files(folder: Path, days: float = 0, kind: str = "", limit: int = 30,
               now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    if not folder.is_dir():
        raise FileToolError(f"složka {folder.name} neexistuje")
    want = {"obrazky": "images", "fotky": "images", "dokumenty": "documents", "videa": "videos",
            "hudba": "music"}.get(_plain(kind), _plain(kind))
    items = []
    for p in folder.iterdir():
        try:
            if p.name.startswith(("~$", ".")) or p.name.lower() in ("desktop.ini", "thumbs.db"):
                continue
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if days and now - mtime > float(days) * 86400:
            continue
        if want and kind_of(p) != want:
            continue
        items.append((mtime, p))
    items.sort(key=lambda t: t[0], reverse=True)
    limit = max(1, min(int(limit or 30), 100))
    return {"folder": str(folder), "count": len(items),
            "files": [_entry(p, now) for _m, p in items[:limit]]}


def find_files(name: str, folders: dict[str, Path], where: str = "", days: float = 0, limit: int = 20,
               budget_s: float = 4.0, max_depth: int = 5, now: float | None = None) -> dict[str, Any]:
    """Hledá podle části jména (bez diakritiky: „dopis babicka“ najde „Dopis babičce.docx“)."""
    now = now or time.time()
    # kmen slova: „babicka“ najde „babičce“, „dopisy“ najde „dopis“ (čeština skloňuje)
    words = [w[:max(4, len(w) - 2)] if len(w) >= 6 else w for w in re.findall(r"\w+", _plain(name)) if len(w) >= 2]
    if not words:
        raise FileToolError("chybí, co hledat")
    roots = [resolve(where, folders)] if where else list(dict.fromkeys(b.resolve() for b in folders.values()))
    deadline = time.time() + budget_s
    hits: list[tuple[float, Path]] = []
    stack = [(r, 0) for r in roots if r.is_dir()]
    scanned = 0
    while stack and time.time() < deadline and scanned < 30000:
        d, depth = stack.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for p in children:
            scanned += 1
            if p.name.startswith((".", "~$")) or p.name.lower() in ("appdata", "node_modules", "$recycle.bin"):
                continue
            plain = _plain(p.name)
            try:
                is_dir = p.is_dir()
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if all(w in plain for w in words) and not (days and now - mtime > float(days) * 86400):
                hits.append((mtime, p))
            if is_dir and depth < max_depth and not p.is_symlink():
                stack.append((p, depth + 1))
    hits.sort(key=lambda t: t[0], reverse=True)
    return {"query": name, "count": len(hits), "complete": not stack,
            "files": [_entry(p, now, folders) for _m, p in hits[:max(1, min(int(limit or 20), 50))]]}


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    for i in range(2, 1000):
        cand = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not cand.exists():
            return cand
    raise FileToolError("příliš mnoho souborů se stejným jménem")


def move_files(names: list[str], source: Path, target: Path, folders: dict[str, Path],
               copy: bool = False) -> dict[str, Any]:
    if not _inside(source, folders) or not _inside(target, folders):
        raise FileToolError("mimo uživatelské složky Aiko nesahá")
    if not target.is_dir():
        raise FileToolError(f"cílová složka {target.name} neexistuje – vytvořte ji nástrojem create_folder")
    if not isinstance(names, list) or not names or len(names) > 200:
        raise FileToolError("names musí být seznam 1–200 jmen souborů")
    done, skipped = [], []
    for n in names:
        n = str(n)
        src = (source / n).resolve()
        if BAD_NAME.search(n) or "/" in n or "\\" in n or src.parent != source.resolve() or not src.is_file():
            skipped.append({"name": n, "why": "není ve zdrojové složce"})
            continue
        if src.resolve() == (target / n).resolve():
            skipped.append({"name": n, "why": "už je v cílové složce"})
            continue
        dst = _unique(target / src.name)
        (shutil.copy2 if copy else shutil.move)(str(src), str(dst))
        done.append({"name": src.name, "to": dst.name})
    return {"action": "copied" if copy else "moved", "target": str(target), "done": done, "skipped": skipped,
            "undo": [{"from": str(target / d["to"]), "to": str(source / d["name"])} for d in done] if not copy else []}


def openable(path: Path, folders: dict[str, Path]) -> Path:
    """Soubor nebo složka, kterou smí Aiko otevřít (ve výchozí aplikaci / v Průzkumníku)."""
    if not path.exists():
        raise FileToolError(f"„{path.name}“ neexistuje")
    if not _inside(path, folders):
        raise FileToolError("mimo uživatelské složky Aiko nesahá")
    if path.is_file() and kind_of(path) == "program":
        raise FileToolError(f"„{path.name}“ je program nebo zástupce – Aiko programy ze souborů nespouští "
                            "(stažené programy bývají podvod)")
    return path


def printable(path: Path, folders: dict[str, Path]) -> Path:
    path = openable(path, folders)
    if not path.is_file() or kind_of(path) not in ("documents", "images"):
        raise FileToolError("tisknout jde jen dokument nebo obrázek")
    return path


def known_folders_windows(reg_get: Any = None) -> dict[str, Path]:
    """Skutečné cesty složek (i přesměrované do OneDrive) z registru Windows."""
    keys = {"desktop": "Desktop", "documents": "Personal", "pictures": "My Pictures", "music": "My Music",
            "videos": "My Video", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
    fallback = {"desktop": "Desktop", "documents": "Documents", "pictures": "Pictures", "music": "Music",
                "videos": "Videos", "downloads": "Downloads"}
    out = {}
    for k, reg in keys.items():
        raw = reg_get(reg) if reg_get else None
        p = Path(os.path.expandvars(str(raw))) if raw else Path.home() / fallback[k]
        if p.is_dir():
            out[k] = p
    return out
