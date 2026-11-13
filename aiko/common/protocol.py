"""Protokol mezi serverem (mozkem) a démony na zařízeních (svaly).

Tvar je převzatý z MCP (Model Context Protocol): démon vystaví katalog
nástrojů (`tools/list`) a server je volá (`tools/call`). Transport je
WebSocket, který otevírá démon směrem k serveru – projde firewallem PC
a po výpadku sítě se sám obnoví.

Zprávy (JSON):
  démon -> server  {"type":"hello", "device_id", "name", "kind", "platform",
                    "tools":[ToolSpec], "token", "undelivered":[Result]}
  server -> démon  {"type":"call", "id", "tool", "args"}
  démon -> server  {"type":"result", "id", "ok", "data" | "error", "refused"?}
  démon -> server  {"type":"status", "status":{battery, in_call, ...}}
  server -> démon  {"type":"ping"} / démon -> server {"type":"pong"}

Vzájemné ověření (když je nastavený párovací token):
  démon -> server  hello s "auth":"hmac1", "nonce": nd   (token se NEPOSÍLÁ)
  server -> démon  {"type":"challenge", "nonce": ns, "proof": HMAC(token, "server|nd|ns")}
  démon -> server  {"type":"auth", "proof": HMAC(token, "device|ns|nd")}
Démon tak pozná podvržený server ohlášený přes mDNS (nedostane od něj žádný příkaz)
a server pozná cizí zařízení. Token po síti nikdy neprojde.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from .safety import Level, tool_level

PROTOCOL_VERSION = 1
SERVICE_TYPE = "_aiko._tcp.local."

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class ToolSpec:
    name: str
    description: str
    level: Level
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "level": int(self.level),
            "inputSchema": {
                "type": "object",
                "properties": self.params,
                "required": self.required,
            },
        }


def tool(description: str, level: Level | None = None) -> Callable:
    """Označí metodu démona jako MCP nástroj.

    Úroveň se bere z `safety.TOOL_LEVELS`; parametr `level` ji může jen zvýšit.
    """

    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        params: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            ann = p.annotation
            if isinstance(ann, str):
                ann = {"str": str, "int": int, "float": float, "bool": bool}.get(ann.split("|")[0].strip(), str)
            params[pname] = {"type": _PY_TO_JSON.get(ann, "string")}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        fn._aiko_tool = ToolSpec(  # type: ignore[attr-defined]
            name=fn.__name__,
            description=description,
            level=tool_level(fn.__name__, int(level) if level is not None else None),
            params=params,
            required=required,
        )
        return fn

    return deco


AUTH_SCHEME = "hmac1"


def new_nonce() -> str:
    return secrets.token_hex(16)


def auth_proof(token: str, role: str, first: str, second: str) -> str:
    """HMAC-SHA256 důkaz znalosti tokenu. `role` = "server" | "device" – důkazy nejdou zaměnit."""
    msg = f"{role}|{first}|{second}".encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def proof_ok(token: str, role: str, first: str, second: str, proof: Any) -> bool:
    return isinstance(proof, str) and hmac.compare_digest(auth_proof(token, role, first, second), proof)


API_KEY_LABEL = "aiko-api-v1"


def api_key(token: str) -> str:
    """Klíč pro HTTP API a stránky serveru (Authorization: Bearer …, cookie v prohlížeči).

    Je odvozený z párovacího tokenu, ne token sám: HTTP v domácí síti není šifrované, a kdo by klíč
    odposlechl, mohl by nanejvýš psát Aiko věty – vydávat se za server a ovládat zařízení chce token."""
    return hmac.new(token.encode(), API_KEY_LABEL.encode(), hashlib.sha256).hexdigest()


class ToolRefused(Exception):
    """Démon odmítl akci (blocklist, probíhající hovor...)."""
