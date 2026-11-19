"""Jiřík Link – spárování tohoto počítače s Jiříkem (spouští instalátor).

    python -m aiko.link.pair --url http://192.168.1.10:8773/j/<sid>

Výměna klíčů X25519 → oba počítače ukážou stejný šestimístný kód → uživatel ho potvrdí v Jiříkovi →
Jiřík pošle klíč zařízení zašifrovaný AES-256-GCM. Klíč se uloží šifrovaně (DPAPI) do link.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ..common import dpapi

HOME = Path(__file__).resolve().parent.parent.parent
_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


def _outbound_ip(target_host: str, port: int = 80) -> str:
    if not target_host:
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target_host, port or 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def _mac(target_host: str = "", port: int = 80) -> str:
    """Skutečná MAC adresa síťové karty směrem k Jiříkovi (kvůli Wake-on-LAN); jinak uuid.getnode()."""
    if sys.platform == "win32" and target_host:
        ip = _outbound_ip(target_host, port)
        if ip:
            try:
                cmd = (
                    f"$a = Get-NetIPAddress -IPAddress '{ip}' -AddressFamily IPv4 -ErrorAction SilentlyContinue "
                    "| Select-Object -First 1; "
                    "if ($a) { (Get-NetAdapter -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue "
                    "| Select-Object -First 1).MacAddress }"
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                    capture_output=True, text=True, timeout=6,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                mac = r.stdout.strip().replace("-", ":").lower()
                if _MAC_RE.match(mac):
                    return mac
            except (OSError, subprocess.TimeoutExpired):
                pass
    n = uuid.getnode()
    if (n >> 40) & 1:                                  # náhodná (ne skutečná) adresa
        return ""
    return ":".join(f"{(n >> s) & 0xff:02x}" for s in range(40, -1, -8))


def _http(url: str, data: bytes | None = None, timeout: float = 15, retries: int = 3) -> tuple[int, bytes]:
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt + 1 < retries:
                time.sleep(1.0 + attempt)
    raise ConnectionError(f"Nelze se spojit s Jiříkem ({last_err})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    a = ap.parse_args()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from .crypto import pair_key, sas

    base = a.url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    main_host = (parsed.hostname or "").strip()
    pair_port = parsed.port or 8773
    sid = base.rsplit("/", 1)[-1]
    mac = _mac(main_host, pair_port)

    key = X25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    try:
        status, body = _http(
            base + "/hello",
            json.dumps({"pub": base64.b64encode(pub).decode(), "host": socket.gethostname(), "mac": mac}).encode(),
        )
    except ConnectionError as e:
        print(f"\n  {e}\n  Zkontrolujte, že jsou oba počítače ve stejné síti a na hlavním počítači běží Jiřík.\n")
        return 1
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
        try:
            status, body = _http(base + "/result", retries=2)
        except ConnectionError:
            continue
        if status == 202:
            continue
        if status != 200:
            print("  Párování bylo zrušeno nebo vypršelo.\n")
            return 1
        res = json.loads(body)
        conf = json.loads(AESGCM(pair_key(shared, sid)).decrypt(
            base64.b64decode(res["nonce"]), base64.b64decode(res["data"]), sid.encode()))
        plain_token = str(conf["token"])
        conf["token"] = dpapi.protect(plain_token)
        conf["mac"] = mac
        if main_host:
            conf["hosts"] = [main_host]
        path = HOME / "link.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(conf, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        if dpapi.unprotect(reloaded["token"]) != plain_token:
            raise RuntimeError("Ověření DPAPI šifrování link.json selhalo.")
        print(f"  Spárováno jako „{conf['name']}“.\n")
        return 0
    print("  Čas na potvrzení vypršel.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
