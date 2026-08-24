"""Central access point for the LLM system prompts.

The default prompt text still lives with the code that uses it
(digest.py / inference.py) so it stays next to the schemas it must satisfy.
This module lets the user override any of them from Settings > Prompts: a
non-empty override in config wins, otherwise the built-in default is used.

Imports of digest/inference are done lazily inside functions to avoid an
import cycle (those modules import this one at module load).
"""
from __future__ import annotations

from timescribe import appconfig

# Stable ids, display order, and human labels for the editable prompts.
ORDER = ["digest", "ticket_match", "combined", "gap_fill"]
LABELS = {
    "digest": "Activity digest",
    "ticket_match": "Ticket matching (drafts from digest)",
    "combined": "Combined one-pass (digest + drafts)",
    "gap_fill": "Timesheet gap fill",
}


def _defaults() -> dict:
    from timescribe import digest, inference
    return {
        "digest": digest.SYSTEM_PROMPT,
        "ticket_match": inference.SYSTEM_PROMPT,
        "combined": inference.COMBINED_SYSTEM_PROMPT,
        "gap_fill": inference.GAPFILL_SYSTEM_PROMPT,
    }


def default(key: str) -> str:
    """The built-in default prompt for `key`."""
    return _defaults()[key]


def get(key: str) -> str:
    """The effective prompt for `key`: the user's override if set and
    non-empty, otherwise the built-in default."""
    override = (appconfig.load().get("prompts") or {}).get(key)
    if override and override.strip():
        return override
    return _defaults()[key]
