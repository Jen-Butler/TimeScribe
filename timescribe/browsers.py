"""Chromium-family browser detection.

Edge, Chrome, Brave, Vivaldi, and Chromium all store history in the same
SQLite schema and profiles in the same Local State format, so one reader
handles them all -- we just need to know where each keeps its User Data.

Firefox uses a different format (places.sqlite, profiles.ini, a different
timestamp epoch) so it has its own discovery (firefox_profiles) and reader.
"""
from __future__ import annotations
import configparser
import os
from pathlib import Path
from typing import List, Optional, Tuple

FIREFOX_ROOT = r"%APPDATA%\Mozilla\Firefox"

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


def firefox_profiles() -> List[Tuple[str, str, Path]]:
    """(folder, friendly_name, places.sqlite path) for each Firefox profile,
    parsed from profiles.ini. Empty if Firefox isn't installed."""
    root = Path(os.path.expandvars(FIREFOX_ROOT))
    ini = root / "profiles.ini"
    if not ini.exists():
        return []
    cp = configparser.ConfigParser()
    try:
        cp.read(ini, encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    out = []
    for sect in cp.sections():
        if not sect.lower().startswith("profile"):
            continue
        path = cp.get(sect, "Path", fallback=None)
        if not path:
            continue
        is_rel = cp.get(sect, "IsRelative", fallback="1") == "1"
        pdir = (root / path) if is_rel else Path(path)
        places = pdir / "places.sqlite"
        if places.exists():
            name = cp.get(sect, "Name", fallback=pdir.name)
            out.append((pdir.name, name, places))
    return out
