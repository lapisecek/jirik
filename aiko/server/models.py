"""Stahování modelů pro úrovně myšlení (s průběhem do všech oken), měření rychlosti a mazání."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from .config import Config
from .hardware import Hardware

log = logging.getLogger("aiko.models")
Emit = Callable[[dict[str, Any]], Awaitable[None]]


class Models:
    def __init__(self, cfg: Config, hardware: Hardware, emit: Emit):
        self.cfg, self.hw, self.emit = cfg, hardware, emit
        self.pulls: dict[str, dict[str, Any]] = {}          # model → průběh stahování
        self._tasks: dict[str, asyncio.Task] = {}
        self._installed: tuple[float, set[str]] = (0.0, set())

    async def installed(self, fresh: bool = False) -> set[str]:
        if not fresh and time.time() - self._installed[0] < 15:
            return self._installed[1]
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                tags = (await c.get(f"{self.cfg.ollama_url}/api/tags")).json().get("models", [])
            names = {m.get("name", "") for m in tags}
        except Exception:  # noqa: BLE001
            names = set()
        self._installed = (time.time(), names)
        return names

    def pulling(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.pulls.items() if not v.get("done")}

    async def pull(self, model: str) -> None:
        if model in self._tasks and not self._tasks[model].done():
            return
        self.pulls[model] = {"model": model, "completed": 0, "total": 0, "status": "Připravuji…"}
        self._tasks[model] = asyncio.create_task(self._pull(model))

    async def cancel(self, model: str) -> None:
        t = self._tasks.get(model)
        if t and not t.done():
            t.cancel()

    async def _pull(self, model: str) -> None:
        p = self.pulls[model]
        last = 0.0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as c:
                async with c.stream("POST", f"{self.cfg.ollama_url}/api/pull",
                                    json={"model": model, "stream": True}) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        d = json.loads(line)
                        if d.get("error"):
                            raise RuntimeError(d["error"])
                        if d.get("total"):
                            p["total"], p["completed"] = d["total"], d.get("completed", 0)
                        p["status"] = {"pulling manifest": "Připravuji…", "verifying sha256 digest": "Ověřuji…",
                                       "writing manifest": "Dokončuji…", "success": "Hotovo"}.get(
                            d.get("status", ""), "Stahuji…" if d.get("total") else p["status"])
                        if time.time() - last > 0.4:
                            last = time.time()
                            await self.emit({"type": "pull", **p})
            p.update(status="Měřím rychlost…", completed=p.get("total", 0))
            await self.emit({"type": "pull", **p})
            await self.installed(fresh=True)
            await self.bench(model)
            p.update(done=True, status="Hotovo")
        except asyncio.CancelledError:
            p.update(done=True, error="Stahování zrušeno")
        except Exception as e:  # noqa: BLE001
            log.warning("Stažení %s selhalo: %s", model, e)
            msg = str(e)
            if "newer version" in msg.lower():
                msg = "Ollama je moc stará – aktualizujte ji (Setup.bat) a zkuste to znovu."
            p.update(done=True, error=msg)
        await self.emit({"type": "pull", **p})
        await self.emit({"type": "models.changed"})

    async def bench(self, model: str) -> float:
        """Rychlost generování (tokeny/s) – stejný kontext jako chat, ať se model nepřenačítá."""
        try:
            async with httpx.AsyncClient(timeout=600) as c:
                r = await c.post(f"{self.cfg.ollama_url}/api/generate", json={
                    "model": model, "prompt": "Popiš ve třech větách, jak funguje duha.", "stream": False,
                    "think": False, "keep_alive": self.cfg.keep_alive,
                    "options": {"num_predict": 96, "num_ctx": self.cfg.num_ctx, "temperature": 0.7}})
                d = r.json()
            tok_s = float(d.get("eval_count", 0)) / max(float(d.get("eval_duration", 1)) / 1e9, 1e-6)
            if tok_s > 0:
                self.hw.save_bench(model, tok_s)
                log.info("Rychlost %s: %.1f tokenů/s", model, tok_s)
            return tok_s
        except Exception as e:  # noqa: BLE001
            log.info("Měření %s se nepovedlo: %s", model, e)
            return 0.0

    async def delete(self, model: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request("DELETE", f"{self.cfg.ollama_url}/api/delete", json={"model": model})
            await self.installed(fresh=True)
            self.hw.bench.pop(model, None)
            await self.emit({"type": "models.changed"})
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False
