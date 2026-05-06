from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """Fresh tmp DB + a writable LLM config; reload the relevant modules."""
    db_file = tmp_path / "tutor.db"
    llm_cfg = tmp_path / "llm.toml"
    llm_cfg.write_text(
        '[roles.grader]\n'
        'provider = "openai_compat"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'model = "qwen3"\n'
        '\n'
        '[roles.tutor]\n'
        'provider = "anthropic"\n'
        'model = "claude-haiku-4-5-20251001"\n'
        'api_key_env = "ANTHROPIC_API_KEY"\n'
        '\n'
        '[roles.gpt]\n'
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
        '\n'
        '[roles.gem]\n'
        'provider = "gemini"\n'
        'model = "gemini-1.5-flash"\n'
        'api_key_env = "GOOGLE_API_KEY"\n'
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_CONFIG_PATH", str(llm_cfg))

    from app import config as cm
    importlib.reload(cm)
    from app import db as dm
    importlib.reload(dm)
    dm.init_db()

    from app.llm import router as rm
    importlib.reload(rm)
    return rm, dm


# ---------------------------------------------------------------------------
# Router behaviour
# ---------------------------------------------------------------------------

def test_router_loads_role_map(fresh_env):
    rm, _ = fresh_env
    r = rm.LLMRouter()
    assert set(r.roles()) == {"grader", "tutor", "gpt", "gem"}
    assert r._roles["grader"]["provider"] == "openai_compat"
    assert r._roles["tutor"]["provider"] == "anthropic"


def test_invoke_writes_audit_row(fresh_env):
    rm, dm = fresh_env

    class FakeProvider:
        name = "openai_compat"
        model = "qwen3"

        def invoke(self, messages, *, response_schema=None, files=None):
            return AIMessage(
                content="four",
                usage_metadata={
                    "input_tokens": 7,
                    "output_tokens": 1,
                    "total_tokens": 8,
                },
            )

    r = rm.LLMRouter()
    r._providers["grader"] = FakeProvider()
    out = r.invoke("grader", [{"role": "user", "content": "2+2="}])
    assert out.content == "four"

    with dm.get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT role_bucket, provider, model, role, tokens_in, tokens_out, "
                "       latency_ms, content "
                "FROM llm_messages"
            )
        ).fetchall()
    assert len(rows) == 1
    role_bucket, provider, model, role, tin, tout, lat, content = rows[0]
    assert (role_bucket, provider, model, role) == ("grader", "openai_compat", "qwen3", "assistant")
    assert tin == 7 and tout == 1
    assert lat is not None and lat >= 0
    assert "2+2=" in content
    assert "four" in content


def test_invoke_writes_row_on_error(fresh_env):
    rm, dm = fresh_env

    class BoomProvider:
        name = "anthropic"
        model = "claude-haiku-4-5-20251001"

        def invoke(self, messages, *, response_schema=None, files=None):
            raise RuntimeError("provider blew up")

    r = rm.LLMRouter()
    r._providers["tutor"] = BoomProvider()
    with pytest.raises(RuntimeError):
        r.invoke("tutor", [{"role": "user", "content": "hi"}])

    with dm.get_engine().connect() as conn:
        rows = conn.execute(text("SELECT provider, content FROM llm_messages")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "anthropic"
    assert "provider blew up" in rows[0][1]


def test_invoke_unknown_role_raises(fresh_env):
    rm, _ = fresh_env
    r = rm.LLMRouter()
    with pytest.raises(KeyError):
        r.invoke("nope", [{"role": "user", "content": "x"}])


def test_astream_concatenates_and_audits(fresh_env):
    rm, dm = fresh_env

    class StreamProvider:
        name = "openai_compat"
        model = "qwen3"

        async def astream(self, messages, *, response_schema=None, files=None):
            for piece in ["he", "llo", "!"]:
                yield piece

    r = rm.LLMRouter()
    r._providers["grader"] = StreamProvider()

    async def drain():
        out = []
        async for chunk in r.astream("grader", [{"role": "user", "content": "hi"}]):
            out.append(chunk)
        return out

    pieces = asyncio.run(drain())
    assert "".join(pieces) == "hello!"

    with dm.get_engine().connect() as conn:
        row = conn.execute(text("SELECT content FROM llm_messages")).fetchone()
    assert "hello!" in row[0]


# ---------------------------------------------------------------------------
# Provider classes — mocked
# ---------------------------------------------------------------------------

def test_anthropic_provider_invokes_sdk_with_system_split():
    from app.llm.providers import anthropic as ap

    p = ap.AnthropicProvider(model="claude-haiku-4-5-20251001", api_key="test-key")

    fake_resp = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    fake_resp.content = [text_block]
    fake_resp.usage.input_tokens = 11
    fake_resp.usage.output_tokens = 3
    fake_resp.model = "claude-haiku-4-5-20251001"
    fake_resp.stop_reason = "end_turn"

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp
    p._client = fake_client

    out = p.invoke(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
    )

    assert isinstance(out, AIMessage)
    assert out.content == "ok"
    assert out.usage_metadata == {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}

    call = fake_client.messages.create.call_args
    assert call.kwargs["system"] == "be terse"
    assert call.kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert call.kwargs["model"] == "claude-haiku-4-5-20251001"


def test_anthropic_provider_response_schema_uses_tool():
    from app.llm.providers import anthropic as ap
    from app.llm.verdicts import GradeVerdict

    p = ap.AnthropicProvider(model="claude-haiku-4-5-20251001", api_key="k")

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {
        "verdict": "correct",
        "score": 1.0,
        "rationale": "matches rubric",
        "weakest_concept": None,
    }
    fake_resp = MagicMock()
    fake_resp.content = [tool_block]
    fake_resp.usage.input_tokens = 5
    fake_resp.usage.output_tokens = 2
    fake_resp.model = "claude-haiku-4-5-20251001"
    fake_resp.stop_reason = "tool_use"

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp
    p._client = fake_client

    out = p.invoke(
        [{"role": "user", "content": "grade this"}],
        response_schema=GradeVerdict,
    )

    parsed = out.additional_kwargs["parsed"]
    assert isinstance(parsed, GradeVerdict)
    assert parsed.verdict == "correct"

    call = fake_client.messages.create.call_args
    assert call.kwargs["tools"][0]["name"] == "respond"
    assert call.kwargs["tool_choice"] == {"type": "tool", "name": "respond"}


def test_openai_provider_defaults_to_openai_endpoint():
    from app.llm.providers import openai as om
    p = om.OpenAIProvider(model="gpt-4o-mini", api_key="sk-test")
    assert p.name == "openai"
    assert p.base_url is None
    assert p.model == "gpt-4o-mini"
    assert p.api_key == "sk-test"


def test_openai_compat_falls_back_to_dummy_key():
    from app.llm.providers import openai_compat as oc
    p = oc.OpenAICompatProvider(model="qwen3", base_url="http://127.0.0.1:8080/v1")
    assert p.api_key == "not-needed"
    assert p.base_url == "http://127.0.0.1:8080/v1"


def test_gemini_provider_mocked(monkeypatch):
    fake_mod = types.ModuleType("google.generativeai")
    fake_mod.configure = lambda **kw: None

    class FakeModel:
        def __init__(self, model):
            self.model = model

        def generate_content(self, prompt, generation_config=None):
            r = MagicMock()
            r.text = "gemini-says-hi"
            r.usage_metadata = MagicMock()
            r.usage_metadata.prompt_token_count = 4
            r.usage_metadata.candidates_token_count = 2
            r.usage_metadata.total_token_count = 6
            return r

    fake_mod.GenerativeModel = FakeModel
    google_pkg = types.ModuleType("google")
    google_pkg.generativeai = fake_mod
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_mod)

    from app.llm.providers import gemini as gm
    importlib.reload(gm)
    p = gm.GeminiProvider(model="gemini-1.5-flash", api_key="test")
    out = p.invoke([{"role": "user", "content": "hi"}])
    assert out.content == "gemini-says-hi"
    assert out.usage_metadata["input_tokens"] == 4
    assert out.usage_metadata["output_tokens"] == 2


# ---------------------------------------------------------------------------
# Verdict schema
# ---------------------------------------------------------------------------

def test_grade_verdict_validates():
    from app.llm.verdicts import GradeVerdict

    v = GradeVerdict(verdict="partial", score=0.5, rationale="halfway", weakest_concept="bins")
    assert v.weakest_concept == "bins"
    with pytest.raises(Exception):
        GradeVerdict(verdict="bogus", score=1.0, rationale="x")
    with pytest.raises(Exception):
        GradeVerdict(verdict="correct", score=2.0, rationale="x")


# ---------------------------------------------------------------------------
# Live llama.cpp test — skipped if server isn't reachable
# ---------------------------------------------------------------------------

def _llama_reachable() -> bool:
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8080/v1/models", timeout=1.0)
        return r.status_code < 500
    except Exception:
        return False


@pytest.mark.skipif(not _llama_reachable(), reason="local llama.cpp server not reachable")
def test_openai_compat_against_local_llama(fresh_env):
    rm, dm = fresh_env
    r = rm.LLMRouter()
    out = r.invoke(
        "grader",
        [{"role": "user", "content": "Reply with exactly the single character: 4"}],
    )
    assert isinstance(out, AIMessage)
    assert isinstance(out.content, str) and out.content.strip()

    with dm.get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT role_bucket, provider, model, latency_ms FROM llm_messages"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "grader"
    assert row[1] == "openai_compat"
