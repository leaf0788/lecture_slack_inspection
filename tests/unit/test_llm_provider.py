"""LLM_PROVIDER で判定の呼び出し先（OpenAI／Bedrock）が切り替わること"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app_inspect import handler as inspect_handler
from app_inspect.services.models import ModerationResult
from common import bedrock_client


def _cfg(provider):
    return SimpleNamespace(use_mock_openai=False, llm_provider=provider, bedrock_model_id="apac.amazon.nova-lite-v1:0",
                           openai_api_key="sk-test", openai_model="gpt-6-luna")


def _run(monkeypatch, provider):
    seen = {}

    def fake_run_moderation(client, model, text, extra_articles=None):
        seen["client"], seen["model"] = client, model
        return ModerationResult(is_violation=False, severity="low", categories=[], rationale="", recommended_reply="")

    monkeypatch.setattr(inspect_handler, "run_moderation", fake_run_moderation)
    monkeypatch.setattr(inspect_handler, "emit_metric", lambda *a, **k: None)
    monkeypatch.setattr(inspect_handler, "log_info", lambda *a, **k: None)
    sentinel = MagicMock(name="bedrock")
    monkeypatch.setattr(inspect_handler, "get_shared_client", lambda: sentinel)
    monkeypatch.setattr(inspect_handler, "OpenAI", lambda api_key: MagicMock(name="openai"))
    notion = MagicMock()
    notion.query_workspace_page_id.return_value = None
    inspect_handler._build_moderation_executor({}, _cfg(provider), notion, "T1")("hello")
    return seen, sentinel


def test_bedrock_provider_uses_bedrock_client_and_model(monkeypatch):
    seen, sentinel = _run(monkeypatch, "bedrock")
    assert seen["client"] is sentinel
    assert seen["model"] == "apac.amazon.nova-lite-v1:0"


def test_openai_provider_is_still_the_default(monkeypatch):
    seen, sentinel = _run(monkeypatch, "openai")
    assert seen["client"] is not sentinel
    assert seen["model"] == "gpt-6-luna"


def test_llm_provider_env(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert bedrock_client.llm_provider() == "openai"
    monkeypatch.setenv("LLM_PROVIDER", "Bedrock")
    assert bedrock_client.llm_provider() == "bedrock"
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    assert bedrock_client.llm_provider() == "openai"
