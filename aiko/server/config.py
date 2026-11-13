"""Konfigurace serveru (aiko.toml). Vše má rozumné výchozí hodnoty."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    user_name: str = ""                       # jméno uživatele (1. pád)
    greeting: str = ""                        # oslovení v 5. pádě („Adame“); prázdné = jméno
    user_female: bool = False
    home_city: str = ""                       # pro počasí, když město nezazní

    host: str = "127.0.0.1"
    port: int = 8770
    token: str = ""                           # klíč pro přístup k UI z jiných počítačů (prázdný = jen localhost)
    allowed_hosts: list[str] = field(default_factory=list)
    data_dir: Path = Path("data")
    agent_runs_keep_days: int = 14
    mdns: bool = False

    # model – Qwen3.5 4B: text + obrázky + nástroje, celý se vejde do 6 GB grafiky
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen3.5:4b"
    model_uncensored: str = "huihui_ai/qwen3.5-abliterated:4b"
    num_ctx: int = 8192                       # stejný kontext pro chat i agenta – Ollama model nepřenačítá
    keep_alive: str = "30m"

    # GUI agent (klikání po obrazovce) – používá stejný model jako chat
    vlm_provider: str = "auto"
    vlm_local_first: bool = True
    ollama_vlm_model: str = "qwen3.5:4b"
    ollama_vlm_max_pixels: int = 1_000_000
    ollama_vlm_num_ctx: int = 8192
    agent_max_steps: int = 25
    agent_zoom_refine: bool = True
    claude_model: str = "claude-opus-5-5"
    gemini_vlm_model: str = "gemini-3.5-flash"
    providers: list = field(default_factory=list)

    # hlas
    piper_model: str = ""
    speech_rate: float = 1.0

    # propojení s dalšími počítači (Aiko Link)
    link_port: int = 8771                     # TLS brána pro vzdálené počítače
    link_udp_port: int = 8772                 # probouzení vzdálených počítačů
    pair_port: int = 8773                     # párovací stránka (jen během párování, max. 15 min)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    p = Path(path or os.environ.get("AIKO_CONFIG", "aiko.toml"))
    if p.exists():
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        a = raw.get("aiko", {})
        cfg.user_name = str(a.get("user_name", cfg.user_name) or "")
        cfg.greeting = str(a.get("greeting", cfg.greeting) or "")
        cfg.user_female = bool(a.get("user_female", cfg.user_female))
        cfg.home_city = str(a.get("home_city", cfg.home_city) or "")
        cfg.host = a.get("host", cfg.host)
        cfg.port = int(a.get("port", cfg.port))
        cfg.token = a.get("token", cfg.token)
        cfg.allowed_hosts = [str(h) for h in a.get("allowed_hosts", cfg.allowed_hosts)]
        cfg.data_dir = Path(a.get("data_dir", cfg.data_dir))
        cfg.mdns = bool(a.get("mdns", cfg.mdns))
        m = raw.get("model", {})
        cfg.ollama_url = m.get("ollama_url", cfg.ollama_url)
        cfg.model = m.get("standard", cfg.model)
        cfg.model_uncensored = m.get("uncensored", cfg.model_uncensored)
        cfg.num_ctx = int(m.get("num_ctx", cfg.num_ctx))
        cfg.keep_alive = str(m.get("keep_alive", cfg.keep_alive))
        g = raw.get("agent", {})
        cfg.vlm_provider = g.get("provider", cfg.vlm_provider)
        cfg.agent_max_steps = int(g.get("max_steps", cfg.agent_max_steps))
        cfg.ollama_vlm_max_pixels = int(g.get("max_pixels", cfg.ollama_vlm_max_pixels))
        cfg.agent_zoom_refine = bool(g.get("zoom_refine", cfg.agent_zoom_refine))
        v = raw.get("voice", {})
        cfg.piper_model = v.get("piper_model", cfg.piper_model)
        cfg.speech_rate = float(v.get("speech_rate", cfg.speech_rate))
        lk = raw.get("link", {})
        cfg.link_port = int(lk.get("port", cfg.link_port))
        cfg.link_udp_port = int(lk.get("udp_port", cfg.link_udp_port))
        cfg.pair_port = int(lk.get("pair_port", cfg.pair_port))
        if raw.get("providers"):
            from .openai_compat import from_config

            cfg.providers = from_config(raw["providers"])
        for key, env in (("gemini_api_key", "GEMINI_API_KEY"), ("anthropic_api_key", "ANTHROPIC_API_KEY")):
            value = str(raw.get("keys", {}).get(key, "")).strip()
            if value and not os.environ.get(env):
                os.environ[env] = value
    # osobní nastavení (jméno, oslovení…) mimo git: aiko.user.toml vedle aiko.toml
    user = p.with_name("aiko.user.toml")
    if user.exists():
        a = tomllib.loads(user.read_text(encoding="utf-8")).get("aiko", {})
        cfg.user_name = str(a.get("user_name", cfg.user_name) or "")
        cfg.greeting = str(a.get("greeting", cfg.greeting) or "")
        cfg.user_female = bool(a.get("user_female", cfg.user_female))
        cfg.home_city = str(a.get("home_city", cfg.home_city) or "")
    cfg.ollama_vlm_model = cfg.model
    cfg.ollama_vlm_num_ctx = cfg.num_ctx
    cfg.token = os.environ.get("AIKO_TOKEN", cfg.token)
    return cfg
