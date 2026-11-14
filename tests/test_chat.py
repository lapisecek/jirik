"""Testy správy historie, obnovení po chybě/přerušení a vysvětlování chyb v Chat."""
from __future__ import annotations

import httpx
import pytest

from aiko.server.chat import Chat
from aiko.server.config import Config
from aiko.server.hardware import Hardware
from aiko.server.link import Link
from aiko.server.models import Models
from aiko.server.registry import Registry
from aiko.server.settings import SettingsStore
from aiko.server.voice import Speaker, Voice


async def _noop_emit(_msg):
    return None


def _chat(tmp_path) -> Chat:
    cfg = Config(data_dir=tmp_path)
    reg = Registry()
    hw = Hardware(tmp_path)
    link = Link(cfg, reg, _noop_emit)
    settings = SettingsStore(tmp_path)
    voice = Voice(cfg)
    speaker = Speaker(voice, _noop_emit, tmp_path)
    models = Models(cfg, hw, _noop_emit)
    return Chat(cfg, reg, link, speaker, settings, hw, models, _noop_emit, space="chat")


def test_long_tool_turn_history_survives_save_and_reload(tmp_path):
    chat = _chat(tmp_path)
    chat.history.append({"role": "user", "content": "nakresli domek"})
    for i in range(45):
        chat.history.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "computer", "arguments": {"action": "click", "x": i, "y": i}}}],
        })
        chat.history.append({"role": "tool", "tool_name": "computer", "content": f"krok {i}"})
    chat._save()

    reloaded = _chat(tmp_path)
    assert reloaded.history[0]["role"] == "user"
    ctx = reloaded._context()
    assert ctx and ctx[0]["role"] == "user"


def test_incomplete_turn_cleanup_on_cancel_and_exception(tmp_path):
    chat = _chat(tmp_path)
    chat.history = [
        {"role": "user", "content": "otevri"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "open", "arguments": {"target": "a"}}},
                {"function": {"name": "open", "arguments": {"target": "b"}}},
            ],
        },
        {"role": "tool", "tool_name": "open", "content": "ok"},
    ]
    msg = {"text": "částečně", "tools": []}
    chat._close_incomplete_turn(msg, "(stopped by the user)")
    # Doplní jen druhý nedokončený nástroj a uzavře odpovědí asistenta
    assert [h["role"] for h in chat.history] == ["user", "assistant", "tool", "tool", "assistant"]
    assert chat.history[3]["tool_name"] == "open"
    assert chat.history[3]["content"] == "(stopped by the user)"

    # Nezodpovězený prázdný uživatelský tah při chybě se uklidí
    chat2 = _chat(tmp_path)
    chat2.history = [{"role": "user", "content": "ahoj"}]
    chat2._close_incomplete_turn({"text": ""}, "(failed)", drop_empty_user=True)
    assert chat2.history == []


@pytest.mark.asyncio
async def test_send_strips_data_url_image_prefix_and_explain_messages(tmp_path, monkeypatch):
    chat = _chat(tmp_path)

    async def fake_turn(_msg):
        return None

    monkeypatch.setattr(chat, "_turn", fake_turn)
    await chat.send("co je na obrázku?", images=["data:image/jpeg;base64,AAAA"])
    assert chat.history[-1]["images"] == ["AAAA"]
    if chat._task:
        await chat._task

    assert "přerušilo" in chat._explain(httpx.ReadError("boom"))
    assert "včas" in chat._explain(httpx.ReadTimeout("timeout"))
    assert "Ollama neběží" in chat._explain(httpx.ConnectError("conn"))
    req = httpx.Request("POST", "http://localhost:11434/api/chat")
    resp = httpx.Response(404, request=req)
    assert "není stažený" in chat._explain(httpx.HTTPStatusError("404", request=req, response=resp))
