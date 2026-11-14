"""Testy bezpečných souborových operací v uživatelských složkách."""
from __future__ import annotations

import pytest

from aiko.agents import pc_files


def _folders(tmp_path):
    f = {
        "desktop": tmp_path / "Desktop",
        "documents": tmp_path / "Documents",
        "downloads": tmp_path / "Downloads",
    }
    for p in f.values():
        p.mkdir()
    return f


def test_resolve_blocks_traversal_and_outside_paths(tmp_path):
    folders = _folders(tmp_path)
    assert pc_files.resolve("plocha", folders) == folders["desktop"].resolve()
    assert pc_files.resolve("stažené soubory/fotky", folders) == (folders["downloads"] / "fotky").resolve()
    with pytest.raises(pc_files.FileToolError):
        pc_files.resolve("desktop/../Documents", folders)
    with pytest.raises(pc_files.FileToolError):
        pc_files.resolve("windows", folders)


def test_find_move_and_openable_safety(tmp_path):
    folders = _folders(tmp_path)
    doc = folders["downloads"] / "Dopis babičce.docx"
    doc.write_text("ahoj", encoding="utf-8")
    exe = folders["downloads"] / "Faktura.pdf.exe"
    exe.write_bytes(b"MZ")

    hits = pc_files.find_files("dopis babicka", folders)
    assert hits["count"] == 1
    assert hits["files"][0]["name"] == "Dopis babičce.docx"

    res = pc_files.move_files(["Dopis babičce.docx"], folders["downloads"], folders["documents"], folders)
    assert res["action"] == "moved"
    assert res["done"] == [{"name": "Dopis babičce.docx", "to": "Dopis babičce.docx"}]
    assert (folders["documents"] / "Dopis babičce.docx").exists()

    # Druhý přesun téhož jména nepřepíše existující soubor, ale přidá „(2)“
    doc.write_text("novy", encoding="utf-8")
    res2 = pc_files.move_files(["Dopis babičce.docx"], folders["downloads"], folders["documents"], folders)
    assert res2["done"] == [{"name": "Dopis babičce.docx", "to": "Dopis babičce (2).docx"}]

    assert pc_files.openable(folders["documents"] / "Dopis babičce.docx", folders)
    with pytest.raises(pc_files.FileToolError):
        pc_files.openable(exe, folders)
    with pytest.raises(pc_files.FileToolError):
        pc_files.printable(folders["documents"], folders)
