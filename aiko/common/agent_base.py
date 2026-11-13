"""Základ lehkého MCP démona na zařízení.

Žádné AI na koncovém zařízení – démon jen vystaví hotové nástroje (tools),
ohlásí se serveru přes WebSocket a vykoná, co server zavolá.

Ochrana proti sebeodpojení (TDD kap. 1): každé volání běží jako uzavřená
lokální transakce. Když nástroj během běhu shodí síť (restart Wi-Fi karty),
dokončí se lokálně a výsledek se doručí po obnovení spojení (`undelivered`).
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import socket
import time
from typing import Any

from .discovery import find_server
from .protocol import AUTH_SCHEME, PROTOCOL_VERSION, ToolRefused, ToolSpec, auth_proof, new_nonce, proof_ok
from .safety import Level

log = logging.getLogger("aiko.agent")


class DeviceAgent:
    kind = "device"          # "phone" | "pc"
    platform = "unknown"

    def __init__(self, name: str, device_id: str | None = None,
                 server_url: str | None = None, token: str = "", pin: str = "", idle_exit: float = 0):
        self.name = name
        self.pin = (pin or "").replace(":", "").lower()     # SHA-256 otisk certifikátu serveru (Aiko Link)
        self.idle_exit = float(idle_exit or 0)               # po kolika s bez práce se démon sám ukončí
        self._last_call = time.monotonic()
        self.device_id = device_id or f"{self.kind}-{socket.gethostname().lower()}"
        self.server_url = server_url
        self.token = token
        self._ws = None
        self._undelivered: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, tuple[ToolSpec, Any]] = {}
        for attr in dir(type(self)):
            fn = getattr(type(self), attr, None)
            spec = getattr(fn, "_aiko_tool", None)
            if spec is not None:
                self._tools[spec.name] = (spec, getattr(self, attr))

    # --- pro potomky ----------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        """Stav zařízení posílaný serveru (baterie, hovor...)."""
        return {}

    async def guard(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        """Pojistka na zařízení. Vyhoď ToolRefused, pokud akci nelze provést."""
        if spec.level >= Level.ORANGE:
            st = await self.status()
            if st.get("in_call"):
                raise ToolRefused("Během hovoru nic neměním.")

    # --- veřejné API ----------------------------------------------------------
    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [s for s, _ in self._tools.values()]

    async def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Lokálně vykoná nástroj (používá se i v testech)."""
        if name not in self._tools:
            return {"ok": False, "error": f"neznámý nástroj {name}"}
        spec, fn = self._tools[name]
        unknown = set(args) - set(spec.params)
        if unknown:
            return {"ok": False, "error": f"neznámé parametry {sorted(unknown)}"}
        try:
            await self.guard(spec, args)
            if inspect.iscoroutinefunction(fn):
                data = await fn(**args)
            else:
                data = await asyncio.to_thread(fn, **args)
            return {"ok": True, "data": data}
        except ToolRefused as e:
            return {"ok": False, "refused": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001 – chyba se vrací serveru
            log.exception("nástroj %s selhal", name)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def run(self) -> None:
        import websockets

        if not self.token:
            log.warning("Démon běží bez párovacího tokenu – ovládat ho může kterýkoli Aiko server v síti "
                        "(i podvržený přes mDNS). Nastavte stejný token na serveru i zde (--token / AIKO_TOKEN).")
        backoff = 1.0
        while True:
            url = self.server_url or await find_server()
            if not url:
                log.info("Server nenalezen přes mDNS, zkusím znovu…")
                await asyncio.sleep(3)
                continue
            try:
                ssl_ctx = None
                if url.startswith("wss://"):
                    import ssl

                    # Certifikát Aiko je vlastní (bez certifikační autority) – ověřuje se přesným otiskem,
                    # který zařízení dostalo při spárování. Jiný server (podvržený v síti) se nepřipojí.
                    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                async with websockets.connect(url, ping_interval=10, ping_timeout=10, ssl=ssl_ctx,
                                              max_size=8 * 1024 * 1024, open_timeout=10) as ws:
                    if ssl_ctx is not None and not self._pin_ok(ws):
                        await ws.close()
                        raise ConnectionError(f"server {url} má jiný certifikát, než byl spárovaný – odpojuji")
                    hello = await self._hello()
                    await ws.send(json.dumps(hello))
                    if self.token and not await self._verify_server(ws, hello["nonce"]):
                        # podvržený server (např. ohlášený přes mDNS) – žádný jeho příkaz neprovedeme
                        await ws.close()
                        raise ConnectionError(f"server {url} se neprokázal párovacím tokenem – ignoruji ho")
                    self._ws = ws
                    backoff = 1.0
                    self._undelivered.clear()
                    log.info("Připojeno k %s jako %s (%s)", url, self.name, self.device_id)
                    self._last_call = time.monotonic()
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") == "call":
                                asyncio.create_task(self._handle_call(msg))
                            elif msg.get("type") == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                            elif msg.get("type") == "error":
                                log.error("Server: %s", msg.get("error"))
                            elif msg.get("type") == "revoked" and self.idle_exit:
                                log.warning("Zařízení bylo v Aiko odebráno – končím")
                                os._exit(0)
                    finally:
                        hb.cancel()
            except (OSError, Exception) as e:  # noqa: BLE001
                log.warning("Spojení se serverem: %s", e)
            self._ws = None
            if self.idle_exit and time.monotonic() - self._last_call > self.idle_exit:
                log.info("Server je nedostupný – končím (hlídač mě zase probudí)")
                os._exit(0)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    # --- interní --------------------------------------------------------------
    async def _hello(self) -> dict[str, Any]:
        hello = {
            "type": "hello",
            "v": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "platform": self.platform,
            "tools": [s.to_json() for s in self.tool_specs],
            "status": await self._safe_status(),
            "undelivered": list(self._undelivered.values()),
        }
        if self.token:   # token se neposílá – jen nonce pro vzájemné ověření (challenge–response)
            hello.update(auth=AUTH_SCHEME, nonce=new_nonce())
        return hello

    async def _verify_server(self, ws, nonce: str, timeout: float = 10) -> bool:  # noqa: ANN001
        """Server musí dokázat, že zná token (HMAC nad naším nonce). Pak se prokážeme my."""
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        except (asyncio.TimeoutError, ValueError, OSError, Exception):  # noqa: BLE001
            return False
        if msg.get("type") == "error":
            log.error("Server: %s", msg.get("error"))
            return False
        ns = msg.get("nonce")
        if msg.get("type") != "challenge" or not isinstance(ns, str) or not proof_ok(
                self.token, "server", nonce, ns, msg.get("proof")):
            log.error("Server neposlal platný důkaz párovacího tokenu (špatný token, nebo cizí server)")
            return False
        await ws.send(json.dumps({"type": "auth", "proof": auth_proof(self.token, "device", ns, nonce)}))
        return True

    async def _safe_status(self) -> dict[str, Any]:
        try:
            return await self.status()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def _pin_ok(self, ws) -> bool:  # noqa: ANN001
        import hashlib

        if not self.pin:
            log.error("Šifrované spojení bez otisku certifikátu – spárujte zařízení znovu")
            return False
        try:
            der = ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        except Exception:  # noqa: BLE001
            return False
        return bool(der) and hashlib.sha256(der).hexdigest() == self.pin

    async def _heartbeat(self, ws) -> None:  # noqa: ANN001
        while True:
            await asyncio.sleep(10)
            if self.idle_exit and time.monotonic() - self._last_call > self.idle_exit:
                log.info("Žádná práce %d s – ukončuji se (Aiko Link mě zase probudí)", self.idle_exit)
                await ws.close()
                os._exit(0)
            await ws.send(json.dumps({"type": "status", "status": await self._safe_status()}))

    async def _handle_call(self, msg: dict[str, Any]) -> None:
        self._last_call = time.monotonic()
        log.info("→ %s %s", msg.get("tool"), msg.get("args"))
        res = await self.invoke(msg.get("tool", ""), msg.get("args") or {})
        res = {"type": "result", "id": msg.get("id"), **res}
        log.info("← %s ok=%s", msg.get("tool"), res.get("ok"))
        self._last_call = time.monotonic()
        try:
            if self._ws is None:
                raise ConnectionError("offline")
            await self._ws.send(json.dumps(res))
        except Exception:  # noqa: BLE001 – doručíme po reconnectu
            log.info("Výsledek %s doručím po obnovení spojení", msg.get("id"))
            self._undelivered[str(msg.get("id"))] = res


def base_argparser(desc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--server", default=os.environ.get("AIKO_SERVER"),
                   help="ws://host:8765/ws/device (výchozí: najít přes mDNS)")
    p.add_argument("--token", default=os.environ.get("AIKO_TOKEN", ""), help="párovací token serveru")
    p.add_argument("--name", help="jméno zařízení, jak ho uvidí rodina")
    p.add_argument("--id", dest="device_id", help="stabilní ID zařízení")
    p.add_argument("--pin", default=os.environ.get("AIKO_PIN", ""), help="SHA-256 otisk certifikátu serveru (wss)")
    p.add_argument("--idle-exit", type=float, default=float(os.environ.get("AIKO_IDLE_EXIT", "0") or 0),
                   help="ukončit se po N s bez práce (0 = nikdy)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def setup_logging(verbose: bool) -> None:
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
