"""mDNS (Zeroconf) – server se ohlásí v LAN, démony ho samy najdou.

Žádné ruční nastavování IP adres na routeru.
"""
from __future__ import annotations

import asyncio
import logging
import socket

from .protocol import SERVICE_TYPE

log = logging.getLogger("aiko.discovery")


def local_ip() -> str:
    """IP adresa rozhraní, přes které vede výchozí trasa (nic se neposílá)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Advertiser:
    """Registruje službu `_aiko._tcp` pro orchestrátor."""

    def __init__(self, port: int, name: str = "Aiko server"):
        self.port = port
        self.name = name
        self._zc = None
        self._info = None

    async def start(self) -> None:
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            log.warning("zeroconf není nainstalovaný – mDNS vypnuto")
            return
        ip = local_ip()
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            properties={"path": "/ws/device", "v": "1"},
            server=f"aiko-{socket.gethostname().lower()}.local.",
        )
        self._zc = AsyncZeroconf()
        try:
            await self._zc.async_register_service(self._info, allow_name_change=True)
            log.info("mDNS: %s na %s:%s", SERVICE_TYPE, ip, self.port)
        except Exception as e:  # noqa: BLE001 – mDNS je jen pohodlí
            log.warning("mDNS registrace selhala: %s", e)

    async def stop(self) -> None:
        if self._zc and self._info:
            try:
                await self._zc.async_unregister_service(self._info)
            finally:
                await self._zc.async_close()


async def find_server(timeout: float = 4.0) -> str | None:
    """Najde orchestrátor v LAN a vrátí `ws://ip:port/ws/device`."""
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        return None

    found: asyncio.Queue[str] = asyncio.Queue()
    azc = AsyncZeroconf()

    def on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
        if state_change is ServiceStateChange.Added:
            asyncio.ensure_future(_resolve(zeroconf, service_type, name))

    async def _resolve(zeroconf, service_type, name):  # noqa: ANN001
        info = AsyncServiceInfo(service_type, name)
        if await info.async_request(zeroconf, 3000):
            addrs = info.parsed_addresses()
            if addrs:
                path = (info.properties or {}).get(b"path", b"/ws/device").decode()
                await found.put(f"ws://{addrs[0]}:{info.port}{path}")

    browser = AsyncServiceBrowser(azc.zeroconf, [SERVICE_TYPE], handlers=[on_change])
    try:
        return await asyncio.wait_for(found.get(), timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await browser.async_cancel()
        await azc.async_close()
