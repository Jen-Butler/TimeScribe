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
