"""ActivityWatch supervisor: make sure a usable AW server is reachable.

Search order for a launchable AW when nothing is listening:
  1. Bundled portable copy next to the frozen exe:  <exe_dir>\\aw\\activitywatch\\aw-qt.exe
  2. Dev-tree portable copy:                        <project>\\aw_dist\\activitywatch\\aw-qt.exe
  3. Standard install locations (Program Files, %LOCALAPPDATA%\\Programs)

If the configured host is remote (not 127.0.0.1/localhost) we never try to
launch anything -- we just report reachability.
"""
from __future__ import annotations
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from timescribe import appconfig

_launched_proc: Optional[subprocess.Popen] = None


def aw_base_url() -> str:
    """The AW server URL, config-overridable for remote hosts."""
    cfg = appconfig.load()
    return (cfg.get("aw_host") or "http://127.0.0.1:5600").rstrip("/")


def _is_local(url: str) -> bool:
    return "127.0.0.1" in url or "localhost" in url.lower()


def is_running(timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{aw_base_url()}/api/0/info", timeout=timeout,
                      follow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def _candidate_paths() -> list:
    import os
    out = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).parent / "aw" / "activitywatch" / "aw-qt.exe")
    here = Path(__file__).resolve().parent.parent
    out.append(here / "aw_dist" / "activitywatch" / "aw-qt.exe")
    out.append(Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\ActivityWatch\aw-qt.exe")))
    out.append(Path(r"C:\Program Files\ActivityWatch\aw-qt.exe"))
    out.append(Path(r"C:\Program Files (x86)\ActivityWatch\aw-qt.exe"))
    return out


def find_aw_executable() -> Optional[Path]:
    for p in _candidate_paths():
        if p.exists():
            return p
    return None


def ensure_running(wait_seconds: int = 30) -> dict:
    """Make sure AW is reachable. Launch the best-available copy if local.

    Returns {"running": bool, "launched": bool, "source": str}
    """
    global _launched_proc
    if is_running():
        return {"running": True, "launched": False, "source": "already-running"}
    url = aw_base_url()
    if not _is_local(url):
        return {"running": False, "launched": False,
                "source": f"remote host {url} unreachable"}
    exe = find_aw_executable()
    if exe is None:
        return {"running": False, "launched": False, "source": "no aw-qt.exe found"}
    try:
        _launched_proc = subprocess.Popen(
            [str(exe)], cwd=str(exe.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return {"running": False, "launched": False, "source": f"launch failed: {exc}"}
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_running():
            print(f"[aw_manager] launched {exe} and server is up")
            return {"running": True, "launched": True, "source": str(exe)}
        time.sleep(1.5)
    return {"running": False, "launched": True,
            "source": f"launched {exe} but server not up after {wait_seconds}s"}
