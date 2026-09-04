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


# --- AI health: last-known provider status, surfaced in Settings so a low
# credit balance / billing failure is visible instead of silently failing.
# (Anthropic/OpenAI don't expose a live balance to an API key, so we report
# the last call's outcome and flag the specific "out of credit" error.) ---
_ai_health = {"ok": True, "error": None, "provider": None, "billing": False,
              "at": None}


def ai_health() -> dict:
    return dict(_ai_health)


def _is_billing_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(s in m for s in (
        "credit balance is too low", "insufficient", "billing", "quota",
        "payment", "current quota", "402"))


def _mark_ai_ok(provider: str) -> None:
    _ai_health.update(ok=True, error=None, provider=provider, billing=False,
                      at=time.time())


def _mark_ai_error(provider: str, exc: Exception) -> None:
    _ai_health.update(ok=False, error=str(exc)[:300], provider=provider,
                      billing=_is_billing_error(str(exc)), at=time.time())


def _resolve(model: Optional[str]) -> Tuple[str, str]:
    """Return (provider, model) honoring config + per-call override.
    'halo_org' resolves to its underlying provider (the org key's provider)."""
    cfg = appconfig.load()
    provider = (cfg.get("llm_provider") or "anthropic").lower()
    if provider == "halo_org":
        provider = (cfg.get("org_ai_provider") or "openai").lower()
        default = cfg.get("org_ai_model_default", "gpt-4o-mini")
        return (provider, model or default)
    if provider == "openai":
        m = model or cfg.get("openai_model_default", "gpt-4o-mini")
    else:
        provider = "anthropic"
        m = model or cfg.get("anthropic_model_default", "claude-haiku-4-5")
    return provider, m


def _api_key_for(provider: str) -> Optional[str]:
    """The key to use for a provider. In org mode, the shared org key
    (fetched from Halo, cached in the credential store) takes precedence."""
    cfg = appconfig.load()
    if (cfg.get("llm_provider") or "").lower() == "halo_org":
        org = appconfig.get_secret("org_ai_key")
        if org:
            return org
    return appconfig.get_secret(f"{provider}_api_key")


def _call_anthropic(system_prompt, messages, model, temperature, max_tokens) -> str:
    from anthropic import Anthropic
    key = _api_key_for("anthropic")
    if not key:
        raise RuntimeError("No Anthropic API key available (personal or org).")
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
    key = _api_key_for("openai")
    if not key:
        raise RuntimeError("No OpenAI API key available (personal or org).")
    client = OpenAI(api_key=key)
    oai_messages = [{"role": "system", "content": system_prompt}] + messages
    resp = client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=oai_messages,
    )
    return resp.choices[0].message.content or ""


def _strip_fences(text: str) -> str:
    """Models often wrap JSON in ```json ... ``` fences despite instructions.
    Strip a leading/trailing fence pair if present."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


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
            _mark_ai_ok(provider)
            if schema is None:
                return text
            try:
                return schema.model_validate_json(_strip_fences(text))
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
    _mark_ai_error(provider, last_exc)
    raise RuntimeError(f"{provider} call failed: {last_exc!r}")
