"""HTTP/WebSocket rozhraní orchestrátoru.

  /                rodinná stránka (jen čtení)
  /button          simulátor tlačítka + obrazovka seniora
  /ws/device       démony na zařízeních (MCP přes WebSocket)
  /ws/ui           živé události pro stránky
  POST /api/press, /api/utterance, /api/audio, /api/button/audio, /api/stop
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..common.discovery import Advertiser
from ..common.protocol import AUTH_SCHEME, api_key, auth_proof, new_nonce, proof_ok
from .adb_watchdog import AdbWatchdog
from .brain import Brain
from .config import Config, load_config
from .family import Event, FamilyLog
from .orchestrator import Orchestrator
from .registry import Registry, dumps
from .voice import Voice

log = logging.getLogger("aiko.server")
STATIC = Path(__file__).parent / "static"


class Hub:
    """Rozesílá živé události otevřeným stránkám."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def send(self, msg: dict[str, Any]) -> None:
        data = dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)


LAN_SUFFIXES = (".local", ".localhost", ".lan", ".home", ".home.arpa", ".internal")


def _hostname(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):                                  # [::1]:8765
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def host_allowed(host_header: str, extra: list[str] | tuple[str, ...] = ()) -> bool:
    """Ochrana proti DNS rebinding: server odpovídá jen na IP adresy a jména z domácí sítě.

    Útočníkova doména (evil.example → 192.168.1.10) má v hlavičce Host svoje jméno s tečkou,
    jména bez tečky (localhost, aiko-pc) a .local/.lan projdou.
    """
    host = _hostname(host_header)
    if not host or host in {h.lower() for h in extra}:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return "." not in host or host.endswith(LAN_SUFFIXES)


def origin_allowed(origin: str | None, host_header: str) -> bool:
    """Prohlížeč posílá Origin u POST a WebSocketů. Cizí stránka (CSRF, cross-site WebSocket)
    tak nemůže mačkat tlačítko, posílat věty ani se vydávat za zařízení. Bez Origin = ne-prohlížeč."""
    if origin is None:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (host_header or "").strip().lower()


COOKIE = "aiko_key"
# Bez klíče: přihlašovací stránka, styly, démoni (ti se ověřují vlastním challenge–response)
# a /api/hello, kde se server tlačítku prokáže tokenem DŘÍV, než mu tlačítko pošle klíč a zvuk.
OPEN_PATHS = ("/static/", "/login", "/api/login", "/ws/device", "/api/hello")
SERVER_PROOF_ROLE = "server-api"
MAX_AUDIO_BYTES = 8_000_000      # ~4 min řeči 16 kHz mono; větší nahrávka je chyba nebo útok na paměť
MAX_TEXT_CHARS = 2000
PAGES = ("/", "/button")


def is_local(client: Any) -> bool:
    """Požadavek ze stejného počítače (aiko.cli, tlačítko u seniora, záložní server na Raspberry Pi)."""
    host = getattr(client, "host", "") or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Lockout:
    """Po opakovaně špatném klíči z jedné adresy chvíli nic nepouštět (hádání krátkého tokenu)."""

    def __init__(self, limit: int = 10, window_s: float = 600):
        self.limit, self.window = limit, window_s
        self.fails: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < self.window]
        self.fails[ip] = recent
        return len(recent) >= self.limit

    def fail(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.time())
        if len(self.fails) > 1000:                 # paměť nesmí růst donekonečna
            self.fails = dict(list(self.fails.items())[-500:])


class Login(BaseModel):
    key: str


class Utterance(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    source: str = Field("web", max_length=40)


class Press(BaseModel):
    source: str = "web"


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    hub = Hub()
    registry = Registry()
    family = FamilyLog(cfg.data_dir, cfg.notify_webhook)
    brain = Brain(cfg)
    voice = Voice(cfg)
    orch = Orchestrator(cfg, registry, brain, family, emit=hub.send)

    async def devices_changed() -> None:
        await hub.send({"type": "devices", "devices": registry.public()})

    async def on_event(e: Event) -> None:
        await hub.send({"type": "event", "event": e.to_dict(), "stats": family.stats()})

    registry.on_change = devices_changed
    family.on_event = on_event

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        adv = Advertiser(cfg.port) if cfg.mdns else None
        dog = AdbWatchdog() if cfg.adb_watchdog else None
        if adv:
            await adv.start()
        if dog:
            await dog.start()
        if not cfg.token and cfg.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("Párovací token není nastavený – připojit se jako zařízení a ovládat Aiko přes API může "
                        "kdokoli v síti. Nastavte [aiko] token v aiko.toml (stejný i démonům přes --token / "
                        "AIKO_TOKEN).")
        elif cfg.token:
            log.info("Přístup z jiných počítačů jen s klíčem Aiko: prohlížeč se přihlásí tokenem na /login, "
                     "ostatní klienti hlavičkou Authorization (klíč vypíše: python -m aiko.server --api-key)")
        async def warm_ollama() -> None:
            # Model do paměti hned při startu (stejný kontext jako dotazy – jinak by se přenačítal)
            import httpx

            models = {m for m in (cfg.ollama_model, cfg.ollama_vlm_model) if m}
            for m in models:
                try:
                    async with httpx.AsyncClient(timeout=300) as c:
                        await c.post(f"{cfg.ollama_url}/api/generate", json={
                            "model": m, "keep_alive": "60m", "options": {"num_ctx": cfg.ollama_vlm_num_ctx}})
                    log.info("Model %s je načtený v paměti.", m)
                except Exception as e:  # noqa: BLE001
                    log.info("Model %s se nepodařilo předem načíst: %s", m, e)

        ollama_warm = asyncio.create_task(warm_ollama())
        if voice.tts_available:
            # Hlas (Piper) načíst ještě před prvním požadavkem: první načtení knihovny trvá i desítky sekund
            # a brzdilo by celý server uprostřed rozhovoru.
            try:
                await voice.synthesize("Dobrý den.")
                log.info("Hlas Jirka je připravený.")
            except Exception as e:  # noqa: BLE001
                log.warning("Hlas se nepodařilo připravit: %s", e)
        warm_task = None
        if getattr(cfg, "whisper_warmup", True) and voice.stt_available:
            # Na pozadí: server hned přijímá požadavky (i z tlačítka), model se mezitím načte a zahřeje
            warm_task = asyncio.create_task(voice.warmup())
        provider = await brain.provider()
        vlm = await orch.vlm_ready()
        log.info("Aiko server běží na portu %s · porozumění: %s · GUI agent: %s · STT: %s · TTS: %s",
                 cfg.port, provider, vlm.name if vlm else "vypnutý", voice.stt_available, voice.tts_available)
        yield
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
        if not ollama_warm.done():
            ollama_warm.cancel()
        if adv:
            await adv.stop()
        if dog:
            await dog.stop()

    app = FastAPI(title="Aiko LAN", lifespan=lifespan)
    app.state.cfg, app.state.registry, app.state.orch, app.state.family, app.state.voice = (
        cfg, registry, orch, family, voice,
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def _request_ok(headers: Any) -> bool:
        host = headers.get("host", "")
        return host_allowed(host, cfg.allowed_hosts) and origin_allowed(headers.get("origin"), host)

    # --- přístup z jiných počítačů: jen s klíčem odvozeným z párovacího tokenu -------------------
    # Dřív API nemělo žádné přihlášení: kdokoli ve stejné Wi-Fi (hackathon, kavárna) si přečetl záznam
    # pro rodinu a poslal Aiko „ano“ – a tím klikal na počítači seniora. Ze stejného počítače (localhost)
    # zůstává všechno, jak bylo; bez nastaveného tokenu taky (server to hlásí v logu).
    lockout = Lockout()

    def _presented(headers: Any, cookies: Any) -> str:
        auth = str(headers.get("authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return str(cookies.get(COOKIE) or "")

    def _key_ok(given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), api_key(cfg.token).encode())

    def _needs_key(client: Any, path: str) -> bool:
        return bool(cfg.token) and not path.startswith(OPEN_PATHS) and not is_local(client)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):  # noqa: ANN001, ANN202
        if not _request_ok(request.headers):
            log.warning("Odmítnut požadavek %s %s (Host=%s, Origin=%s)", request.method, request.url.path,
                        request.headers.get("host"), request.headers.get("origin"))
            return JSONResponse({"error": "cizí původ požadavku"}, status_code=403)
        path = request.url.path
        if _needs_key(request.client, path):
            ip = getattr(request.client, "host", "?")
            if lockout.blocked(ip):
                return JSONResponse({"error": "příliš mnoho špatných pokusů, zkuste to za 10 minut"},
                                    status_code=429)
            given = _presented(request.headers, request.cookies)
            if not _key_ok(given):
                if given:                          # špatný klíč se počítá, pouhá návštěva stránky ne
                    lockout.fail(ip)
                    log.warning("Odmítnut %s %s z %s: špatný klíč", request.method, path, ip)
                if request.method == "GET" and path in PAGES:
                    return RedirectResponse(f"/login?next={path}", status_code=303)
                return JSONResponse({"error": "chybí klíč Aiko (Authorization: Bearer …)",
                                     "say": "Sem se dá jen s klíčem Aiko."}, status_code=401)
        return await call_next(request)

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(body: Login, request: Request) -> Response:
        """Prohlížeč pošle klíč (odvodí ho z tokenu sám, token po síti nejde) a dostane cookie."""
        ip = getattr(request.client, "host", "?")
        if not cfg.token:
            return JSONResponse({"ok": True, "open": True})
        if lockout.blocked(ip):
            return JSONResponse({"error": "příliš mnoho špatných pokusů, zkuste to za 10 minut"}, status_code=429)
        if not _key_ok(body.key.strip()):
            lockout.fail(ip)
            log.warning("Špatné přihlášení z %s", ip)
            return JSONResponse({"error": "špatný kód"}, status_code=401)
        r = JSONResponse({"ok": True})
        r.set_cookie(COOKIE, api_key(cfg.token), max_age=365 * 24 * 3600, httponly=True, samesite="strict",
                     path="/")
        log.info("Přihlášen prohlížeč z %s", ip)
        return r

    # --- stránky -----------------------------------------------------------------
    @app.get("/")
    async def chat_page() -> FileResponse:
        return FileResponse(STATIC / "chat.html")

    @app.get("/family")
    async def family_page() -> FileResponse:
        return FileResponse(STATIC / "family.html")

    @app.get("/button")
    async def button_page() -> FileResponse:
        return FileResponse(STATIC / "button.html")

    # --- API -----------------------------------------------------------------------
    @app.get("/api/hello")
    async def hello(nonce: str = "") -> dict[str, Any]:
        """Server se prokáže tlačítku: podepíše náhodné číslo klienta párovacím tokenem.

        Tlačítko (Raspberry Pi) hledá server přes mDNS – ohlásit se „jako Aiko“ může v síti kdokoli. Bez
        tohoto ověření by podvržený server dostal klíč API, zvuk z mikrofonu a mohl by seniorovi z tlačítka
        cokoli „říct“. Token po síti nejde, podpis platí jen pro jedno číslo (nejde přehrát jinde)."""
        if not cfg.token:
            return {"aiko": True, "auth": "none"}
        if not (16 <= len(nonce) <= 128 and all(c in "0123456789abcdef" for c in nonce)):
            raise HTTPException(400, "nonce musí být 16–128 hexadecimálních znaků")
        return {"aiko": True, "auth": AUTH_SCHEME,
                "proof": auth_proof(cfg.token, SERVER_PROOF_ROLE, nonce, "")}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {
            "state": orch.state.value, "reply": orch.current.to_dict(),
            "devices": registry.public(), "events": family.recent(100), "stats": family.stats(),
            "senior": cfg.senior_name, "family": cfg.family.name, "family_dative": cfg.family.to_whom,
            "voice": {"stt": voice.stt_available, "tts": voice.tts_available},
            "llm": await brain.provider(),
            "vlm": orch.vlm.name if orch.vlm else None,
        }

    @app.post("/api/local/start")
    async def local_start() -> dict[str, Any]:
        """Tlačítko „Spustit vše“ v chatu: nastartuje, co neběží – Ollamu a ovládání tohoto počítače."""
        import shutil
        import subprocess
        import sys

        import httpx

        started: list[str] = []
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                ollama_ok = (await c.get(f"{cfg.ollama_url}/api/tags")).status_code == 200
        except Exception:  # noqa: BLE001
            ollama_ok = False
        if not ollama_ok and shutil.which("ollama"):
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
            started.append("Ollama")
        if not any(d.get("kind") == "pc" and d.get("online") for d in registry.public()):
            subprocess.Popen([sys.executable, "-m", "aiko.agents.pc", "--server",
                              f"ws://127.0.0.1:{cfg.port}/ws/device", "--name", "Můj počítač"],
                             creationflags=flags)
            started.append("ovládání počítače")
        say = ("Spouštím: " + ", ".join(started) + ". Za chvilku bude vše připravené.") if started             else "Všechno už běží."
        return {"started": started, "ollama_missing": not ollama_ok and not shutil.which("ollama"), "say": say}

    @app.post("/api/press")
    async def press(p: Press | None = None) -> dict[str, Any]:
        return (await orch.press((p or Press()).source)).to_dict()

    @app.post("/api/stop")
    async def stop() -> dict[str, Any]:
        return (await orch.stop("web")).to_dict()

    @app.post("/api/utterance")
    async def utterance(u: Utterance) -> dict[str, Any]:
        return (await orch.handle(u.text, u.source)).to_dict()

    @app.post("/api/audio")
    async def audio(request: Request, rate: int = 16000, source: str = "web-audio") -> dict[str, Any]:
        """Push-to-talk z prohlížeče (a druhý pokus o přepis z tlačítka na Pi): audio -> Whisper -> orchestrátor."""
        text = await _transcribe(request, rate)
        await hub.send({"type": "transcript", "text": text})
        return (await orch.handle(text, source if source in ("web-audio", "button") else "web-audio")).to_dict()

    @app.post("/api/button/audio")
    async def button_audio(request: Request, rate: int = 16000) -> Response:
        """Fyzické tlačítko (ESP32): surové PCM 16 bit mono -> odpověď jako WAV.

        Stav pro LED a bzučák je v hlavičkách X-Aiko-*.
        """
        try:
            text = await _transcribe(request, rate)
        except HTTPException as e:
            if e.status_code in (400, 413):          # chybný / obří požadavek – žádná „odpověď“ seniorovi
                raise
            text = ""                                # bez STT na serveru: tlačítko dostane „nerozuměla jsem“
        await hub.send({"type": "transcript", "text": text})
        reply = await orch.handle(text, "button")
        headers = {
            "X-Aiko-Led": reply.led, "X-Aiko-Tone": reply.tone, "X-Aiko-Screen": reply.screen,
            "X-Aiko-Expects": "1" if reply.expects_answer else "0", "X-Aiko-Say": quote(reply.say),
        }
        wav = await voice.synthesize(reply.say)
        if wav:
            return Response(wav, media_type="audio/wav", headers=headers)
        return Response(status_code=204, headers=headers)

    @app.post("/api/button/press")
    async def button_press() -> Response:
        reply = await orch.press("button")
        wav = await voice.synthesize(reply.say)
        headers = {"X-Aiko-Led": reply.led, "X-Aiko-Tone": reply.tone}
        return Response(wav, media_type="audio/wav", headers=headers) if wav else Response(status_code=204,
                                                                                            headers=headers)

    @app.get("/api/tts")
    async def tts(text: str) -> Response:
        wav = await voice.synthesize(text[:500])
        if not wav:
            raise HTTPException(404, "TTS není k dispozici")
        return Response(wav, media_type="audio/wav")

    async def _read_audio(request: Request) -> bytes:
        """Tělo požadavku s limitem (zvládne i chunked upload z ESP32). Velikost se hlídá průběžně –
        obří nebo nekonečné nahrávání nevyčerpá paměť serveru."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "nahrávka je příliš dlouhá")
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "nahrávka je příliš dlouhá")
        return bytes(buf)

    async def _transcribe(request: Request, rate: int) -> str:
        if not voice.stt_available:
            raise HTTPException(501, "Faster-Whisper není nainstalovaný – použijte textový vstup")
        if not 4000 <= rate <= 96000:
            raise HTTPException(400, "nepodporovaná vzorkovací frekvence")
        data = await _read_audio(request)
        if len(data) < 1600:
            return ""
        return await voice.transcribe(data, request.headers.get("content-type", "audio/wav"), rate)

    # --- WebSockety --------------------------------------------------------------------
    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket) -> None:
        if not _request_ok(ws.headers):
            await ws.close(code=1008)
            return
        if _needs_key(ws.client, "/ws/ui") and not _key_ok(_presented(ws.headers, ws.cookies)):
            await ws.close(code=1008)              # živé hlášky a záznam pro rodinu jen s klíčem
            return
        await ws.accept()
        hub.clients.add(ws)
        try:
            await ws.send_text(dumps({"type": "snapshot", **(await state())}))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    @app.websocket("/ws/device")
    async def ws_device(ws: WebSocket) -> None:
        # démony nejsou prohlížeč – WebSocket s hlavičkou Origin otevřela webová stránka
        if ws.headers.get("origin") is not None or not host_allowed(ws.headers.get("host", ""),
                                                                     cfg.allowed_hosts):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close()
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello" or not hello.get("device_id"):
            await ws.close()
            return
        if cfg.token and not await _device_authenticated(ws, hello):
            log.warning("Odmítnuto zařízení %s: neprokázalo se párovacím tokenem", hello.get("device_id"))
            try:
                await ws.send_text(dumps({"type": "error", "error": "špatný párovací token"}))
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
            return
        send_lock = asyncio.Lock()

        async def send(msg: dict[str, Any]) -> None:
            async with send_lock:
                await ws.send_text(dumps(msg))

        dev = await registry.attach(hello, send)
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                t = msg.get("type")
                if t == "result":
                    registry.on_result(msg)
                elif t == "status":
                    await registry.update_status(dev.device_id, msg.get("status") or {})
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            await registry.detach(dev.device_id, send)

    async def _device_authenticated(ws: WebSocket, hello: dict[str, Any]) -> bool:
        nd = hello.get("nonce")
        if hello.get("auth") == AUTH_SCHEME and isinstance(nd, str) and 8 <= len(nd) <= 128:
            ns = new_nonce()
            await ws.send_text(dumps({"type": "challenge", "nonce": ns,
                                      "proof": auth_proof(cfg.token, "server", nd, ns)}))
            try:
                msg = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
            except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
                return False
            return isinstance(msg, dict) and msg.get("type") == "auth" and proof_ok(
                cfg.token, "device", ns, nd, msg.get("proof"))
        # starší démon posílá token přímo (funguje, ale podvržený server by ho viděl)
        if hmac.compare_digest(str(hello.get("token") or "").encode(), cfg.token.encode()):
            log.warning("Zařízení %s posílá token v čitelné podobě – aktualizujte démona", hello.get("device_id"))
            return True
        return False

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Chyba při %s", request.url.path)
        return JSONResponse({"say": "Něco se pokazilo. Zkuste to prosím znovu.", "error": str(exc)},
                            status_code=500)

    return app
