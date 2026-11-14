"""Hlas Jirka (Piper TTS) a jediný přehrávač řeči pro celý počítač.

Řeč přehrává SERVER, ne okna s chatem. Dřív mluvilo každé otevřené okno (aplikace, záložka
v prohlížeči…) samo za sebe – se dvěma okny zazněla každá věta dvakrát s malým zpožděním (ozvěna).
Teď je přehrávač jen jeden, ať je otevřených oken kolik chce, a každá věta zazní právě jednou.
"""
from __future__ import annotations

import asyncio
import io
import itertools
import logging
import re
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Config

log = logging.getLogger("aiko.voice")

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class Voice:
    """Piper v procesu serveru – model zůstává v paměti, věta trvá desetiny sekundy."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._voice = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        if not (self.cfg.piper_model and Path(self.cfg.piper_model).exists()):
            return False
        try:
            import importlib

            importlib.import_module("piper")
        except ImportError:
            return False
        return True

    async def synthesize(self, text: str) -> bytes | None:
        if not text or not self.available:
            return None
        try:
            return await asyncio.to_thread(self._synth, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Piper selhal: %s", e)
            return None

    def _synth(self, text: str) -> bytes:
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice

                self._voice = PiperVoice.load(self.cfg.piper_model)
                log.info("Hlas Jirka načtený")
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                try:
                    from piper import SynthesisConfig

                    self._voice.synthesize_wav(text, w, syn_config=SynthesisConfig(
                        length_scale=1 / max(self.cfg.speech_rate, 0.5)))
                except ImportError:
                    self._voice.synthesize_wav(text, w)
            return buf.getvalue()


# --- text pro řeč -----------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"[*_`#>|~]+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_BULLET = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.M)
_SENTENCE = re.compile(r"(?<=[.!?…:;])\s+|\n+")


def speakable(text: str) -> str:
    """Markdown → čistá věta: bez kódu, odkazů, hvězdiček a emoji."""
    t = _FENCE.sub(" ", text)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub("", t)
    t = _BULLET.sub("", t)
    t = _MARKS.sub("", t)
    t = _EMOJI.sub("", t)
    return " ".join(t.split())


class Speaker:
    """Jediná fronta řeči. Syntéza další věty běží souběžně s přehráváním té předchozí."""

    def __init__(self, voice: Voice, emit: Emit, data_dir: Path):
        self.voice = voice
        self.emit = emit
        self.enabled = True
        self._gen = 0
        self._text_q: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()
        self._audio_q: asyncio.Queue[tuple[int, str, bytes | None]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._speaking: str = ""
        self._feeds: dict[str, dict[str, Any]] = {}
        self._tmp = Path(tempfile.gettempdir()) / "aiko-speech"
        self._tmp.mkdir(exist_ok=True)
        self._files = itertools.cycle(range(4))
        self._playing = False

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._synth_loop()), asyncio.create_task(self._play_loop())]

    async def close(self) -> None:
        self.hush()
        for t in self._tasks:
            t.cancel()

    # --- vstupy ----------------------------------------------------------------------------
    def say(self, msg_id: str, text: str) -> None:
        if not self.enabled:
            return
        parts = self._sentences(speakable(text))
        log.debug("Řeč: %d vět (%s)", len(parts), msg_id)
        for part in parts:
            self._text_q.put_nowait((self._gen, msg_id, part))

    def feed(self, msg_id: str, delta: str, final: bool = False) -> None:
        """Průběžný text odpovědi: hotové věty jdou do řeči hned, zbytek čeká na další text."""
        f = self._feeds.setdefault(msg_id, {"buf": ""})
        f["buf"] += delta
        buf = f["buf"]
        if final:
            cut = len(buf)
        else:
            if buf.count("```") % 2:                     # uvnitř bloku kódu – ten se nečte
                return
            m = None
            for m in _SENTENCE.finditer(buf):
                pass
            if not m or m.end() < 24:
                return
            cut = m.end()
        chunk, f["buf"] = buf[:cut], buf[cut:]
        if final:
            self._feeds.pop(msg_id, None)
        if chunk.strip():
            self.say(msg_id, chunk)

    def hush(self) -> None:
        self._gen += 1
        self._feeds.clear()
        for q in (self._text_q, self._audio_q):
            while not q.empty():
                q.get_nowait()
        self._stop_sound()

    @staticmethod
    def _sentences(text: str) -> list[str]:
        out: list[str] = []
        for p in (s.strip() for s in _SENTENCE.split(text)):
            if not p:
                continue
            if out and (len(out[-1]) < 28 or len(p) < 10) and len(out[-1]) + len(p) < 300:
                out[-1] += " " + p
            else:
                out.append(p[:480])
        return out

    # --- smyčky ------------------------------------------------------------------------------
    async def _synth_loop(self) -> None:
        while True:
            gen, mid, text = await self._text_q.get()
            if gen != self._gen:
                continue
            wav = await self.voice.synthesize(text)
            if gen == self._gen:
                await self._audio_q.put((gen, mid, wav))

    async def _play_loop(self) -> None:
        while True:
            gen, mid, wav = await self._audio_q.get()
            if gen != self._gen or not wav:
                continue
            if self._speaking != mid:
                log.info("Mluvím (%s)", mid)
                self._speaking = mid
                await self.emit({"type": "speech", "on": True, "id": mid})
            try:
                await self._play(wav)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Přehrání selhalo: %s", e)
            if self._audio_q.empty() and self._text_q.empty():
                await asyncio.sleep(0.15)               # další věta se možná ještě dopočítává
                if self._audio_q.empty() and self._text_q.empty():
                    self._speaking = ""
                    await self.emit({"type": "speech", "on": False, "id": mid})

    async def _play(self, wav: bytes) -> None:
        with wave.open(io.BytesIO(wav), "rb") as w:
            seconds = w.getnframes() / float(w.getframerate() or 22050)
        if sys.platform != "win32":
            return
        import winsound

        path = self._tmp / f"s{next(self._files)}.wav"
        path.write_bytes(wav)
        gen = self._gen
        self._playing = True
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        try:
            end = asyncio.get_running_loop().time() + seconds + 0.05
            while asyncio.get_running_loop().time() < end:
                if gen != self._gen:                      # ztišeno uprostřed věty
                    return
                await asyncio.sleep(0.05)
        finally:
            self._playing = False

    def _stop_sound(self) -> None:
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(None, 0)                 # zastaví právě hrající zvuk
        if self._speaking:
            mid, self._speaking = self._speaking, ""
            try:
                asyncio.get_running_loop().create_task(self.emit({"type": "speech", "on": False, "id": mid}))
            except RuntimeError:
                pass
