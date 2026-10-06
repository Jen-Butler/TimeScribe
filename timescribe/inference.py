"""Correlate the day's activity digest with open tickets and draft
time entries. Uses the planner model (Sonnet) -- this is the
reasoning-heavy step."""
from __future__ import annotations
import json
from datetime import date as date_cls
from typing import List, Optional
from pydantic import BaseModel, Field

from timescribe import appconfig, digest as digest_mod, drafts, prompts
from timescribe.llm import generate

SYSTEM_PROMPT = """You are a billing assistant for an MSP technician. You receive:
1. Today's activity digest: timestamped entries describing what the technician did.
2. Their open tickets: id, client, subject.

Your job: correlate activity to tickets and draft time entries.

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
    ticket_id: Optional[int] = None
    start_time: str
    end_time: str
    note: str
    confidence: float = 0.0
    no_charge: bool = False
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
- CALENDAR MEETINGS ARE CONTEXT, NOT GROUND TRUTH. The activity feed is the
  source of truth for what actually happened. Use a calendar entry only to:
  (a) confirm the digest covers that time window, and (b) SUGGEST a ticket
  for activity that the feed shows really occurred in that window. A meeting
  on the calendar does NOT prove the user attended or did that work.
  * Only create a ticket-matched draft for a meeting's window if the feed
    shows corroborating activity then -- a Teams window focused, related
    browser/app activity, or notes. If the feed shows the user was doing
    something ELSE during a scheduled meeting, follow the feed, not the
    calendar.
  * NEVER copy a meeting's subject or ticket into a draft note as if it were
    the work done. Write the note from the actual activity. Do not invent
    engagement, outcomes, or ticket work that the feed doesn't support.
  * A scheduled meeting with NO corroborating activity in the feed is not
    billable work -- omit it or leave it unmatched with reason "calendar
    only, no activity". Do not fabricate a draft from the calendar alone.
- Round to nearest 5 min. Minimum entry 10 min. confidence: 0.9+ explicit
  ticket/subject evidence in the ACTIVITY FEED; 0.7-0.9 strong feed-based
  client+topic match; below 0.7, or calendar-only with no feed support ->
  unmatched.
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
                   since=None, until=None, replace_drafts: bool = True) -> dict:
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

    # Clamp to elapsed time: no digest entries or drafts for the future.
    now = _dtm.now()
    if until > now:
        until = now
    if since >= until:
        print("[combined] window is entirely in the future; nothing to do")
        return {"entries": [], "drafts": [], "unmatched": []}

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

    # Time that's already logged (posted through this app OR entered
    # directly on the Halo timesheet) must not be drafted again.
    ts_rows = []
    try:
        ts = adapter.get_day_timesheet(since, until)
        ts_rows = ts.get("rows", [])
    except Exception as exc:
        print(f"[combined] timesheet fetch failed (continuing): {exc}")
    covered = drafts.covered_windows(target, ts_rows)
    covered_lines = [f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                     for s, e in covered]

    user_prompt = (
        f"Date: {target.isoformat()} (window {win_lo}-{win_hi})\n\n"
        f"RAW ACTIVITY FEED:\n---\n{feed}\n---\n\n"
        + (f"CALENDAR CONTEXT ({len(meeting_lines)} scheduled meetings) -- "
           "hints for ensuring digest coverage and SUGGESTING a ticket only "
           "where the activity feed shows the work really happened; not proof "
           "of attendance or billable work on their own:\n"
           + "\n".join(meeting_lines) + "\n\n" if meeting_lines else "")
        + (f"TIME ALREADY LOGGED ({len(covered_lines)} windows) -- the "
           "technician has already entered time covering these ranges. "
           "Still produce digest entries for them, but do NOT produce any "
           "draft or unmatched item overlapping them:\n"
           + "\n".join(covered_lines) + "\n\n" if covered_lines else "")
        + f"OPEN TICKETS ({len(ticket_lines)}):\n" + "\n".join(ticket_lines)
        + f"\n\nAll time_ranges must fall within {win_lo}-{win_hi}. "
        "Produce the JSON described in the system prompt."
    )

    resp = generate(
        system_prompt=prompts.get("combined"), user_prompt=user_prompt,
        model=_planner_model(cfg),
        max_tokens=8000, schema=CombinedResponse)

    # Persist the digest so the digest list + timeline still work.
    digest_mod.save_digest(target, resp.entries)

    existing = drafts.load(target)
    posted = [x for x in existing if x.get("status") == "posted"]

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
    new_items = drafts.filter_ignored(target, new_items)

    if not replace_drafts:
        # Automatic passes are ADDITIVE: never modify or remove existing
        # drafts (you may be curating them). Only ADD drafts for windows not
        # already covered by an existing draft (any status) or by logged
        # time, so new activity through the day still produces drafts.
        occupied = list(covered)
        for x in existing:
            s2 = drafts._hm_to_min(x.get("start_time"))
            e2 = drafts._hm_to_min(x.get("end_time"))
            if s2 is not None and e2 is not None and e2 > s2:
                occupied.append((s2, e2))
        additions = drafts.filter_covered(new_items, occupied)
        if additions:
            drafts.save(target, existing + additions)
        print(f"[combined] additive: {len(existing)} existing kept, "
              f"{len(additions)} added")
        return {"entries": resp.entries, "drafts": existing + additions,
                "unmatched": [u.model_dump() for u in resp.unmatched],
                "preserved_posted": len(posted)}

    # Explicit rebuild: replace unposted drafts (posted are kept).
    new_items = drafts.filter_covered(new_items, covered)   # hard backstop
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

    # The activity log is the real source; Halo tickets are only the
    # matching context. If Halo is unreachable or has no open tickets, still
    # build the drafts -- just unticketed (quick time) for the user to assign
    # by hand -- rather than failing with nothing.
    tickets = []
    halo_note = ""
    try:
        tickets = adapter.list_open_tickets() or []
    except Exception as exc:
        halo_note = f"Halo unavailable ({str(exc)[:120]})"
        print(f"[inference] {halo_note}; building unticketed drafts")
    if not tickets and not halo_note:
        halo_note = "no open tickets to match against"

    ticket_lines = [f"[#{t.id}] [{t.client}] {t.subject[:100]}" for t in tickets]
    digest_lines = [f"{e['time_range']} — {e['summary']}" for e in entries]

    cfg = appconfig.load()
    from datetime import datetime as _dtm, time as _tm
    ts_rows = []
    try:
        ts = adapter.get_day_timesheet(_dtm.combine(target, _tm.min),
                                       _dtm.combine(target, _tm.max))
        ts_rows = ts.get("rows", [])
    except Exception as exc:
        print(f"[inference] timesheet fetch failed (continuing): {exc}")
    covered = drafts.covered_windows(target, ts_rows)
    covered_lines = [f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                     for s, e in covered]

    user_prompt = (
        f"Date: {target.isoformat()}\n\n"
        f"ACTIVITY DIGEST ({len(digest_lines)} entries):\n"
        + "\n".join(digest_lines)
        + (f"\n\nTIME ALREADY LOGGED ({len(covered_lines)} windows) -- do "
           "NOT produce any draft or unmatched item overlapping these:\n"
           + "\n".join(covered_lines) if covered_lines else "")
        + (f"\n\nOPEN TICKETS ({len(ticket_lines)}):\n" + "\n".join(ticket_lines)
           if ticket_lines else
           "\n\nOPEN TICKETS: none available (" + halo_note + "). Put EVERY "
           "entry in \"unmatched\" with a factual summary; the user will assign "
           "tickets by hand. Do not invent ticket ids.")
        + "\n\nProduce the JSON described in the system prompt."
    )

    resp = generate(
        system_prompt=prompts.get("ticket_match"),
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
    proposed = len(new_items)
    for it in new_items:
        print(f"[inference] proposed {it.get('start_time')}-{it.get('end_time')} "
              f"#{it.get('ticket_id') or 'quick'}: {(it.get('note') or '')[:80]}")
    new_items = drafts.filter_ignored(target, new_items)
    skipped_ignored = proposed - len(new_items)
    before_cov = len(new_items)
    new_items = drafts.filter_covered(new_items, covered)   # hard backstop
    skipped_covered = before_cov - len(new_items)
    drafts.save(target, posted + new_items)
    print(f"[inference] {len(new_items)} drafts built "
          f"({len(resp.drafts)} matched, {len(resp.unmatched)} unmatched, "
          f"{skipped_covered} already logged, {skipped_ignored} ignored)"
          + (f" -- {halo_note}" if halo_note else ""))

    # Explain the outcome -- a bare "0 drafts" is indistinguishable from a
    # failure, when usually it just means the day is already on the timesheet.
    covered_min = sum(e - s for s, e in covered)
    digest_span = ""
    if entries:
        digest_span = f"{entries[0]['time_range'].split('-')[0].strip()}–" \
                      f"{entries[-1]['time_range'].split('-')[-1].strip()}"
    reasons = []
    if skipped_covered:
        reasons.append(f"{skipped_covered} skipped: already logged in Halo"
                       + (f" ({', '.join(covered_lines)})" if covered_lines else ""))
    if skipped_ignored:
        reasons.append(f"{skipped_ignored} skipped: previously ignored")
    if not new_items and not reasons and proposed == 0:
        reasons.append("the AI found nothing billable in the activity log"
                       + (f" ({digest_span})" if digest_span else ""))

    out = {
        "drafts": new_items,
        "unmatched": [u.model_dump() for u in resp.unmatched],
        "preserved_posted": len(posted),
        "proposed": proposed,
        "skipped_covered": skipped_covered,
        "skipped_ignored": skipped_ignored,
        "covered_minutes": covered_min,
        "covered_windows": covered_lines,
    }
    notes = []
    if reasons:
        notes.append("; ".join(reasons))
    if halo_note:
        notes.append(f"Built without ticket matching: {halo_note}. Assign tickets by hand.")
    if notes:
        out["note"] = " ".join(notes)
    return out


GAPFILL_SYSTEM_PROMPT = """You are a billing assistant for an MSP technician.
You are given, for one day:
- GAPS: time windows on the timesheet with NO logged time yet.
- ACTIVITY: a raw, timestamped feed of what actually happened (browser
  visits, app focus, AFK markers).
- AFK PERIODS: windows the person was away from the keyboard.
- ALREADY LOGGED: windows already on the timesheet (never overlap these).
- OPEN TICKETS and MEETINGS for attribution.

Produce draft time entries that FILL THE GAPS, and ONLY the gaps. Rules:
- BE FACTUAL. Base every entry strictly on the ACTIVITY feed for that
  window. Never invent work that the activity doesn't show.
- Every entry's time range must fall INSIDE a listed gap and must not
  overlap ALREADY LOGGED windows.
- If a gap (or part of one) is AFK / idle with no meaningful activity, emit
  an entry for it marked as away time: ticket_id null, no_charge true,
  note "AFK - away from keyboard (no activity captured)". Do not fabricate
  billable work for idle time.
- When activity in a gap clearly relates to a ticket (client/profile or
  subject match) or a meeting, attribute it and write a professional note.
  Otherwise ticket_id null (quick time) with a factual note of what the
  activity shows.
- Round to 5 minutes, minimum 10-minute entries, entries non-overlapping."""


def fill_timesheet_gaps(adapter, target: Optional[date_cls] = None) -> dict:
    """Look at the Halo timesheet, find uncovered windows within work hours,
    and propose factual draft entries from the day's activity to fill them
    (AFK gaps become explicit away-time entries). Appends to existing
    drafts; never replaces or touches posted entries."""
    from datetime import datetime as _dtm, time as _tm
    from timescribe import activitywatch as _aw
    target = target or date_cls.today()
    since = _dtm.combine(target, _tm.min)
    until = _dtm.combine(target, _tm.max)
    cfg = appconfig.load()

    def _m(hm):
        try:
            h, mm = map(int, (hm or "").split(":")); return h * 60 + mm
        except (ValueError, AttributeError):
            return None
    def _fmt(x):
        return f"{x // 60:02d}:{x % 60:02d}"

    ts_rows = []
    try:
        ts = adapter.get_day_timesheet(since, until)
        ts_rows = ts.get("rows", [])
    except Exception as exc:
        print(f"[gapfill] timesheet fetch failed: {exc}")

    covered = drafts.covered_windows(target, ts_rows)      # merged occupied windows
    ws = _m(cfg.get("work_start", "09:00")) or 540
    we = _m(cfg.get("work_end", "17:00")) or 1020
    # Gaps = [ws, we] minus covered windows.
    gaps, cur = [], ws
    for s, e in covered:
        s, e = max(s, ws), min(e, we)
        if s > cur:
            gaps.append((cur, min(s, we)))
        cur = max(cur, e)
    if cur < we:
        gaps.append((cur, we))
    gaps = [(s, e) for s, e in gaps if e - s >= 10]
    if not gaps:
        return {"drafts": [], "note": "No gaps in the timesheet for this day."}

    feed = digest_mod.build_signal_feed(since, until)
    afk = _aw.get_inactivity_periods(since, until)
    afk_lines = [f"{a['start'].strftime('%H:%M')}-{a['end'].strftime('%H:%M')}"
                 for a in afk]
    tickets = []
    try:
        tickets = adapter.list_open_tickets()
    except Exception:
        pass
    meeting_lines = []
    try:
        for mt in adapter.get_day_meetings(since, until):
            tag = "Teams" if mt.get("is_teams") else "meeting"
            meeting_lines.append(f"{mt['start']}-{mt['end']} [{tag}] "
                                 f"{('#'+str(mt['ticket_id'])) if mt.get('ticket_id') else ''} "
                                 f"{mt.get('client','')}: {mt.get('subject','')}")
    except Exception:
        pass

    gap_lines = [f"{_fmt(s)}-{_fmt(e)}" for s, e in gaps]
    cov_lines = [f"{_fmt(s)}-{_fmt(e)}" for s, e in covered]
    ticket_lines = [f"[#{t.id}] [{t.client}] {t.subject[:100]}" for t in tickets]
    user_prompt = (
        f"Date: {target.isoformat()}\n\n"
        f"GAPS TO FILL ({len(gap_lines)}):\n" + "\n".join(gap_lines)
        + f"\n\nACTIVITY:\n---\n{feed}\n---\n"
        + (f"\nAFK PERIODS:\n" + "\n".join(afk_lines) if afk_lines else "")
        + (f"\n\nALREADY LOGGED (do not overlap):\n" + "\n".join(cov_lines) if cov_lines else "")
        + (f"\n\nMEETINGS:\n" + "\n".join(meeting_lines) if meeting_lines else "")
        + f"\n\nOPEN TICKETS ({len(ticket_lines)}):\n" + "\n".join(ticket_lines)
        + "\n\nProduce the JSON described in the system prompt."
    )

    resp = generate(system_prompt=prompts.get("gap_fill"), user_prompt=user_prompt,
                    model=_planner_model(cfg), max_tokens=6000,
                    schema=InferenceResponse)

    tmap = {t.id: t for t in tickets}
    new_items = []
    for d in resp.drafts:
        t = tmap.get(d.ticket_id) if d.ticket_id else None
        new_items.append({
            **d.model_dump(),
            "client": t.client if t else "",
            "subject": t.subject if t else ("(AFK)" if d.no_charge and not d.ticket_id
                                            else "(quick time)"),
            "status": "draft",
        })
    # Backstops: only keep entries inside gaps, drop ignored, drop anything
    # overlapping already-logged windows.
    def _in_gap(it):
        s, e = _m(it.get("start_time")), _m(it.get("end_time"))
        if s is None or e is None or e <= s:
            return False
        mid = (s + e) / 2
        return any(gs <= mid < ge for gs, ge in gaps)
    new_items = [it for it in new_items if _in_gap(it)]
    new_items = drafts.filter_ignored(target, new_items)
    new_items = drafts.filter_covered(new_items, covered)

    # Append to existing (don't clobber current drafts); skip ones that
    # overlap an existing unposted draft's window.
    existing = drafts.load(target)
    def _clashes(it):
        s, e = _m(it["start_time"]), _m(it["end_time"])
        for x in existing:
            xs, xe = _m(x.get("start_time")), _m(x.get("end_time"))
            if xs is None or xe is None:
                continue
            if min(e, xe) - max(s, xs) > 0:
                return True
        return False
    added = [it for it in new_items if not _clashes(it)]
    drafts.save(target, existing + added)
    print(f"[gapfill] {len(gaps)} gaps -> proposed {len(resp.drafts)}, added {len(added)}")
    return {"drafts": added, "gaps": gap_lines, "added": len(added)}
