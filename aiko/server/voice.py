"""Hlasová vrstva na serveru: Faster-Whisper (STT) + Piper (TTS).

Obojí je volitelné. Bez nich funguje textové rozhraní a prohlížeč
(simulátor tlačítka) mluví vlastním hlasem přes Web Speech API.
Nahrávka hlasu se po převodu na text okamžitě zahodí.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
import wave
from pathlib import Path

from .config import Config

log = logging.getLogger("aiko.voice")

PROMPT_HINT = (
    "Senior mluví česky s asistentkou Aiko o telefonu a počítači. "
    "Zmizel mi WhatsApp. Napiš zprávu Lence. Zavolej Petrovi. Nic neslyším. "
    "Otevři Seznam. Volal mi někdo z banky kvůli AnyDesku."
)

BASE_HOTWORDS = (
    "WhatsApp", "SMS", "Seznam", "AnyDesk", "TeamViewer", "Messenger", "Facebook", "YouTube",
    "e-mail", "Wi-Fi", "internet", "heslo", "PIN", "baterie", "hlasitost", "fotky", "zpráva",
)

SENIOR_PHONETIC_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(vot\s*sap\w*|vat\s*sap\w*|vacap\w*|vocap\w*|watsap\w*|wotzap\w*|whatsap\b|whatsappu)\b", re.I), "WhatsApp"),
    (re.compile(r"\b(zmizel\s+mi)\s+vojt[eě]ch\b", re.I), r"\1 WhatsApp"),
    (re.compile(r"\b(es\s*-?\s*em\s*-?\s*es\w*|esemesk[aouy]|smsk[aouy])\b", re.I), "SMS"),
    (re.compile(r"\b(any\s*desk\w*|eny\s*desk\w*|anidesk\w*|enydesk\w*)\b", re.I), "AnyDesk"),
    (re.compile(r"\b(team\s*viewer\w*|t[yý]m\s*v[iíj][uů]v\w*|tym\s*vjuvr\w*)\b", re.I), "TeamViewer"),
    (re.compile(r"\b(j[uů]t[uů]b\w*|jutube\w*)\b", re.I), "YouTube"),
    (re.compile(r"\b(fejsbuk\w*|face\s*buk\w*)\b", re.I), "Facebook"),
    (re.compile(r"\b(mesend[zž]r\w*|mesind[zž]r\w*)\b", re.I), "Messenger"),
    (re.compile(r"\b(internety)\b", re.I), "internet"),
    (re.compile(r"\b(sezn[aá]mek)\b", re.I), "Seznam"),
)

RX_SERVER_HALLUCINATION = re.compile(
    r"titulky\s*(vytvoril|vytvořil|přeložil|prelozil|napsal|uprava|úprava)|"
    r"johnyx|d[eě]kuji\s+za\s+(pozornost|shl[eé]dnut[ií]|sledov[aá]n[ií])|"
    r"odeb[ií]rejte|subtitles?\s+by|amara\.org|thank\s+you\s+for\s+watching|"
    r"^[♪♫\s\.\,\!\?\-\–\—…]+$",
    re.I,
)


def pcm_to_wav(pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _one_pole_highpass(x, alpha: float, x_prev: float):
    """y[i] = alpha·(y[i-1] + x[i] − x[i-1]), y[-1] = 0, x[-1] = x_prev – celé pole najednou.

    Se scipy přes `lfilter`, jinak numpy po blocích 256 vzorků (uzavřený tvar rekurence); výsledek je
    shodný s původní smyčkou po vzorcích, jen ~100× rychlejší (10 s řeči: ~3 ms místo ~0,3 s)."""
    import numpy as np

    try:
        from scipy.signal import lfilter
    except Exception:
        lfilter = None
    if lfilter is not None:
        y, _ = lfilter([alpha, -alpha], [1.0, -alpha], x, zi=[-alpha * x_prev])
        return y
    e = np.diff(x, prepend=x_prev) * alpha            # y[i] = alpha·y[i-1] + e[i]
    block = 256
    powers = alpha ** np.arange(block, dtype=np.float64)
    inv = 1.0 / powers
    out = np.empty_like(e)
    y_prev = 0.0
    for s in range(0, e.size, block):
        seg = e[s:s + block]
        n = seg.size
        yb = powers[:n] * (alpha * y_prev + np.cumsum(seg * inv[:n]))
        out[s:s + n] = yb
        y_prev = float(yb[-1])
    return out


def prepare_mic_float32(data: bytes, content_type: str = "audio/wav", default_rate: int = 16000):
    """Převede WAV nebo surové 16-bit PCM přímo na vyčištěné 16kHz float32 pole pro Whisper.

    Provádí:
      1. Inteligentní downmix stereo -> mono (detekuje odpojený druhý kanál i protifázi).
      2. 2-stupňovou horní propust 75 Hz (odstraní DC složku a 50Hz brum bez utlumení sykavek a úst).
      3. Pop-Immune AGC (zesílí tichý hlas seniora, aniž by jediné lupnutí ztlumilo zbytek věty).
      4. Ořez hluchého okrajového ticha se zachováním 150 ms náběhu a 220 ms doznění hlásek.
    Vrací None, pokud vstup není WAV/PCM (např. WebM/Opus – pak si ho dekóduje PyAV).
    """
    try:
        import numpy as np

        ct = (content_type or "").lower()
        raw_pcm: bytes | None = None
        rate = default_rate
        channels = 1
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            with wave.open(io.BytesIO(data), "rb") as w:
                if w.getsampwidth() != 2:
                    return None
                rate = w.getframerate() or default_rate
                channels = max(1, w.getnchannels())
                raw_pcm = w.readframes(w.getnframes())
        elif "wav" not in ct and ("pcm" in ct or "octet-stream" in ct):
            raw_pcm = data

        if not raw_pcm or len(raw_pcm) < 64:
            return None

        n_bytes = len(raw_pcm) - (len(raw_pcm) % (2 * channels))
        if n_bytes <= 0:
            return None
        samples = np.frombuffer(raw_pcm[:n_bytes], dtype="<i2").astype(np.float64)

        if channels == 2:
            left = samples[0::2]
            right = samples[1::2]
            rms_l = float(np.sqrt(np.mean(left * left))) if left.size else 0.0
            rms_r = float(np.sqrt(np.mean(right * right))) if right.size else 0.0
            max_rms, min_rms = max(rms_l, rms_r), min(rms_l, rms_r)
            if max_rms > 120.0 and min_rms < max_rms * 0.15:
                samples = left if rms_l >= rms_r else right
            elif min_rms > 80.0:
                denom = math.sqrt(float(np.sum(left * left)) * float(np.sum(right * right)))
                corr = float(np.sum(left * right)) / denom if denom > 0 else 0.0
                samples = 0.5 * (left - right) if corr < -0.55 else 0.5 * (left + right)
            else:
                samples = 0.5 * (left + right)
        elif channels > 2:
            samples = samples.reshape(-1, channels).mean(axis=1)

        if rate != 16000 and samples.size > 1:
            # Pásmově omezené převzorkování (polyfázní scipy nebo zrcadlově doplněné FFT bez okrajového lupnutí)
            resampled = None
            try:
                from scipy.signal import resample_poly

                g = math.gcd(16000, rate)
                resampled = resample_poly(samples, 16000 // g, rate // g)
            except Exception:
                resampled = None
            if resampled is None:
                n_in = int(samples.size)
                target_len = max(1, int(round(n_in * 16000.0 / rate)))
                pad = min(n_in - 1, max(32, rate // 40))
                padded = np.concatenate([samples[pad:0:-1], samples, samples[-2 : -2 - pad : -1]])
                padded_out_len = max(1, int(round(padded.size * 16000.0 / rate)))
                spec = np.fft.rfft(padded)
                out_spec = np.zeros(padded_out_len // 2 + 1, dtype=np.complex128)
                k = min(spec.size, out_spec.size)
                out_spec[:k] = spec[:k]
                res = np.fft.irfft(out_spec, n=padded_out_len) * (padded_out_len / padded.size)
                start_out = int(round(pad * 16000.0 / rate))
                sliced = res[start_out : start_out + target_len]
                if sliced.size < target_len:
                    sliced = np.pad(sliced, (0, target_len - sliced.size))
                samples = sliced
            else:
                samples = resampled
            rate = 16000

        n = int(samples.size)
        if n < 32:
            return None

        # 2-stupňová horní propust 75 Hz (bez dolní propusti, aby zůstala čistá artikulace a sykavky)
        rc = 1.0 / (2.0 * math.pi * 75.0)
        alpha = rc / (rc + 1.0 / rate)
        samples = _one_pole_highpass(samples, alpha, float(samples[0]))
        samples = _one_pole_highpass(samples, alpha, 0.0)

        # Pop-Immune AGC po 20ms oknech (čistý šum místnosti bez řeči 6× nezesilujeme)
        win = max(160, int(rate * 0.020))
        num = n // win
        if num >= 4:
            frames = samples[: num * win].reshape(num, win)
            f_rms = np.sqrt(np.mean(frames * frames, axis=1))
            f_peak = np.max(np.abs(frames), axis=1)
            floor = float(np.percentile(f_rms, 15))
            speech_gate = max(120.0, floor * 1.75)
            active_rms = f_rms[f_rms >= speech_gate]
            if active_rms.size == 0:
                active_rms = f_rms
            ref_rms = float(np.percentile(active_rms, 75))
            if float(np.max(f_rms)) >= 95.0 or ref_rms >= 80.0:
                gain = 1.0
                if 40.0 < ref_rms < 4800.0:
                    gain = min(6.0, 4800.0 / ref_rms)
                active_peaks = f_peak[f_rms >= speech_gate]
                if active_peaks.size == 0:
                    active_peaks = f_peak
                sustained_peak = float(np.percentile(active_peaks, 85))
                if sustained_peak > 0 and sustained_peak * gain > 24000.0:
                    gain = 24000.0 / sustained_peak
                padded = np.concatenate([f_peak[:1], f_peak, f_peak[-1:]])   # špička okna a obou sousedů
                loud = np.maximum(np.maximum(padded[:-2], padded[1:-1]), padded[2:]) * gain
                gate = np.where(loud > 24000.0, 24000.0 / np.maximum(loud, 1e-9), 1.0)
                centers = np.arange(num, dtype=np.float64) * win + (win - 1) / 2.0
                curve = np.interp(np.arange(n, dtype=np.float64), centers, gate)
                samples = np.clip(samples * (gain * curve), -28000.0, 28000.0)
            else:
                samples = np.clip(samples, -28000.0, 28000.0)

        audio = (samples / 32768.0).astype(np.float32)

        # Ořez přebytečného ticha na začátku a na konci (se štědrou rezervou 150 ms / 220 ms)
        min_keep = int(rate * 0.45)
        if n > min_keep and num >= 6:
            frames_f32 = audio[: num * win].reshape(num, win)
            rms_f32 = np.sqrt(np.mean(frames_f32 * frames_f32, axis=1))
            floor_f32 = float(np.percentile(rms_f32, 15))
            active_idx = np.flatnonzero(rms_f32 >= max(0.0025, floor_f32 * 2.1))
            if active_idx.size > 0:
                start = max(0, int(active_idx[0]) * win - int(rate * 0.150))
                end = min(n, int(active_idx[-1] + 1) * win + int(rate * 0.220))
                if end - start >= min_keep:
                    audio = audio[start:end]

        return np.ascontiguousarray(audio, dtype=np.float32)
    except Exception:
        return None


def _normalize_server_transcript(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if RX_SERVER_HALLUCINATION.search(cleaned):
        return ""
    for rx, repl in SENIOR_PHONETIC_FIXES:
        cleaned = rx.sub(repl, cleaned)
    return cleaned


class Voice:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._model_lock = asyncio.Lock()
        self._run_lock = threading.Lock()
        self._transcribe_params: set[str] | None = None
        self.last_elapsed_s: float = 0.0
        self._piper_voice = None
        self._tts_lock = threading.Lock()

    @property
    def stt_available(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    @property
    def tts_available(self) -> bool:
        if not (self.cfg.piper_model and Path(self.cfg.piper_model).exists()):
            return False
        return importlib.util.find_spec("piper") is not None or bool(self._piper_bin())

    def _piper_bin(self) -> str | None:
        b = self.cfg.piper_bin
        return b if Path(b).exists() else shutil.which(b)

    def _hotwords(self) -> str:
        words = list(BASE_HOTWORDS)
        for c in getattr(self.cfg, "all_contacts", []):
            if getattr(c, "name", ""):
                words.append(c.name)
            if getattr(c, "dative", ""):
                words.append(c.dative)
        return ", ".join(dict.fromkeys(w for w in words if w))

    def _filter_kwargs(self, model, kwargs: dict) -> dict:
        if self._transcribe_params is None:
            try:
                params = inspect.signature(model.transcribe).parameters
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                    self._transcribe_params = set()
                else:
                    self._transcribe_params = set(params)
            except (TypeError, ValueError):
                self._transcribe_params = set()
        if not self._transcribe_params:
            return kwargs
        return {k: v for k, v in kwargs.items() if k in self._transcribe_params}

    @staticmethod
    def _filter_init_kwargs(cls: object, kwargs: dict) -> dict:
        try:
            params = inspect.signature(cls).parameters  # type: ignore[arg-type]
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return kwargs
            return {k: v for k, v in kwargs.items() if k in params}
        except Exception:
            return kwargs

    def _resolve_compute_type(self) -> str:
        configured = str(getattr(self.cfg, "whisper_compute_type", "auto") or "auto").strip().lower()
        if configured and configured != "auto":
            return configured
        dev = str(getattr(self.cfg, "whisper_device", "auto") or "auto").strip().lower()
        if dev == "cuda":
            return "float16"
        if dev == "auto":
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    return "float16"
            except Exception:
                pass
        return "int8"

    async def _ensure_model(self):
        async with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                threads = int(getattr(self.cfg, "whisper_threads", 0) or 0)
                if threads <= 0:
                    threads = min(6, max(4, os.cpu_count() or 4))
                compute_type = self._resolve_compute_type()
                log.info(
                    "Načítám Faster-Whisper model %s (device=%s, %s, %d vláken)…",
                    self.cfg.whisper_model, self.cfg.whisper_device, compute_type, threads,
                )

                def _load_model() -> object:
                    base_kw = {
                        "device": self.cfg.whisper_device,
                        "compute_type": compute_type,
                        "cpu_threads": threads,
                        "num_workers": 1,
                    }
                    try:
                        params = inspect.signature(WhisperModel).parameters
                        supports_local = "local_files_only" in params or any(
                            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                        )
                    except Exception:
                        supports_local = True
                    if supports_local:
                        try:
                            return WhisperModel(
                                self.cfg.whisper_model,
                                **self._filter_init_kwargs(WhisperModel, {**base_kw, "local_files_only": True}),
                            )
                        except Exception as cache_err:
                            log.debug(
                                "Model '%s' není v lokální cache (%s) – zkouším stáhnout z HuggingFace.",
                                self.cfg.whisper_model, cache_err,
                            )
                    return WhisperModel(
                        self.cfg.whisper_model,
                        **self._filter_init_kwargs(WhisperModel, base_kw),
                    )

                self._model = await asyncio.to_thread(_load_model)
        return self._model

    async def warmup(self) -> None:
        """Přednačte a zahřeje Faster-Whisper model hned při startu serveru, aby první přepis trval ~0,5–1 s."""
        if not self.stt_available:
            return
        try:
            model = await self._ensure_model()
            if model is None:
                return
            import numpy as np

            noise = (np.random.default_rng(0).standard_normal(4000) * 0.003).astype(np.float32)
            hotwords = self._hotwords()
            t0 = time.monotonic()

            def _warm() -> None:
                with self._run_lock:
                    segments, _ = model.transcribe(
                        noise,
                        **self._filter_kwargs(model, {
                            "language": "cs",
                            "beam_size": max(1, int(getattr(self.cfg, "whisper_beam_size", 1))),
                            "best_of": 1,
                            "temperature": 0.0,
                            "without_timestamps": True,
                            "condition_on_previous_text": False,
                            "vad_filter": False,
                            "suppress_blank": True,
                            "max_new_tokens": 32,
                            "language_detection_segments": 1,
                            "initial_prompt": PROMPT_HINT,
                            "hotwords": hotwords or None,
                        }),
                    )
                    for _ in segments:
                        pass

            await asyncio.to_thread(_warm)
            log.info("Server Faster-Whisper zahřátý (%.2f s) – připraven na okamžitý přepis.", time.monotonic() - t0)
        except Exception as e:
            log.debug("Zahřátí serverového Whisperu přeskočeno: %s", e)

    async def transcribe(self, data: bytes, content_type: str = "audio/wav", rate: int = 16000) -> str:
        if not self.stt_available:
            raise RuntimeError("Faster-Whisper není nainstalovaný (pip install faster-whisper)")
        model = await self._ensure_model()
        # Čištění zvuku mimo smyčku událostí – jinak by po dobu výpočtu stály WebSockety všech zařízení
        audio_f32 = await asyncio.to_thread(prepare_mic_float32, data, content_type, rate)
        if audio_f32 is None and "wav" not in content_type and ("pcm" in content_type or "octet-stream" in content_type):
            data = pcm_to_wav(data, rate)

        has_voiced_energy = True
        if audio_f32 is not None and audio_f32.size > 0:
            import numpy as np

            has_voiced_energy = float(np.max(np.abs(audio_f32))) >= 0.006

        beam = max(1, int(getattr(self.cfg, "whisper_beam_size", 1)))
        hotwords = self._hotwords()
        prompt = PROMPT_HINT
        if self._filter_kwargs(model, {"hotwords": hotwords}).get("hotwords") is None and hotwords:
            prompt = f"{PROMPT_HINT} {hotwords}."
            hotwords = ""

        def run() -> str:
            with self._run_lock:
                src = audio_f32 if audio_f32 is not None else io.BytesIO(data)
                kw = self._filter_kwargs(model, {
                    "language": "cs",
                    "beam_size": beam,
                    "best_of": 1,
                    "temperature": 0.0,
                    "without_timestamps": True,
                    "condition_on_previous_text": False,
                    "vad_filter": audio_f32 is None,
                    "vad_parameters": {"threshold": 0.35, "min_speech_duration_ms": 120,
                                       "min_silence_duration_ms": 350, "speech_pad_ms": 200},
                    "suppress_blank": True,
                    "compression_ratio_threshold": 2.2,
                    "language_detection_segments": 1,
                    "max_new_tokens": 128,
                    "initial_prompt": prompt,
                    "hotwords": hotwords or None,
                })
                segments, _ = model.transcribe(src, **kw)
                raw = " ".join(s.text.strip() for s in segments if s.text).strip()
                cleaned = _normalize_server_transcript(raw)
                if not cleaned and has_voiced_energy and audio_f32 is not None and audio_f32.size >= 5600:
                    kw_retry = self._filter_kwargs(model, {
                        "language": "cs",
                        "beam_size": 1,
                        "best_of": 1,
                        "temperature": 0.0,
                        "without_timestamps": True,
                        "condition_on_previous_text": False,
                        "vad_filter": False,
                        "suppress_blank": True,
                        "max_new_tokens": 96,
                        "initial_prompt": None,
                        "hotwords": None,
                        "no_speech_threshold": None,
                        "log_prob_threshold": None,
                    })
                    segments2, _ = model.transcribe(audio_f32, **kw_retry)
                    raw2 = " ".join(s.text.strip() for s in segments2 if s.text).strip()
                    cleaned = _normalize_server_transcript(raw2)
                return cleaned

        t0 = time.monotonic()
        text = await asyncio.to_thread(run)
        self.last_elapsed_s = time.monotonic() - t0
        log.info("STT (%.2f s): %r", self.last_elapsed_s, text)
        return text

    async def synthesize(self, text: str) -> bytes | None:
        """Vrátí WAV s českou řečí (hlas Jirka, pomaleji ~0,85×), nebo None.

        Hlas běží přímo v procesu serveru (knihovna piper-tts, model zůstává v paměti) – první věta ~1 s,
        další desetiny sekundy. Starší cesta přes program `piper` zůstává jako záloha."""
        if not self.tts_available or not text:
            return None
        if importlib.util.find_spec("piper") is not None:
            try:
                return await asyncio.to_thread(self._piper_python, text)
            except Exception as e:  # noqa: BLE001
                log.warning("Piper (Python) selhal: %s", e)
        if not self._piper_bin():
            return None
        fd, out = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._piper_bin(), "--model", self.cfg.piper_model, "--output_file", out,
                "--length_scale", f"{1 / max(self.cfg.speech_rate, 0.5):.2f}", "--sentence_silence", "0.35",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), 30)
            if proc.returncode != 0:
                log.warning("Piper selhal: %s", err.decode(errors="ignore")[-300:])
                return None
            return Path(out).read_bytes()
        finally:
            Path(out).unlink(missing_ok=True)

    def _piper_python(self, text: str) -> bytes:
        with self._tts_lock:
            if self._piper_voice is None:
                from piper import PiperVoice

                log.info("Načítám hlas Piper: %s", self.cfg.piper_model)
                self._piper_voice = PiperVoice.load(self.cfg.piper_model)
            voice = self._piper_voice
            scale = 1 / max(self.cfg.speech_rate, 0.5)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                if hasattr(voice, "synthesize_wav"):               # piper-tts >= 1.3
                    try:
                        from piper import SynthesisConfig

                        voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=scale))
                    except ImportError:
                        voice.synthesize_wav(text, w)
                else:                                              # piper-tts 1.2
                    voice.synthesize(text, w, length_scale=scale, sentence_silence=0.35)
            return buf.getvalue()
