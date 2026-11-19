"""Testy webtools SSRF/parserů, config rozlišení cest a FastAPI HTTP API."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from aiko.server import webtools
from aiko.server.app import Lockout, create_app, host_allowed, origin_allowed
from aiko.server.config import Config, load_config


def test_config_resolves_relative_paths_against_toml_dir(tmp_path, monkeypatch):
    toml_file = tmp_path / "sub" / "aiko.toml"
    toml_file.parent.mkdir()
    toml_file.write_text('[aiko]\ndata_dir = "mydata"\n[voice]\npiper_model = "voices/jirka.onnx"\n',
                         encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(toml_file)
    assert cfg.data_dir == toml_file.parent.resolve() / "mydata"
    assert cfg.piper_model == str(toml_file.parent.resolve() / "voices/jirka.onnx")


def test_ssrf_guards_reject_local_and_private_targets(monkeypatch):
    with pytest.raises(webtools.WebToolError):
        webtools._check_public_url("http://127.0.0.1:8770/api/state")
    with pytest.raises(webtools.WebToolError):
        webtools._check_public_url("http://localhost:8770/")
    with pytest.raises(webtools.WebToolError):
        webtools._check_public_url("http://192.168.1.1/")
    with pytest.raises(webtools.WebToolError):
        webtools._check_public_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(webtools.WebToolError):
        webtools._check_public_url("http://[::1]:8770/")
    with pytest.raises(webtools.WebToolError):
        webtools._check_public_url("https://anydesk.com/en")

    import socket
    orig_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port):
        if host in ("example.org", "public.example"):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return orig_getaddrinfo(host, port)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert webtools._check_public_url("https://example.org/test") == "example.org"


@pytest.mark.asyncio
async def test_fetch_page_blocks_redirect_to_loopback(monkeypatch):
    async def fake_get(self, url, **kwargs):
        req = httpx.Request("GET", str(url))
        if "public.example" in str(url):
            return httpx.Response(302, request=req, headers={"Location": "http://127.0.0.1:8770/api/state"})
        return httpx.Response(200, request=req, text="secret")

    import socket
    orig_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port):
        if host == "public.example":
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return orig_getaddrinfo(host, port)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(webtools.WebToolError, match="místní adresy"):
        await webtools.fetch_page("https://public.example/start")


def test_parse_youtube_and_views():
    html = (
        '<script>var ytInitialData = {"contents":{"videoRenderer":'
        '{"videoId":"dQw4w9WgXcQ","title":{"runs":[{"text":"Píseň"}]},'
        '"ownerText":{"runs":[{"text":"Karel"}]},"viewCountText":{"simpleText":"12 345 zhlédnutí"},'
        '"lengthText":{"simpleText":"3:32"}}}};</script>'
    )
    vids = webtools.parse_youtube(html)
    assert len(vids) == 1
    assert vids[0]["id"] == "dQw4w9WgXcQ"
    assert webtools.views_number(vids[0]["views"]) == 12345


def test_fastapi_endpoints_and_guards(tmp_path, monkeypatch):
    import aiko.server.app as app_mod

    monkeypatch.setattr(app_mod, "firewall_rule_present", lambda cfg: True)
    cfg = Config(data_dir=tmp_path, ollama_url="http://127.0.0.1:1", link_port=0, pair_port=0)
    app = create_app(cfg)
    with TestClient(app, base_url="http://127.0.0.1:8770", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/hello").json() == {"aiko": True}
        st = client.get("/api/state").json()
        assert st["type"] == "snapshot"
        assert "chat" in st["spaces"]

        r = client.post("/api/settings", json={"voice": False, "level": "medium", "autonomous": False})
        assert r.status_code == 200
        assert r.json()["voice"] is False
        assert r.json()["level"] == "medium"
        assert r.json()["autonomous"] is False
        assert client.post("/api/settings", json={"autonomous": True}).json()["autonomous"] is True

        assert client.post("/api/settings", json={"level": "invalid"}).status_code == 400

        opened = client.post("/api/link/pair/open").json()
        assert opened["ok"] is True
        assert opened["pairing"]["state"] == "waiting"
        assert client.post("/api/link/pair/close").json() == {"ok": True}
        assert app.state.link.pairing is None

    assert host_allowed("127.0.0.1:8770")
    assert host_allowed("jirik.local:8770")
    assert not host_allowed("evil.com")
    assert origin_allowed("http://127.0.0.1:8770", "127.0.0.1:8770")
    assert not origin_allowed("https://evil.com", "127.0.0.1:8770")
    assert not origin_allowed("null", "127.0.0.1:8770")

    lo = Lockout(limit=2, window_s=60)
    lo.fail("1.2.3.4")
    assert not lo.blocked("1.2.3.4")
    lo.fail("1.2.3.4")
    assert lo.blocked("1.2.3.4")
