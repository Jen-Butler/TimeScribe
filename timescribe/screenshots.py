"""Local screenshot recorder (Steps-Recorder style) for reviewing what was
on screen during a time entry.

Entirely local and OPT-IN (off by default): screenshots are never uploaded
to Halo or anywhere else. Captures the moment the focused window changes
(debounced so alt-tab flicker doesn't spam) plus a periodic shot every N
minutes so a long stretch in one app is still covered. Skips capture while
the user is idle (no input for `screenshots_idle_skip_s`) and never records
the dashboard itself. Images are downscaled JPEGs under
<data>/screenshots/YYYY-MM-DD/ with a per-day index.json, and anything
older than the retention window is purged automatically.

Windows-only foreground/idle detection via ctypes (no extra dependencies).
"""
from __future__ import annotations
import ctypes
import json
import os
import shutil
import threading
import time
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from platformdirs import user_data_dir

from timescribe import appconfig

APP_NAME = "timescribe"

_state = {"running": False, "paused": False, "last_capture": None,
          "count_today": 0, "last_purge": 0.0, "last_error": None}
_lock = threading.Lock()


# ---------- storage ----------

def _dir() -> Path:
    p = Path(user_data_dir(APP_NAME)) / "screenshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _day_dir(d: date_cls) -> Path:
    p = _dir() / d.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_path(d: date_cls) -> Path:
    return _day_dir(d) / "index.json"


def load_index(d: date_cls) -> List[dict]:
    p = _index_path(d)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_index(d: date_cls, items: List[dict]) -> None:
    p = _index_path(d)
    tmp = p.with_name("index.json.tmp")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    os.replace(tmp, p)


def file_path(d: date_cls, name: str) -> Optional[Path]:
    """Resolve a screenshot file for serving; only names in the day's index
    are valid (blocks path traversal)."""
    if not any(it.get("file") == name for it in load_index(d)):
        return None
    p = (_day_dir(d) / name).resolve()
    if not str(p).startswith(str(_dir().resolve())) or not p.exists():
        return None
    return p


def storage_bytes() -> int:
    root = _dir()
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def purge_old(retention_days: int) -> int:
    """Delete day folders older than the retention window. Returns count."""
    cutoff = date_cls.today() - timedelta(days=max(1, int(retention_days)))
    removed = 0
    for p in _dir().iterdir():
        if not p.is_dir():
            continue
        try:
            d = date_cls.fromisoformat(p.name)
        except ValueError:
            continue
        if d < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed


def purge_all() -> None:
    shutil.rmtree(_dir(), ignore_errors=True)
    _dir()
    with _lock:
        _state["count_today"] = 0


def status() -> dict:
    s = dict(_state)
    s["storage_bytes"] = storage_bytes()
    s["enabled"] = bool(appconfig.load().get("screenshots_enabled", False))
    return s


def set_paused(paused: bool) -> None:
    _state["paused"] = bool(paused)


# ---------- Windows foreground window + idle (ctypes) ----------

_IS_WIN = os.name == "nt"
_user32 = ctypes.windll.user32 if _IS_WIN else None
_kernel32 = ctypes.windll.kernel32 if _IS_WIN else None


def _exe_for_pid(pid: int) -> str:
    if not _IS_WIN:
        return ""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def foreground() -> Tuple[str, str]:
    """(exe name, window title) of the currently focused window."""
    if not _IS_WIN:
        return ("", "")
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ("", "")
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return (_exe_for_pid(pid.value), buf.value)
    except Exception:
        return ("", "")


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input."""
    if not _IS_WIN:
        return 0.0
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return ((_kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


# ---------- capture ----------

def capture(app: str, title: str, reason: str) -> Optional[dict]:
    """Grab all screens, downscale to max width, save JPEG, index it."""
    from PIL import ImageGrab
    cfg = appconfig.load()
    max_w = int(cfg.get("screenshots_max_width", 0) or 0)   # 0 = full resolution
    quality = int(cfg.get("screenshots_quality", 80))
    img = ImageGrab.grab(all_screens=True)
    if max_w and img.width > max_w:
        from PIL import Image
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    img = img.convert("RGB")
    now = datetime.now()
    d = now.date()
    safe_app = "".join(c for c in (app or "app") if c.isalnum())[:24] or "app"
    fname = f"{now.strftime('%H%M%S')}_{safe_app}.jpg"
    path = _day_dir(d) / fname
    img.save(path, "JPEG", quality=quality, optimize=True)
    rec = {"t": now.strftime("%H:%M:%S"),
           "min": round(now.hour * 60 + now.minute + now.second / 60, 2),
           "file": fname, "app": app or "", "title": (title or "")[:200],
           "reason": reason, "bytes": path.stat().st_size}
    with _lock:
        idx = load_index(d)
        idx.append(rec)
        _save_index(d, idx)
        _state["last_capture"] = now.isoformat(timespec="seconds")
        _state["count_today"] = len(idx)
    return rec


def _excluded(app: str, title: str, cfg: dict) -> bool:
    """Never record the dashboard itself, or user-excluded apps (e.g. a
    password manager)."""
    if "timescribe" in (title or "").lower():
        return True
    ex = [str(x).lower() for x in (cfg.get("screenshots_exclude_apps") or [])]
    return (app or "").lower() in ex


def _loop() -> None:
    _state["running"] = True
    print("[shots] recorder thread started (captures only while enabled)")
    last_key = None
    change_t = 0.0
    pending = False
    last_periodic = 0.0
    while True:
        try:
            cfg = appconfig.load()
            if not cfg.get("screenshots_enabled", False) or _state["paused"]:
                last_key = None
                time.sleep(3)
                continue
            interval = max(30, int(cfg.get("screenshots_interval_min", 2)) * 60)
            idle_limit = int(cfg.get("screenshots_idle_skip_s", 60))
            now = time.time()

            if idle_seconds() > idle_limit:          # away / locked -> nothing to see
                time.sleep(3)
                continue

            app, title = foreground()
            key = (app, title)
            if key != last_key:                       # focus changed: start debounce
                last_key, change_t, pending = key, now, True
            elif pending and now - change_t >= 3 and app and not _excluded(app, title, cfg):
                capture(app, title, "switch")         # settled on the new window
                pending, last_periodic = False, now

            if now - last_periodic >= interval and app and not _excluded(app, title, cfg):
                capture(app, title, "interval")
                last_periodic, pending = now, False

            if now - _state["last_purge"] > 3600:     # hourly retention sweep
                purge_old(int(cfg.get("screenshots_retention_days", 14)))
                _state["last_purge"] = now
            _state["last_error"] = None
        except Exception as exc:
            _state["last_error"] = str(exc)[:200]
            print(f"[shots] loop error: {exc!r}")
        time.sleep(2)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="screenshots").start()
