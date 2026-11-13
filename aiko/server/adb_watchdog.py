"""Self-Healing Wireless ADB (TDD kap. 3, metoda 1).

Telefon se jednou spáruje se serverem přes Bezdrátové ladění (Android 11+).
Server pak v LAN hlídá mDNS službu `_adb-tls-connect._tcp`. Když senior
telefon restartuje a ten se připojí na Wi-Fi, server se k němu do pár sekund
sám znovu připojí (a volitelně nahodí Shizuku). Senior nemusí nic klikat.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("aiko.adb")

ADB_SERVICE = "_adb-tls-connect._tcp.local."
SHIZUKU_START = "sh /sdcard/Android/data/moe.shizuku.privileged.api/start.sh"


async def adb(*args: str, timeout: float = 15) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec("adb", *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode or 0, out.decode(errors="ignore").strip()


class AdbWatchdog:
    def __init__(self, start_shizuku: bool = False):
        self.start_shizuku = start_shizuku
        self._azc = None
        self._browser = None
        self.connected: set[str] = set()

    async def start(self) -> None:
        if not shutil.which("adb"):
            log.info("adb není v PATH – Self-Healing ADB vypnuto")
            return
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            return

        self._azc = AsyncZeroconf()

        def on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
            if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                asyncio.ensure_future(self._resolve(zeroconf, service_type, name, AsyncServiceInfo))

        self._browser = AsyncServiceBrowser(self._azc.zeroconf, [ADB_SERVICE], handlers=[on_change])
        log.info("Self-Healing ADB: hlídám %s", ADB_SERVICE)

    async def _resolve(self, zc, service_type, name, info_cls) -> None:  # noqa: ANN001
        info = info_cls(service_type, name)
        if not await info.async_request(zc, 3000):
            return
        for addr in info.parsed_addresses():
            target = f"{addr}:{info.port}"
            code, out = await adb("connect", target)
            if code == 0 and ("connected" in out and "cannot" not in out):
                if target not in self.connected:
                    log.info("ADB znovu připojeno: %s", target)
                self.connected.add(target)
                if self.start_shizuku:
                    await adb("-s", target, "shell", SHIZUKU_START, timeout=30)
                break
            log.debug("adb connect %s: %s", target, out)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.async_cancel()
        if self._azc:
            await self._azc.async_close()
