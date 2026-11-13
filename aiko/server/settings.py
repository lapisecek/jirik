"""Nastavení, která uživatel mění v aplikaci (hlas, necenzurovaný režim). Ukládá se do data/settings.json
a platí pro všechna otevřená okna najednou."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("aiko.settings")


@dataclass
class Settings:
    voice: bool = True
    uncensored: bool = False
    level: str = "low"              # úroveň myšlení: low | medium | smart | ultra

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.value = Settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in self.value.to_dict().items():
                if type(raw.get(k)) is type(v):
                    setattr(self.value, k, raw[k])
        except (OSError, ValueError):
            pass

    def update(self, changes: dict[str, Any]) -> Settings:
        current = self.value.to_dict()
        for k, v in changes.items():
            if k in current and type(v) is type(current[k]):
                setattr(self.value, k, v)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.value.to_dict()), encoding="utf-8")
        except OSError as e:
            log.warning("Nastavení nejde uložit: %s", e)
        return self.value
