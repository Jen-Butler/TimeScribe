"""In-app background scheduler.

- Every `digest_interval_minutes` (default 120) during work hours on
  weekdays: re-digest today (00:00 -> now), replacing the stored digest.
  Full-day re-digest keeps entries coherent -- no incremental fragments.
- At `eod_time` (default 17:15): final digest + auto-generate drafts +
  tray notification so the user reviews while the day is fresh.

All failures are logged and swallowed; the loop never dies.
"""
from __future__ import annotations
import threading
import time as time_mod
import traceback
from datetime import date as date_cls, datetime, time as time_cls
from typing import Callable, Optional

from timescribe import appconfig

_state = {
    "last_digest": None,       # datetime of last successful digest
    "last_eod": None,          # date of last EOD run
    "next_digest": None,       # datetime of next planned digest
    "running": False,
    "last_error": None,
}


def status() -> dict:
    return {
        "running": _state["running"],
        "last_digest": _state["last_digest"].isoformat(timespec="minutes") if _state["last_digest"] else None,
        "next_digest": _state["next_digest"].isoformat(timespec="minutes") if _state["next_digest"] else None,
        "last_eod": _state["last_eod"].isoformat() if _state["last_eod"] else None,
        "last_error": _state["last_error"],
    }


def _llm_key_available() -> bool:
    cfg = appconfig.load()
    provider = (cfg.get("llm_provider") or "anthropic").lower()
    if provider == "mcp":
        return False        # MCP-only mode: no in-app LLM, no auto-digest
    if provider == "openai":
        return appconfig.get_secret("openai_api_key") is not None
    return appconfig.get_secret("anthropic_api_key") is not None


def _parse_hhmm(s: str, default: time_cls) -> time_cls:
    try:
        h, m = s.split(":")
        return time_cls(int(h), int(m))
    except (ValueError, AttributeError):
        return default


def _in_work_hours(now: datetime, cfg: dict) -> bool:
    if now.weekday() >= 5:          # Sat/Sun
        return False
    start = _parse_hhmm(cfg.get("work_start", "09:00"), time_cls(9, 0))
    end   = _parse_hhmm(cfg.get("work_end", "17:00"),  time_cls(17, 0))
    return start <= now.time() <= end


def _run_digest_safe() -> bool:
    try:
        from timescribe import digest
        digest.run_digest(date_cls.today())     # full day so far, replace
        _state["last_digest"] = datetime.now()
        _state["last_error"] = None
        return True
    except Exception as exc:
        _state["last_error"] = f"digest: {exc}"
        print(f"[scheduler] digest failed: {exc!r}")
        traceback.print_exc()
        return False


def _run_eod_safe(notify: Optional[Callable[[str, str], None]]) -> None:
    try:
        _run_digest_safe()
        from timescribe.server import get_adapter
        from timescribe import inference
        a = get_adapter()
        if a is None or not a.is_authenticated():
            if notify:
                notify("TimeScribe", "Day digested â€” connect Halo to draft time entries.")
            return
        result = inference.generate_drafts(a, date_cls.today())
        n = len(result.get("drafts", []))
        _state["last_eod"] = date_cls.today()
        if notify:
            notify("TimeScribe",
                   f"{n} draft time entr{'y' if n == 1 else 'ies'} ready â€” open the dashboard to review.")
    except Exception as exc:
        _state["last_error"] = f"eod: {exc}"
        print(f"[scheduler] EOD run failed: {exc!r}")
        traceback.print_exc()


def _loop(notify: Optional[Callable[[str, str], None]]):
    _state["running"] = True
    print("[scheduler] started")
    while True:
        try:
            cfg = appconfig.load()
            if not cfg.get("auto_digest_enabled", True):
                time_mod.sleep(60)
                continue
            now = datetime.now()
            interval_min = int(cfg.get("digest_interval_minutes", 120))
            eod = _parse_hhmm(cfg.get("eod_time", "17:15"), time_cls(17, 15))

            # ActivityWatch watchdog: if the server dropped, relaunch it
            # (at most once per 15 minutes to avoid thrash)
            last_aw = _state.get("last_aw_check")
            if last_aw is None or (now - last_aw).total_seconds() >= 900:
                _state["last_aw_check"] = now
                from timescribe import aw_manager
                if not aw_manager.is_running():
                    r = aw_manager.ensure_running(wait_seconds=20)
                    print(f"[scheduler] AW watchdog: {r}")

            # EOD trigger: once per weekday after eod time
            if (now.weekday() < 5 and now.time() >= eod
                    and _state["last_eod"] != date_cls.today()
                    and _llm_key_available()):
                print("[scheduler] EOD run")
                _run_eod_safe(notify)

            # Periodic digest during work hours
            due = (_state["last_digest"] is None
                   or (now - _state["last_digest"]).total_seconds() >= interval_min * 60)
            if (_in_work_hours(now, cfg) and due
                    and _llm_key_available()):
                print("[scheduler] periodic digest")
                _run_digest_safe()

            # Compute next planned run for status display
            if _state["last_digest"]:
                from datetime import timedelta
                _state["next_digest"] = _state["last_digest"] + timedelta(minutes=interval_min)
            time_mod.sleep(60)
        except Exception as exc:
            _state["last_error"] = f"loop: {exc}"
            print(f"[scheduler] loop error (continuing): {exc!r}")
            time_mod.sleep(60)


def start(notify: Optional[Callable[[str, str], None]] = None) -> threading.Thread:
    t = threading.Thread(target=_loop, args=(notify,), daemon=True,
                         name="pad-scheduler")
    t.start()
    return t

