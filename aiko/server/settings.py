"""Nastavení, která uživatel mění v aplikaci (hlas, necenzurovaný režim). Ukládá se do data/settings.json
a platí pro všechna otevřená okna najednou."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("aiko.settings")


MIN_CTX = 2048
MAX_CTX = 65536
DEFAULT_CTX = 8192
CTX_PRESETS = (4096, 8192, 16384, 32768, 65536)


def clamp_ctx(value: int) -> int:
    return max(MIN_CTX, min(MAX_CTX, int(value)))


@dataclass
class Settings:
    voice: bool = True
    uncensored: bool = False
    level: str = "low"              # úroveň myšlení: low | medium | smart | ultra
    autonomous: bool = True         # autonomní práce: plánování kroků, pokračování a ověření dokončení
    num_ctx: int = DEFAULT_CTX      # velikost kontextového okna (tokeny)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.value = Settings()
        self.explicit: set[str] = set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in self.value.to_dict().items():
                if type(raw.get(k)) is type(v):
                    val = clamp_ctx(raw[k]) if k == "num_ctx" else raw[k]
                    setattr(self.value, k, val)
                    self.explicit.add(k)
        except (OSError, ValueError):
            pass

    def update(self, changes: dict[str, Any]) -> Settings:
        current = self.value.to_dict()
        for k, v in changes.items():
            if k in current and type(v) is type(current[k]):
                val = clamp_ctx(v) if k == "num_ctx" else v
                setattr(self.value, k, val)
                self.explicit.add(k)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.value.to_dict()), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("Nastavení nejde uložit: %s", e)
        return self.value
