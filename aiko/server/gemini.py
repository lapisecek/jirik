"""Společný klient pro Gemini API (AI Studio klíče „AIza…“ i nové „AQ.…“).

Bezplatné klíče často narazí na přetížení (503) nebo limit (429) – pak se
automaticky zkusí další model ze seznamu. Klíč jde v hlavičce, nikdy v URL.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("aiko.gemini")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Lite modely bývají při špičce volné – ověřeno: gemini-3.5-flash-lite zvládá JSON i obrázek (~3 s).
FALLBACKS = ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-flash-latest")
RETRY_STATUS = {429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    pass


def api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")


async def generate(model: str, system: str, parts: list[dict[str, Any]], *, json_mode: bool = True,
                   timeout: float = 30) -> str:
    """Vrátí text odpovědi. Při přetížení zkusí záložní modely."""
    key = api_key()
    if not key:
        raise GeminiError("chybí GEMINI_API_KEY")
    chain = [model] + [m for m in FALLBACKS if m != model]
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, **({"responseMimeType": "application/json"} if json_mode else {})},
    }
    last = ""
    async with httpx.AsyncClient(timeout=timeout) as c:
        for m in chain:
            try:
                r = await c.post(URL.format(model=m), headers={"x-goog-api-key": key}, json=body)
            except httpx.TimeoutException:
                last = f"{m}: timeout"
                log.warning("Gemini %s neodpověděl včas, zkouším další model", m)
                continue
            if r.status_code == 200:
                try:
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError, ValueError):
                    last = f"{m}: prázdná odpověď"
                    continue
            last = f"{m}: HTTP {r.status_code} {_error_message(r)}"
            if r.status_code in RETRY_STATUS or r.status_code == 404:
                log.warning("Gemini %s: HTTP %s, zkouším další model", m, r.status_code)
                continue
            break                                  # 400/401/403 – jiný model nepomůže
    raise GeminiError(last)


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"])[:160]
    except (ValueError, KeyError, TypeError):
        return r.text[:160]


def loads(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    return json.loads(text[text.find("{"):text.rfind("}") + 1])
