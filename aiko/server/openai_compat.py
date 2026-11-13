"""Klient pro služby s API ve stylu OpenAI (POST {base_url}/chat/completions).

Jedním kódem pokrývá Groq, OpenRouter, Z.ai (GLM), NVIDIA NIM, Cerebras, Mistral,
GitHub Models i lokální LM Studio. V aiko.toml je každá služba jako [[providers]].
Pozn.: pro Claude se tento klient nepoužívá – Claude jde přes oficiální SDK.
"""
from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("aiko.providers")

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass
class Provider:
    name: str                       # „groq“, „openrouter“, „zai“…
    base_url: str                   # např. https://api.groq.com/openai/v1
    model: str
    api_key: str = ""
    vision: bool = False            # umí obrázky → může sloužit GUI agentovi
    use: list[str] = field(default_factory=lambda: ["llm"])   # "llm" (věty, otázky) a/nebo "vlm" (agent)
    json_mode: bool = True          # posílat response_format json_object
    max_side: int = 1280            # obrázek se zmenší (méně tokenů, rychlejší)
    timeout: float = 45.0

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


class ProviderError(RuntimeError):
    def __init__(self, msg: str, retry: bool = True):
        super().__init__(msg)
        self.retry = retry


class QuotaExhausted(ProviderError):
    """Denní/měsíční limit účtu (např. OpenRouter free-models-per-day) – jiný model na stejném klíči nepomůže."""


QUOTA_MARKERS = ("per-day", "per day", "per_day", "daily", "quota", "insufficient credits", "credits")


def is_quota_error(status: int, msg: str) -> bool:
    m = msg.lower()
    return status in (402, 429) and any(k in m for k in QUOTA_MARKERS)


# Účty (adresa + klíč) s vyčerpaným limitem → do kdy se na ně nic neposílá. Sdílí to porozumění
# větám i GUI agent: všechny bloky [[providers]] se stejným klíčem čerpají jeden denní limit
# a každý odmítnutý pokus byl další dotaz navíc.
_EXHAUSTED: dict[tuple[str, str], float] = {}
QUOTA_PAUSE_S = 3600.0


def _acc(p: "Provider") -> tuple[str, str]:
    return p.base_url.rstrip("/"), p.api_key


def exhausted_until(p: "Provider") -> float:
    t = _EXHAUSTED.get(_acc(p), 0.0)
    return t if t > time.time() else 0.0


def _reset_time(r: httpx.Response) -> float:
    """Kdy se limit obnoví (OpenRouter posílá X-RateLimit-Reset v ms) – jinak za hodinu zkusit znovu."""
    raw: Any = r.headers.get("x-ratelimit-reset")
    if raw is None:
        try:
            meta = (r.json().get("error") or {}).get("metadata") or {}
            raw = (meta.get("headers") or {}).get("X-RateLimit-Reset")
        except (ValueError, AttributeError):
            raw = None
    try:
        t = float(raw)
        t = t / 1000 if t > 1e11 else t
        if time.time() < t < time.time() + 2 * 86400:
            return t
    except (TypeError, ValueError):
        pass
    return time.time() + QUOTA_PAUSE_S


def image_to_jpeg(b64_webp: str, max_side: int) -> tuple[str, float]:
    """WebP ze zařízení → JPEG (podporují ho všichni). Vrací (base64, faktor zpět do souřadnic snímku)."""
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(b64_webp))).convert("RGB")
    scale = min(1.0, max_side / max(img.size))
    if scale < 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode(), 1 / scale


async def chat(p: Provider, system: str, text: str, image_b64: str | None = None, *, json_mode: bool = True,
               transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, float]:
    """Vrátí (text odpovědi, faktor souřadnic). Chyby 429/5xx/timeout → ProviderError(retry=True)."""
    until = exhausted_until(p)
    if until:
        raise QuotaExhausted(f"{p.name}: limit účtu vyčerpán do {time.strftime('%H:%M', time.localtime(until))} "
                             f"(dotaz se neposílá)", retry=False)
    factor = 1.0
    content: Any = text
    if image_b64:
        if not p.vision:
            raise ProviderError(f"{p.name}: model {p.model} neumí obrázky", retry=True)
        jpeg, factor = image_to_jpeg(image_b64, p.max_side)
        content = [{"type": "text", "text": text},
                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg}"}}]
    body: dict[str, Any] = {
        "model": p.model, "temperature": 0, "max_tokens": 1200,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    }
    if json_mode and p.json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {p.api_key}", "X-Title": "Aiko"}
    url = p.base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=p.timeout, transport=transport) as c:
        try:
            r = await c.post(url, headers=headers, json=body)
            if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
                body.pop("response_format")              # služba JSON režim neumí → jen instrukcí v promptu
                r = await c.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            raise ProviderError(f"{p.name}: timeout") from None
        except httpx.HTTPError as e:
            raise ProviderError(f"{p.name}: {type(e).__name__}") from None
        if r.status_code != 200:
            msg = _error(r)
            if r.status_code in (400, 404) and "model" in msg.lower():
                msg += await _model_hint(c, p)
            if is_quota_error(r.status_code, msg):
                _EXHAUSTED[_acc(p)] = _reset_time(r)
                log.warning("%s: došel limit účtu – do %s se na tento klíč nic neposílá", p.name,
                            time.strftime("%H:%M", time.localtime(_EXHAUSTED[_acc(p)])))
                raise QuotaExhausted(f"{p.name}: HTTP {r.status_code} {msg}", retry=False)
            raise ProviderError(f"{p.name}: HTTP {r.status_code} {msg}", retry=r.status_code in RETRY_STATUS
                                or r.status_code in (400, 401, 403, 404))
        try:
            out = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            raise ProviderError(f"{p.name}: nečekaná odpověď") from None
        if isinstance(out, list):                         # některé služby vrací části
            out = "".join(part.get("text", "") for part in out if isinstance(part, dict))
        if not (out or "").strip():                      # „přemýšlecí“ modely občas vrátí prázdný obsah
            raise ProviderError(f"{p.name}: prázdná odpověď")
        return out.strip(), factor


def _error(r: httpx.Response) -> str:
    try:
        e = r.json().get("error")
        return str(e.get("message") if isinstance(e, dict) else e)[:200]
    except (ValueError, AttributeError):
        return r.text[:200]


async def _model_hint(c: httpx.AsyncClient, p: Provider) -> str:
    """Špatný název modelu → vypíše, jaké modely služba nabízí (pomůže s aiko.toml)."""
    try:
        r = await c.get(p.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {p.api_key}"})
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        hint = [i for i in ids if any(k in i.lower() for k in ("vl", "vision", "4v", "llama-4", "pixtral"))]
        return f" | dostupné modely (s obrázky): {', '.join((hint or ids)[:15])}"
    except Exception:  # noqa: BLE001
        return ""


def from_config(raw: list[dict[str, Any]]) -> list[Provider]:
    import os

    out = []
    for d in raw or []:
        key = str(d.get("api_key") or os.environ.get(str(d.get("api_key_env") or ""), "") or "").strip()
        use = d.get("use", ["llm"])
        out.append(Provider(
            name=str(d.get("name", "provider")), base_url=str(d.get("base_url", "")), model=str(d.get("model", "")),
            api_key=key, vision=bool(d.get("vision", False)), use=[use] if isinstance(use, str) else list(use),
            json_mode=bool(d.get("json_mode", True)), max_side=int(d.get("max_side", 1280)),
            timeout=float(d.get("timeout", 45))))
    return out
