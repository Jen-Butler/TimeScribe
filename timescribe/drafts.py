"""Draft time entries: stored per-day, with lifecycle
draft -> approved/rejected -> posted."""
from __future__ import annotations
import json
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import List, Optional

from platformdirs import user_data_dir

APP_NAME = "timescribe"


def _dir() -> Path:
    p = Path(user_data_dir(APP_NAME)) / "drafts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(d: date_cls) -> Path:
    return _dir() / f"{d.isoformat()}.json"


def load(d: date_cls) -> List[dict]:
    p = _path(d)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save(d: date_cls, items: List[dict]) -> None:
    _path(d).write_text(json.dumps(items, indent=2), encoding="utf-8")


def set_status(d: date_cls, index: int, status: str,
               posted_id: Optional[str] = None) -> dict:
    items = load(d)
    if not (0 <= index < len(items)):
        raise IndexError(f"draft index {index} out of range")
    items[index]["status"] = status
    if posted_id:
        items[index]["posted_id"] = posted_id
    save(d, items)
    return items[index]


def _ignored_path(d: date_cls) -> Path:
    return _dir() / f"{d.isoformat()}.ignored.json"


def load_ignored(d: date_cls) -> List[dict]:
    p = _ignored_path(d)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def add_ignored(d: date_cls, item: dict) -> None:
    """Remember an entry the user dismissed so regenerated digests don't
    resurface it. We store a light signature (time window + ticket)."""
    sig = {
        "start_time": item.get("start_time", ""),
        "end_time": item.get("end_time", ""),
        "ticket_id": item.get("ticket_id"),
        "note": (item.get("note") or "")[:80],
    }
    ig = load_ignored(d)
    ig.append(sig)
    _ignored_path(d).write_text(json.dumps(ig, indent=2), encoding="utf-8")


def _overlap_min(a_s, a_e, b_s, b_e) -> int:
    lo, hi = max(a_s, b_s), min(a_e, b_e)
    return max(0, hi - lo)


def is_ignored(item: dict, ignored: List[dict]) -> bool:
    """A new draft matches an ignored one if it's for the same ticket
    (both None counts as same) and its time window substantially overlaps
    the ignored window -- tolerant of the AI shifting times between runs."""
    def _mins(hm):
        try:
            h, m = map(int, (hm or "").split(":"))
            return h * 60 + m
        except (ValueError, AttributeError):
            return None
    s, e = _mins(item.get("start_time")), _mins(item.get("end_time"))
    if s is None or e is None or e <= s:
        return False
    for ig in ignored:
        if (ig.get("ticket_id") or None) != (item.get("ticket_id") or None):
            continue
        igs, ige = _mins(ig.get("start_time")), _mins(ig.get("end_time"))
        if igs is None or ige is None or ige <= igs:
            continue
        overlap = _overlap_min(s, e, igs, ige)
        shorter = min(e - s, ige - igs)
        if shorter > 0 and overlap / shorter >= 0.5:
            return True
    return False


def filter_ignored(d: date_cls, items: List[dict]) -> List[dict]:
    """Drop items matching this day's ignore list."""
    ig = load_ignored(d)
    if not ig:
        return items
    return [it for it in items if not is_ignored(it, ig)]


def _minutes(start_hm: str, end_hm: str) -> int:
    try:
        sh, sm = map(int, start_hm.split(":"))
        eh, em = map(int, end_hm.split(":"))
        return max(0, (eh * 60 + em) - (sh * 60 + sm))
    except (ValueError, AttributeError):
        return 0


def posted_log(days_back: int = 14) -> List[dict]:
    """All POSTED entries across the last N days, newest day first."""
    out = []
    today = date_cls.today()
    for offset in range(days_back + 1):
        d = today - timedelta(days=offset)
        for i, item in enumerate(load(d)):
            if item.get("status") == "posted":
                out.append({**item, "day": d.isoformat(), "index": i,
                            "minutes": _minutes(item.get("start_time", ""),
                                                 item.get("end_time", ""))})
    return out


def day_summary(d: date_cls, digest_entries: List[dict]) -> dict:
    """Totals for the summary strip: captured / drafted / approved / posted."""
    def tr_minutes(tr: str) -> int:
        bits = (tr or "").split("-")
        return _minutes(bits[0].strip(), bits[1].strip()) if len(bits) == 2 else 0

    captured = sum(tr_minutes(e.get("time_range", "")) for e in digest_entries)
    items = load(d)
    by_status = {"draft": 0, "approved": 0, "rejected": 0, "posted": 0}
    for it in items:
        m = _minutes(it.get("start_time", ""), it.get("end_time", ""))
        by_status[it.get("status", "draft")] = by_status.get(it.get("status", "draft"), 0) + m
    return {
        "captured_minutes": captured,
        "draft_minutes": by_status["draft"],
        "approved_minutes": by_status["approved"],
        "posted_minutes": by_status["posted"],
        "unbilled_minutes": max(0, captured - by_status["posted"] - by_status["approved"]),
    }
