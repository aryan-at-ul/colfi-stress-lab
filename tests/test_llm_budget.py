from __future__ import annotations

from adcs.live.supervisor import _report_run
from adcs.llm.client import (
    Completion,
    LLM,
    ModelSpec,
    _anthropic,
    _openai_compatible,
    provider_is_configured,
    stage_output_tokens,
)


class Audit:
    def __init__(self):
        self.events = []

    def log(self, event, **payload):
        self.events.append((event, payload))


def test_context_budget_uses_langchain_estimate_and_exact_model_override(monkeypatch):
    monkeypatch.setenv(
        "ADCS_MODEL_TOKEN_LIMITS_JSON",
        '{"ollama:test":{"context_window":2048,"max_output":512}}',
    )
    monkeypatch.setenv("ADCS_LLM_MAX_PROMPT_TOKENS", "300")
    stats = LLM("live").context_stats(
        "ollama:test", "Return JSON.", "large evidence " * 600, 512
    )
    assert stats["input_tokens_estimated"] > 300
    assert stats["hard_prompt_limit"] == 1  # 2048 - 512 - safety reserve
    assert stats["fits"] is False


def test_truncated_structured_output_retries_with_larger_budget(monkeypatch):
    monkeypatch.setenv("ADCS_LLM_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("ADCS_LLM_TRUNCATION_RETRIES", "2")
    audit = Audit()
    llm = LLM("live", audit=audit)
    budgets = []

    def fake_complete(spec, system, user, temperature, max_tokens, sample_seed=None):
        del system, user, temperature, sample_seed
        budgets.append(max_tokens)
        parsed = spec if isinstance(spec, ModelSpec) else ModelSpec.parse(spec)
        if len(budgets) == 1:
            return Completion(
                '{"value":', parsed, "first", False, 1.0,
                {"completion_tokens": max_tokens}, "length", True, 100, max_tokens,
            )
        return Completion(
            '{"value":1}', parsed, "second", False, 1.0,
            {"completion_tokens": 8}, "stop", False, 100, max_tokens,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    result = llm.complete_json(
        "deepseek:deepseek-v4-flash", "Return JSON.", "Do it.",
        required=("value",), max_tokens=1024, attempts=1,
    )
    assert result["value"] == 1
    assert budgets == [1024, 2048]
    assert audit.events[0][0] == "llm_truncation_retry"


def test_openai_compatible_transport_retains_finish_reason(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "adcs.llm.client._post",
        lambda *args, **kwargs: {
            "choices": [{
                "message": {"content": '{"partial":true}'},
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens": 4096},
        },
    )
    text, usage, finish_reason = _openai_compatible(
        ModelSpec("deepseek", "deepseek-v4-flash"), "system", "user", 0, 4096
    )
    assert text == '{"partial":true}'
    assert usage["completion_tokens"] == 4096
    assert finish_reason == "length"


def test_xai_transport_accepts_existing_key_alias_and_requests_json(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "xai-test-key")
    captured = {}

    def fake_post(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return {
            "choices": [{"message": {"content": '{"ok":true}'},
                         "finish_reason": "stop"}],
            "usage": {"completion_tokens": 4},
        }

    monkeypatch.setattr("adcs.llm.client._post", fake_post)
    text, _, reason = _openai_compatible(
        ModelSpec("xai", "grok-4.3"), "system", "user", 0.2, 512
    )
    assert text == '{"ok":true}'
    assert reason == "stop"
    assert captured["url"] == "https://api.x.ai/v1/chat/completions"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert provider_is_configured("xai:grok-4.3") is True


def test_provider_default_omits_temperature_from_request(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return {
            "choices": [{"message": {"content": '{"ok":true}'},
                         "finish_reason": "stop"}],
            "usage": {"completion_tokens": 4},
        }

    monkeypatch.setattr("adcs.llm.client._post", fake_post)
    _openai_compatible(
        ModelSpec("deepseek", "deepseek-v4-flash"),
        "system", "user", None, 512,
    )
    assert "temperature" not in captured["body"]


def test_anthropic_alias_and_sonnet_5_omit_deprecated_temperature(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-test-key")
    captured = {}

    def fake_post(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return {
            "content": [{"type": "text", "text": '{"ok":true}'}],
            "usage": {"output_tokens": 4}, "stop_reason": "end_turn",
        }

    monkeypatch.setattr("adcs.llm.client._post", fake_post)
    text, _, reason = _anthropic(
        ModelSpec("anthropic", "claude-sonnet-5"),
        "system", "user", 0.2, 512,
    )
    assert text == '{"ok":true}'
    assert reason == "end_turn"
    assert "temperature" not in captured["body"]
    assert provider_is_configured("anthropic:claude-sonnet-5") is True


def test_report_projection_drops_raw_trace_and_has_lean_mode():
    record = {
        "run_id": "CASE-STRESS:INST-01:S1",
        "institution_id": "INST-01",
        "institution_name": "Institution 1",
        "condition": "stress",
        "model": "deepseek:test",
        "sample": 1,
        "status": "complete",
        "trace": [{"detail": "raw trace" * 1000}],
        "tool_results": [{"tool": "portfolio_shock", "raw": "x" * 10000}],
        "output": {
            "stance": "risk_off",
            "urgency": 4,
            "confidence": 0.8,
            "executive_decision": "Reduce exposure.",
            "actions": [{
                "asset_id": "sp500", "action": "sell", "size_pct": 10,
                "timing": "staged", "rationale": "Evidence based." * 100,
            }],
            "constraints_considered": ["Stay within mandate."],
            "evidence_ids": ["MKT-SP500"],
        },
    }
    normal = _report_run(record, lean=False)
    lean = _report_run(record, lean=True)
    assert "trace" not in normal
    assert normal["tools_executed"] == ["portfolio_shock"]
    assert len(normal["decision"]["actions"][0]["rationale"]) <= 240
    assert "rationale" not in lean["decision"]["actions"][0]
    assert "executive_decision" not in lean["decision"]


def test_stage_output_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("ADCS_REPORT_MAX_OUTPUT_TOKENS", "20000")
    assert stage_output_tokens("report", 12288) == 20000
