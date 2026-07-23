"""Chromium-family browser detection.

Edge, Chrome, Brave, Vivaldi, and Chromium all store history in the same
SQLite schema and profiles in the same Local State format, so one reader
handles them all -- we just need to know where each keeps its User Data.

Firefox uses a different format (places.sqlite) and is out of scope here.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import List, Optional, Tuple

# key -> (display name, User Data dir path template with env vars)
BROWSERS = {
    "edge":     ("Microsoft Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    "chrome":   ("Google Chrome",  r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    "brave":    ("Brave",          r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
    "vivaldi":  ("Vivaldi",        r"%LOCALAPPDATA%\Vivaldi\User Data"),
    "chromium": ("Chromium",       r"%LOCALAPPDATA%\Chromium\User Data"),
}


def user_data_dir(key: str, edge_override: Optional[str] = None) -> Path:
    """Resolve a browser's User Data directory. Edge honors the legacy
    edge_user_data_dir config override."""
    if key == "edge" and edge_override:
        return Path(os.path.expandvars(edge_override))
    tmpl = BROWSERS.get(key, (None, ""))[1]
    return Path(os.path.expandvars(tmpl))


def installed_browsers(edge_override: Optional[str] = None) -> List[Tuple[str, str, Path]]:
    """(key, display_name, user_data_path) for each browser present on disk."""
    out = []
    for key, (name, _) in BROWSERS.items():
        ud = user_data_dir(key, edge_override if key == "edge" else None)
        if ud.exists():
            out.append((key, name, ud))
    return out
