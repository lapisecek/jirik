"""HTTP/WebSocket rozhraní Aiko.

  /                 chat
  /ws/ui            živé události pro všechna otevřená okna (jedna konverzace, jeden hlas)
  /ws/device        ovládání tohoto počítače (démon aiko.agents.pc)
  POST /api/chat, /api/stop, /api/clear, /api/settings, /api/link/*
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
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..common.protocol import AUTH_SCHEME, api_key, auth_proof, new_nonce, proof_ok
from .chat import Chat
from .config import Config, load_config
from .hardware import LEVELS, Hardware
from .link import Link, firewall_rule_present, network_is_public, open_firewall
from .models import Models
from .registry import Registry, dumps
from .settings import MAX_CTX, MIN_CTX, SettingsStore, clamp_ctx
from .voice import Speaker, Voice

log = logging.getLogger("aiko.server")
STATIC = Path(__file__).parent / "static"


class Hub:
    """Rozesílá živé události všem otevřeným oknům."""

    def __init__(self) -> None:
        # okno → None (živé), nebo seznam událostí, které čekají, než okno dostane úvodní snímek stavu
        self.clients: dict[WebSocket, list[str] | None] = {}

    async def send(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        data = dumps(msg)
        for ws, buf in list(self.clients.items()):
            if buf is not None:
                buf.append(data)
                continue
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                self.clients.pop(ws, None)

    async def join(self, ws: WebSocket, snapshot: Callable[[], dict[str, Any]]) -> None:
        """Snímek stavu jako první zpráva; co se mezitím stalo, přijde hned za ním (nic se neztratí ani nezdvojí)."""
        self.clients[ws] = []
        await ws.send_text(dumps(snapshot()))
        while self.clients.get(ws):
            pending, self.clients[ws] = self.clients[ws], []
            for d in pending:
                await ws.send_text(d)
        if ws in self.clients:
            self.clients[ws] = None


LAN_SUFFIXES = (".local", ".localhost", ".lan", ".home", ".home.arpa", ".internal")


def _hostname(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def host_allowed(host_header: str, extra: list[str] | tuple[str, ...] = ()) -> bool:
    """Ochrana proti DNS rebinding: jen IP adresy a jména z domácí sítě."""
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
    """Cizí stránka v prohlížeči nesmí posílat Aiko příkazy (CSRF, cross-site WebSocket)."""
    if origin is None:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (host_header or "").strip().lower()


def is_local(client: Any) -> bool:
    host = getattr(client, "host", "") or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


COOKIE = "aiko_key"
OPEN_PATHS = ("/static/", "/login", "/api/login", "/ws/device", "/api/hello")
MAX_TEXT = 16000
MAX_IMAGE = 6_000_000


class Login(BaseModel):
    key: str


class ChatIn(BaseModel):
    text: str = Field("", max_length=MAX_TEXT)
    images: list[str] = Field(default_factory=list, max_length=4)
    space: str = Field("chat", max_length=12)
    target: str = Field("", max_length=60)


class SpaceIn(BaseModel):
    space: str = Field("", max_length=12)


class SettingsIn(BaseModel):
    voice: bool | None = None
    uncensored: bool | None = None
    level: str | None = None
    autonomous: bool | None = None
    num_ctx: int | None = None


class LevelIn(BaseModel):
    level: str
    uncensored: bool | None = None


class ModelIn(BaseModel):
    model: str = Field(max_length=120)


class PairIn(BaseModel):
    name: str = Field(max_length=40)


class ConfirmIn(BaseModel):
    ok: bool
    name: str = Field("", max_length=40)


class FirewallIn(BaseModel):
    switch_private: bool = False


class Lockout:
    def __init__(self, limit: int = 10, window_s: float = 600):
        self.limit, self.window = limit, window_s
        self.fails: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < self.window]
        return len(self.fails[ip]) >= self.limit

    def fail(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.time())


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    hub = Hub()
    registry = Registry()
    settings = SettingsStore(cfg.data_dir)
    if settings.value.level not in LEVELS:
        settings.update({"level": "low"})
    if "num_ctx" in settings.explicit:
        cfg.num_ctx = cfg.ollama_vlm_num_ctx = clamp_ctx(settings.value.num_ctx)
    else:
        cfg.num_ctx = cfg.ollama_vlm_num_ctx = clamp_ctx(cfg.num_ctx)
        settings.value.num_ctx = cfg.num_ctx
    voice = Voice(cfg)
    speaker = Speaker(voice, hub.send, cfg.data_dir)
    speaker.enabled = settings.value.voice
    link = Link(cfg, registry, hub.send)
    hardware = Hardware(cfg.data_dir)
    models = Models(cfg, hardware, hub.send)
    spaces = {s: Chat(cfg, registry, link, speaker, settings, hardware, models, hub.send, space=s)
              for s in ("chat", "devices")}
    chat = spaces["chat"]

    def space(name: str) -> Chat:
        return spaces.get(name) or chat

    def devices() -> list[dict[str, Any]]:
        return [{"id": d.device_id, "name": d.name, "online": d.available, "remote": d.device_id.startswith("link-"),
                 "locked": bool((d.status or {}).get("locked"))}
                for d in registry.devices.values() if d.kind == "pc"]

    last_devices: list[Any] = [None]

    async def devices_changed() -> None:
        now = devices()
        if now != last_devices[0]:                    # stav chodí každých 10 s – posílat jen změny
            last_devices[0] = now
            await hub.send({"type": "devices", "devices": now, "nodes": link.public()})

    registry.on_change = devices_changed

    async def models_info() -> dict[str, Any]:
        names = await models.installed()
        return {"hardware": hardware.probe(), "levels": hardware.describe(names, settings.value.uncensored),
                "pulls": models.pulling(), "installed": sorted(names)}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        speaker.start()
        await link.start()
        tasks = [asyncio.create_task(chat.warm())]
        if voice.available:
            try:                          # hlas načíst předem v hlavním vlákně (~1 s; ve vlákně ~12 s)
                voice._synth("Ahoj.")
            except Exception as e:  # noqa: BLE001
                log.warning("Hlas se nepodařilo načíst: %s", e)
        log.info("Jiřík běží na %s:%s · úroveň %s · hlas %s", cfg.host, cfg.port, settings.value.level,
                 "ano" if voice.available else "ne")
        yield
        for t in tasks:
            t.cancel()
        for s in spaces.values():
            await s.stop()
        await speaker.close()
        await link.stop()

    app = FastAPI(title="Jiřík", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.chat, app.state.registry, app.state.link = chat, registry, link
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    lockout = Lockout()

    def _request_ok(headers: Any) -> bool:
        host = headers.get("host", "")
        return host_allowed(host, cfg.allowed_hosts) and origin_allowed(headers.get("origin"), host)

    def _presented(headers: Any, cookies: Any) -> str:
        auth = str(headers.get("authorization") or "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else str(cookies.get(COOKIE) or "")

    def _key_ok(given: str) -> bool:
        return bool(cfg.token) and bool(given) and hmac.compare_digest(given.encode(), api_key(cfg.token).encode())

    def _needs_key(client: Any, path: str) -> bool:
        return not path.startswith(OPEN_PATHS) and not is_local(client)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        if not _request_ok(request.headers):
            return JSONResponse({"error": "cizí původ požadavku"}, status_code=403)
        path = request.url.path
        if _needs_key(request.client, path):
            ip = getattr(request.client, "host", "?")
            if lockout.blocked(ip):
                return JSONResponse({"error": "příliš mnoho pokusů"}, status_code=429)
            given = _presented(request.headers, request.cookies)
            if not _key_ok(given):
                if given:
                    lockout.fail(ip)
                if request.method == "GET" and path == "/":
                    return RedirectResponse("/login", status_code=303)
                return JSONResponse({"error": "chybí klíč Jiříka"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(body: Login, request: Request) -> Response:
        ip = getattr(request.client, "host", "?")
        if not cfg.token:
            return JSONResponse({"error": "přístup z jiných počítačů je vypnutý"}, status_code=403)
        if lockout.blocked(ip):
            return JSONResponse({"error": "příliš mnoho pokusů, zkuste to za 10 minut"}, status_code=429)
        if not _key_ok(body.key.strip()):
            lockout.fail(ip)
            return JSONResponse({"error": "špatný kód"}, status_code=401)
        r = JSONResponse({"ok": True})
        r.set_cookie(COOKIE, api_key(cfg.token), max_age=365 * 24 * 3600, httponly=True, samesite="strict")
        return r

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(STATIC / "chat.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/hello")
    async def hello() -> dict[str, Any]:
        return {"aiko": True}

    def build_snapshot(status: dict[str, Any], minfo: dict[str, Any]) -> dict[str, Any]:
        return {"type": "snapshot", "settings": settings.value.to_dict(), "status": status, "voice": voice.available,
                "spaces": {k: {"messages": s.messages, "current": s._current, "context": s.context_info()}
                           for k, s in spaces.items()},
                "models": minfo, "devices": devices(), "nodes": link.public(), "pairing": link.pair_public(),
                "user_name": cfg.greeting or cfg.user_name}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return build_snapshot(await chat.status(), await models_info())

    @app.post("/api/chat")
    async def send(body: ChatIn) -> dict[str, Any]:
        if sum(len(i) for i in body.images) > MAX_IMAGE:
            raise HTTPException(413, "obrázky jsou moc velké")
        images = [i.split(",", 1)[1] if i.startswith("data:") else i for i in body.images]
        return {"id": await space(body.space).send(body.text, images, body.target)}

    @app.post("/api/stop")
    async def stop(body: SpaceIn | None = None) -> dict[str, Any]:
        if body and body.space:
            await space(body.space).stop()
        else:
            for s in spaces.values():
                await s.stop()
        return {"ok": True}

    @app.post("/api/hush")
    async def hush() -> dict[str, Any]:
        speaker.hush()
        return {"ok": True}

    @app.post("/api/clear")
    async def clear(body: SpaceIn | None = None) -> dict[str, Any]:
        await space(body.space if body else "chat").clear()
        return {"ok": True}

    @app.post("/api/settings")
    async def set_settings(body: SettingsIn) -> dict[str, Any]:
        before_model, *_ = await chat.pick()
        before_ctx = cfg.num_ctx
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        if "level" in changes and changes["level"] not in LEVELS:
            raise HTTPException(400, "neznámá úroveň")
        if "num_ctx" in changes:
            raw_ctx = changes["num_ctx"]
            if isinstance(raw_ctx, bool) or not (MIN_CTX <= int(raw_ctx) <= MAX_CTX):
                raise HTTPException(400, f"velikost kontextu musí být {MIN_CTX} až {MAX_CTX}")
            changes["num_ctx"] = int(raw_ctx)
        new = settings.update(changes)
        speaker.enabled = new.voice
        if not new.voice:
            speaker.hush()
        after_model, *_ = await chat.pick()
        cfg.ollama_vlm_model = after_model
        if new.num_ctx != before_ctx:
            cfg.num_ctx = new.num_ctx
            cfg.ollama_vlm_num_ctx = new.num_ctx
            for s in spaces.values():
                await s.fit_after_resize()
        if after_model != before_model or (new.num_ctx != before_ctx and not chat.busy):
            unload = before_model if after_model != before_model else ""
            asyncio.create_task(chat.warm(unload=unload))
        await hub.send({"type": "settings", "settings": new.to_dict(), "status": await chat.status(),
                        "models": await models_info(),
                        "contexts": {k: s.context_info() for k, s in spaces.items()}})
        return new.to_dict()

    @app.post("/api/context/compact")
    async def compact_context(body: SpaceIn | None = None) -> dict[str, Any]:
        return await space(body.space if body else "chat").compact_context(force=True)

    # --- modely (úrovně myšlení) ----------------------------------------------------------------
    @app.get("/api/models")
    async def get_models() -> dict[str, Any]:
        return await models_info()

    @app.post("/api/models/pull")
    async def pull(body: LevelIn) -> dict[str, Any]:
        lv = LEVELS.get(body.level)
        if not lv:
            raise HTTPException(400, "neznámá úroveň")
        model = lv.uncensored if (settings.value.uncensored if body.uncensored is None else body.uncensored) \
            else lv.model
        need = lv.size_gb + 0.5
        if hardware.probe()["disk_free_gb"] < need:
            return {"ok": False, "error": f"Na disku chybí místo – potřeba je asi {need:.0f} GB."}
        await models.pull(model)
        return {"ok": True, "model": model}

    @app.post("/api/models/cancel")
    async def cancel(body: ModelIn) -> dict[str, Any]:
        await models.cancel(body.model)
        return {"ok": True}

    @app.post("/api/models/delete")
    async def delete(body: ModelIn) -> dict[str, Any]:
        active, *_ = await chat.pick()
        if body.model == active:
            return {"ok": False, "error": "Tento model se právě používá – přepněte nejdřív na jinou úroveň."}
        return {"ok": await models.delete(body.model)}

    # --- Jiřík Link -----------------------------------------------------------------------------
    @app.post("/api/link/pair/open")
    async def pair_open() -> dict[str, Any]:
        try:
            p = await link.open_pairing()
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        firewall, net_pub = await asyncio.gather(
            asyncio.to_thread(firewall_rule_present, cfg),
            asyncio.to_thread(network_is_public),
        )
        if not firewall:
            open_firewall(cfg)
        return {"ok": True, "pairing": p, "firewall_prompt": not firewall,
                "firewall_ok": firewall, "network_public": net_pub}

    @app.post("/api/link/firewall")
    async def link_firewall(body: FirewallIn | None = None) -> dict[str, Any]:
        open_firewall(cfg, switch_private=bool(body and body.switch_private))
        return {"ok": True}

    @app.post("/api/link/pair/confirm")
    async def pair_confirm(body: ConfirmIn) -> dict[str, Any]:
        try:
            return await link.confirm_pairing(body.ok, body.name)
        except LookupError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/link/pair/close")
    async def pair_close() -> dict[str, Any]:
        await link.close_pairing()
        link.pairing = None
        await hub.send({"type": "pairing", "pairing": None})
        return {"ok": True}

    @app.post("/api/link/remove/{nid}")
    async def unpair(nid: str) -> dict[str, Any]:
        return {"ok": await link.remove(nid)}

    @app.post("/api/link/rename/{nid}")
    async def rename(nid: str, body: PairIn) -> dict[str, Any]:
        return {"ok": await link.rename(nid, body.name)}

    @app.post("/api/link/wake/{nid}")
    async def wake(nid: str) -> dict[str, Any]:
        try:
            await link.wake(nid, timeout=30)
            return {"ok": True}
        except (LookupError, TimeoutError) as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/devices/{did}/screen")
    async def device_screen(did: str) -> dict[str, Any]:
        """Náhled obrazovky zařízení pro záložku Zařízení."""
        from .chat import _thumb

        if did == "local":
            dev = chat._local()
        else:
            try:
                dev = await link.wake(did, timeout=30)
            except (LookupError, TimeoutError) as e:
                return {"ok": False, "error": str(e)}
        if not dev or not dev.available or "desktop_view" not in dev.tools:
            return {"ok": False, "error": "Zařízení teď není připojené."}
        try:
            view = await registry.call(dev, "desktop_view", {"marks": False}, timeout=45)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        if not view.get("image_webp_base64"):
            return {"ok": False, "error": view.get("error") or "Snímek se nepodařilo pořídit.",
                    "locked": bool(view.get("locked"))}
        return {"ok": True, "image": _thumb(view["image_webp_base64"], 960), "window": view.get("active_window", ""),
                "ts": time.time()}

    # --- WebSockety ------------------------------------------------------------------------------
    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket) -> None:
        if not _request_ok(ws.headers) or (_needs_key(ws.client, "/ws/ui")
                                           and not _key_ok(_presented(ws.headers, ws.cookies))):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            status, minfo = await chat.status(), await models_info()
            await hub.join(ws, lambda: build_snapshot(status, minfo))
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.clients.pop(ws, None)

    @app.websocket("/ws/device")
    async def ws_device(ws: WebSocket) -> None:
        # jen démon z tohoto počítače; vzdálené počítače chodí přes šifrovanou bránu Aiko Link
        if ws.headers.get("origin") is not None or not is_local(ws.client):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close()
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello" or not hello.get("device_id") \
                or str(hello["device_id"]).startswith("link-"):
            await ws.close()
            return
        if cfg.token and not await _device_auth(ws, hello):
            await ws.close()
            return
        hello["name"] = "Tento počítač"
        lock = asyncio.Lock()

        async def send(msg: dict[str, Any]) -> None:
            async with lock:
                await ws.send_text(dumps(msg))

        dev = await registry.attach(hello, send)
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") == "result":
                    registry.on_result(msg)
                elif msg.get("type") == "status":
                    await registry.update_status(dev.device_id, msg.get("status") or {})
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            await registry.detach(dev.device_id, send)

    async def _device_auth(ws: WebSocket, hello: dict[str, Any]) -> bool:
        nd = hello.get("nonce")
        if hello.get("auth") != AUTH_SCHEME or not isinstance(nd, str) or not 8 <= len(nd) <= 128:
            return False
        ns = new_nonce()
        await ws.send_text(dumps({"type": "challenge", "nonce": ns, "proof": auth_proof(cfg.token, "server", nd, ns)}))
        try:
            msg = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            return False
        return isinstance(msg, dict) and msg.get("type") == "auth" and proof_ok(
            cfg.token, "device", ns, nd, msg.get("proof"))

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Chyba při %s", request.url.path)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return app
