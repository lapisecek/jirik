"""Aiko Link – hlídač na vzdáleném počítači.

    pythonw -m aiko.link.node

Jen standardní knihovna, žádná vlákna, žádné připojení ven. Čeká na podepsanou UDP zprávu od Aiko:
  ping → odpoví pong (Aiko pak ví, že je počítač zapnutý),
  wake → spustí plného démona (aiko.agents.pc), který se připojí k Aiko přes TLS s připnutým certifikátem.
Démon po 10 minutách bez práce sám skončí; hlídač zůstává (~10 MB RAM, nulové CPU).
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..common import dpapi
from .crypto import sign

HOME = Path(__file__).resolve().parent.parent.parent
CONF = HOME / "link.json"
LOG = HOME / "logs"
MAX_SKEW = 120
IDLE_EXIT = 600


def log(*a: object) -> None:
    try:
        LOG.mkdir(exist_ok=True)
        with open(LOG / "node.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(map(str, a)) + "\n")
    except OSError:
        pass


class Node:
    def __init__(self, conf: dict) -> None:
        self.nid = conf["node_id"]
        self.name = conf.get("name") or socket.gethostname()
        self.token = dpapi.unprotect(conf["token"])
        self.mac = str(conf.get("mac", ""))
        self.pin = conf["pin"]
        self.port = int(conf.get("port", 8771))
        self.udp_port = int(conf.get("udp_port", 8772))
        self.seen: dict[str, float] = {}
        self.proc: subprocess.Popen | None = None
        self.proc_host = ""
        self.last_spawn = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def spawn(self, host: str) -> None:
        if self.running() and self.proc_host == host:
            return
        if time.time() - self.last_spawn < 5:
            return
        if self.running():                              # Aiko má jinou adresu – démona přesměrovat
            self.proc.terminate()
        self.last_spawn = time.time()
        exe = Path(sys.executable)
        py = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
        env = {**os.environ, "AIKO_TOKEN": self.token, "AIKO_PIN": self.pin, "PYTHONUTF8": "1",
               "AIKO_IDLE_EXIT": str(IDLE_EXIT)}
        LOG.mkdir(exist_ok=True)
        with open(LOG / "agent.log", "a", encoding="utf-8") as out:
            self.proc = subprocess.Popen(
                [str(py), "-m", "aiko.agents.pc", "--server", f"wss://{host}:{self.port}/ws/device",
                 "--id", self.nid, "--name", self.name],
                cwd=HOME, env=env, stdout=out, stderr=out,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.proc_host = host
        log("wake from", host, "→ agent pid", self.proc.pid)

    def handle(self, data: bytes, addr: tuple[str, int], sock: socket.socket) -> None:
        if len(data) > 2048:
            return
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("v") != 1 or msg.get("id") != self.nid \
                or msg.get("t") not in ("ping", "wake"):
            return
        n, ts, mac = msg.get("n"), msg.get("ts"), msg.get("mac")
        now = time.time()
        if not isinstance(n, str) or not isinstance(ts, int) or not isinstance(mac, str) \
                or abs(now - ts) > MAX_SKEW or n in self.seen:
            return
        if not hmac.compare_digest(sign(self.token, msg), mac):
            return
        self.seen[n] = now
        if len(self.seen) > 512:
            self.seen = {k: v for k, v in self.seen.items() if now - v < MAX_SKEW * 2}
        if msg["t"] == "wake":
            self.spawn(addr[0])
        pong = {"v": 1, "t": "pong", "id": self.nid, "ts": int(now), "n": n, "agent": self.running(), "hw": self.mac}
        pong["mac"] = sign(self.token, pong)
        try:
            sock.sendto(json.dumps(pong).encode(), addr)
        except OSError:
            pass

    def serve(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", self.udp_port))
        log("listening on udp", self.udp_port, "as", self.name)
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except (ConnectionResetError, OSError):
                continue
            self.handle(data, addr, sock)


def main() -> None:
    os.chdir(HOME)
    try:
        conf = json.loads(CONF.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log("missing link.json:", e)
        raise SystemExit(1)
    try:
        Node(conf).serve()
    except OSError as e:                               # už běží jiná instance
        log("cannot bind:", e)
        raise SystemExit(0)


if __name__ == "__main__":
    main()
