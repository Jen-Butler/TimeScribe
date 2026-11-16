"""Draft time entries: stored per-day, with lifecycle
draft -> approved/rejected -> posted."""
from __future__ import annotations
import json
import threading
import time
import os
import shutil
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


_io_lock = threading.RLock()


def _read_json(p: Path, retries: int = 4):
    """Read + parse, retrying briefly on OS errors (on Windows a file being
    replaced by a concurrent save raises PermissionError for a moment).
    Returns the list, or raises json.JSONDecodeError / OSError."""
    last = None
    for i in range(retries):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except OSError as exc:
            last = exc
            time.sleep(0.05 * (i + 1))
    raise last


def load(d: date_cls) -> List[dict]:
    p = _path(d)
    bak = p.with_name(p.name + ".bak")
    corrupt = p.with_name(p.name + ".corrupt")
    with _io_lock:
        if not p.exists():
            # No live file but a backup/sidelined copy exists: the live file
            # was lost (or moved aside by an earlier false alarm). Recover it
            # rather than reporting "no drafts" and letting the next save
            # overwrite history.
            for src in (corrupt, bak):
                if src.exists():
                    try:
                        items = _read_json(src)
                    except (json.JSONDecodeError, OSError):
                        continue
                    print(f"[drafts] {p.name} missing; recovered {len(items)} drafts from {src.name}")
                    save(d, items)
                    return items
            return []
        try:
            return _read_json(p)
        except OSError as exc:
            # Still unreadable after retries: a lock/AV scan/permission issue,
            # NOT corruption. Serve the backup, leave the live file alone.
            print(f"[drafts] {p.name} temporarily unreadable ({exc}); serving backup")
            if bak.exists():
                try:
                    return _read_json(bak)
                except (json.JSONDecodeError, OSError):
                    pass
            return []
        except json.JSONDecodeError as exc:
            # Genuinely corrupt: preserve it for forensics and fall back.
            print(f"[drafts] {p.name} corrupt ({exc}); sidelining and using backup")
            try:
                p.replace(corrupt)
            except OSError:
                pass
            if bak.exists():
                try:
                    items = _read_json(bak)
                    save(d, items)
                    return items
                except (json.JSONDecodeError, OSError):
                    pass
            return []


def save(d: date_cls, items: List[dict]) -> None:
    """Persist the day's drafts. Atomic (unique temp file + os.replace so a
    crash mid-write can't truncate the file), serialised across threads,
    retried on transient Windows sharing errors, and keeps the previous
    version as a .bak so an accidental wipe/replace is always recoverable."""
    p = _path(d)
    with _io_lock:
        try:
            if p.exists() and p.stat().st_size > 0:
                shutil.copy2(p, p.with_name(p.name + ".bak"))
        except OSError:
            pass
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        last = None
        for i in range(5):
            try:
                os.replace(tmp, p)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.05 * (i + 1))
        try:
            tmp.unlink()
        except OSError:
            pass
        raise last


def restore_backup(d: date_cls) -> List[dict]:
    """Recover a day's drafts from its .bak (the version before the last
    save). Promotes the backup to the live file and returns it."""
    p = _path(d)
    for src in (p.with_name(p.name + ".corrupt"), p.with_name(p.name + ".bak")):
        if src.exists():
            try:
                items = _read_json(src)
            except (json.JSONDecodeError, OSError):
                continue
            save(d, items)
            return items
    return []


def set_status(d: date_cls, index: int, status: str,
               posted_id: Optional[str] = None, extra: Optional[dict] = None) -> dict:
    items = load(d)
    if not (0 <= index < len(items)):
        raise IndexError(f"draft index {index} out of range")
    items[index]["status"] = status
    if posted_id:
        items[index]["posted_id"] = posted_id
    if extra:
        items[index].update(extra)
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
    """A new draft matches an ignored one if its time window substantially
    overlaps the ignored window -- tolerant of the AI shifting times (and
    tickets) between runs. Ignoring means "don't draft this stretch of
    time again", so the ticket is deliberately NOT part of the match."""
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
        igs, ige = _mins(ig.get("start_time")), _mins(ig.get("end_time"))
        if igs is None or ige is None or ige <= igs:
            continue
        overlap = _overlap_min(s, e, igs, ige)
        shorter = min(e - s, ige - igs)
        if shorter > 0 and overlap / shorter >= 0.5:
            return True
    return False


def ignored_windows(d: date_cls) -> List[tuple]:
    """[start_min, end_min) windows the user has dismissed for this day."""
    out = []
    for ig in load_ignored(d):
        s, e = _hm_to_min(ig.get("start_time")), _hm_to_min(ig.get("end_time"))
        if s is not None and e is not None and e > s:
            out.append((s, e))
    return out


def filter_ignored(d: date_cls, items: List[dict]) -> List[dict]:
    """Drop items matching this day's ignore list."""
    ig = load_ignored(d)
    if not ig:
        return items
    return [it for it in items if not is_ignored(it, ig)]


def _hm_to_min(hm: str):
    try:
        h, m = map(int, str(hm).split(":")[:2])
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def covered_windows(d: date_cls, timesheet_rows: Optional[List[dict]] = None) -> List[tuple]:
    """Merged [start_min, end_min) windows already accounted for on this
    day: locally POSTED drafts plus (optionally) the agent's actual Halo
    timesheet rows -- so time entered directly in Halo counts too."""
    wins = []
    for x in load(d):
        if x.get("status") == "posted":
            s, e = _hm_to_min(x.get("start_time")), _hm_to_min(x.get("end_time"))
            if s is not None and e is not None and e > s:
                wins.append((s, e))
    for r in timesheet_rows or []:
        s, e = _hm_to_min(r.get("start")), _hm_to_min(r.get("end"))
        if s is not None and e is not None and e > s:
            wins.append((s, e))
    # merge overlapping/adjacent windows
    wins.sort()
    merged: List[tuple] = []
    for s, e in wins:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def filter_covered(items: List[dict], windows: List[tuple],
                   min_frac: float = 0.5) -> List[dict]:
    """Drop suggested drafts whose time range is mostly (>= min_frac)
    inside windows that are already logged. Stops every digest re-run
    from re-suggesting time the technician has already entered (possibly
    with edited notes/times, so text matching can't catch it)."""
    if not windows:
        return items
    out, dropped = [], 0
    for it in items:
        s, e = _hm_to_min(it.get("start_time")), _hm_to_min(it.get("end_time"))
        if s is None or e is None or e <= s:
            out.append(it)
            continue
        overlap = sum(_overlap_min(s, e, ws, we) for ws, we in windows)
        if overlap >= (e - s) * min_frac:
            dropped += 1
            continue
        out.append(it)
    if dropped:
        print(f"[drafts] dropped {dropped} suggestion(s) already covered by "
              f"posted/timesheet entries")
    return out


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
