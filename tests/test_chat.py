"""Testy správy historie, obnovení po chybě/přerušení a vysvětlování chyb v Chat."""
from __future__ import annotations

import httpx
import pytest

from aiko.server.chat import Chat, MAX_CONTINUATION_STREAK, _looks_unfinished
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


def test_looks_unfinished_detects_planned_steps_and_ignores_finished_replies():
    assert _looks_unfinished("Teď se podívám na soubory na ploše.")
    assert _looks_unfinished("Otevřu prohlížeč a vyhledám to.")
    assert _looks_unfinished("Pokračuji dalším krokem...")
    assert _looks_unfinished("Další krok:")
    assert _looks_unfinished("Let me check the disk space first.")
    assert not _looks_unfinished("Hotovo, otevřel jsem Kalkulačku.")
    assert not _looks_unfinished("Ahoj! Jak ti můžu pomoct?")
    assert not _looks_unfinished("Našel jsem 3 soubory. Chceš některý smazat?")
    assert not _looks_unfinished("Pokud chceš, můžu to otevřít.")


@pytest.mark.asyncio
async def test_autonomous_continuation_on_premature_stop_and_toggle_off(tmp_path, monkeypatch):
    chat = _chat(tmp_path)
    events = []

    async def capture(ev):
        events.append(ev)

    chat._emit = capture
    monkeypatch.setattr(chat.models, "installed", lambda fresh=False: _async_val({"qwen3.5:4b"}))

    # 1) S zapnutým autonomním režimem model napíše záměr bez nástroje → smyčka ho popostrčí k akci a dokončí
    turns = iter([
        ("Teď se podívám na počasí v Praze.", []),
        ("", [{"function": {"name": "weather", "arguments": {"place": "Praha"}}}]),
        ("V Praze je 12 °C a jasno.", []),
    ])

    async def fake_stream(msg, model, think, messages, tools, speak=True):
        content, calls = next(turns)
        msg["text"] += content
        return content, calls

    async def fake_tool(msg, name, args, note=""):
        return {"temp": 12, "place": args.get("place")}

    async def fake_verify(model, goal, step_log, plan, reply):
        return True, ""

    monkeypatch.setattr(chat, "_stream", fake_stream)
    monkeypatch.setattr(chat, "_run_tool", fake_tool)
    monkeypatch.setattr(chat, "_verify_complete", fake_verify)

    chat.settings.update({"autonomous": True})
    chat.history.append({"role": "user", "content": "Jaké je počasí v Praze?"})
    msg = {"id": "a1", "role": "assistant", "text": "", "thinking": "", "tools": []}
    await chat._rounds(msg)
    assert msg["text"] == "V Praze je 12 °C a jasno."
    assert [h["role"] for h in chat.history] == ["user", "assistant", "tool", "assistant"]

    # 2) S vypnutým autonomním režimem skončí hned po prvním kroku bez nástroje
    chat.history = [{"role": "user", "content": "Jaké je počasí?"}]
    chat.settings.update({"autonomous": False})
    turns2 = iter([
        ("Teď se podívám na počasí.", []),
        ("", [{"function": {"name": "weather", "arguments": {"place": "Praha"}}}]),
    ])
    monkeypatch.setattr(chat, "_stream", lambda m, *_a, **_kw: _async_val(_stream_step(m, next(turns2))))
    msg2 = {"id": "a2", "role": "assistant", "text": "", "thinking": "", "tools": []}
    await chat._rounds(msg2)
    assert msg2["text"] == "Teď se podívám na počasí."
    assert len(chat.history) == 2


@pytest.mark.asyncio
async def test_autonomous_task_plan_guard_verifier_and_streak_cap(tmp_path, monkeypatch):
    chat = _chat(tmp_path)
    monkeypatch.setattr(chat.models, "installed", lambda fresh=False: _async_val({"qwen3.5:4b"}))

    # Plán s 2 kroky: po 1. kroku zkusí předčasně skončit i zavolat task_done → hlídání plánu ho udrží v práci
    script = iter([
        ("", [{"function": {"name": "task_plan", "arguments": {
            "goal": "Počasí a vyhledání",
            "steps": [{"title": "Zjistit počasí", "status": "in_progress"}, {"title": "Vyhledat novinky", "status": "pending"}],
        }}}]),
        ("", [{"function": {"name": "weather", "arguments": {"place": "Brno"}}}]),
        ("Počasí už znám.", []),                                     # ještě otevřený krok 2 → pokračuje
        ("", [{"function": {"name": "task_done", "arguments": {"summary": "Předčasný konec"}}}]),  # zamítnuto
        ("", [
            {"function": {"name": "task_plan", "arguments": {"steps": [
                {"title": "Zjistit počasí", "status": "done"},
                {"title": "Vyhledat novinky", "status": "done"},
            ]}}},
            {"function": {"name": "task_done", "arguments": {"summary": "Vše hotovo v Brně."}}},
        ]),
    ])
    monkeypatch.setattr(chat, "_stream", lambda m, *_a, **_kw: _async_val(_stream_step(m, next(script))))
    monkeypatch.setattr(chat, "_run_tool", lambda m, n, a, note="": _async_val({"ok": True}))

    chat.settings.update({"autonomous": True})
    chat.history.append({"role": "user", "content": "Zjisti počasí v Brně a novinky."})
    msg = {"id": "a3", "role": "assistant", "text": "", "thinking": "", "tools": []}
    await chat._rounds(msg)
    assert msg["text"] == "Vše hotovo v Brně."
    assert msg["plan"]["steps"][1]["status"] == "done"

    # Ověřovač dokončení (_verify_complete) donutí pokračovat, i když neexistuje plán
    chat.history = [{"role": "user", "content": "Najdi soubor a přečti ho"}]
    verify_calls = iter([(False, "Read the found file"), (True, "")])
    script2 = iter([
        ("", [{"function": {"name": "web_search", "arguments": {"query": "test"}}}]),
        ("Našel jsem odkaz.", []),
        ("", [{"function": {"name": "read_page", "arguments": {"url": "https://example.org"}}}]),
        ("Přečetl jsem celý obsah.", []),
    ])
    monkeypatch.setattr(chat, "_stream", lambda m, *_a, **_kw: _async_val(_stream_step(m, next(script2))))
    monkeypatch.setattr(chat, "_verify_complete", lambda *_a: _async_val(next(verify_calls)))
    msg4 = {"id": "a4", "role": "assistant", "text": "", "thinking": "", "tools": []}
    await chat._rounds(msg4)
    assert msg4["text"] == "Přečetl jsem celý obsah."

    # Pojistka proti nekonečné smyčce: po MAX_CONTINUATION_STREAK pokusech bez nástroje skončí bezpečně
    chat.history = [{"role": "user", "content": "Udělej něco"}]
    calls_count = 0

    async def stubborn_stream(m, *_a, **_kw):
        nonlocal calls_count
        calls_count += 1
        m["text"] += "Teď to udělám."
        return "Teď to udělám.", []

    monkeypatch.setattr(chat, "_stream", stubborn_stream)
    msg5 = {"id": "a5", "role": "assistant", "text": "", "thinking": "", "tools": []}
    await chat._rounds(msg5)
    assert calls_count == MAX_CONTINUATION_STREAK + 1
    assert msg5["text"] == "Teď to udělám."


def test_context_compaction_keeps_original_user_goal_on_long_tasks(tmp_path):
    chat = _chat(tmp_path)
    chat.history.append({"role": "user", "content": "Původní dlouhý úkol uživatele"})
    for i in range(25):
        chat.history.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run_command", "arguments": {"command": f"echo {i}"}}}]})
        chat.history.append({"role": "tool", "tool_name": "run_command", "content": f"vystup-{i} " + ("x" * 900)})
    ctx = chat._context()
    assert ctx[0]["role"] == "user"
    assert ctx[0]["content"] == "Původní dlouhý úkol uživatele"
    assert len(ctx) < len(chat.history)


@pytest.mark.asyncio
async def test_old_memories_and_turns_evicted_when_context_full(tmp_path):
    import time

    chat = _chat(tmp_path)
    chat.cfg.num_ctx = 4096

    # Naplníme dlouhodobé paměti i starší běhy agenta
    for i in range(10):
        assert chat.memories.remember(f"Stará paměť číslo {i}: uživatel preferuje detailní poznámku č. {i} " + ("abc " * 12))
    old_t = time.time() - 86400 * 10
    rows = chat.memories._load()
    for idx, r in enumerate(rows):
        r["time"] = old_t + idx
        r["last_used"] = old_t + idx
    chat.memories._save(rows)
    assert chat.memories.count() == 10

    for i in range(12):
        chat.agent_memory.remember(f"Úkol {i}", "pc", "done", "ok", [f"krok {i}"])
    before_agent = chat.agent_memory.count()

    # Naplníme historii několika starými tahy přesahujícími kontext 4096
    for i in range(6):
        chat.history.append({"role": "user", "content": f"Starý dotaz {i}: " + ("text " * 120)})
        chat.history.append({"role": "assistant", "content": f"Stará odpověď {i}: " + ("odpoved " * 120)})
    chat.history.append({"role": "user", "content": "Nejnovější důležitý úkol"})

    _, fit = chat._fit_context(mutate=True)
    assert fit["evicted_turns"] >= 1
    assert fit["evicted_memories"] >= 1
    assert chat.memories.count() < 10
    assert chat.agent_memory.count() < before_agent
    assert any(h.get("content") == "Nejnovější důležitý úkol" for h in chat.history)

    info = chat.context_info()
    assert info["limit"] == 4096
    assert info["used"] <= info["limit"]
    assert 0 <= info["pct"] <= 100
    assert info["evicted_turns"] >= 1
    assert info["evicted_memories"] >= 1

    # Ruční pročištění po zmenšení kontextu také spolehlivě uvolní staré paměti/tahy
    compacted = await chat.compact_context(force=True)
    assert compacted["limit"] == 4096


@pytest.mark.asyncio
async def test_bullet_task_plan_create_plan_remember_and_usage_stream(tmp_path, monkeypatch):
    import json

    chat = _chat(tmp_path)
    events = []

    async def capture(ev):
        events.append(ev)

    chat._emit = capture
    monkeypatch.setattr(chat.models, "installed", lambda fresh=False: _async_val({"qwen3.5:4b"}))

    # Nasimulujeme streamované odpovědi z Ollamy vč. přesného počtu tokenů v `done: true`
    responses = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "create_plan", "arguments": {
                    "title": "Architektura migrace",
                    "summary": "Bezpečná migrace dat a ověření integrity.",
                    "sections": [
                        {"heading": "Fáze 1: Záloha", "items": ["Export dat", "Kontrola hashů"]},
                        {"heading": "Fáze 2: Ověření", "items": ["Spuštění testů"]},
                    ],
                }}},
                {"function": {"name": "task_plan", "arguments": {
                    "goal": "Migrace dat",
                    "steps": [
                        {"title": "Zálohovat data", "status": "done", "section": "Příprava", "items": ["Export JSON"]},
                        {"title": "Ověřit integritu", "status": "in_progress", "items": ["Kontrola hashů", "Testy"]},
                    ],
                }}},
                {"function": {"name": "remember", "arguments": {"fact": "Záloha patří do D:\\Backups"}}},
            ]}},
            {"done": True, "prompt_eval_count": 1420, "eval_count": 80},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "task_plan", "arguments": {"steps": [
                    {"title": "Zálohovat data", "status": "done", "items": ["Export JSON"]},
                    {"title": "Ověřit integritu", "status": "done", "items": ["Kontrola hashů", "Testy"]},
                ]}}},
                {"function": {"name": "task_done", "arguments": {"summary": "Plán vytvořen, body splněny."}}},
            ]}},
            {"done": True, "prompt_eval_count": 1600, "eval_count": 60},
        ],
    ]
    resp_iter = iter(responses)

    class _FakeResp:
        status_code = 200

        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def aiter_lines(self):
            for ln in self._lines:
                yield json.dumps(ln)

    class _FakeClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, *_a, **_kw):
            return _FakeResp(next(resp_iter))

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    chat.settings.update({"autonomous": True})
    chat.history.append({"role": "user", "content": "Navrhni složitou migraci a splň kroky."})
    msg = {"id": "ap1", "role": "assistant", "text": "", "thinking": "", "tools": []}
    await chat._rounds(msg)

    assert msg["doc_plan"]["title"] == "Architektura migrace"
    assert "## Fáze 1: Záloha" in msg["doc_plan"]["markdown"]
    assert "- Export dat" in msg["doc_plan"]["markdown"]
    assert msg["plan"]["steps"][1]["items"] == ["Kontrola hashů", "Testy"]
    assert msg["text"] == "Plán vytvořen, body splněny."
    assert "Záloha patří do D:\\Backups" in chat.memories.texts()
    ctx_events = [e["context"] for e in events if e.get("type") == "context"]
    assert any(c["used"] == 1660 for c in ctx_events)



async def _async_val(val):
    return val


def _stream_step(msg, pair):
    content, calls = pair
    msg["text"] += content
    return content, calls
