"""Provider-agnostic LLM calls (Anthropic or OpenAI) with rate-limit
retry + pydantic schema validation.

Provider selection: config key `llm_provider` = "anthropic" (default)
or "openai". Keys live in the OS credential store via appconfig secrets:
`anthropic_api_key` / `openai_api_key`.
"""
from __future__ import annotations
import json
import time
from typing import Any, Optional, Tuple, Type
from pydantic import BaseModel, ValidationError

from timescribe import appconfig


def _resolve(model: Optional[str]) -> Tuple[str, str]:
    """Return (provider, model) honoring config + per-call override."""
    cfg = appconfig.load()
    provider = (cfg.get("llm_provider") or "anthropic").lower()
    if provider == "openai":
        m = model or cfg.get("openai_model_default", "gpt-4o-mini")
    else:
        provider = "anthropic"
        m = model or cfg.get("anthropic_model_default", "claude-haiku-4-5")
    return provider, m


def _call_anthropic(system_prompt, messages, model, temperature, max_tokens) -> str:
    from anthropic import Anthropic
    key = appconfig.get_secret("anthropic_api_key")
    if not key:
        raise RuntimeError("No Anthropic API key set. Add it in the Setup card.")
    client = Anthropic(api_key=key)
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        system=system_prompt, messages=messages,
    )
    return resp.content[0].text if resp.content else ""


def _call_openai(system_prompt, messages, model, temperature, max_tokens) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    key = appconfig.get_secret("openai_api_key")
    if not key:
        raise RuntimeError("No OpenAI API key set. Add it in the Setup card.")
    client = OpenAI(api_key=key)
    oai_messages = [{"role": "system", "content": system_prompt}] + messages
    resp = client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=oai_messages,
    )
    return resp.choices[0].message.content or ""


def generate(*, system_prompt: str, user_prompt: str,
             model: Optional[str] = None, temperature: float = 0.3,
             max_tokens: int = 4096, schema: Optional[Type[BaseModel]] = None,
             retries: int = 2) -> Any:
    provider, model = _resolve(model)
    call = _call_openai if provider == "openai" else _call_anthropic
    messages = [{"role": "user", "content": user_prompt}]

    last_exc = None
    for attempt in range(retries + 1):
        try:
            text = call(system_prompt, messages, model, temperature, max_tokens)
            if schema is None:
                return text
            try:
                return schema.model_validate_json(text)
            except (json.JSONDecodeError, ValidationError) as ve:
                if attempt < retries:
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content":
                        f"That failed schema validation: {ve}. Reply with valid "
                        f"JSON only, no preamble."})
                    continue
                raise RuntimeError(f"Schema validation failed after retries: {ve}")
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if ("rate_limit" in msg or "429" in msg) and attempt < retries:
                print(f"[llm] {provider} rate limited; waiting 30s...")
                time.sleep(30)
                continue
            if attempt < retries:
                time.sleep(2)
                continue
            break
    raise RuntimeError(f"{provider} call failed: {last_exc!r}")
