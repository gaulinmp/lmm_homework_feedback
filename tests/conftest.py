"""Test-suite-wide fixtures.

Most tests don't care where the LLM sidecar log goes — but if the real
``settings.LLM_LOG_PATH`` were used, every router-exercising test would
pollute ``data/logs/llm_messages.jsonl`` in the repo. Redirect it to a
per-session tmp file by default.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _redirect_llm_sidecar(tmp_path_factory, monkeypatch):
    """Send sidecar writes to a per-test tmp file, never the repo data dir."""
    from app import config as config_module

    tmp_log = tmp_path_factory.mktemp("llm_log") / "llm_messages.jsonl"
    monkeypatch.setattr(
        config_module.settings, "LLM_LOG_PATH", tmp_log, raising=False
    )
    yield
