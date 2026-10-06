"""Central access point for the LLM system prompts.

Each prompt has two parts:

* an EDITABLE instructions body (tone, rules, attribution guidance) that the
  user can change from Settings > Prompts. Its default text lives with the
  code that uses it (digest.py / inference.py).
* a LOCKED output-format contract (the STRICT-JSON schema the app parses).
  This is defined here, is NOT user-editable, and is always appended to
  whatever body is in effect -- so a user edit can never break JSON parsing.

`get(key)` returns body + locked, which is what actually goes to the model.

digest/inference are imported lazily inside functions to avoid an import
cycle (those modules import this one at module load).
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

# LOCKED, non-editable output contracts. Always appended to the body.
FORMATS = {
    "digest": '{"entries": [{"time_range": "HH:MM-HH:MM", "summary": "..."}]}',
    "ticket_match": """{
  "drafts": [
    {
      "ticket_id": 12647,
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "note": "Professional first-person-omitted billing note describing the work",
      "confidence": 0.9,
      "evidence": "ticket_number | subject_topic | client_only",
      "match_reason": "one sentence citing the activity that matches the ticket SUBJECT",
      "activity_refs": ["09:15-09:45"]
    }
  ],
  "unmatched": [
    {"time_range": "HH:MM-HH:MM", "summary": "...", "reason": "no plausible ticket"}
  ]
}""",
    "combined": """{
  "entries": [
    {"time_range": "HH:MM-HH:MM", "summary": "past-tense factual activity"}
  ],
  "drafts": [
    {"ticket_id": 12647, "start_time": "HH:MM", "end_time": "HH:MM",
     "note": "professional billing note", "confidence": 0.9,
     "evidence": "ticket_number | subject_topic | client_only",
     "match_reason": "one sentence citing the activity that matches the ticket SUBJECT",
     "activity_refs": ["09:15-09:45"]}
  ],
  "unmatched": [
    {"time_range": "HH:MM-HH:MM", "summary": "...", "reason": "no plausible ticket"}
  ]
}""",
    "gap_fill": """{"drafts":[{"ticket_id":123 or null,"start_time":"HH:MM","end_time":"HH:MM",
  "note":"...","confidence":0.0-1.0,"no_charge":false,
  "evidence":"ticket_number | subject_topic | client_only","match_reason":"..."}],
 "unmatched":[]}""",
}

_LOCKED_INTRO = (
    "OUTPUT FORMAT (system-enforced, not editable): respond with STRICT JSON "
    "only -- no markdown, no prose outside the JSON -- exactly in this shape:"
)


def _editables() -> dict:
    from timescribe import digest, inference
    return {
        "digest": digest.SYSTEM_PROMPT,
        "ticket_match": inference.SYSTEM_PROMPT,
        "combined": inference.COMBINED_SYSTEM_PROMPT,
        "gap_fill": inference.GAPFILL_SYSTEM_PROMPT,
    }


def editable_default(key: str) -> str:
    """The built-in EDITABLE body for `key` (no JSON contract)."""
    return _editables()[key]


def locked(key: str) -> str:
    """The read-only output-format contract appended for `key`."""
    return f"{_LOCKED_INTRO}\n{FORMATS[key]}"


def body(key: str) -> str:
    """The effective editable body: user override if set, else default."""
    override = (appconfig.load().get("prompts") or {}).get(key)
    if override and override.strip():
        return override
    return editable_default(key)


def get(key: str) -> str:
    """The full prompt sent to the model: editable body + locked contract."""
    return f"{body(key)}\n\n{locked(key)}"
