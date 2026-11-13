"""Textové „tlačítko“ v terminálu – místo mluvení napíšete větu.

    python -m aiko.cli                       # server na tomto PC
    python -m aiko.cli --server http://192.168.1.10:8765

Napište např. „Zmizel mi WhatsApp“, Aiko odpoví a případně se zeptá – odpovězte „ano“ / „ne“.
„stop“ zastaví akci, Ctrl+C ukončí.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import httpx

COLORS = {"hotovo": "\033[32m", "odmitnuto": "\033[31m", "chyba": "\033[31m", "potvrzeni": "\033[33m"}
RESET = "\033[0m"
DIM = "\033[2m"


def auth_headers(token: str) -> dict[str, str]:
    from .common.protocol import api_key

    return {"Authorization": f"Bearer {api_key(token)}"} if token else {}


def live_events(server: str, verbose: bool, headers: dict[str, str] | None = None) -> None:
    """Průběžně vypisuje, co Aiko dělá („Otevírám YouTube…“, „Klikám na …“), i během čekání na odpověď.
    Dřív to viděla jen webová stránka /button, v terminálu bylo ticho až do konce."""
    try:
        from websockets.sync.client import connect
    except ImportError:
        return
    url = server.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/ws/ui"
    tty = sys.stdout.isatty()
    while True:
        try:
            with connect(url, open_timeout=5, additional_headers=headers or {}) as ws:
                for raw in ws:
                    msg = json.loads(raw)
                    line = ""
                    if msg.get("type") == "say" and msg.get("text"):
                        line = f"  Aiko: {msg['text']}"
                    elif msg.get("type") == "agent" and verbose:
                        line = f"    (krok {msg.get('step')}: {msg.get('action')} · {str(msg.get('thought'))[:80]})"
                    if line:
                        print(f"\r{DIM}{line}{RESET}" if tty else line, flush=True)
        except Exception:  # noqa: BLE001 – server se restartuje, zkusíme znovu
            time.sleep(3)


def main() -> None:
    p = argparse.ArgumentParser(description="Aiko – textové tlačítko v terminálu")
    p.add_argument("--server", default="http://localhost:8765")
    p.add_argument("-v", "--verbose", action="store_true", help="vypisovat i jednotlivé kroky agenta")
    p.add_argument("--token", default=os.environ.get("AIKO_TOKEN", ""),
                   help="párovací token serveru – nutný jen z jiného počítače, když ho server má")
    a = p.parse_args()
    for s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # agent na pomalé grafice běží i 10 minut – průběh se vypisuje živě, odpověď se počká
    headers = auth_headers(a.token)
    c = httpx.Client(base_url=a.server, timeout=httpx.Timeout(900, connect=10), headers=headers)
    try:
        resp = c.get("/api/state")
    except httpx.HTTPError as e:
        raise SystemExit(f"Server {a.server} neodpovídá ({e}). Spustili jste `python -m aiko.server`?")
    if resp.status_code in (401, 429):
        raise SystemExit(f"Server {a.server} chce párovací token (HTTP {resp.status_code}). Spusťte s "
                         "--token \"...\" nebo nastavte $env:AIKO_TOKEN – stejný jako [aiko] token v aiko.toml.")
    st = resp.json()
    devs = ", ".join(f"{d['name']} ({'online' if d['online'] else 'offline'})" for d in st["devices"]) or "žádná"
    print(f"Aiko · porozumění: {st['llm']} · zařízení: {devs}")
    print("Napište větu (např. „Zmizel mi WhatsApp“). Ctrl+C ukončí.\n")
    threading.Thread(target=live_events, args=(a.server, a.verbose, headers), daemon=True).start()

    while True:
        try:
            text = input("Vy: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        try:
            r = c.post("/api/utterance", json={"text": text, "source": "cli"}).json()
        except httpx.HTTPError as e:
            print(f"(chyba spojení: {e})")
            continue
        color = COLORS.get(r.get("screen", ""), "") if sys.stdout.isatty() else ""
        tag = f" [{r['level']}]" if r.get("level") else ""
        print(f"{color}Aiko{tag}: {r.get('say')}{RESET if color else ''}")
        if r.get("guide"):                         # postup pro seniora, když to Aiko nedokončila
            print("\n  Jak to dokončit:")
            for i, step in enumerate(r["guide"], 1):
                print(f"   {i}. {step}")
        print()


if __name__ == "__main__":
    main()
