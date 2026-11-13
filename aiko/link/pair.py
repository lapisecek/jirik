"""Jiřík Link – spárování tohoto počítače s Jiříkem (spouští instalátor).

    python -m aiko.link.pair --url http://192.168.1.10:8773/j/<sid>

Výměna klíčů X25519 → oba počítače ukážou stejný šestimístný kód → uživatel ho potvrdí v Jiříkovi →
Jiřík pošle klíč zařízení zašifrovaný AES-256-GCM. Klíč se uloží šifrovaně (DPAPI) do link.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..common import dpapi

HOME = Path(__file__).resolve().parent.parent.parent


def _mac() -> str:
    n = uuid.getnode()
    if (n >> 40) & 1:                                  # náhodná (ne skutečná) adresa
        return ""
    return ":".join(f"{(n >> s) & 0xff:02x}" for s in range(40, -1, -8))


def _http(url: str, data: bytes | None = None, timeout: float = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    a = ap.parse_args()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from .crypto import pair_key, sas

    base = a.url.rstrip("/")
    sid = base.rsplit("/", 1)[-1]
    key = X25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    status, body = _http(base + "/hello", json.dumps({"pub": base64.b64encode(pub).decode(),
                                                      "host": socket.gethostname(), "mac": _mac()}).encode())
    if status != 200:
        print("\n  Párování se nepodařilo – otevřete v Jiříkovi „Přidat zařízení“ znovu.\n")
        return 1
    main_pub = base64.b64decode(json.loads(body)["pub"])
    shared = key.exchange(X25519PublicKey.from_public_bytes(main_pub))
    code = sas(shared, main_pub, pub)
    print("\n  ┌──────────────────────────────┐")
    print(f"  │   Kód pro Jiříka:  {code[:3]} {code[3:]}   │")
    print("  └──────────────────────────────┘")
    print("  Zkontrolujte, že Jiřík ukazuje stejný kód, a potvrďte ho tam.\n")
    end = time.time() + 15 * 60
    while time.time() < end:
        time.sleep(1.5)
        status, body = _http(base + "/result")
        if status == 202:
            continue
        if status != 200:
            print("  Párování bylo zrušeno nebo vypršelo.\n")
            return 1
        res = json.loads(body)
        conf = json.loads(AESGCM(pair_key(shared, sid)).decrypt(
            base64.b64decode(res["nonce"]), base64.b64decode(res["data"]), sid.encode()))
        conf["token"] = dpapi.protect(conf["token"])
        conf["mac"] = _mac()
        path = HOME / "link.json"
        path.write_text(json.dumps(conf, indent=1), encoding="utf-8")
        print(f"  Spárováno jako „{conf['name']}“.\n")
        return 0
    print("  Čas na potvrzení vypršel.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
