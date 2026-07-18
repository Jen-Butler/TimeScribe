"""Query the local ActivityWatch server (localhost:5600) for window-focus
and AFK events. Gracefully returns [] if AW isn't running."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import httpx

def _aw_base() -> str:
    """Config-aware base URL (supports remote AW hosts via aw_host key)."""
    from timescribe.aw_manager import aw_base_url
    return aw_base_url() + "/api/0"


def _get(path: str, params=None):
    resp = httpx.get(f"{_aw_base()}/{path.lstrip('/')}", params=params,
                     timeout=10, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def is_available() -> bool:
    try:
        _get("info")
        return True
    except Exception:
        return False


def _find_bucket(kind: str) -> Optional[str]:
    try:
        buckets = _get("buckets")
    except Exception:
        return None
    for bid, meta in buckets.items():
        if meta.get("type") == kind:
            return bid
    return None


def get_window_events(since: datetime, until: Optional[datetime] = None) -> List[dict]:
    """Return [{time, duration_s, app, title}] window-focus events."""
    bucket = _find_bucket("currentwindow")
    if not bucket:
        return []
    until = until or datetime.now()
    params = {
        "start": since.astimezone(timezone.utc).isoformat(),
        "end":   until.astimezone(timezone.utc).isoformat(),
        "limit": 5000,
    }
    try:
        events = _get(f"buckets/{bucket}/events", params=params)
    except Exception:
        return []
    out = []
    for e in events:
        data = e.get("data") or {}
        try:
            t = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone()
        except (KeyError, ValueError):
            continue
        out.append({
            "time": t.replace(tzinfo=None),
            "duration_s": float(e.get("duration") or 0),
            "app": data.get("app") or "",
            "title": data.get("title") or "",
        })
    out.sort(key=lambda x: x["time"])
    return out


def get_afk_periods(since: datetime, until: Optional[datetime] = None,
                    min_minutes: int = 5) -> List[dict]:
    """Return [{start, end, minutes}] periods where the user was AFK."""
    bucket = _find_bucket("afkstatus")
    if not bucket:
        return []
    until = until or datetime.now()
    params = {
        "start": since.astimezone(timezone.utc).isoformat(),
        "end":   until.astimezone(timezone.utc).isoformat(),
        "limit": 5000,
    }
    try:
        events = _get(f"buckets/{bucket}/events", params=params)
    except Exception:
        return []
    out = []
    for e in events:
        if (e.get("data") or {}).get("status") != "afk":
            continue
        dur_min = float(e.get("duration") or 0) / 60
        if dur_min < min_minutes:
            continue
        try:
            start = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        out.append({"start": start,
                    "end": start + timedelta(minutes=dur_min),
                    "minutes": round(dur_min)})
    out.sort(key=lambda x: x["start"])
    return out


def get_inactivity_periods(since: datetime, until: Optional[datetime] = None,
                           min_minutes: int = 5) -> List[dict]:
    """Union of (a) explicit AFK events and (b) gaps in window-event
    coverage (machine asleep / locked / AW not running). Overlapping
    periods are merged. Returns [{start, end, minutes}] sorted."""
    until = until or datetime.now()
    periods = [(a["start"], a["end"]) for a in
               get_afk_periods(since, until, min_minutes=min_minutes)]

    win = get_window_events(since, until)
    for a, b in zip(win, win[1:]):
        a_end = a["time"] + timedelta(seconds=a["duration_s"])
        gap_min = (b["time"] - a_end).total_seconds() / 60
        if gap_min >= min_minutes:
            periods.append((a_end, b["time"]))

    if not periods:
        return []
    periods.sort()
    merged = [list(periods[0])]
    for s, e in periods[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [{"start": s, "end": e,
             "minutes": round((e - s).total_seconds() / 60)}
            for s, e in merged
            if (e - s).total_seconds() / 60 >= min_minutes]
