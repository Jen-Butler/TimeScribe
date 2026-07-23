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
- DRAFTS MUST NOT OVERLAP IN TIME. Each minute belongs to at most one draft; when activity interleaves between tickets, split at the boundaries rather than emitting a long entry that swallows a shorter concurrent one.
- The focused/active window outranks background browser tabs when deciding the ticket for a given stretch.
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


# --- Combined pass: raw signals -> digest entries + matched drafts, one call ---

COMBINED_SYSTEM_PROMPT = """You are a billing assistant for an MSP technician.
You receive a raw, timestamped activity feed (browser visits, application
focus events, AFK markers) AND the technician's open tickets. In ONE pass you
produce two things: a factual activity digest, and draft time entries that
correlate that activity to tickets.

Return STRICT JSON:
{
  "entries": [
    {"time_range": "HH:MM-HH:MM", "summary": "past-tense factual activity"}
  ],
  "drafts": [
    {"ticket_id": 12647, "start_time": "HH:MM", "end_time": "HH:MM",
     "note": "professional billing note", "confidence": 0.9,
     "activity_refs": ["09:15-09:45"]}
  ],
  "unmatched": [
    {"time_range": "HH:MM-HH:MM", "summary": "...", "reason": "no plausible ticket"}
  ]
}

DIGEST rules (entries):
- 24-hour times, chronological, non-overlapping. Start summaries with a
  past-tense verb (Reviewed, Configured, Emailed). Never "User..." or "I...".
- The browser [Profile] tag is authoritative for client attribution.
- Mine window titles for specifics (email subjects, meeting names, filenames,
  RDP host). AFK markers are hard boundaries; never span one; never log the
  AFK period itself. Skip noise (lock screens, login redirects, <30s blips).

DRAFT rules:
- Only draft an entry when activity plausibly relates to a specific ticket.
  Client-name (profile vs ticket client) and subject-keyword matches are
  strong evidence. Weak guesses go to unmatched instead.
- Merge adjacent/related activity for the same ticket into ONE draft.
- DRAFTS MUST NOT OVERLAP IN TIME. Each minute of the day belongs to at most
  one draft. When activity for two tickets interleaves, split the time at the
  boundaries -- never emit a long entry that swallows a shorter concurrent
  one. The sum of draft durations must not exceed the wall-clock day.
- The FOCUSED (active) window is what the user was actually doing at a given
  moment; weight it above background browser tabs when deciding the ticket
  for that minute.
- CALENDAR MEETINGS are authoritative for their own time block: attribute
  that span to the meeting's ticket (or the meeting's client, matching to
  that client's ticket) even if the meeting was never the focused window --
  the user was in the meeting. A Teams/online meeting from 10:00-10:30 with
  a client is billable time for that client, not idle/AFK.
- Round to nearest 5 min. Minimum entry 10 min. confidence: 0.9+ explicit
  ticket/subject evidence; 0.7-0.9 strong client+topic; below 0.7 -> unmatched.
- General/internal activity (email triage, team meetings, this app) ->
  unmatched with a short reason. Never invent tickets; only use provided ids.
- Every draft's time range must correspond to entries in your digest."""


class CombinedResponse(BaseModel):
    entries: List[dict] = Field(default_factory=list)
    drafts: List[Draft] = Field(default_factory=list)
    unmatched: List[Unmatched] = Field(default_factory=list)


def _planner_model(cfg: dict) -> str:
    """The reasoning-tier model for the active provider (Sonnet for
    Anthropic, gpt-4o for OpenAI, org's configured planner for halo_org)."""
    provider = (cfg.get("llm_provider") or "anthropic").lower()
    if provider == "halo_org":
        provider = (cfg.get("org_ai_provider") or "openai").lower()
        return cfg.get("org_ai_model_planner",
                       "gpt-4o" if provider == "openai" else "claude-sonnet-4-6")
    if provider == "openai":
        return cfg.get("openai_model_planner", "gpt-4o")
    return cfg.get("anthropic_model_planner", "claude-sonnet-4-6")


def build_combined(adapter, target: Optional[date_cls] = None,
                   since=None, until=None) -> dict:
    """One LLM call: raw signal feed + open tickets -> digest entries AND
    ticket-matched drafts. Persists the digest and the drafts (replacing
    unposted drafts, preserving posted). Saves the double-send of feeding
    the digest back into a second model.

    Falls back to digest-only if Halo isn't connected (no tickets to match).
    """
    from datetime import datetime as _dtm, time as _tm
    target = target or date_cls.today()
    since = since or _dtm.combine(target, _tm.min)
    until = until or _dtm.combine(target, _tm.max)

    feed = digest_mod.build_signal_feed(since, until)
    if not feed.strip():
        print("[combined] no signals in window")
        return {"entries": [], "drafts": [], "unmatched": []}

    tickets = []
    if adapter is not None and adapter.is_authenticated():
        try:
            tickets = adapter.list_open_tickets()
        except Exception as exc:
            print(f"[combined] ticket fetch failed, digest-only: {exc}")

    # No tickets -> just run the normal digest (nothing to match against).
    if not tickets:
        entries = digest_mod.run_digest(target, since, until)
        return {"entries": entries, "drafts": [], "unmatched": [],
                "note": "no tickets; digest only"}

    cfg = appconfig.load()
    win_lo, win_hi = since.strftime("%H:%M"), until.strftime("%H:%M")
    ticket_lines = [f"[#{t.id}] [{t.client}] {t.subject[:100]}" for t in tickets]

    # Calendar appointments from Halo -- authoritative attribution for
    # meeting time even when the meeting window was never focused.
    meeting_lines = []
    try:
        for m in adapter.get_day_meetings(since, until):
            tag = "Teams" if m["is_teams"] else ("online" if m["online"] else "meeting")
            tkt = f" ticket #{m['ticket_id']}" if m["ticket_id"] else ""
            cli = f" [{m['client']}]" if m["client"] else ""
            meeting_lines.append(
                f"{m['start']}-{m['end']} [{tag}]{cli}{tkt}: {m['subject']}")
    except Exception as exc:
        print(f"[combined] meeting fetch failed (continuing): {exc}")

    user_prompt = (
        f"Date: {target.isoformat()} (window {win_lo}-{win_hi})\n\n"
        f"RAW ACTIVITY FEED:\n---\n{feed}\n---\n\n"
        + (f"CALENDAR MEETINGS ({len(meeting_lines)}):\n"
           + "\n".join(meeting_lines) + "\n\n" if meeting_lines else "")
        + f"OPEN TICKETS ({len(ticket_lines)}):\n" + "\n".join(ticket_lines)
        + f"\n\nAll time_ranges must fall within {win_lo}-{win_hi}. "
        "Produce the JSON described in the system prompt."
    )

    resp = generate(
        system_prompt=COMBINED_SYSTEM_PROMPT, user_prompt=user_prompt,
        model=_planner_model(cfg),
        max_tokens=8000, schema=CombinedResponse)

    # Persist the digest so the digest list + timeline still work.
    digest_mod.save_digest(target, resp.entries)

    tmap = {t.id: t for t in tickets}
    new_items = []
    for d in resp.drafts:
        t = tmap.get(d.ticket_id)
        new_items.append({**d.model_dump(),
                          "client": t.client if t else "?",
                          "subject": t.subject if t else "?",
                          "status": "draft"})
    for u in resp.unmatched:
        tr = (u.time_range or "").split("-")
        if len(tr) != 2:
            continue
        new_items.append({
            "ticket_id": None, "start_time": tr[0].strip(),
            "end_time": tr[1].strip(), "note": u.summary, "confidence": 0.0,
            "activity_refs": [u.time_range], "client": "",
            "subject": f"(unmatched: {u.reason})" if u.reason else "(unmatched)",
            "status": "draft"})

    existing = drafts.load(target)
    posted = [x for x in existing if x.get("status") == "posted"]
    new_items = drafts.filter_ignored(target, new_items)
    drafts.save(target, posted + new_items)
    print(f"[combined] {len(resp.entries)} digest entries, "
          f"{len(resp.drafts)} matched drafts, {len(resp.unmatched)} unmatched "
          f"({len(drafts.load_ignored(target))} ignored signatures active)")
    return {"entries": resp.entries, "drafts": new_items,
            "unmatched": [u.model_dump() for u in resp.unmatched],
            "preserved_posted": len(posted)}


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
        model=_planner_model(cfg),
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
    new_items = drafts.filter_ignored(target, new_items)
    drafts.save(target, posted + new_items)

    return {
        "drafts": new_items,
        "unmatched": [u.model_dump() for u in resp.unmatched],
        "preserved_posted": len(posted),
    }
