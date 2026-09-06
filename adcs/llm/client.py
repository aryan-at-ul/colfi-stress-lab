"""Live-only structured LLM transport.

There is deliberately no mock provider, simulated policy, or response cache in
the runtime. A successful completion always represents a fresh provider call.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately


TIMEOUT = int(os.environ.get("ADCS_LLM_TIMEOUT_SECONDS", "180"))

PROVIDER_LIMITS = {
    # Conservative defaults. Exact models can be overridden with
    # ADCS_MODEL_TOKEN_LIMITS_JSON without changing application code.
    "deepseek": (1_000_000, 384_000),
    "openai": (128_000, 16_384),
    "anthropic": (180_000, 16_384),
    "google": (900_000, 32_768),
    "mistral": (120_000, 16_384),
    "groq": (120_000, 8_192),
    "openrouter": (120_000, 16_384),
    "together": (120_000, 16_384),
    # Grok 4.6 has a 500k context window; use the lower common xAI limit so
    # mixed Grok registries never overstate the context available to a model.
    "xai": (500_000, 32_768),
    "ollama": (32_000, 8_192),
}
TRUNCATION_REASONS = {"length", "max_tokens", "max_output_tokens"}


class LLMError(RuntimeError):
    pass


class LLMContextError(LLMError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    @classmethod
    def parse(cls, value: str) -> "ModelSpec":
        if ":" not in value:
            raise ValueError(f"model must use provider:model format, got {value!r}")
        provider, model = value.split(":", 1)
        if not provider.strip() or not model.strip():
            raise ValueError("provider and model must both be non-empty")
        if provider.strip().lower() in {"sim", "mock"}:
            raise ValueError("simulated and mock providers are not supported")
        return cls(provider.strip().lower(), model.strip())

    def __str__(self):
        return f"{self.provider}:{self.model}"


@dataclass
class Completion:
    text: str
    spec: ModelSpec
    prompt_sha256: str
    cached: bool
    latency_ms: float
    usage: dict
    finish_reason: str | None = None
    truncated: bool = False
    input_tokens_estimated: int = 0
    max_output_tokens: int = 0


def stage_output_tokens(stage: str, default: int) -> int:
    name = f"ADCS_{stage.upper()}_MAX_OUTPUT_TOKENS"
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise LLMError(f"{name} must be an integer") from exc
    if value < 256:
        raise LLMError(f"{name} must be at least 256")
    return value


def _limits(spec: ModelSpec) -> tuple[int, int]:
    context_window, max_output = PROVIDER_LIMITS.get(spec.provider, (64_000, 8_192))
    raw = os.environ.get("ADCS_MODEL_TOKEN_LIMITS_JSON", "").strip()
    if raw:
        try:
            configured = json.loads(raw)
            selected = configured.get(str(spec), configured.get(spec.provider, {}))
            context_window = int(selected.get("context_window", context_window))
            max_output = int(selected.get("max_output", max_output))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            raise LLMError("ADCS_MODEL_TOKEN_LIMITS_JSON is invalid") from exc
    return context_window, max_output


def _global_output_cap() -> int:
    try:
        return int(os.environ.get("ADCS_LLM_MAX_OUTPUT_TOKENS", "32768"))
    except ValueError as exc:
        raise LLMError("ADCS_LLM_MAX_OUTPUT_TOKENS must be an integer") from exc


def _prompt_cap(context_window: int, output_tokens: int) -> int:
    try:
        configured = int(os.environ.get(
            "ADCS_LLM_MAX_PROMPT_TOKENS", str(context_window - output_tokens - 2048)
        ))
    except ValueError as exc:
        raise LLMError("ADCS_LLM_MAX_PROMPT_TOKENS must be an integer") from exc
    return max(1, min(configured, context_window - output_tokens - 2048))


def _soft_prompt_cap(hard_cap: int) -> int:
    try:
        configured = int(os.environ.get("ADCS_LLM_COMPACT_AT_TOKENS", "48000"))
    except ValueError as exc:
        raise LLMError("ADCS_LLM_COMPACT_AT_TOKENS must be an integer") from exc
    return max(1, min(configured, hard_cap))


def _estimate_prompt_tokens(system: str, user: str) -> int:
    return count_tokens_approximately([
        SystemMessage(content=system), HumanMessage(content=user),
    ])


def _usage_output_tokens(usage: dict) -> int | None:
    for key in ("completion_tokens", "output_tokens", "candidatesTokenCount"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _prompt_hash(spec: ModelSpec, system: str, user: str, temperature: float | None,
                 max_tokens: int) -> str:
    payload = json.dumps([str(spec), system, user, temperature, max_tokens],
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise LLMError(f"{name} is not configured")
    return value


def _need_any(*names: str) -> str:
    """Return the first configured credential without exposing its value.

    ``CLAUDE_API_KEY`` and ``GROQ_API_KEY`` are accepted as compatibility
    aliases because existing deployments already use those names. New xAI
    deployments should prefer ``XAI_API_KEY``; Groq and xAI are separate
    providers and cannot safely share one key when both are enabled.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise LLMError(f"one of {', '.join(names)} must be configured")


PROVIDER_CREDENTIALS = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "google": ("GOOGLE_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "xai": ("XAI_API_KEY", "GROQ_API_KEY"),
}


def provider_is_configured(spec: ModelSpec | str) -> bool:
    parsed = ModelSpec.parse(spec) if isinstance(spec, str) else spec
    if parsed.provider == "ollama":
        return True
    return any(
        bool(os.environ.get(name))
        for name in PROVIDER_CREDENTIALS.get(parsed.provider, ())
    )


def _post(url: str, headers: dict, body: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode(errors="replace")
        raise LLMError(f"provider HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LLMError(f"provider transport failed: {type(exc).__name__}: {exc}") from exc
    except (json.JSONDecodeError, KeyError) as exc:
        raise LLMError(f"provider returned an invalid response: {exc}") from exc


OPENAI_COMPATIBLE = {
    "openai": ("https://api.openai.com/v1", ("OPENAI_API_KEY",)),
    "deepseek": (os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                 ("DEEPSEEK_API_KEY",)),
    "groq": ("https://api.groq.com/openai/v1", ("GROQ_API_KEY",)),
    "mistral": ("https://api.mistral.ai/v1", ("MISTRAL_API_KEY",)),
    "openrouter": ("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",)),
    "together": ("https://api.together.xyz/v1", ("TOGETHER_API_KEY",)),
    "xai": (os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
            ("XAI_API_KEY", "GROQ_API_KEY")),
}


def _openai_compatible(spec: ModelSpec, system: str, user: str,
                       temperature: float | None,
                       max_tokens: int) -> tuple[str, dict, str | None]:
    base, key_names = OPENAI_COMPATIBLE[spec.provider]
    body = {
        "model": spec.model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    if spec.provider == "deepseek" and spec.model.startswith("deepseek-v4-"):
        body["thinking"] = {"type": "disabled"}
        body["response_format"] = {"type": "json_object"}
    if spec.provider == "openai":
        body["max_completion_tokens"] = body.pop("max_tokens")
        body["response_format"] = {"type": "json_object"}
    elif spec.provider == "xai":
        body["response_format"] = {"type": "json_object"}
    if "reasoner" in spec.model:
        body.pop("temperature", None)
    result = _post(f"{base.rstrip('/')}/chat/completions",
                   {"Authorization": f"Bearer {_need_any(*key_names)}"}, body)
    choice = result["choices"][0]
    message = choice["message"]
    return (message.get("content") or "", result.get("usage", {}),
            choice.get("finish_reason"))


def _anthropic(spec: ModelSpec, system: str, user: str,
               temperature: float | None,
               max_tokens: int) -> tuple[str, dict, str | None]:
    body = {
        "model": spec.model, "max_tokens": max_tokens,
        "system": system, "messages": [{"role": "user", "content": user}],
    }
    # Anthropic rejects non-default temperature on Sonnet 5 and newer model
    # generations. Preserve temperature for Haiku 4.5, which still supports it.
    if temperature is not None and not spec.model.startswith(
        ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-mythos-5")
    ):
        body["temperature"] = temperature
    result = _post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": _need_any("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
         "anthropic-version": "2023-06-01"},
        body,
    )
    text = "".join(block.get("text", "") for block in result.get("content", [])
                   if block.get("type") == "text")
    return text, result.get("usage", {}), result.get("stop_reason")


def _google(spec: ModelSpec, system: str, user: str,
            temperature: float | None,
            max_tokens: int) -> tuple[str, dict, str | None]:
    key = _need("GOOGLE_API_KEY")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{spec.model}:generateContent?key={key}")
    generation_config = {
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",
    }
    if temperature is not None:
        generation_config["temperature"] = temperature
    result = _post(url, {}, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": generation_config,
    })
    candidate = result["candidates"][0]
    parts = candidate["content"]["parts"]
    return ("".join(part.get("text", "") for part in parts),
            result.get("usageMetadata", {}), candidate.get("finishReason"))


def _ollama(spec: ModelSpec, system: str, user: str,
            temperature: float | None,
            max_tokens: int) -> tuple[str, dict, str | None]:
    base = os.environ.get("OLLAMA_BASE", "http://localhost:11434/v1")
    body = {
        "model": spec.model,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    result = _post(f"{base.rstrip('/')}/chat/completions",
                   {"Authorization": "Bearer ollama"}, body)
    choice = result["choices"][0]
    return (choice["message"].get("content") or "", result.get("usage", {}),
            choice.get("finish_reason"))


class LLM:
    def __init__(self, mode: str | None = None, audit=None):
        selected = (mode or os.environ.get("ADCS_LLM_MODE", "")).lower()
        if selected != "live":
            raise LLMError("the runtime is live-only; set ADCS_LLM_MODE=live")
        self.audit = audit
        self.calls = 0

    def context_stats(self, spec: ModelSpec | str, system: str, user: str,
                      max_tokens: int) -> dict:
        parsed = ModelSpec.parse(spec) if isinstance(spec, str) else spec
        context_window, provider_output_cap = _limits(parsed)
        output_cap = min(provider_output_cap, _global_output_cap())
        effective_output = min(max_tokens, output_cap)
        hard_prompt_cap = _prompt_cap(context_window, effective_output)
        estimated = _estimate_prompt_tokens(system, user)
        return {
            "model": str(parsed),
            "input_tokens_estimated": estimated,
            "hard_prompt_limit": hard_prompt_cap,
            "compact_at_tokens": _soft_prompt_cap(hard_prompt_cap),
            "requested_output_tokens": max_tokens,
            "effective_output_tokens": effective_output,
            "provider_output_limit": provider_output_cap,
            "context_window": context_window,
            "fits": estimated <= hard_prompt_cap,
        }

    def complete(self, spec: ModelSpec | str, system: str, user: str,
                 temperature: float | None = None, max_tokens: int = 1200,
                 sample_seed: int | None = None) -> Completion:
        del sample_seed  # providers are sampled independently; unsupported seeds are not claimed
        parsed = ModelSpec.parse(spec) if isinstance(spec, str) else spec
        stats = self.context_stats(parsed, system, user, max_tokens)
        effective_output = stats["effective_output_tokens"]
        if not stats["fits"]:
            if self.audit:
                self.audit.log("llm_context_rejected", **stats)
            raise LLMContextError(
                f"{parsed}: estimated prompt size {stats['input_tokens_estimated']} exceeds "
                f"the configured hard limit {stats['hard_prompt_limit']} tokens"
            )
        prompt_sha256 = _prompt_hash(parsed, system, user, temperature, effective_output)
        started = time.time()
        if parsed.provider in OPENAI_COMPATIBLE:
            text, usage, finish_reason = _openai_compatible(
                parsed, system, user, temperature, effective_output
            )
        elif parsed.provider == "anthropic":
            text, usage, finish_reason = _anthropic(
                parsed, system, user, temperature, effective_output
            )
        elif parsed.provider == "google":
            text, usage, finish_reason = _google(
                parsed, system, user, temperature, effective_output
            )
        elif parsed.provider == "ollama":
            text, usage, finish_reason = _ollama(
                parsed, system, user, temperature, effective_output
            )
        else:
            raise LLMError(f"unsupported live provider: {parsed.provider}")
        latency = (time.time() - started) * 1000
        truncated = str(finish_reason or "").lower() in TRUNCATION_REASONS
        self.calls += 1
        if self.audit:
            self.audit.log("llm_call", model=str(parsed),
                           prompt_sha256=prompt_sha256, cached=False,
                           sampling_mode=(
                               "provider_default" if temperature is None
                               else "explicit_temperature"
                           ),
                           temperature=temperature,
                           latency_ms=round(latency, 1), usage=usage,
                           finish_reason=finish_reason, truncated=truncated,
                           input_tokens_estimated=stats["input_tokens_estimated"],
                           max_output_tokens=effective_output,
                           context_window=stats["context_window"])
        return Completion(
            text, parsed, prompt_sha256, False, latency, usage,
            finish_reason, truncated, stats["input_tokens_estimated"], effective_output,
        )

    def complete_json(self, spec, system: str, user: str,
                      required: tuple[str, ...], temperature: float | None = None,
                      max_tokens: int = 1200, attempts: int = 3,
                      sample_seed: int | None = None) -> dict:
        previous = ""
        parsed = ModelSpec.parse(spec) if isinstance(spec, str) else spec
        _, provider_cap = _limits(parsed)
        hard_output_cap = min(provider_cap, _global_output_cap())
        try:
            truncation_retries = int(os.environ.get("ADCS_LLM_TRUNCATION_RETRIES", "2"))
        except ValueError as exc:
            raise LLMError("ADCS_LLM_TRUNCATION_RETRIES must be an integer") from exc
        budget = min(max_tokens, hard_output_cap)
        for attempt in range(attempts):
            prompt = user if attempt == 0 else (
                f"{user}\n\nYour previous response was not a valid JSON object with "
                f"the required keys {', '.join(required)}. Return only corrected JSON. "
                f"Previous response: {previous[:700]}")
            cut_attempt = 0
            while True:
                completion = self.complete(
                    parsed, system, prompt, temperature, budget,
                    sample_seed=sample_seed,
                )
                used = _usage_output_tokens(completion.usage)
                likely_cut = completion.truncated or (
                    used is not None and used >= int(budget * 0.95)
                    and _extract_json(completion.text) is None
                )
                if not likely_cut:
                    break
                if cut_attempt >= truncation_retries or budget >= hard_output_cap:
                    raise LLMError(
                        f"{parsed}: structured response was truncated at {budget} "
                        f"tokens (finish_reason={completion.finish_reason!r}); "
                        "raise the exact model limit or reduce the payload"
                    )
                next_budget = min(hard_output_cap, max(budget + 1024, budget * 2))
                if self.audit:
                    self.audit.log(
                        "llm_truncation_retry", model=str(parsed),
                        finish_reason=completion.finish_reason,
                        previous_max_output_tokens=budget,
                        next_max_output_tokens=next_budget,
                        output_tokens_observed=used,
                    )
                budget = next_budget
                cut_attempt += 1
            previous = completion.text
            value = _extract_json(previous)
            if value is not None and all(key in value for key in required):
                value["_raw"] = previous
                value["_prompt_sha256"] = completion.prompt_sha256
                value["_cached"] = False
                return value
        raise LLMError(f"{spec}: provider did not return required structured JSON")


def _extract_json(text: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    for end in range(len(cleaned) - 1, start, -1):
        if cleaned[end] != "}":
            continue
        try:
            value = json.loads(cleaned[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            continue
    return None
