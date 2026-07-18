"""Read Microsoft Edge browsing history across all profiles."""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

WEBKIT_EPOCH_OFFSET = 11644473600


def webkit_to_datetime(us: int) -> datetime:
    return datetime.fromtimestamp((us / 1_000_000) - WEBKIT_EPOCH_OFFSET)


def datetime_to_webkit(dt: datetime) -> int:
    return int((dt.timestamp() + WEBKIT_EPOCH_OFFSET) * 1_000_000)


def default_user_data_dir() -> str:
    return os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")


def _profile_names(user_data: Path) -> Dict[str, str]:
    ls = user_data / "Local State"
    if not ls.exists():
        return {}
    try:
        data = json.loads(ls.read_text(encoding="utf-8", errors="replace"))
        cache = (data.get("profile") or {}).get("info_cache") or {}
        return {k: (v.get("name") or k) for k, v in cache.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError):
        return {}


def discover_profiles(user_data_dir: str) -> List[Tuple[str, str, Path]]:
    ud = Path(os.path.expandvars(user_data_dir or default_user_data_dir()))
    if not ud.exists():
        raise FileNotFoundError(f"Edge User Data not found at {ud}")
    names = _profile_names(ud)
    out = []
    for child in sorted(ud.iterdir()):
        if not child.is_dir():
            continue
        hp = child / "History"
        if hp.exists():
            out.append((child.name, names.get(child.name, child.name), hp))
    return out


def _read_one(history_path: Path, since: datetime, until: Optional[datetime],
              ignore_prefixes, profile: str, dedupe_seconds: int = 5) -> List[dict]:
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copy2(history_path, tmp)
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        q = ("SELECT v.visit_time, u.url, u.title, v.visit_duration "
             "FROM visits v JOIN urls u ON v.url = u.id WHERE v.visit_time > ?")
        args = [datetime_to_webkit(since)]
        if until:
            q += " AND v.visit_time <= ?"
            args.append(datetime_to_webkit(until))
        q += " ORDER BY v.visit_time ASC"
        visits = []
        last_seen: Dict[str, datetime] = {}
        for row in conn.execute(q, args):
            url = row["url"] or ""
            if not url or any(url.startswith(p) for p in (ignore_prefixes or [])):
                continue
            t = webkit_to_datetime(row["visit_time"])
            prev = last_seen.get(url)
            if prev and (t - prev).total_seconds() < dedupe_seconds:
                continue
            last_seen[url] = t
            visits.append({
                "time": t, "url": url, "title": row["title"] or "",
                "domain": urlparse(url).netloc or "(unknown)",
                "profile": profile,
            })
        conn.close()
        return visits
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def read_all_history(user_data_dir: str, since: datetime,
                     until: Optional[datetime] = None,
                     ignore_prefixes: Iterable[str] = (),
                     exclude_profiles: Iterable[str] = ()) -> List[dict]:
    excl = {s.strip().lower() for s in (exclude_profiles or []) if s.strip()}
    merged = []
    counts = []
    for folder, friendly, hp in discover_profiles(user_data_dir):
        if folder.lower() in excl or friendly.lower() in excl:
            continue
        try:
            v = _read_one(hp, since, until, ignore_prefixes, friendly)
        except (OSError, sqlite3.Error):
            continue
        merged.extend(v)
        counts.append(f"{friendly}={len(v)}")
    merged.sort(key=lambda x: x["time"])
    print(f"[history] profiles: {', '.join(counts) or '(none)'}")
    return merged
