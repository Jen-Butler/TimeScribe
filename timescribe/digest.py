"""Merge browser history + ActivityWatch signals and produce a timestamped
activity digest via Claude. Results stored per-day in the app data dir."""
from __future__ import annotations
import json
from datetime import date as date_cls, datetime, time as time_cls, timedelta
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from platformdirs import user_data_dir
from timescribe import appconfig, history, activitywatch
from timescribe.llm import generate

APP_NAME = "timescribe"

SYSTEM_PROMPT = """You are a personal activity logger for an MSP technician. You receive a merged, timestamped feed of:
- Browser visits: `HH:MM [Profile | domain/path] Title`
- Application focus events: `HH:MM [APP appname] Window title (Nm focused)`
- AFK markers: `----- AFK: N minutes -----`

Produce concise, factual activity entries. Return STRICT JSON:
{"entries": [{"time_range": "HH:MM-HH:MM", "summary": "..."}]}

Rules:
- 24-hour times. Entries chronological, non-overlapping.
- Start summaries with a past-tense verb (Reviewed, Configured, Emailed, Built). Never "User ..." or "I ...".
- The [Profile] tag on browser visits is authoritative for client attribution. Name the client when identifiable ("Configured integration runbooks for Contoso on HaloPSA").
- App events reveal non-browser work: Outlook = email, Teams = meetings/chat, Code/VS Code = development, terminal apps = ops work. Merge app + browser signals into one narrative when they're clearly the same task.
- MINE WINDOW TITLES FOR SPECIFICS. Titles usually carry the real content:
  * Outlook: "RE: Server outage - Contoso - Message (HTML)" -> the email subject AND likely client. Say "Emailed about the Contoso server outage", not "worked in Outlook".
  * Teams: "Maria Lopez | Chat | Teams" or a meeting name -> who/what. Name the person or meeting.
  * Word/Excel/PDF: the document filename is the deliverable -> "Edited 'Contoso-SOW-v3.docx'".
  * VS Code/editors: "workflow.py - rewst-integration" -> file and project.
  * If several titles for one app share a client name or topic, that client/topic IS the activity -- name it.
- Only fall back to the generic app name when titles are truly uninformative (e.g. "Inbox - Outlook" with nothing else).
- AFK markers are hard boundaries -- never span an entry across one, and never log the AFK period itself.
- Group continuous same-task activity into single entries. Split when the task/client changes.
- NEVER invent activities or produce generic filler ("Worked on project X"). If signals are unclear: "Browsed unidentified pages on <domain>."
- Skip noise: lock screens, login redirects, task switcher blips (<30s focus).
- Every time_range MUST fall within the window stated in the prompt."""


class Entry(BaseModel):
    time_range: str
    summary: str


class EntriesResponse(BaseModel):
    entries: List[Entry] = Field(default_factory=list)


def data_dir() -> Path:
    p = Path(user_data_dir(APP_NAME)) / "digests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _digest_path(d: date_cls) -> Path:
    return data_dir() / f"{d.isoformat()}.json"


def load_digest(d: date_cls) -> List[dict]:
    p = _digest_path(d)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_digest(d: date_cls, entries: List[dict]) -> None:
    _digest_path(d).write_text(json.dumps(entries, indent=2), encoding="utf-8")


def build_signal_feed(since: datetime, until: datetime) -> str:
    """Merge browser + AW into one chronological text feed for the LLM."""
    cfg = appconfig.load()
    visits = history.read_enabled_history(
        since=since, until=until,
        ignore_prefixes=("chrome-extension://", "edge-extension://",
                         "moz-extension://", "chrome://", "edge://", "about:"),
        enabled_map=cfg.get("browser_profiles", {}),
        exclude_profiles=cfg.get("exclude_profiles", []),
        edge_override=cfg.get("edge_user_data_dir") or None,
    )
    windows = activitywatch.get_window_events(since, until)
    afk = activitywatch.get_inactivity_periods(since, until)

    lines = []  # (sort_time, text)
    for v in visits:
        t = v["time"]
        lines.append((t, f"{t:%H:%M} [{v['profile']} | {v['domain']}] {v['title'][:120]}"))
    for w in windows:
        if w["duration_s"] < 30:
            continue
        t = w["time"]
        mins = round(w["duration_s"] / 60, 1)
        lines.append((t, f"{t:%H:%M} [APP {w['app']}] {w['title'][:120]} ({mins}m focused)"))
    for a in afk:
        lines.append((a["start"], f"----- AFK: {a['minutes']} minutes ({a['start']:%H:%M}-{a['end']:%H:%M}) -----"))

    lines.sort(key=lambda x: x[0])
    feed = "\n".join(text for _, text in lines)
    print(f"[digest] {len(visits)} visits, {len(windows)} window events, "
          f"{len(afk)} AFK periods -> {len(lines)} feed lines")
    return feed


def run_digest(target: Optional[date_cls] = None,
               since: Optional[datetime] = None,
               until: Optional[datetime] = None) -> List[dict]:
    """Run a digest. Default: full target day (today). Returns entries and
    persists them (replacing that day's stored digest for full-day runs)."""
    target = target or date_cls.today()
    full_day = since is None and until is None
    if full_day:
        since = datetime.combine(target, time_cls.min)
        until = datetime.combine(target, time_cls.max)

    feed = build_signal_feed(since, until)
    if not feed.strip():
        print("[digest] no signals in window")
        return []

    win_lo = since.strftime("%H:%M")
    win_hi = until.strftime("%H:%M")
    user_prompt = (
        f"Activity feed for {target.isoformat()} ({win_lo}-{win_hi}):\n"
        f"---\n{feed}\n---\n\n"
        f"WINDOW: all time_ranges must be within {win_lo}-{win_hi}.\n"
        "Produce the JSON described in the system prompt."
    )

    resp = generate(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
                    schema=EntriesResponse, max_tokens=8000)
    entries = [e.model_dump() for e in resp.entries]

    if full_day:
        save_digest(target, entries)
    else:
        existing = load_digest(target)
        existing.extend(entries)
        existing.sort(key=lambda e: e.get("time_range", ""))
        save_digest(target, existing)
    print(f"[digest] {len(entries)} entries written for {target}")
    return entries
