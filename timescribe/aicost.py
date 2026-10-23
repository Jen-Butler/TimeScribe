"""AI spend metering and an estimated credit balance.

Neither Anthropic nor OpenAI exposes "credits remaining" to an ordinary API
key (only admin-level historical cost reports), so TimeScribe keeps its own
books: every call's token counts (from the provider's response) are priced
per model and appended to a local ledger. The user enters the balance they
see in the provider console once; from then on the app shows

    estimated remaining = entered balance - metered spend since that moment

and warns below a threshold. The ledger only knows about calls made by THIS
app, so if the key is shared the estimate drifts -- re-enter the balance
whenever you check the console.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from platformdirs import user_data_dir

from timescribe import appconfig

# $ per 1M tokens (input, output). Matched by longest prefix of the model id.
# Edit here when list prices change; unknown models fall back to DEFAULT.
PRICES = {
    # Anthropic
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4": (0.80, 4.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "o4-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
}
DEFAULT_PRICE = (3.00, 15.00)

_lock = threading.Lock()


def _ledger_path() -> Path:
    d = Path(user_data_dir("timescribe"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "ai_usage.jsonl"


def price_for(model: str) -> tuple:
    m = (model or "").lower()
    best = None
    for prefix, p in PRICES.items():
        if m.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, p)
    return best[1] if best else DEFAULT_PRICE


def cost_usd(model: str, in_tokens: int, out_tokens: int) -> float:
    pi, po = price_for(model)
    return (in_tokens or 0) / 1e6 * pi + (out_tokens or 0) / 1e6 * po


def record(provider: str, model: str, in_tokens: int, out_tokens: int) -> float:
    """Append one call to the ledger; returns its cost."""
    c = cost_usd(model, in_tokens, out_tokens)
    row = {"ts": time.time(), "provider": provider, "model": model,
           "in": int(in_tokens or 0), "out": int(out_tokens or 0), "cost": round(c, 6)}
    with _lock:
        with _ledger_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return c


def _rows(since_ts: float = 0.0) -> list:
    p = _ledger_path()
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("ts", 0) >= since_ts:
                out.append(r)
    return out


def set_balance(provider: str, amount: float) -> dict:
    """Record 'the console says $amount right now' for a provider."""
    cfg = appconfig.load()
    bal = dict(cfg.get("ai_balance") or {})
    bal[provider] = {"amount": float(amount), "as_of": time.time(),
                     "as_of_iso": datetime.now().isoformat(timespec="seconds")}
    cfg["ai_balance"] = bal
    appconfig.save(cfg)
    return bal[provider]


def summary(provider: Optional[str] = None) -> dict:
    """Spend + estimated remaining for the active provider."""
    cfg = appconfig.load()
    if not provider:
        provider = (cfg.get("llm_provider") or "anthropic").lower()
        if provider == "halo_org":
            provider = (cfg.get("org_ai_provider") or "openai").lower()
    bal = (cfg.get("ai_balance") or {}).get(provider) or {}
    threshold = float(cfg.get("ai_balance_warn", 5.0) or 0)
    now = time.time()
    day0 = datetime.combine(date.today(), datetime.min.time()).timestamp()
    month0 = datetime(date.today().year, date.today().month, 1).timestamp()
    rows = [r for r in _rows(min(month0, bal.get("as_of", now)) if bal else month0)
            if r.get("provider") == provider]
    def tot(since):
        return round(sum(r["cost"] for r in rows if r["ts"] >= since), 4)
    spent_since = tot(bal["as_of"]) if bal else None
    remaining = round(bal["amount"] - spent_since, 2) if bal else None
    models = {}
    for r in rows:
        if r["ts"] >= month0:
            models[r["model"]] = round(models.get(r["model"], 0) + r["cost"], 4)
    return {
        "provider": provider,
        "balance_set": bool(bal),
        "balance_amount": bal.get("amount"),
        "balance_as_of": bal.get("as_of_iso"),
        "spent_since_balance": spent_since,
        "estimated_remaining": remaining,
        "today": tot(day0),
        "month": tot(month0),
        "calls_today": sum(1 for r in rows if r["ts"] >= day0),
        "threshold": threshold,
        "low": bool(bal) and remaining is not None and remaining <= threshold,
        "models_month": models,
    }
