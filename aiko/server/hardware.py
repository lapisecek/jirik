"""Úrovně myšlení a co na ně tento počítač stačí.

Všechny úrovně jsou z rodiny Qwen3.5: každá velikost umí text, obrázky, nástroje i přemýšlení a používá
stejné souřadnice obrazovky (0–1000) – klikání se chová stejně, ať je zvolená kterákoli úroveň.

Odhad rychlosti: generování je omezené přenosem vah z paměti. Co se vejde do grafiky, čte se rychle,
zbytek z RAM pomalu. Čas na token ≈ GB v grafice / rychlost grafiky + GB v RAM / rychlost RAM.
Rychlost grafiky se kalibruje měřením (benchmark po stažení), RAM má rozumný výchozí odhad.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("aiko.hardware")


@dataclass(frozen=True)
class Level:
    key: str
    name: str
    blurb: str
    model: str
    uncensored: str
    think: bool
    size_gb: float          # velikost vah (q4_K_M)
    params: str


LEVELS: dict[str, Level] = {
    "low": Level("low", "Nízká", "Okamžité odpovědi, běžné úkoly na počítači.", "qwen3.5:4b",
                 "huihui_ai/qwen3.5-abliterated:4b", False, 3.4, "4B"),
    "medium": Level("medium", "Střední", "Chytřejší odpovědi a spolehlivější klikání.", "qwen3.5:9b",
                    "huihui_ai/qwen3.5-abliterated:9b", False, 6.6, "9B"),
    "smart": Level("smart", "Chytrá", "Před odpovědí přemýšlí – složitější úkoly a plánování.", "qwen3.5:9b",
                   "huihui_ai/qwen3.5-abliterated:9b", True, 6.6, "9B · přemýšlení"),
    "ultra": Level("ultra", "Ultra", "Největší model s přemýšlením – nejlepší výsledky, nejpomalejší.",
                   "qwen3.5:27b", "huihui_ai/qwen3.5-abliterated:27b", True, 17.0, "27B · přemýšlení"),
}
ORDER = ["low", "medium", "smart", "ultra"]


def _nvidia() -> tuple[str, float, float]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "", 0.0, 0.0
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()
        name, total, used = [p.strip() for p in out[0].split(",")]
        return name, float(total) / 1024, float(used) / 1024
    except Exception:  # noqa: BLE001
        return "", 0.0, 0.0


class Hardware:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "bench.json"
        try:
            self.bench: dict[str, float] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.bench = {}
        self._probe: dict[str, Any] | None = None
        self._probe_t = 0.0

    def probe(self) -> dict[str, Any]:
        if self._probe and time.time() - self._probe_t < 60:
            return self._probe
        gpu, vram, vram_used = _nvidia()
        ram = ram_free = 0.0
        try:
            import psutil

            vm = psutil.virtual_memory()
            ram, ram_free = vm.total / 2**30, vm.available / 2**30
        except Exception:  # noqa: BLE001
            pass
        models_dir = Path.home() / ".ollama"
        disk = shutil.disk_usage(models_dir if models_dir.exists() else Path.home()).free / 2**30
        cpu = ""
        if sys.platform == "win32":
            try:
                import platform

                cpu = platform.processor()
            except Exception:  # noqa: BLE001
                pass
        self._probe = {"gpu": gpu, "vram_gb": round(vram, 1), "vram_used_gb": round(vram_used, 1),
                       "ram_gb": round(ram, 1), "ram_free_gb": round(ram_free, 1), "disk_free_gb": round(disk, 1),
                       "cpu": cpu}
        self._probe_t = time.time()
        return self._probe

    def save_bench(self, model: str, tok_s: float) -> None:
        self.bench[model] = round(tok_s, 1)
        try:
            self.path.write_text(json.dumps(self.bench), encoding="utf-8")
        except OSError:
            pass

    def estimate(self, level: Level, model: str) -> dict[str, Any]:
        """Vejde se? Jak rychle? Kolikrát pomaleji než Nízká?"""
        hw = self.probe()
        vram, ram = hw["vram_gb"], hw["ram_gb"]
        need = level.size_gb + 1.0                        # váhy + kontext a provozní rezerva
        gpu_part = max(0.0, min(level.size_gb, vram - 1.3)) if vram else 0.0
        cpu_part = max(0.0, level.size_gb - gpu_part)
        fits = need <= vram + max(0.0, ram - 4.0)         # Windows a aplikace si nechají ~4 GB RAM
        # kalibrace grafiky: z naměřené rychlosti nějakého modelu, který je celý v grafice
        gpu_bw = 140.0
        for lv in LEVELS.values():
            for m in (lv.model, lv.uncensored):
                if m in self.bench and lv.size_gb <= vram - 1.3:
                    gpu_bw = max(self.bench[m] * lv.size_gb, 20.0)
        cpu_bw = 38.0
        per_token = (gpu_part / gpu_bw if gpu_part else 0) + (cpu_part / cpu_bw if cpu_part else 0)
        est = round(1 / per_token, 1) if per_token else 0.0
        measured = self.bench.get(model)
        tok_s = measured or est
        base = self.bench.get(LEVELS["low"].model) or self.bench.get(LEVELS["low"].uncensored) or \
            round(1 / (min(LEVELS["low"].size_gb, max(vram - 1.3, 0.1)) / gpu_bw), 1)
        if not fits:
            fit = "no"
        elif cpu_part <= 0.05:
            fit = "gpu"
        elif gpu_part >= level.size_gb * 0.6:
            fit = "mostly"
        else:
            fit = "cpu"
        return {"fits": fits, "fit": fit, "gpu_share": round(100 * gpu_part / level.size_gb) if level.size_gb else 0,
                "tok_s": tok_s, "measured": bool(measured), "slower": round(base / tok_s, 1) if tok_s else None,
                "need_gb": round(need, 1), "download_gb": level.size_gb}

    def describe(self, installed: set[str], uncensored: bool) -> list[dict[str, Any]]:
        out = []
        for key in ORDER:
            lv = LEVELS[key]
            model = lv.uncensored if uncensored else lv.model
            out.append({**asdict(lv), "active_model": model, "installed": model in installed,
                        **self.estimate(lv, model)})
        return out
