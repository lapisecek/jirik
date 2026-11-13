"""Spuštění: python -m aiko.server [--config aiko.toml]"""
from __future__ import annotations

import argparse

import uvicorn

from ..common.agent_base import setup_logging
from .app import create_app
from .config import load_config


def main() -> None:
    p = argparse.ArgumentParser(description="Aiko – místní chat, který ovládá počítač")
    p.add_argument("--config", help="cesta k aiko.toml")
    p.add_argument("--port", type=int)
    p.add_argument("--no-mdns", action="store_true", help="(zachováno kvůli kompatibilitě)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    setup_logging(a.verbose)
    cfg = load_config(a.config)
    if a.port:
        cfg.port = a.port
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", ws_max_size=16 * 1024 * 1024)


if __name__ == "__main__":
    main()
