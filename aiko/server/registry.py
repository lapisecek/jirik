"""Registr zařízení v LAN a volání jejich MCP nástrojů."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..common.safety import Level, normalize, tool_level

log = logging.getLogger("aiko.registry")


class DeviceError(Exception):
    def __init__(self, msg: str, refused: bool = False):
        super().__init__(msg)
        self.refused = refused


@dataclass
class Device:
    device_id: str
    name: str
    kind: str
    platform: str
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    online: bool = False
    last_seen: float = 0.0
    send: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    def available(self) -> bool:
        """Připojený démon a zároveň dosažitelné zařízení (např. ADB k telefonu)."""
        return self.online and self.status.get("reachable", True) is not False

    def level_of(self, tool: str) -> Level:
        spec = self.tools.get(tool) or {}
        return tool_level(tool, spec.get("level"))

    def public(self) -> dict[str, Any]:
        return {
            "id": self.device_id, "name": self.name, "kind": self.kind,
            "platform": self.platform, "online": self.available,
            "last_seen": self.last_seen, "status": self.status,
            "tools": sorted(self.tools),
        }


class Registry:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        # pending přežije reconnect – výsledek může dorazit po obnovení spojení
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self._ids = itertools.count(1)
        self.on_change: Callable[[], Awaitable[None]] | None = None

    # --- spojení --------------------------------------------------------------
    async def attach(self, hello: dict[str, Any], send: Callable[[dict[str, Any]], Awaitable[None]]) -> Device:
        did = str(hello["device_id"])
        dev = self.devices.get(did) or Device(did, hello.get("name") or did, hello.get("kind", "device"),
                                              hello.get("platform", ""))
        dev.name = hello.get("name") or dev.name
        dev.kind = hello.get("kind", dev.kind)
        dev.platform = hello.get("platform", dev.platform)
        dev.tools = {t["name"]: t for t in hello.get("tools", [])}
        dev.status = hello.get("status") or {}
        dev.online = True
        dev.last_seen = time.time()
        dev.send = send
        self.devices[did] = dev
        for res in hello.get("undelivered") or []:
            self.on_result(res)
        log.info("Zařízení online: %s (%s, %d nástrojů)", dev.name, dev.kind, len(dev.tools))
        await self._changed()
        return dev

    async def detach(self, device_id: str, send: Callable | None = None) -> None:
        dev = self.devices.get(device_id)
        if dev and (send is None or dev.send is send):
            dev.online = False
            dev.send = None
            log.info("Zařízení offline: %s", dev.name)
            await self._changed()

    async def update_status(self, device_id: str, status: dict[str, Any]) -> None:
        dev = self.devices.get(device_id)
        if dev:
            dev.status = status
            dev.last_seen = time.time()
            await self._changed()

    def on_result(self, msg: dict[str, Any]) -> None:
        entry = self._pending.pop(str(msg.get("id")), None)
        if entry and not entry[1].done():
            entry[1].set_result(msg)

    async def _changed(self) -> None:
        if self.on_change:
            await self.on_change()

    # --- dotazy ---------------------------------------------------------------
    def online(self, kinds: tuple[str, ...] | None = None) -> list[Device]:
        return [d for d in self.devices.values() if d.available and (not kinds or d.kind in kinds)]

    def pick(self, kinds: tuple[str, ...], hint: str | None = None) -> Device | None:
        """Vybere cílové zařízení. `hint` je ID, jméno nebo druh z porozumění."""
        cands = [d for d in self.devices.values() if d.kind in kinds]
        if hint and hint not in ("auto", ""):
            h = normalize(hint)
            exact = [d for d in cands if d.device_id == hint or normalize(d.name) == h]
            kind = [d for d in cands if d.kind == hint]
            part = [d for d in cands if h and h in normalize(d.name)]
            for group in (exact, kind, part):
                if group:
                    cands = group
                    break
        cands.sort(key=lambda d: (not d.available, -d.last_seen))
        return cands[0] if cands else None

    def public(self) -> list[dict[str, Any]]:
        return [d.public() for d in self.devices.values()]

    # --- volání ---------------------------------------------------------------
    async def call(self, device: Device, tool: str, args: dict[str, Any] | None = None,
                   timeout: float = 30.0) -> Any:
        if not device.available or device.send is None:
            raise DeviceError(f"{device.name} není připojené")
        if device.tools and tool not in device.tools:
            raise DeviceError(f"{device.name} neumí {tool}")
        cid = f"c{next(self._ids)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = (device.device_id, fut)
        try:
            await device.send({"type": "call", "id": cid, "tool": tool, "args": args or {}})
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise DeviceError(f"{device.name} neodpovědělo včas ({tool})") from None
        finally:
            self._pending.pop(cid, None)
        if not msg.get("ok"):
            raise DeviceError(msg.get("error") or "neznámá chyba", refused=bool(msg.get("refused")))
        return msg.get("data")


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False)
