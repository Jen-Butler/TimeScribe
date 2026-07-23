"""Local FastAPI backend. Binds 127.0.0.1 only."""
from __future__ import annotations
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from timescribe import appconfig
from timescribe.psa.halo import HaloPSAAdapter

app = FastAPI(title="TimeScribe Desktop", docs_url=None, redoc_url=None)

_adapter: Optional[HaloPSAAdapter] = None
_oauth_lock = threading.Lock()


def get_adapter() -> Optional[HaloPSAAdapter]:
    global _adapter
    cfg = appconfig.load()
    if not cfg.get("halo_base_url") or not cfg.get("halo_client_id"):
        return None
    if (_adapter is None
            or _adapter.base_url != cfg["halo_base_url"].rstrip("/")
            or _adapter.client_id != cfg["halo_client_id"]):
        _adapter = HaloPSAAdapter(
            base_url=cfg["halo_base_url"],
            client_id=cfg["halo_client_id"],
        )
    return _adapter


# ---------- UI ----------

def _ui_path() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        # PyInstaller: data files land under sys._MEIPASS (onefile) or the
        # exe dir (onedir). We add ui/ via the spec's datas.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "ui" / "index.html"
    return Path(__file__).parent / "ui" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index():
    return _ui_path().read_text(encoding="utf-8")


# ---------- API ----------

class ConfigUpdate(BaseModel):
    halo_base_url: Optional[str] = None
    halo_client_id: Optional[str] = None
    timezone: Optional[str] = None
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    exclude_profiles: Optional[list] = None
    llm_provider: Optional[str] = None      # "anthropic" | "openai"
    aw_host: Optional[str] = None
    digest_weekdays: Optional[list] = None      # [0..6], Mon=0
    digest_interval_minutes: Optional[int] = None
    combined_digest_drafts: Optional[bool] = None
    browser_profiles: Optional[dict] = None     # "browserkey::folder" -> bool


class SecretUpdate(BaseModel):
    name: str          # "anthropic_api_key"
    value: str


@app.get("/api/status")
def status():
    cfg = appconfig.load()
    a = get_adapter()
    halo_connected = False
    agent = None
    if a and a.is_authenticated():
        halo_connected = True
        try:
            rec = a.get_current_agent()
            agent = {"id": rec.get("id"), "name": rec.get("name")}
        except Exception:
            agent = None
    return {
        "halo_configured": bool(cfg.get("halo_base_url") and cfg.get("halo_client_id")),
        "halo_connected": halo_connected,
        "agent": agent,
        "anthropic_key_set": appconfig.get_secret("anthropic_api_key") is not None,
        "openai_key_set": appconfig.get_secret("openai_api_key") is not None,
        "org_ai_key_set": appconfig.get_secret("org_ai_key") is not None,
        "llm_provider": cfg.get("llm_provider", "anthropic"),
        "config": {k: v for k, v in cfg.items() if "key" not in k.lower()},
    }


@app.post("/api/config")
def update_config(body: ConfigUpdate):
    cfg = appconfig.load()
    for k, v in body.model_dump(exclude_none=True).items():
        cfg[k] = v
    appconfig.save(cfg)
    return {"ok": True}


@app.post("/api/secret")
def set_secret(body: SecretUpdate):
    if body.name not in ("anthropic_api_key", "openai_api_key"):
        raise HTTPException(400, "unknown secret name")
    appconfig.set_secret(body.name, body.value.strip())
    return {"ok": True}


@app.post("/api/org_ai/fetch")
def org_ai_fetch():
    """Pull the org-shared AI key from the encrypted Halo custom field and
    cache it locally (Credential Manager). Techs run this once after
    connecting; the key is never shown."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    cfg = appconfig.load()
    cid = int(cfg.get("org_ai_client_id") or 0)
    if not cid:
        raise HTTPException(400, "No org AI client id configured yet")
    key = a.get_org_ai_key(cid, cfg.get("org_ai_field_name", "CFTimeScribeAIKey"))
    if not key:
        raise HTTPException(404, "No org AI key found on that client "
                            "(admin hasn't published one, or it's not readable)")
    appconfig.set_secret("org_ai_key", key)
    return {"ok": True, "provider": cfg.get("org_ai_provider", "openai")}


class OrgAIPublish(BaseModel):
    client_id: int
    field_name: Optional[str] = None
    provider: Optional[str] = None
    key: str


@app.post("/api/org_ai/publish")
def org_ai_publish(body: OrgAIPublish):
    """Admin helper: store the org AI key into the encrypted custom field on
    the given Halo client, and record the client/provider in config."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    cfg = appconfig.load()
    field = body.field_name or cfg.get("org_ai_field_name", "CFTimeScribeAIKey")
    try:
        a.publish_org_ai_key(body.client_id, field, body.key.strip())
    except Exception as exc:
        raise HTTPException(502, str(exc))
    cfg["org_ai_client_id"] = body.client_id
    cfg["org_ai_field_name"] = field
    if body.provider:
        cfg["org_ai_provider"] = body.provider
    appconfig.save(cfg)
    return {"ok": True}


@app.post("/api/halo/connect")
def halo_connect():
    a = get_adapter()
    if a is None:
        raise HTTPException(400, "Set Halo base URL and Client ID first")
    if not _oauth_lock.acquire(blocking=False):
        raise HTTPException(409, "OAuth flow already in progress")
    try:
        a.connect()   # opens browser, blocks until callback or timeout
        rec = a.get_current_agent()
        return {"ok": True, "agent": {"id": rec.get("id"), "name": rec.get("name")}}
    except Exception as exc:
        raise HTTPException(500, f"OAuth failed: {exc}")
    finally:
        _oauth_lock.release()


@app.get("/api/tickets")
def tickets(limit: int = 25):
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    items = a.list_open_tickets()
    return {"count": len(items),
            "tickets": [
                {"id": t.id, "client": t.client, "subject": t.subject,
                 "status": t.status}
                for t in items[:limit]
            ]}


@app.get("/api/browsers")
def browsers_list():
    """Installed Chromium browsers and their profiles, with enabled state,
    for the Settings browser-source picker."""
    from timescribe import history as _history
    cfg = appconfig.load()
    edge_override = cfg.get("edge_user_data_dir") or None
    enabled = cfg.get("browser_profiles", {})
    excl = {s.strip().lower() for s in cfg.get("exclude_profiles", []) if s.strip()}
    grouped = {}
    for key, name, folder, friendly, hp in _history.discover_all_sources(edge_override):
        sid = f"{key}::{folder}"
        default = not (folder.lower() in excl or friendly.lower() in excl)
        grouped.setdefault(key, {"key": key, "browser": name, "profiles": []})
        grouped[key]["profiles"].append({
            "sid": sid, "folder": folder, "name": friendly,
            "enabled": enabled.get(sid, default),
        })
    return {"browsers": list(grouped.values())}


@app.get("/api/ticket")
def ticket_detail(id: int):
    """One ticket's detail, for the timesheet click-through panel."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    try:
        return a.get_ticket(id)
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ---------- Activity / digest ----------

from datetime import date as _date
from timescribe import digest as _digest
from timescribe import activitywatch as _aw


@app.get("/api/activity/status")
def activity_status():
    return {
        "activitywatch": _aw.is_available(),
    }


@app.post("/api/digest/run")
def digest_run(day: str = None, start: str = None, end: str = None):
    """Run a digest. Optional start/end (HH:MM) restrict to a window
    within the day; otherwise the whole day is digested."""
    from datetime import datetime as _dtt, time as _time
    target = _date.fromisoformat(day) if day else _date.today()
    since = until = None
    if start or end:
        s = _dtt.strptime(start or "00:00", "%H:%M").time()
        e = _dtt.strptime(end or "23:59", "%H:%M").time()
        since = _dtt.combine(target, s)
        until = _dtt.combine(target, e)
    try:
        entries = _digest.run_digest(target, since=since, until=until)
        return {"ok": True, "count": len(entries), "entries": entries}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/digest")
def digest_get(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "entries": _digest.load_digest(target)}


# ---------- Time entry drafts ----------

from datetime import datetime as _dt
from timescribe import inference as _inference
from timescribe import drafts as _drafts
from timescribe.psa.adapter import TimeEntry as _TimeEntry


class DraftAction(BaseModel):
    index: int
    action: str = ""     # "approve" | "reject" (unused for split)


@app.post("/api/drafts/generate")
def drafts_generate(day: str = None):
    """Import the day's digest entries directly as drafts -- NO second LLM
    pass. The digest summaries become the notes; ticket assignment is the
    technician's call (or use /api/drafts/suggest for AI matching)."""
    target = _date.fromisoformat(day) if day else _date.today()
    entries = _digest.load_digest(target)
    if not entries:
        return {"drafts": [], "error": "No digest for this day. Run the digest first."}
    existing = _drafts.load(target)
    posted = [x for x in existing if x.get("status") == "posted"]
    new_items = []
    for e in entries:
        tr = (e.get("time_range") or "").split("-")
        if len(tr) != 2:
            continue
        new_items.append({
            "ticket_id": None,
            "start_time": tr[0].strip(),
            "end_time": tr[1].strip(),
            "note": e.get("summary", ""),
            "confidence": 0.0,
            "client": "", "subject": "(unassigned)",
            "status": "draft",
        })
    new_items = _drafts.filter_ignored(target, new_items)
    _drafts.save(target, posted + new_items)
    return {"drafts": new_items, "preserved_posted": len(posted)}


@app.post("/api/drafts/suggest")
def drafts_suggest(day: str = None):
    """AI pass: raw activity + open tickets -> digest + ticket-matched
    drafts in one combined LLM call (refreshes both). Falls back to the
    classic digest-then-match if combined mode is disabled."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    cfg = appconfig.load()
    try:
        if cfg.get("combined_digest_drafts", True):
            return _inference.build_combined(a, target)
        return _inference.generate_drafts(a, target)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/drafts/add")
def drafts_add(day: str = None):
    """Add a blank manual draft (works in every mode, incl. MCP-only)."""
    from datetime import datetime as _now_dt
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    now = _now_dt.now()
    start_m = (now.hour * 60 + now.minute) // 5 * 5
    end_m = min(start_m + 30, 23 * 60 + 55)
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    items.append({
        "ticket_id": None,
        "start_time": fmt(start_m),
        "end_time": fmt(end_m),
        "note": "",
        "confidence": 0.0,
        "client": "", "subject": "(manual entry)",
        "status": "draft",
    })
    _drafts.save(target, items)
    return {"ok": True, "index": len(items) - 1}


@app.post("/api/drafts/split")
def drafts_split(body: DraftAction, day: str = None):
    """Split a draft into two halves at its midpoint (rounded to 5 min)."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") == "posted":
        raise HTTPException(400, "cannot split a posted entry")
    try:
        sh, sm = map(int, item["start_time"].split(":"))
        eh, em = map(int, item["end_time"].split(":"))
    except (ValueError, KeyError):
        raise HTTPException(400, "draft has malformed times")
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if end_m - start_m < 10:
        raise HTTPException(400, "entry too short to split (needs >=10 min)")
    mid_m = start_m + round((end_m - start_m) / 2 / 5) * 5
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    first = dict(item, end_time=fmt(mid_m), status="draft")
    second = dict(item, start_time=fmt(mid_m), status="draft")
    items[body.index] = first
    items.insert(body.index + 1, second)
    _drafts.save(target, items)
    return {"ok": True, "count": len(items)}


@app.get("/api/drafts")
def drafts_get(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "drafts": _drafts.load(target)}


@app.post("/api/drafts/action")
def drafts_action(body: DraftAction, day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    status = {"approve": "approved", "reject": "rejected"}.get(body.action)
    if not status:
        raise HTTPException(400, "action must be approve or reject")
    return _drafts.set_status(target, body.index, status)


@app.post("/api/drafts/delete")
def drafts_delete(body: DraftAction, day: str = None):
    """Remove an entry from the local list. Does NOT touch anything
    already created in the PSA -- delete that in Halo if needed."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    if items[body.index].get("status") == "posted":
        raise HTTPException(400, "posted entries can't be deleted; they live in the Posted tab")
    removed = items.pop(body.index)
    _drafts.save(target, items)
    return {"ok": True, "removed": removed.get("note", "")[:60], "count": len(items)}


class DraftMerge(BaseModel):
    anchor_index: int      # entry that absorbs (keeps its ticket id)
    other_index: int       # entry merged in and removed


@app.post("/api/drafts/merge")
def drafts_merge(body: DraftMerge, day: str = None):
    """Merge `other` into `anchor`: append its note, union the time span,
    keep the anchor's ticket/client. The anchor keeps its identity; the
    other entry is removed. Neither may be posted."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    ai, oi = body.anchor_index, body.other_index
    if not (0 <= ai < len(items)) or not (0 <= oi < len(items)) or ai == oi:
        raise HTTPException(400, "bad merge indices")
    anchor, other = items[ai], items[oi]
    if anchor.get("status") == "posted" or other.get("status") == "posted":
        raise HTTPException(400, "can't merge posted entries")

    def _m(hm):
        try:
            h, m = map(int, (hm or "").split(":")); return h * 60 + m
        except (ValueError, AttributeError):
            return None
    def _fmt(x):
        return f"{x // 60:02d}:{x % 60:02d}"

    starts = [v for v in (_m(anchor.get("start_time")), _m(other.get("start_time"))) if v is not None]
    ends = [v for v in (_m(anchor.get("end_time")), _m(other.get("end_time"))) if v is not None]
    if starts:
        anchor["start_time"] = _fmt(min(starts))
    if ends:
        anchor["end_time"] = _fmt(max(ends))
    # Append notes (skip duplicates / empties).
    def _join(a, b):
        a, b = (a or "").strip(), (b or "").strip()
        if not b or b in a:
            return a
        return (a + "\n" + b).strip()
    anchor["note"] = _join(anchor.get("note"), other.get("note"))
    anchor["private_note"] = _join(anchor.get("private_note"), other.get("private_note"))
    # Absorbing means this is now hand-curated: full confidence, keep draft.
    anchor["confidence"] = max(anchor.get("confidence", 0) or 0,
                               other.get("confidence", 0) or 0)
    anchor["status"] = "draft"
    items.pop(oi)
    _drafts.save(target, items)
    # anchor index shifts down by 1 if the removed item was before it
    new_anchor = ai - 1 if oi < ai else ai
    return {"ok": True, "anchor_index": new_anchor, "count": len(items)}


@app.post("/api/drafts/ignore")
def drafts_ignore(body: DraftAction, day: str = None):
    """Remove an entry AND remember it, so future digests don't resurface
    the same activity. Only for unposted entries."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    if items[body.index].get("status") == "posted":
        raise HTTPException(400, "can't ignore a posted entry")
    ignored = items.pop(body.index)
    _drafts.add_ignored(target, ignored)
    _drafts.save(target, items)
    return {"ok": True, "count": len(items)}


@app.get("/api/drafts/ignored")
def drafts_ignored_list(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "ignored": _drafts.load_ignored(target)}


@app.post("/api/drafts/unignore")
def drafts_unignore(body: DraftAction, day: str = None):
    """Remove an entry from the day's ignore list (index into that list)."""
    target = _date.fromisoformat(day) if day else _date.today()
    ig = _drafts.load_ignored(target)
    if not (0 <= body.index < len(ig)):
        raise HTTPException(404, "ignored index out of range")
    ig.pop(body.index)
    _drafts._ignored_path(target).write_text(__import__("json").dumps(ig, indent=2),
                                             encoding="utf-8")
    return {"ok": True, "count": len(ig)}


class DraftUpdate(BaseModel):
    index: int
    ticket_id: Optional[int] = None      # None/blank -> Quick Time
    note: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    no_charge: Optional[bool] = None     # override ticket rate -> no charge
    private_note: Optional[str] = None   # agent-only note on the action


@app.post("/api/drafts/update")
def drafts_update(body: DraftUpdate, day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") == "posted":
        raise HTTPException(400, "cannot edit a posted entry")
    item["ticket_id"] = body.ticket_id          # explicit None clears it
    if body.note is not None:
        item["note"] = body.note
    if body.no_charge is not None:
        item["no_charge"] = body.no_charge
    if body.private_note is not None:
        item["private_note"] = body.private_note
    if body.start_time:
        item["start_time"] = body.start_time
    if body.end_time:
        item["end_time"] = body.end_time
    # Re-resolve display fields for the assigned ticket
    a = get_adapter()
    item["client"] = ""
    item["subject"] = "(quick time)" if not body.ticket_id else "?"
    if body.ticket_id and a and a.is_authenticated():
        try:
            for t in a.list_open_tickets():
                if t.id == body.ticket_id:
                    item["client"], item["subject"] = t.client, t.subject
                    break
        except Exception:
            pass
    _drafts.save(target, items)
    return item


@app.post("/api/drafts/post")
def drafts_post(day: str = None):
    """Post all APPROVED drafts to the PSA."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    results = []
    for i, item in enumerate(items):
        if item.get("status") != "approved":
            continue
        try:
            posted_id = a.create_time_entry(_entry_from_item(item, target))
            _drafts.set_status(target, i, "posted", posted_id=posted_id)
            results.append({"index": i, "ok": True, "posted_id": posted_id})
        except Exception as exc:
            results.append({"index": i, "ok": False, "error": str(exc)})
    return {"results": results}


def _entry_from_item(item: dict, target) -> "_TimeEntry":
    """Build a TimeEntry from a stored draft item, applying the no-charge
    override (chargerate 0, configurable via halo_nocharge_rate) and any
    private note."""
    cfg = appconfig.load()
    tid = item.get("ticket_id")
    charge_rate = None
    if item.get("no_charge"):
        charge_rate = float(cfg.get("halo_nocharge_rate", 0))
    return _TimeEntry(
        ticket_id=int(tid) if tid else None,
        start_local=_dt.combine(target, _dt.strptime(item["start_time"], "%H:%M").time()),
        end_local=_dt.combine(target, _dt.strptime(item["end_time"], "%H:%M").time()),
        note=item["note"],
        charge_rate=charge_rate,
        billable=not item.get("no_charge"),
        private_note=item.get("private_note") or None,
    )


@app.post("/api/drafts/repost")
def drafts_repost(body: DraftUpdate, day: str = None):
    """Re-post an already-posted entry to the PSA, applying any edits sent
    with the request first. Creates a NEW entry in the PSA and records its
    id; it does not delete the previous one."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") != "posted":
        raise HTTPException(400, "only posted entries can be re-posted")
    # Apply edits from the request (posted entries are otherwise locked).
    item["ticket_id"] = body.ticket_id
    if body.note is not None:
        item["note"] = body.note
    if body.no_charge is not None:
        item["no_charge"] = body.no_charge
    if body.private_note is not None:
        item["private_note"] = body.private_note
    if body.start_time:
        item["start_time"] = body.start_time
    if body.end_time:
        item["end_time"] = body.end_time
    # Refresh client/subject display for the (possibly new) ticket.
    item["subject"] = "(quick time)" if not body.ticket_id else item.get("subject", "?")
    if body.ticket_id:
        try:
            for t in a.list_open_tickets():
                if t.id == body.ticket_id:
                    item["client"], item["subject"] = t.client, t.subject
                    break
        except Exception:
            pass
    # Was the previous Halo action still there, or had it been deleted in
    # Halo before this repost? Reporting this avoids surprise duplicates.
    prev_id = item.get("posted_id")
    prev_exists = None
    try:
        prev_exists = a.action_exists(prev_id)
    except Exception:
        prev_exists = None
    try:
        posted_id = a.create_time_entry(_entry_from_item(item, target))
    except Exception as exc:
        # Pass Halo's actual complaint to the UI instead of a bare 500.
        raise HTTPException(502, str(exc))
    item["posted_id"] = posted_id
    item["status"] = "posted"
    _drafts.save(target, items)
    return {"ok": True, "posted_id": posted_id,
            "previous_id": prev_id, "previous_exists": prev_exists}


class ActionRef(BaseModel):
    action_id: str


@app.post("/api/halo/delete_action")
def halo_delete_action(body: ActionRef):
    """Delete a Halo action by id -- used to remove a copy posted to the
    wrong ticket after reposting it to the right one."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    try:
        a.delete_action(body.action_id)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True}


@app.get("/api/timesheet")
def timesheet(day: str = None):
    """What Halo already has on the logged-in agent's timesheet for a day."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    from datetime import time as _time
    try:
        ts = a.get_day_timesheet(_dt.combine(target, _time.min),
                                 _dt.combine(target, _time.max))
    except Exception as exc:
        raise HTTPException(502, str(exc))
    ts["rows"].sort(key=lambda r: r.get("start") or "99")
    # Split logged time into billable vs no-charge from each row's charge
    # type. Derive no-charge as (logged - billable) so the parts always sum
    # to the displayed logged total -- avoids independent-rounding drift.
    billable = sum((r.get("hours") or 0) for r in ts["rows"]
                   if "no charge" not in (r.get("charge_type") or "").lower())
    logged = ts.get("actual_hours")
    if logged is None:
        logged = sum((r.get("hours") or 0) for r in ts["rows"])
    ts["billable_hours"] = round(billable, 2)
    ts["nonbillable_hours"] = round(max(0.0, round(logged, 2) - ts["billable_hours"]), 2)
    base = (appconfig.load().get("halo_base_url") or "").rstrip("/")
    return {"day": target.isoformat(), "halo_base_url": base, **ts}


@app.get("/api/timeline")
def timeline(day: str = None):
    """Raw activity data for one day as minutes-from-midnight, for the
    timeline overlay: window focus, browser visits, AFK, and the digest."""
    from datetime import date as _d, datetime as _dtm, time as _time, timedelta as _timedelta
    from timescribe import history as _history
    target = _d.fromisoformat(day) if day else _d.today()
    since = _dtm.combine(target, _time.min)
    until = _dtm.combine(target, _time.max)

    def mins(dt):
        return round(dt.hour * 60 + dt.minute + dt.second / 60, 2)

    cfg = appconfig.load()
    try:
        visits = _history.read_enabled_history(
            since=since, until=until,
            ignore_prefixes=("chrome-extension://", "edge-extension://",
                             "moz-extension://", "chrome://", "edge://", "about:"),
            enabled_map=cfg.get("browser_profiles", {}),
            exclude_profiles=cfg.get("exclude_profiles", []),
            edge_override=cfg.get("edge_user_data_dir") or None)
    except Exception as exc:
        print(f"[timeline] history read failed: {exc}")
        visits = []
    windows = _aw.get_window_events(since, until)
    afk = _aw.get_inactivity_periods(since, until)

    win_out = []
    for w in windows:
        end = w["time"] + _timedelta(seconds=w["duration_s"])
        win_out.append({"start": mins(w["time"]), "end": mins(end),
                        "app": w["app"], "title": w["title"],
                        "clock": w["time"].strftime("%H:%M")})
    vis_out = [{"t": mins(v["time"]), "domain": v["domain"],
                "title": v["title"], "profile": v["profile"],
                "clock": v["time"].strftime("%H:%M")} for v in visits]
    afk_out = [{"start": mins(a["start"]), "end": mins(a["end"]),
                "minutes": a["minutes"]} for a in afk]

    digest_out = []
    for e in _digest.load_digest(target):
        tr = (e.get("time_range") or "").split("-")
        if len(tr) == 2:
            try:
                sh, sm = map(int, tr[0].strip().split(":"))
                eh, em = map(int, tr[1].strip().split(":"))
                digest_out.append({"start": sh * 60 + sm, "end": eh * 60 + em,
                                   "summary": e.get("summary", "")})
            except ValueError:
                pass

    span = ([w["start"] for w in win_out] + [w["end"] for w in win_out]
            + [v["t"] for v in vis_out])
    lo = min(span) if span else 8 * 60
    hi = max(span) if span else 18 * 60
    return {"day": target.isoformat(),
            "start_min": max(0, int(lo // 60 * 60)),
            "end_min": min(1440, int(-(-hi // 60) * 60)),
            "windows": win_out, "visits": vis_out,
            "afk": afk_out, "digest": digest_out}


# ---------- Logs ----------

@app.get("/api/logs")
def get_logs(lines: int = 200):
    """Tail of app.log for the Settings > Logs view."""
    from pathlib import Path
    from platformdirs import user_data_dir
    log_path = Path(user_data_dir("timescribe")) / "logs" / "app.log"
    if not log_path.exists():
        return {"path": str(log_path), "lines": ["(no log file yet)"]}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return {"path": str(log_path), "lines": [f"(could not read log: {exc})"]}
    lines = max(10, min(lines, 2000))
    return {"path": str(log_path), "lines": [l.rstrip("\n") for l in all_lines[-lines:]]}


# ---------- MCP config helper ----------

@app.get("/api/mcp/config")
def mcp_config():
    """Ready-to-paste MCP registration for Claude Desktop / Cowork / Claude Code."""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        command = _sys.executable
        args = ["mcp"]
    else:
        # The tray app runs under pythonw.exe (no console window), but MCP
        # servers must use python.exe: Claude Desktop talks to the server
        # over stdio, and pythonw detaches from it -> "Server disconnected".
        command = _sys.executable
        if command.lower().endswith("pythonw.exe"):
            command = command[:-len("pythonw.exe")] + "python.exe"
        args = ["-m", "timescribe.mcp_server"]
    entry = {"command": command, "args": args}
    import json as _json
    return {
        "server_name": "timescribe-activity",
        "entry": entry,
        "claude_desktop_json": _json.dumps(
            {"mcpServers": {"timescribe-activity": entry}}, indent=2),
        "claude_code_cmd": ("claude mcp add timescribe-activity --scope user -- "
                            + command + " " + " ".join(args)),
    }


# ---------- Posted log + summary ----------

@app.get("/api/posted")
def posted(days: int = 14):
    cfg = appconfig.load()
    base = (cfg.get("halo_base_url") or "").rstrip("/")
    items = _drafts.posted_log(days_back=days)
    for it in items:
        tid = it.get("ticket_id")
        it["halo_url"] = f"{base}/tickets?id={tid}" if (base and tid) else None
    return {"days": days, "count": len(items), "entries": items}


@app.get("/api/summary")
def summary(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(),
            **_drafts.day_summary(target, _digest.load_digest(target))}


# ---------- Scheduler ----------

from timescribe import scheduler as _scheduler


@app.get("/api/scheduler/status")
def scheduler_status():
    return _scheduler.status()
