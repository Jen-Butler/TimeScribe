"""Correlate the day's activity digest with open tickets and draft
time entries. Uses the planner model (Sonnet) -- this is the
reasoning-heavy step."""
from __future__ import annotations
import json
from datetime import date as date_cls
from typing import List, Optional
from pydantic import BaseModel, Field

from timescribe import appconfig, digest as digest_mod, drafts
from timescribe.llm import generate

SYSTEM_PROMPT = """You are a billing assistant for an MSP technician. You receive:
1. Today's activity digest: timestamped entries describing what the technician did.
2. Their open tickets: id, client, subject.

Your job: correlate activity to tickets and draft time entries.

Return STRICT JSON:
{
  "drafts": [
    {
      "ticket_id": 12647,
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "note": "Professional first-person-omitted billing note describing the work",
      "confidence": 0.9,
      "activity_refs": ["09:15-09:45"]
    }
  ],
  "unmatched": [
    {"time_range": "HH:MM-HH:MM", "summary": "...", "reason": "no plausible ticket"}
  ]
}

Rules:
- Only draft an entry when the activity plausibly relates to a specific ticket. Client-name match (profile vs ticket client) is strong evidence. Subject-keyword match is strong evidence. Weak guesses go to unmatched instead.
- Merge adjacent/related digest entries for the same ticket into ONE draft spanning the combined time.
- Round times to the nearest 5 minutes. Minimum entry: 10 minutes.
- The note should read like a professional time entry: what was done, for what system/feature, outcome if visible. No "the user" phrasing -- write like a technician writes ("Configured integration runbook for asset sync; tested against dev environment").
- confidence: 0.9+ = explicit ticket-number or exact-subject evidence; 0.7-0.9 = strong client+topic match; below 0.7 = send to unmatched instead.
- General/internal activity (email triage, team meetings, personal browsing, this app) -> unmatched with a short reason.
- Never invent tickets. Only use ticket_ids from the provided list."""


class Draft(BaseModel):
    ticket_id: int
    start_time: str
    end_time: str
    note: str
    confidence: float = 0.0
    activity_refs: List[str] = Field(default_factory=list)


class Unmatched(BaseModel):
    time_range: str
    summary: str
    reason: str = ""


class InferenceResponse(BaseModel):
    drafts: List[Draft] = Field(default_factory=list)
    unmatched: List[Unmatched] = Field(default_factory=list)


def generate_drafts(adapter, target: Optional[date_cls] = None) -> dict:
    """Run inference for a day. Persists drafts (replacing any existing
    UNPOSTED drafts for that day; posted ones are preserved)."""
    target = target or date_cls.today()
    entries = digest_mod.load_digest(target)
    if not entries:
        return {"drafts": [], "unmatched": [],
                "error": "No digest for this day. Run the digest first."}

    tickets = adapter.list_open_tickets()
    if not tickets:
        return {"drafts": [], "unmatched": [], "error": "No open tickets found."}

    ticket_lines = [f"[#{t.id}] [{t.client}] {t.subject[:100]}" for t in tickets]
    digest_lines = [f"{e['time_range']} — {e['summary']}" for e in entries]

    cfg = appconfig.load()
    user_prompt = (
        f"Date: {target.isoformat()}\n\n"
        f"ACTIVITY DIGEST ({len(digest_lines)} entries):\n"
        + "\n".join(digest_lines)
        + f"\n\nOPEN TICKETS ({len(ticket_lines)}):\n"
        + "\n".join(ticket_lines)
        + "\n\nProduce the JSON described in the system prompt."
    )

    resp = generate(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=cfg.get("anthropic_model_planner", "claude-sonnet-4-6"),
        max_tokens=8000,
    schema=InferenceResponse)

    # Build ticket lookup for display enrichment
    tmap = {t.id: t for t in tickets}
    new_items = []
    for d in resp.drafts:
        t = tmap.get(d.ticket_id)
        new_items.append({
            **d.model_dump(),
            "client": t.client if t else "?",
            "subject": t.subject if t else "?",
            "status": "draft",
        })
    # Unmatched activity becomes pickable drafts with no ticket
    # (would post as Quick Time unless the user assigns a ticket id).
    for u in resp.unmatched:
        tr = (u.time_range or "").split("-")
        if len(tr) != 2:
            continue
        new_items.append({
            "ticket_id": None,
            "start_time": tr[0].strip(),
            "end_time": tr[1].strip(),
            "note": u.summary,
            "confidence": 0.0,
            "activity_refs": [u.time_range],
            "client": "",
            "subject": f"(unmatched: {u.reason})" if u.reason else "(unmatched)",
            "status": "draft",
        })

    # Preserve already-posted drafts, replace the rest
    existing = drafts.load(target)
    posted = [x for x in existing if x.get("status") == "posted"]
    drafts.save(target, posted + new_items)

    return {
        "drafts": new_items,
        "unmatched": [u.model_dump() for u in resp.unmatched],
        "preserved_posted": len(posted),
    }
