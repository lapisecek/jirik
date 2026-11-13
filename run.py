"""Spustí místní Aiko: server (chat + hlas Jirka) a ovládání tohoto počítače, pak otevře chat v prohlížeči.

Spouštějte přes „Start Aiko.bat“. Ukončení: zavřete toto okno nebo Ctrl+C."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8770
URL = f"http://127.0.0.1:{PORT}"


def up(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    os.chdir(HERE)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "AIKO_CONFIG": str(HERE / "aiko.toml")}
    env.pop("AIKO_TOKEN", None)
    if not (HERE / "voices" / "cs_CZ-jirka-medium.onnx").exists():
        print("[!] Chybí hlas voices/cs_CZ-jirka-medium.onnx – Aiko bude mluvit hlasem prohlížeče.")
    if not up("http://localhost:11434/api/tags"):
        print("[!] Ollama neběží – spusťte aplikaci Ollama. Bez ní Aiko rozumí jen jednoduchým větám "
              "a neumí klikat na obrazovce.")
    if up(f"{URL}/api/hello"):
        print(f"Aiko už běží – otevírám {URL}")
        webbrowser.open(URL)
        return 0
    py = sys.executable
    procs = [subprocess.Popen([py, "-m", "aiko.server", "--config", "aiko.toml", "--no-mdns"], cwd=HERE, env=env)]
    try:
        for _ in range(360):
            if up(f"{URL}/api/hello", 1.0):
                break
            if procs[0].poll() is not None:
                print("[!] Server se nespustil – chyba je vypsaná výše.")
                return 1
            time.sleep(0.5)
        procs.append(subprocess.Popen([py, "-m", "aiko.agents.pc", "--server", f"ws://127.0.0.1:{PORT}/ws/device",
                                       "--name", "Můj počítač"], cwd=HERE, env=env))
        time.sleep(1.5)
        webbrowser.open(URL)
        print(f"\n=== Aiko běží: {URL}  (ukončení: zavřete toto okno nebo Ctrl+C) ===\n")
        while procs[0].poll() is None:        # ovládání počítače jde znovu spustit tlačítkem „Spustit vše“
            time.sleep(1)
        print("[!] Server Aiko skončil.")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    sys.exit(main())
