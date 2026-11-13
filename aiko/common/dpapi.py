"""Šifrování tajemství na disku přes Windows DPAPI (vázané na účet uživatele, jen standardní knihovna).

Klíče zařízení Jiřík Link tak v souborech nejsou čitelně: soubor zkopírovaný na jiný počítač nebo
přečtený jiným uživatelem je k ničemu. Mimo Windows se ukládá prostý text (jen pro vývoj)."""
from __future__ import annotations

import base64
import ctypes
import sys

PREFIX = "dpapi:"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _call(fn, data: bytes, entropy: bytes) -> bytes:  # noqa: ANN001
    src, ent, out = _blob(data), _blob(entropy), _Blob()
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError("DPAPI selhalo")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(secret: str, entropy: str = "jirik-link") -> str:
    if sys.platform != "win32":
        return secret
    crypt = ctypes.windll.crypt32
    return PREFIX + base64.b64encode(_call(crypt.CryptProtectData, secret.encode(), entropy.encode())).decode()


def unprotect(value: str, entropy: str = "jirik-link") -> str:
    if not value.startswith(PREFIX):
        return value                                    # starší / nešifrovaný záznam
    crypt = ctypes.windll.crypt32
    raw = base64.b64decode(value[len(PREFIX):])
    return _call(crypt.CryptUnprotectData, raw, entropy.encode()).decode()
