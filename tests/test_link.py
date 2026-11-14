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

from aiko.link.crypto import pair_key, sas, sign, verify
from aiko.server.config import Config
from aiko.server.link import Link, Pairing, magic_packet, open_firewall, package_zip
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
    assert "JirikLink/aiko/link/node.py" in names
    assert "JirikLink/aiko/agents/pc.py" in names
    assert not any(n.endswith(("nodes.json", "link.json", ".toml")) for n in names)


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
