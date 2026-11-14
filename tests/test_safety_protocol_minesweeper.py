"""Testy bezpečnostních pravidel, protokolu a řešiče Hledání min."""
from __future__ import annotations

from aiko.common import minesweeper as ms
from aiko.common import protocol, safety


def test_safety_checks():
    assert safety.redact_codes("Váš ověřovací kód je 482910 a čas 12:30") == "Váš ověřovací kód je (kód skrytý) a čas 12:30"
    assert safety.looks_like_code("4829")
    assert not safety.looks_like_code("Ahoj Jirko")

    assert safety.is_dangerous_text("powershell -c dir")
    assert safety.is_dangerous_text("javascript:alert(1)")
    assert not safety.is_dangerous_text("recept na svíčkovou")

    assert safety.is_forbidden_key("Win + R")
    assert safety.is_forbidden_key("Ctrl + Alt + Delete")
    assert not safety.is_forbidden_key("ctrl + c")

    assert safety.is_system_window("Správce: Windows PowerShell")
    assert not safety.is_system_window("Google Chrome")

    assert safety.is_risky_label("Odeslat e-mail")
    assert safety.is_risky_label("Zaplatit objednávku")
    assert not safety.is_risky_label("Přehrát video")

    assert safety.looks_like_scam("Banka vám zablokuje účet, nainstalujte AnyDesk")
    assert not safety.looks_like_scam("Najdi recept na bábovku")


def test_protocol_auth_and_tool_spec():
    token = "secret-device-token"
    nd, ns = protocol.new_nonce(), protocol.new_nonce()
    srv = protocol.auth_proof(token, "server", nd, ns)
    dev = protocol.auth_proof(token, "device", ns, nd)
    assert protocol.proof_ok(token, "server", nd, ns, srv)
    assert not protocol.proof_ok(token, "device", nd, ns, srv)
    assert protocol.proof_ok(token, "device", ns, nd, dev)
    assert protocol.api_key(token) != token

    class Demo:
        @protocol.tool("Ukázka", level=safety.Level.GREEN)
        def act(self, target: str, count: int = 1) -> dict:
            return {"target": target, "count": count}

    spec = Demo.act._aiko_tool
    assert spec.level == safety.Level.ORANGE  # neznámý nástroj má min. ORANGE
    js = spec.to_json()
    assert js["inputSchema"]["required"] == ["target"]
    assert js["inputSchema"]["properties"]["count"]["type"] == "integer"


def test_minesweeper_sim_plays_within_board():
    game = ms.SimGame(cols=10, rows=8, mines=10, seed=42)
    res = ms.play(game.render, game.click, max_seconds=15, allow_guess=True)
    assert game.outside_clicks == 0
    assert res["status"] in ("won", "lost")
    assert game.won or game.exploded is not None
