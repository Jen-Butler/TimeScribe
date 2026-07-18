"""Local MCP server exposing browser + activity data to Claude (Cowork /
Claude Desktop). Deliberately scoped: READ-ONLY activity data. No ticket
access, no time-entry creation -- those are separate concerns.

Transport: stdio (spawned as a subprocess by the MCP client; no ports).
"""
from __future__ import annotations
import json
from datetime import date as date_cls, datetime, time as time_cls, timedelta

from mcp.server.fastmcp import FastMCP

from timescribe import appconfig, history, activitywatch, digest as digest_mod

mcp = FastMCP("timescribe-activity")


def _day_bounds(day: str):
    d = date_cls.fromisoformat(day) if day else date_cls.today()
    return d, datetime.combine(d, time_cls.min), datetime.combine(d, time_cls.max)


@mcp.tool()
def get_browser_history(day: str = "", limit: int = 500) -> str:
    """Get Microsoft Edge browsing history for a specific day, across all
    configured profiles (excluding any profiles the user has excluded).

    Args:
        day: ISO date YYYY-MM-DD. Empty = today.
        limit: max visits returned (default 500).

    Returns JSON: {day, count, visits: [{time, profile, domain, title, url}]}
    """
    d, since, until = _day_bounds(day)
    cfg = appconfig.load()
    ud = cfg.get("edge_user_data_dir") or history.default_user_data_dir()
    visits = history.read_all_history(
        ud, since=since, until=until,
        ignore_prefixes=("chrome-extension://", "edge-extension://", "edge://", "about:"),
        exclude_profiles=cfg.get("exclude_profiles", []),
    )
    out = [{
        "time": v["time"].strftime("%H:%M"),
        "profile": v["profile"],
        "domain": v["domain"],
        "title": v["title"][:150],
        "url": v["url"][:200],
    } for v in visits[:limit]]
    return json.dumps({"day": d.isoformat(), "count": len(visits), "visits": out})


@mcp.tool()
def get_window_activity(day: str = "", min_focus_seconds: int = 30) -> str:
    """Get application window-focus events from ActivityWatch for a day:
    which apps were focused, window titles, and for how long.

    Args:
        day: ISO date YYYY-MM-DD. Empty = today.
        min_focus_seconds: skip events shorter than this (default 30).

    Returns JSON: {day, count, events: [{time, app, title, minutes}]}
    """
    d, since, until = _day_bounds(day)
    events = activitywatch.get_window_events(since, until)
    out = [{
        "time": e["time"].strftime("%H:%M"),
        "app": e["app"],
        "title": e["title"][:150],
        "minutes": round(e["duration_s"] / 60, 1),
    } for e in events if e["duration_s"] >= min_focus_seconds]
    return json.dumps({"day": d.isoformat(), "count": len(out), "events": out})


@mcp.tool()
def get_inactivity_periods(day: str = "", min_minutes: int = 5) -> str:
    """Get periods of inactivity (AFK, machine asleep/locked) for a day.
    Useful for understanding breaks and non-working time.

    Returns JSON: {day, periods: [{start, end, minutes}]}
    """
    d, since, until = _day_bounds(day)
    periods = activitywatch.get_inactivity_periods(since, until, min_minutes=min_minutes)
    out = [{
        "start": p["start"].strftime("%H:%M"),
        "end": p["end"].strftime("%H:%M"),
        "minutes": p["minutes"],
    } for p in periods]
    return json.dumps({"day": d.isoformat(), "periods": out})


@mcp.tool()
def get_activity_digest(day: str = "") -> str:
    """Get the stored AI-generated activity digest for a day, if the
    TimeScribe desktop app has produced one. Returns timestamped
    entries summarizing what the user was doing.

    Returns JSON: {day, entries: [{time_range, summary}]}
    """
    d, _, _ = _day_bounds(day)
    return json.dumps({"day": d.isoformat(), "entries": digest_mod.load_digest(d)})


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
