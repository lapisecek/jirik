"""Rodinná vrstva: záznam událostí a upozornění.

Pravidlo: co nemusíme mít, nemáme. Do záznamu jde typ akce, čas a výsledek –
nikdy obsah zpráv, přepis řeči ani snímky obrazovky.
Rodina záznam jen čte. Telefon na dálku ovládat nemůže.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("aiko.family")

RETENTION_DAYS = 90
ALERT_RETENTION_DAYS = 365


@dataclass
class Event:
    text: str
    kind: str = "info"          # info | success | fail | refused | alert
    intent: str = ""
    device: str = ""
    level: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FamilyLog:
    def __init__(self, data_dir: Path, webhook: str = "", family_name: str = "rodina"):
        self.path = Path(data_dir) / "events.jsonl"
        self.webhook = webhook
        self.family_name = family_name
        self.events: list[Event] = []
        self.on_event: Callable[[Event], Awaitable[None]] | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                self.events.append(Event(**json.loads(line)))
            except (ValueError, TypeError):
                continue
        self._prune()

    def _prune(self) -> None:
        now = time.time()
        keep = []
        for e in self.events:
            days = ALERT_RETENTION_DAYS if e.kind == "alert" else RETENTION_DAYS
            if now - e.ts < days * 86400:
                keep.append(e)
        if len(keep) != len(self.events):
            self.events = keep
            self._rewrite()

    def _rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in self.events),
                             encoding="utf-8")

    async def add(self, event: Event, notify: bool = False) -> Event:
        self.events.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        log.info("[rodina] %s: %s", event.kind, event.text)
        if self.on_event:
            await self.on_event(event)
        if notify:
            asyncio.create_task(self._push(event))
        return event

    async def clear(self) -> None:
        """Senior řekl „smaž můj záznam“."""
        self.events = []
        self._rewrite()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events[-limit:]][::-1]

    def stats(self) -> dict[str, Any]:
        week = time.time() - 7 * 86400
        recent = [e for e in self.events if e.ts >= week]
        return {
            "resolved_week": sum(1 for e in recent if e.kind == "success"),
            "alerts_week": sum(1 for e in recent if e.kind in ("alert", "refused")),
            "failed_week": sum(1 for e in recent if e.kind == "fail"),
            "last": self.events[-1].to_dict() if self.events else None,
        }

    async def _push(self, e: Event) -> None:
        """Upozornění rodině mimo stránku (ntfy.sh nebo Home Assistant webhook)."""
        if not self.webhook:
            return
        title = "Aiko: pozor" if e.kind in ("alert", "refused") else "Aiko"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                if "ntfy" in self.webhook:
                    await c.post(self.webhook, content=e.text.encode("utf-8"), headers={
                        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
                        "Priority": "urgent" if e.kind == "alert" else "default",
                        "Tags": "warning" if e.kind in ("alert", "refused") else "robot"})
                else:
                    await c.post(self.webhook, json={"title": title, "message": e.text, **e.to_dict()})
        except httpx.HTTPError as ex:
            log.warning("Upozornění rodině se nepodařilo odeslat: %s", ex)
