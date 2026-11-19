"""Testy Jiřík Link: kryptografie, párovací životní cyklus, balíček a firewall příkaz."""
from __future__ import annotations

import base64
import io
import json
import subprocess
import time
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aiko.common import dpapi
from aiko.common.agent_base import DeviceAgent
from aiko.link.crypto import pair_key, sas, sign, verify
from aiko.link.node import Node
from aiko.server.config import Config
from aiko.server.link import Link, Pairing, _ip_rank, magic_packet, open_firewall, package_zip
from aiko.server.registry import Registry


async def _noop_emit(_msg):
    return None


def test_crypto_sign_sas_and_magic_packet():
    token = "ab" * 32
    msg = {"v": 1, "id": "link-1", "t": "ping", "n": "nonce1", "ts": 123456}
    msg["mac"] = sign(token, msg)
    assert verify(token, msg)
    tampered = {**msg, "t": "wake"}
    assert not verify(token, tampered)

    k1 = X25519PrivateKey.generate()
    k2 = X25519PrivateKey.generate()
    p1 = k1.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    p2 = k2.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    s1 = k1.exchange(X25519PublicKey.from_public_bytes(p2))
    s2 = k2.exchange(X25519PublicKey.from_public_bytes(p1))
    assert sas(s1, p1, p2) == sas(s2, p1, p2)
    assert pair_key(s1, "sid-1") == pair_key(s2, "sid-1")

    pkt = magic_packet("aa:bb:cc:dd:ee:ff")
    assert pkt == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16


def test_package_zip_has_no_secrets_and_contains_installer():
    raw = package_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = set(z.namelist())
    assert "JirikLink/install.ps1" in names
    assert "JirikLink/requirements.txt" in names
    assert "JirikLink/requirements-ocr.txt" in names
    assert "JirikLink/uninstall.ps1" in names
    assert "JirikLink/aiko/link/node.py" in names
    assert "JirikLink/aiko/agents/pc.py" in names
    assert not any(n.endswith(("nodes.json", "link.json", ".toml")) for n in names)


def test_ip_ranking_prefers_real_lan_over_virtual_adapters():
    ips = ["192.168.56.1", "100.100.1.2", "172.24.16.1", "192.168.1.50", "10.0.0.22"]
    ranked = sorted(ips, key=lambda ip: _ip_rank(ip, "10.0.0.22"))
    assert ranked[0] == "10.0.0.22"
    assert ranked[1] == "192.168.1.50"
    assert ranked.index("192.168.1.50") < ranked.index("172.24.16.1")
    assert ranked.index("172.24.16.1") < ranked.index("100.100.1.2")
    assert ranked.index("100.100.1.2") < ranked.index("192.168.56.1")


@pytest.mark.asyncio
async def test_pairing_lifecycle_done_and_cleanup(tmp_path):
    cfg = Config(data_dir=tmp_path, link_port=0, pair_port=0)
    link = Link(cfg, Registry(), _noop_emit)
    try:
        info = await link.open_pairing()
        assert info["state"] == "waiting"
        p = link.pairing
        assert isinstance(p, Pairing)

        peer_priv = X25519PrivateKey.generate()
        peer_pub = peer_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        st, _, body, _ = await link._pair_route("POST", f"/j/{p.sid}/hello", json.dumps({
            "pub": base64.b64encode(peer_pub).decode(),
            "host": "TEST-PC",
            "mac": "aa:bb:cc:dd:ee:ff",
        }).encode(), ("127.0.0.1", 50000), "127.0.0.1:8773")
        assert st == 200
        main_pub = base64.b64decode(json.loads(body)["pub"])
        shared = peer_priv.exchange(X25519PublicKey.from_public_bytes(main_pub))
        assert p.state == "code"
        assert p.code == sas(shared, main_pub, peer_pub)

        conf = await link.confirm_pairing(True, "Pracovní PC")
        assert conf["ok"] is True
        assert link.nodes[conf["id"]]["ip"] == "127.0.0.1"

        st_bat, _, bat_bytes, _ = await link._pair_route(
            "GET", f"/j/{p.sid}/Jirik-Link-Setup.bat", b"", ("127.0.0.1", 50000), "127.0.0.1:8773"
        )
        assert st_bat == 200
        assert b"Unblock-File" in bat_bytes

        st2, _, res_body, _ = await link._pair_route(
            "GET", f"/j/{p.sid}/result", b"", ("127.0.0.1", 50000), "127.0.0.1:8773"
        )
        assert st2 == 200
        res = json.loads(res_body)
        dec = json.loads(AESGCM(pair_key(shared, p.sid)).decrypt(
            base64.b64decode(res["nonce"]), base64.b64decode(res["data"]), p.sid.encode()))
        assert dec["name"] == "Pracovní PC"
        assert p.state == "done" and p.done_at > 0

        # Po vypršení krátkého okna od dokončení se staré "done" samo uklidí
        p.done_at = time.time() - 65
        assert link.pair_public() is None
        assert link.pairing is None
    finally:
        await link.stop()


def test_open_firewall_uses_encoded_command(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    open_firewall(Config(link_port=8771, pair_port=8773))
    if captured:  # na Windows zachytí volání PowerShellu
        joined = " ".join(captured["cmd"])
        assert "-EncodedCommand" in joined
        assert "Jirik Link" not in joined  # příkaz je čistě zakódovaný v base64 UTF-16LE


def test_node_udp_ping_and_outbound_tls_fallback(tmp_path, monkeypatch):
    import aiko.link.node as node_mod

    monkeypatch.setattr(node_mod, "LOG", tmp_path / "logs")

    token = "cd" * 32
    node = Node({
        "node_id": "link-test1",
        "name": "Notebook",
        "token": dpapi.protect(token),
        "pin": "00" * 32,
        "port": 8771,
        "udp_port": 8772,
        "hosts": ["192.168.1.10"],
    })
    sent = []

    class DummySock:
        def sendto(self, data, addr):
            sent.append((json.loads(data), addr))

    ping = {"v": 1, "t": "ping", "id": "link-test1", "ts": int(time.time()), "n": "n1"}
    ping["mac"] = sign(token, ping)
    node.handle(json.dumps(ping).encode(), ("192.168.1.10", 45000), DummySock())
    assert len(sent) == 1
    assert sent[0][0]["t"] == "pong" and verify(token, sent[0][0])

    # Zfalšovaný nebo opakovaný paket se ignoruje
    node.handle(json.dumps(ping).encode(), ("192.168.1.10", 45000), DummySock())
    assert len(sent) == 1

    # Když UDP od startu mlčí a TLS brána je dostupná, přejde na záložní odchozí spojení
    spawned = []
    node.last_udp = 0.0
    node.start_ts = time.monotonic() - 60.0
    monkeypatch.setattr(node_mod, "_tcp_reachable", lambda h, p: h == "192.168.1.10" and p == 8771)
    monkeypatch.setattr(node, "spawn", lambda host, idle_exit=600, fallback=False: spawned.append((host, idle_exit, fallback)))
    node._maybe_fallback()
    assert spawned == [("192.168.1.10", 0, True)]


@pytest.mark.asyncio
async def test_live_pair_module_and_tls_gateway_handshake(tmp_path, monkeypatch):
    import asyncio
    import sys

    import aiko.link.pair as pair_mod

    cfg = Config(data_dir=tmp_path / "srv", link_port=0, pair_port=0)
    reg = Registry()
    link = Link(cfg, reg, _noop_emit)
    await link.open_pairing()
    pair_port = link._pair_server.sockets[0].getsockname()[1]
    assert link._gateway is not None
    gw_port = link._gateway.sockets[0].getsockname()[1]

    fake_home = tmp_path / "client"
    fake_home.mkdir()
    monkeypatch.setattr(pair_mod, "HOME", fake_home)
    monkeypatch.setattr(pair_mod, "_mac", lambda *_a: "aa:bb:cc:dd:ee:ff")
    monkeypatch.setattr(sys, "argv", ["pair.py", "--url", f"http://127.0.0.1:{pair_port}/j/{link.pairing.sid}"])

    async def auto_confirm():
        for _ in range(40):
            if link.pairing and link.pairing.state == "code":
                await link.confirm_pairing(True, "Druhý PC")
                return
            await asyncio.sleep(0.05)

    confirm_task = asyncio.create_task(auto_confirm())
    rc = await asyncio.to_thread(pair_mod.main)
    await confirm_task
    assert rc == 0

    saved = json.loads((fake_home / "link.json").read_text(encoding="utf-8"))
    assert saved["name"] == "Druhý PC"
    assert saved["hosts"] == ["127.0.0.1"]
    plain_token = dpapi.unprotect(saved["token"])

    # Ověření živého TLS WebSocket spojení s otiskem certifikátu a HMAC ověřením
    agent = DeviceAgent(
        name=saved["name"],
        device_id=saved["node_id"],
        server_url=f"wss://127.0.0.1:{gw_port}/ws/device",
        token=plain_token,
        pin=saved["pin"],
    )
    agent_task = asyncio.create_task(agent.run())
    try:
        for _ in range(50):
            dev = reg.devices.get(saved["node_id"])
            if dev and dev.available:
                break
            await asyncio.sleep(0.05)
        dev = reg.devices.get(saved["node_id"])
        assert dev is not None and dev.available
    finally:
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass
        await link.stop()


def test_powershell_installers_parse_cleanly():
    import re
    import sys

    from aiko.server.link import SETUP_BAT

    if sys.platform != "win32":
        pytest.skip("PowerShell AST check runs on Windows")
    bat = SETUP_BAT.replace("{BASE}", "http://127.0.0.1:8773/j/abc").replace("{HASH}", "0" * 64)
    ps_inline = re.search(r'-Command "(.*)"', bat).group(1)
    ps_b64 = base64.b64encode(ps_inline.encode("utf-8")).decode("ascii")
    ps_check = (
        "$errs = $null; "
        "foreach ($f in @('aiko/link/install/install.ps1', 'aiko/link/install/uninstall.ps1')) { "
        "  [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f).Path, [ref]$null, [ref]$errs); "
        "  if ($errs) { throw ($errs | Out-String) } "
        "}; "
        f"$inline = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{ps_b64}')); "
        "[void][System.Management.Automation.Language.Parser]::ParseInput($inline, [ref]$null, [ref]$errs); "
        "if ($errs) { throw ($errs | Out-String) }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_check], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr + r.stdout
