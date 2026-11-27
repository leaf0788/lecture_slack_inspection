import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from common.bedrock_client import BedrockChatClient, BedrockInvocationError
from app_inspect.services.violation_detector import ViolationDetector


def _tool_response(payload: dict, in_tok=100, out_tok=20, cache_read=0):
    return {
        "output": {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": payload}}]}},
        "usage": {"inputTokens": in_tok, "outputTokens": out_tok, "cacheReadInputTokens": cache_read},
        "stopReason": "tool_use",
        "metrics": {"latencyMs": 123},
    }


def _client_error(code: str):
    return ClientError({"Error": {"Code": code, "Message": code}}, "Converse")


RF = {"type": "json_schema", "json_schema": {"name": "warn_decision", "strict": True,
                                             "schema": {"type": "object", "properties": {"decision": {"type": "string"}}}}}


def test_structured_output_is_forced_through_a_single_tool():
    rt = MagicMock()
    rt.converse.return_value = _tool_response({"decision": "warn"})
    c = BedrockChatClient(runtime=rt)

    resp = c.chat.completions.create(model="amazon.nova-lite-v1:0",
                                     messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                                     response_format=RF)

    req = rt.converse.call_args.kwargs
    assert req["modelId"] == "amazon.nova-lite-v1:0"
    assert req["system"] == [{"text": "S"}]
    assert req["messages"] == [{"role": "user", "content": [{"text": "U"}]}]
    assert req["toolConfig"]["toolChoice"] == {"tool": {"name": "warn_decision"}}
    assert req["inferenceConfig"]["temperature"] == 0.0
    assert json.loads(resp.choices[0].message.content) == {"decision": "warn"}
    assert resp.usage.prompt_tokens == 100 and resp.usage.completion_tokens == 20


def test_cache_point_and_cached_tokens_are_counted_in_prompt_tokens():
    rt = MagicMock()
    rt.converse.return_value = _tool_response({"a": 1}, in_tok=50, cache_read=900)
    c = BedrockChatClient(runtime=rt, cache_system=True)
    resp = c.chat.completions.create(model="m", messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                                     response_format=RF)
    assert rt.converse.call_args.kwargs["system"][-1] == {"cachePoint": {"type": "default"}}
    assert resp.usage.prompt_tokens == 950
    assert resp.usage.prompt_tokens_details.cached_tokens == 900


def test_text_json_is_used_when_model_does_not_call_the_tool():
    rt = MagicMock()
    rt.converse.return_value = {"output": {"message": {"content": [{"text": "```json\n{\"decision\": \"safe\"}\n```"}]}},
                                "usage": {}}
    resp = BedrockChatClient(runtime=rt).chat.completions.create(model="m", messages=[{"role": "user", "content": "U"}],
                                                                 response_format=RF)
    assert json.loads(resp.choices[0].message.content) == {"decision": "safe"}


def test_throttling_is_retried_with_backoff_then_succeeds():
    rt = MagicMock()
    rt.converse.side_effect = [_client_error("ThrottlingException"), _client_error("ServiceUnavailableException"),
                               _tool_response({"ok": True})]
    sleeps = []
    c = BedrockChatClient(runtime=rt, max_retries=3, sleep=sleeps.append)
    resp = c.chat.completions.create(model="m", messages=[{"role": "user", "content": "U"}], response_format=RF)
    assert json.loads(resp.choices[0].message.content) == {"ok": True}
    assert rt.converse.call_count == 3
    assert len(sleeps) == 2 and all(s >= 0 for s in sleeps)


def test_persistent_throttling_switches_to_fallback_model():
    rt = MagicMock()
    rt.converse.side_effect = [_client_error("ThrottlingException")] * 2 + [_tool_response({"ok": 1})]
    c = BedrockChatClient(runtime=rt, max_retries=1, fallback_model_id="amazon.nova-micro-v1:0", sleep=lambda _s: None)
    resp = c.chat.completions.create(model="amazon.nova-lite-v1:0", messages=[{"role": "user", "content": "U"}],
                                     response_format=RF)
    assert rt.converse.call_args_list[-1].kwargs["modelId"] == "amazon.nova-micro-v1:0"
    assert resp.model == "amazon.nova-micro-v1:0"


def test_non_retryable_error_is_raised_immediately():
    rt = MagicMock()
    rt.converse.side_effect = _client_error("ValidationException")
    c = BedrockChatClient(runtime=rt, fallback_model_id="other", sleep=lambda _s: None)
    with pytest.raises(BedrockInvocationError) as ei:
        c.chat.completions.create(model="m", messages=[{"role": "user", "content": "U"}], response_format=RF)
    assert ei.value.code == "ValidationException"
    assert rt.converse.call_count == 1


# --- ViolationDetector の bedrock モード（経路 B をそのまま Bedrock で動かす）---

def test_bedrock_mode_runs_route_b_pipeline(tmp_path):
    articles = {"articles": [{"id": "11-xiii", "article": "第11条(xiii)", "content": "宣伝・勧誘", "category": "勧誘"},
                             {"id": "11-xvi", "article": "第11条(xvi)", "content": "その他", "category": "その他"}]}
    path = tmp_path / "a.json"
    path.write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
    rt = MagicMock()
    rt.converse.side_effect = [
        _tool_response({"decision": "warn", "candidates": ["11-xiii", "11-xvi"], "reason": "外部グループ"}),
        _tool_response({"article_id": "11-xiii", "reason": "勧誘"}),
    ]
    client = BedrockChatClient(runtime=rt)
    det = ViolationDetector(openai_client=client, judge_model="amazon.nova-lite-v1:0", articles_path=str(path),
                            detection_mode="auto")
    assert det.detection_mode == "bedrock"
    r = det.detect("Join my WhatsApp group")
    assert r.is_violation is True
    assert r.article_id == "11-xiii"
    assert r.method == "Bedrock"
    assert rt.converse.call_count == 2
