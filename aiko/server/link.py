"""Jiřík Link – ovládání dalších počítačů v síti.

Párování (jednou, ~1 minuta)
  1. V Jiříkovi „Přidat zařízení“ → na 15 minut se otevře párovací stránka v místní síti.
  2. Na druhém počítači otevřete její adresu, stáhnete instalátor a spustíte ho (doinstaluje vše samo).
  3. Oba počítače si vymění klíče X25519 a ukážou stejný šestimístný kód. Kliknutím „Kódy se shodují“
     potvrdíte, že mezi nimi nikdo nesedí (útočník v síti by měl jiný kód). Teprve pak Jiřík pošle
     zařízení jeho klíč – zašifrovaný AES-256-GCM klíčem z výměny.

Provoz
  * Na druhém počítači běží malý hlídač (aiko.link.node, ~13 MB RAM). Na podepsané „probuď se“
    (HMAC-SHA256, časové razítko, jednorázové číslo) spustí plné ovládání, které se připojí zpět přes
    TLS s připnutým certifikátem a vzájemným ověřením klíčem; po 10 minutách bez práce skončí.
  * Spící nebo vypnutý počítač Jiřík zkusí probudit Wake-on-LAN (magic packet na jeho MAC adresu).
  * Klíče jsou na disku šifrované DPAPI (vázané na účet Windows). Chat a API zůstávají na 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import socket
import ssl
import time
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..common import dpapi
from ..common.protocol import AUTH_SCHEME, auth_proof, new_nonce, proof_ok
from ..link.crypto import pair_key, sas, sign, verify
from .config import Config
from .registry import Device, Registry, dumps

log = logging.getLogger("aiko.link")
PKG = Path(__file__).resolve().parent.parent
PAIR_TTL = 15 * 60


def local_ips() -> list[str]:
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("neplatná MAC adresa")
    return b"\xff" * 6 + raw * 16


def package_zip() -> bytes:
    """Kód pro druhý počítač (žádná tajemství – klíč se předá až po potvrzení kódu)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sub in ("__init__.py", "common", "agents", "link"):
            src = PKG / sub
            files = [src] if src.is_file() else sorted(p for p in src.rglob("*.py") if "__pycache__" not in p.parts)
            for f in files:
                z.write(f, "JirikLink/aiko/" + f.relative_to(PKG).as_posix())
        for f in sorted((PKG / "link" / "install").iterdir()):
            z.write(f, "JirikLink/" + f.name)
    return buf.getvalue()


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, on_msg: Callable[[dict[str, Any], tuple[str, int]], None]):
        self.on_msg = on_msg

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > 2048:
            return
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict):
            self.on_msg(msg, addr)


class Pairing:
    """Jedno otevřené párování: stránka ke stažení + výměna klíčů + potvrzení kódu."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        self.sid = secrets.token_urlsafe(9)
        self.key = X25519PrivateKey.generate()
        self.created = time.time()
        self.state = "waiting"              # waiting → code → confirmed | rejected | done | expired
        self.host = ""
        self.mac = ""
        self.code = ""
        self.shared = b""
        self.node_pub = b""
        self.result: dict[str, Any] | None = None
        self.node_id = ""

    @property
    def pub(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def expired(self) -> bool:
        return time.time() - self.created > PAIR_TTL


class Link:
    def __init__(self, cfg: Config, registry: Registry, emit: Callable[[dict[str, Any]], Awaitable[None]]):
        self.cfg = cfg
        self.registry = registry
        self.emit = emit
        self.dir = cfg.data_dir / "link"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.nodes: dict[str, dict[str, Any]] = self._load()
        self._sent: dict[str, tuple[str, float]] = {}
        self._seen: dict[str, float] = {}
        self._udp: asyncio.DatagramTransport | None = None
        self._gateway: Any = None
        self._tasks: list[asyncio.Task] = []
        self._fails: dict[str, list[float]] = {}
        self.pairing: Pairing | None = None
        self._pair_server: asyncio.AbstractServer | None = None
        self._zip: bytes | None = None

    # --- uložená zařízení (klíče šifrované DPAPI) ------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads((self.dir / "nodes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for nid, n in raw.items():
            try:
                out[nid] = {**n, "token": dpapi.unprotect(n["token"])}
            except (OSError, KeyError, ValueError):
                log.warning("Klíč zařízení %s nejde dešifrovat – spárujte ho znovu", nid)
        return out

    def _save(self) -> None:
        data = {nid: {**n, "token": dpapi.protect(n["token"])} for nid, n in self.nodes.items()}
        path = self.dir / "nodes.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    def public(self) -> list[dict[str, Any]]:
        out = []
        for nid, n in self.nodes.items():
            dev = self.registry.devices.get(nid)
            if dev and dev.available:
                state = "online"
            elif time.time() - self._seen.get(nid, 0) < 75:
                state = "sleeping"
            else:
                state = "offline"
            st = (dev.status if dev and dev.available else {}) or {}
            out.append({"id": nid, "name": n["name"], "state": state, "ip": n.get("ip", ""), "mac": n.get("mac", ""),
                        "host": n.get("host", ""), "paired": n.get("paired", 0), "locked": bool(st.get("locked")),
                        "foreground": st.get("foreground", "") if state == "online" else ""})
        return out

    # --- certifikát brány --------------------------------------------------------------------
    def _cert(self) -> tuple[Path, Path, str]:
        cert, key = self.dir / "gateway.crt", self.dir / "gateway.key"
        if not (cert.exists() and key.exists()):
            from datetime import datetime, timedelta, timezone

            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.x509.oid import NameOID

            k = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jirik Link")])
            now = datetime.now(timezone.utc)
            c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
                 .not_valid_after(now + timedelta(days=3650)).sign(k, hashes.SHA256()))
            key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
            cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
            log.info("Vytvořen certifikát Jiřík Link")
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        return cert, key, hashlib.sha256(der).hexdigest()

    @property
    def pin(self) -> str:
        return self._cert()[2]

    # --- start / stop --------------------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        self._udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(self._on_udp), sock=sock)
        self._tasks.append(asyncio.create_task(self._ping_loop()))
        if self.nodes:
            await self._start_gateway()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._udp:
            self._udp.close()
        if self._gateway:
            self._gateway.close()
        await self.close_pairing()

    async def _start_gateway(self) -> None:
        if self._gateway:
            return
        import websockets

        cert, key, _ = self._cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        try:
            self._gateway = await websockets.serve(self._device_conn, "0.0.0.0", self.cfg.link_port, ssl=ctx,
                                                   max_size=12 * 1024 * 1024, ping_interval=15, ping_timeout=15)
            log.info("Jiřík Link: TLS brána poslouchá na portu %d", self.cfg.link_port)
        except OSError as e:
            log.warning("Jiřík Link: bránu nejde spustit na portu %d: %s", self.cfg.link_port, e)

    # --- brána: vzdálení démoni ---------------------------------------------------------------
    def _blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(ip, []) if now - t < 600]
        self._fails[ip] = recent
        return len(recent) >= 8

    async def _device_conn(self, ws: Any) -> None:
        ip = (ws.remote_address or ("?",))[0]
        if self._blocked(ip):
            await ws.close(1008)
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            nid = str(hello.get("device_id") or "")
            node = self.nodes.get(nid)
            nd = hello.get("nonce")
            if not node or hello.get("type") != "hello" or hello.get("auth") != AUTH_SCHEME \
                    or not isinstance(nd, str) or not 8 <= len(nd) <= 128:
                raise PermissionError("neznámé zařízení")
            ns = new_nonce()
            await ws.send(dumps({"type": "challenge", "nonce": ns,
                                 "proof": auth_proof(node["token"], "server", nd, ns)}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if not (isinstance(msg, dict) and msg.get("type") == "auth"
                    and proof_ok(node["token"], "device", ns, nd, msg.get("proof"))):
                raise PermissionError("špatný klíč")
        except Exception as e:  # noqa: BLE001
            self._fails.setdefault(ip, []).append(time.time())
            log.warning("Jiřík Link: odmítnuto spojení z %s (%s)", ip, e)
            try:
                await ws.close(1008)
            except Exception:  # noqa: BLE001
                pass
            return
        hello["name"] = node["name"]
        hello["kind"] = "pc"
        lock = asyncio.Lock()

        async def send(m: dict[str, Any]) -> None:
            async with lock:
                await ws.send(dumps(m))

        if node.get("ip") != ip:
            node["ip"] = ip
            self._save()
        self._seen[nid] = time.time()
        dev = await self.registry.attach(hello, send)
        await self.changed()
        try:
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") == "result":
                    self.registry.on_result(m)
                elif m.get("type") == "status":
                    await self.registry.update_status(dev.device_id, m.get("status") or {})
        except Exception:  # noqa: BLE001
            pass
        finally:
            await self.registry.detach(dev.device_id, send)
            await self.changed()

    # --- UDP: probouzení a „jsi tam?“ ------------------------------------------------------------
    def _send(self, nid: str, kind: str) -> None:
        node = self.nodes.get(nid)
        if not node or not self._udp:
            return
        n = secrets.token_hex(12)
        msg = {"v": 1, "t": kind, "id": nid, "ts": int(time.time()), "n": n, "port": self.cfg.link_port}
        msg["mac"] = sign(node["token"], msg)
        data = json.dumps(msg).encode()
        self._sent[n] = (nid, time.time())
        targets = {("255.255.255.255", self.cfg.link_udp_port)}
        if node.get("ip"):
            targets.add((node["ip"], self.cfg.link_udp_port))
        for t in targets:
            try:
                self._udp.sendto(data, t)
            except OSError:
                pass

    def wol(self, nid: str) -> bool:
        node = self.nodes.get(nid) or {}
        if not node.get("mac") or not self._udp:
            return False
        try:
            pkt = magic_packet(node["mac"])
        except ValueError:
            return False
        targets = {("255.255.255.255", 9), ("255.255.255.255", 7)}
        if node.get("ip"):
            parts = node["ip"].split(".")
            targets.add((".".join(parts[:3] + ["255"]), 9))
        for t in targets:
            try:
                self._udp.sendto(pkt, t)
            except OSError:
                pass
        return True

    def _on_udp(self, msg: dict[str, Any], addr: tuple[str, int]) -> None:
        if msg.get("t") != "pong":
            return
        sent = self._sent.pop(str(msg.get("n")), None)
        nid = str(msg.get("id") or "")
        node = self.nodes.get(nid)
        if not sent or sent[0] != nid or not node or not verify(node["token"], msg):
            return
        was = self._state(nid)
        self._seen[nid] = time.time()
        changed = False
        if node.get("ip") != addr[0]:
            node["ip"], changed = addr[0], True
        if isinstance(msg.get("hw"), str) and len(msg["hw"]) == 17 and node.get("mac") != msg["hw"]:
            node["mac"], changed = msg["hw"], True
        if changed:
            self._save()
        if self._state(nid) != was:
            asyncio.get_running_loop().create_task(self.changed())

    def _state(self, nid: str) -> str:
        return next((n["state"] for n in self.public() if n["id"] == nid), "offline")

    async def changed(self) -> None:
        await self.emit({"type": "nodes", "nodes": self.public()})

    async def _ping_loop(self) -> None:
        while True:
            before = {n["id"]: n["state"] for n in self.public()}
            for nid in list(self.nodes):
                self._send(nid, "ping")
            now = time.time()
            self._sent = {k: v for k, v in self._sent.items() if now - v[1] < 30}
            await asyncio.sleep(3)
            if {n["id"]: n["state"] for n in self.public()} != before:
                await self.changed()
            if self.pairing and self.pairing.expired and self.pairing.state in ("waiting", "code"):
                self.pairing.state = "expired"
                await self._pair_event()
                await self.close_pairing()
            await asyncio.sleep(27)

    async def wake(self, nid: str, timeout: float = 30) -> Device:
        """Probudí zařízení (hlídač, případně Wake-on-LAN) a počká, než se jeho ovládání připojí."""
        dev = self.registry.devices.get(nid)
        if dev and dev.available:
            return dev
        if nid not in self.nodes:
            raise LookupError("neznámé zařízení")
        await self._start_gateway()
        log.info("Jiřík Link: budím %s", self.nodes[nid]["name"])
        asleep = self._state(nid) == "offline"
        if asleep and self.wol(nid):                  # nereaguje – zkusit ho probudit ze spánku
            timeout = max(timeout, 75)
        end, last, last_wol = time.time() + timeout, 0.0, time.time()
        while time.time() < end:
            if time.time() - last > 3:
                self._send(nid, "wake")
                last = time.time()
            if asleep and time.time() - last_wol > 15:
                self.wol(nid)
                last_wol = time.time()
            await asyncio.sleep(0.25)
            dev = self.registry.devices.get(nid)
            if dev and dev.available:
                return dev
        raise TimeoutError(f"{self.nodes[nid]['name']} se neozval – je zapnutý, přihlášený a ve stejné síti?")

    async def remove(self, nid: str) -> bool:
        if self.nodes.pop(nid, None) is None:
            return False
        self._save()
        dev = self.registry.devices.pop(nid, None)
        if dev and dev.send:
            try:
                await dev.send({"type": "revoked"})
            except Exception:  # noqa: BLE001
                pass
        await self.changed()
        await self.registry._changed()
        return True

    async def rename(self, nid: str, name: str) -> bool:
        name = " ".join(str(name).split())[:40]
        if nid not in self.nodes or not name:
            return False
        self.nodes[nid]["name"] = name
        self._save()
        dev = self.registry.devices.get(nid)
        if dev:
            dev.name = name
        await self.changed()
        return True

    # --- párování ------------------------------------------------------------------------------
    def pair_public(self) -> dict[str, Any] | None:
        p = self.pairing
        if not p:
            return None
        return {"sid": p.sid, "state": p.state, "host": p.host, "code": p.code, "node_id": p.node_id,
                "urls": [f"http://{ip}:{self.cfg.pair_port}/j/{p.sid}" for ip in local_ips()],
                "expires": round(p.created + PAIR_TTL - time.time())}

    async def _pair_event(self) -> None:
        await self.emit({"type": "pairing", "pairing": self.pair_public()})

    async def open_pairing(self) -> dict[str, Any]:
        if self.pairing and not self.pairing.expired and self.pairing.state in ("waiting", "code"):
            return self.pair_public() or {}
        await self.close_pairing()
        self.pairing = Pairing()
        self._zip = package_zip()
        try:
            self._pair_server = await asyncio.start_server(self._pair_http, "0.0.0.0", self.cfg.pair_port)
        except OSError as e:
            self.pairing = None
            raise RuntimeError(f"Párovací port {self.cfg.pair_port} je obsazený: {e}") from e
        await self._start_gateway()
        log.info("Párování otevřené na 15 minut")
        await self._pair_event()
        return self.pair_public() or {}

    async def close_pairing(self) -> None:
        if self._pair_server:
            self._pair_server.close()
            self._pair_server = None
        if self.pairing and self.pairing.state in ("waiting", "code"):
            self.pairing.state = "closed"
        self._zip = None

    async def confirm_pairing(self, ok: bool, name: str = "") -> dict[str, Any]:
        p = self.pairing
        if not p or p.state != "code" or p.expired:
            raise LookupError("žádné párování nečeká na potvrzení")
        if not ok:
            p.state = "rejected"
            await self._pair_event()
            await self.close_pairing()
            return {"ok": False}
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nid = "link-" + secrets.token_hex(4)
        token = secrets.token_hex(32)
        name = " ".join((name or p.host or "Počítač").split())[:40]
        self.nodes[nid] = {"name": name, "token": token, "ip": "", "mac": p.mac, "host": p.host,
                           "paired": int(time.time())}
        self._save()
        conf = {"node_id": nid, "name": name, "token": token, "pin": self.pin, "port": self.cfg.link_port,
                "udp_port": self.cfg.link_udp_port}
        nonce = os.urandom(12)
        ct = AESGCM(pair_key(p.shared, p.sid)).encrypt(nonce, json.dumps(conf).encode(), p.sid.encode())
        p.result = {"nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(ct).decode()}
        p.state, p.node_id = "confirmed", nid
        await self._pair_event()
        await self.changed()
        return {"ok": True, "id": nid}

    async def _pair_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Malý HTTP server jen pro párování (stránka, instalátor, výměna klíčů). Nic jiného neumí."""
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(head) > 8192:
                raise ValueError("hlavička je moc dlouhá")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
            body = b""
            n = int(headers.get("content-length", "0") or 0)
            if 0 < n <= 4096:
                body = await asyncio.wait_for(reader.readexactly(n), 10)
            status, ctype, data, extra = await self._pair_route(method, path, body,
                                                               writer.get_extra_info("peername"),
                                                               headers.get("host", ""))
        except Exception as e:  # noqa: BLE001
            status, ctype, data, extra = 400, "text/plain; charset=utf-8", str(e).encode()[:200], {}
        hdr = {"Content-Type": ctype, "Content-Length": str(len(data)), "Connection": "close",
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **extra}
        writer.write(f"HTTP/1.1 {status} {'OK' if status < 400 else 'Error'}\r\n".encode()
                     + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()).encode() + b"\r\n" + data)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _pair_route(self, method: str, path: str, body: bytes, peer: Any,
                          host: str) -> tuple[int, str, bytes, dict[str, str]]:
        p = self.pairing
        parts = [x for x in path.split("?")[0].split("/") if x]
        if not p or p.expired or len(parts) < 2 or parts[0] != "j" or not hmac.compare_digest(parts[1], p.sid):
            return 404, "text/plain; charset=utf-8", "Párování už neplatí – otevřete ho v Jiříkovi znovu.".encode(), {}
        sub = parts[2] if len(parts) > 2 else ""
        base = f"http://{host or local_ips()[0]}/j/{p.sid}"
        if method == "GET" and sub == "":
            return 200, "text/html; charset=utf-8", PAGE.replace("{BASE}", base).encode(), {}
        if method == "GET" and sub == "jirik-link.zip":
            return 200, "application/zip", self._zip or b"", {}
        if method == "GET" and sub == "Jirik-Link-Setup.bat":
            digest = hashlib.sha256(self._zip or b"").hexdigest().upper()
            bat = SETUP_BAT.replace("{BASE}", base).replace("{HASH}", digest)
            return 200, "application/octet-stream", bat.replace("\n", "\r\n").encode("utf-8"), {
                "Content-Disposition": 'attachment; filename="Jirik-Link-Setup.bat"'}
        if method == "POST" and sub == "hello":
            if p.state != "waiting":
                return 409, "application/json", b'{"error":"busy"}', {}
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

            d = json.loads(body or b"{}")
            node_pub = base64.b64decode(d["pub"])
            if len(node_pub) != 32:
                raise ValueError("špatný klíč")
            p.node_pub = node_pub
            p.shared = p.key.exchange(X25519PublicKey.from_public_bytes(node_pub))
            p.code = sas(p.shared, p.pub, node_pub)
            p.host = " ".join(str(d.get("host", ""))[:40].split())
            mac = str(d.get("mac", ""))
            p.mac = mac if len(mac) == 17 else ""
            p.state = "code"
            log.info("Párování: %s (%s) ukazuje kód", p.host, peer[0] if peer else "?")
            await self._pair_event()
            return 200, "application/json", json.dumps({"pub": base64.b64encode(p.pub).decode()}).encode(), {}
        if method == "GET" and sub == "result":
            if p.state == "confirmed" and p.result:
                res = json.dumps(p.result).encode()
                p.state = "done"
                await self._pair_event()
                asyncio.get_running_loop().call_later(2, lambda: asyncio.ensure_future(self.close_pairing()))
                return 200, "application/json", res, {}
            if p.state in ("rejected", "expired", "closed"):
                return 403, "application/json", b'{"error":"rejected"}', {}
            return 202, "application/json", b'{"wait":true}', {}
        return 404, "text/plain", b"", {}


SETUP_BAT = r"""@echo off
chcp 65001 >nul
title Jiřík Link
echo.
echo   Jiřík Link – instalace na tento počítač
echo   Stahuji balíček z Jiříka v místní síti…
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $d=Join-Path $env:TEMP 'JirikLink-setup'; if (Test-Path $d) { Remove-Item $d -Recurse -Force }; New-Item -ItemType Directory $d | Out-Null; Invoke-WebRequest '{BASE}/jirik-link.zip' -OutFile (Join-Path $d 'p.zip') -UseBasicParsing; if ((Get-FileHash (Join-Path $d 'p.zip') -Algorithm SHA256).Hash -ne '{HASH}') { throw 'Balíček je poškozený nebo podvržený – instalace zastavena.' }; Expand-Archive (Join-Path $d 'p.zip') $d -Force; & (Join-Path $d 'JirikLink\install.ps1') -PairUrl '{BASE}'"
echo.
pause
"""

PAGE = """<!doctype html><html lang="cs"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jiřík Link</title><style>
:root{--paper:#F4F2EE;--surface:#FBFAF8;--ink:#1B1A17;--mute:#8A867D;--line:#E2DED6;--accent:#E0492A}
@media (prefers-color-scheme:dark){:root{--paper:#131210;--surface:#1B1A17;--ink:#EEEBE4;--mute:#8C877C;--line:#2A2824;--accent:#F0603E}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--paper);color:var(--ink);
font:15px/1.55 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;padding:16px}
.card{max-width:440px;width:100%;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:28px;animation:up .6s cubic-bezier(.16,1,.3,1) both}
@keyframes up{from{opacity:0;transform:translateY(12px)}}
h1{font:600 24px/1.15 "Segoe UI Variable Display","Segoe UI",sans-serif;margin:16px 0 6px;letter-spacing:-.02em}
p{color:var(--mute);margin:0 0 18px}ol{padding-left:20px;margin:0 0 22px}li{margin:6px 0}
a.btn{display:flex;justify-content:center;align-items:center;gap:10px;height:46px;border-radius:12px;background:var(--ink);color:var(--paper);
text-decoration:none;font-weight:600;transition:transform .25s cubic-bezier(.22,1.18,.36,1)}a.btn:active{transform:scale(.97)}
.dot{width:46px;height:46px;border-radius:50%;background:var(--accent)}small{display:block;margin-top:14px;color:var(--mute);font-size:12.5px}
</style></head><body><div class="card"><div class="dot"></div><h1>Připojit k Jiříkovi</h1>
<p>Tento počítač půjde ovládat z Jiříka. Zabere to asi minutu.</p>
<ol><li>Stáhněte instalátor (prohlížeč se může zeptat – zvolte <b>Ponechat</b>).</li><li>Spusťte ho dvojklikem.</li>
<li>Až uvidíte šestimístný kód, potvrďte v Jiříkovi, že je stejný.</li></ol>
<a class="btn" href="{BASE}/Jirik-Link-Setup.bat">Stáhnout instalátor</a>
<small>Odkaz platí 15 minut a funguje jen v této síti.</small></div></body></html>"""


def firewall_rule_present(cfg: Config) -> bool:
    import subprocess
    import sys

    if sys.platform != "win32":
        return True
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "$r = Get-NetFirewallRule -DisplayName 'Jirik Link' -ErrorAction SilentlyContinue; "
                            "if ($r) { ($r | Get-NetFirewallPortFilter).LocalPort -join ',' }"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ports = set(r.stdout.replace(" ", "").strip().split(","))
        return {str(cfg.link_port), str(cfg.pair_port)} <= ports
    except (OSError, subprocess.TimeoutExpired):
        return False


def open_firewall(cfg: Config) -> None:
    """Jednorázově (okno UAC) povolí bránu a párování Jiřík Link – jen z místní podsítě."""
    import subprocess
    import sys

    if sys.platform != "win32":
        return
    rule = ("Remove-NetFirewallRule -DisplayName 'Jirik Link' -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -DisplayName 'Jirik Link' -Direction Inbound -Protocol TCP "
            f"-LocalPort {int(cfg.link_port)},{int(cfg.pair_port)} -RemoteAddress LocalSubnet "
            "-Profile Private,Domain -Action Allow")
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                      "Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList "
                      f"'-NoProfile','-Command',\"{rule}\""],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
