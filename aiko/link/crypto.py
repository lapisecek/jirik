"""Kryptografie Jiřík Link sdílená Jiříkem i vzdáleným počítačem."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

PAIR_INFO = b"jirik-pair-v1"


def sign(token: str, msg: dict[str, Any]) -> str:
    """HMAC-SHA256 zprávy (bez pole mac) klíčem zařízení."""
    body = json.dumps({k: v for k, v in msg.items() if k != "mac"}, sort_keys=True, separators=(",", ":"))
    return hmac.new(bytes.fromhex(token), body.encode(), hashlib.sha256).hexdigest()


def verify(token: str, msg: dict[str, Any]) -> bool:
    mac = msg.get("mac")
    return isinstance(mac, str) and hmac.compare_digest(sign(token, msg), mac)


def sas(shared: bytes, a: bytes, b: bytes) -> str:
    """Šestimístný kód, který oba počítače ukážou – shodný jen bez útočníka uprostřed."""
    n = int.from_bytes(hashlib.sha256(b"sas|" + shared + a + b).digest()[:4], "big") % 1_000_000
    return f"{n:06d}"


def pair_key(shared: bytes, sid: str) -> bytes:
    """AES-256 klíč pro předání klíče zařízení (HKDF-SHA256 ze sdíleného tajemství X25519)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=sid.encode(), info=PAIR_INFO).derive(shared)
