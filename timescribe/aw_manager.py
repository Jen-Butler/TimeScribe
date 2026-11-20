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
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from timescribe import appconfig

_launched_proc: Optional[subprocess.Popen] = None
_launch_lock = threading.Lock()
# In-app ActivityWatch update progress: idle | downloading | installing | done | error
_update = {"state": "idle", "message": "", "version": None}


def aw_base_url() -> str:
    """The AW server URL, config-overridable for remote hosts."""
    cfg = appconfig.load()
    return (cfg.get("aw_host") or "http://127.0.0.1:5600").rstrip("/")


def _is_local(url: str) -> bool:
    return "127.0.0.1" in url or "localhost" in url.lower()


def is_running(timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{aw_base_url()}/api/0/info", timeout=timeout,
                      follow_redirects=True, trust_env=False)
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
    if _update["state"] in ("downloading", "installing"):
        # The updater stops and restarts AW itself; don't relaunch under it.
        return {"running": False, "launched": False, "source": "update in progress"}
    if is_running():
        return {"running": True, "launched": False, "source": "already-running"}
    url = aw_base_url()
    if not _is_local(url):
        return {"running": False, "launched": False,
                "source": f"remote host {url} unreachable"}
    # Single-flight: only one thread may launch/wait at a time. Concurrent
    # callers block here, then re-check -- launching two aw-qt instances at
    # once makes them race to create their config dirs (FileExistsError
    # popup on first run, ActivityWatch/aw-core TOCTOU bug).
    with _launch_lock:
        if is_running():
            return {"running": True, "launched": False, "source": "already-running"}
        if _launched_proc is not None and _launched_proc.poll() is None:
            # We already launched aw-qt and it's still alive -- it may just
            # need more time to bring the server up. Don't launch another.
            return {"running": False, "launched": False,
                    "source": "previous launch still starting"}
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


# ---------- version check ----------

_ver_cache = {"at": 0.0, "latest": None}


def _vtuple(v: str):
    """'v0.13.2' -> (0, 13, 2); pre-release suffixes (b8, rc1) are ignored."""
    import re
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def latest_stable(max_age_s: int = 12 * 3600) -> Optional[str]:
    """Newest non-beta ActivityWatch release tag from GitHub (cached; a failed
    lookup is retried after an hour). None when offline."""
    now = time.time()
    if _ver_cache["at"] and now - _ver_cache["at"] < max_age_s:
        return _ver_cache["latest"]
    latest = None
    try:
        r = httpx.get("https://api.github.com/repos/ActivityWatch/activitywatch/releases/latest",
                      timeout=6, follow_redirects=True,
                      headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            latest = r.json().get("tag_name") or None
    except Exception:
        latest = None
    _ver_cache["latest"] = latest
    _ver_cache["at"] = now if latest else now - max_age_s + 3600
    return latest


def version_info() -> dict:
    """Installed ActivityWatch version (from the running server) vs the newest
    stable release."""
    installed = None
    try:
        r = httpx.get(f"{aw_base_url()}/api/0/info", timeout=2,
                      follow_redirects=True, trust_env=False)
        if r.status_code == 200:
            installed = r.json().get("version") or None
    except Exception:
        pass
    latest = latest_stable() if installed else None
    newer = bool(installed and latest and _vtuple(latest) > _vtuple(installed))
    return {"installed": installed, "latest": latest, "update_available": newer}


# ---------- in-app update ----------
#
# Only the portable copy TimeScribe ships (next to the frozen exe, or the
# project's aw_dist) is updated here. An ActivityWatch the user installed
# themselves is left to its own installer. The new build is downloaded and
# unpacked BESIDE the live one while AW keeps running; AW is only stopped for
# the folder swap (a few seconds), and the old folder is put back if the new
# one does not come up. Recorded activity lives in the user's AW data folder,
# not in the program folder, so it is untouched.

_AW_IMAGES = ("aw-qt.exe", "aw-watcher-afk.exe", "aw-watcher-window.exe",
              "aw-watcher-input.exe", "aw-server.exe", "aw-server-rust.exe",
              "aw-sync.exe", "aw-notify.exe")


def managed_dir() -> Optional[Path]:
    """Folder of the portable AW that TimeScribe owns (contains aw-qt.exe), or
    None when the AW in use was installed separately."""
    exe = find_aw_executable()
    if exe is None:
        return None
    owned = []
    if getattr(sys, "frozen", False):
        owned.append(Path(sys.executable).parent / "aw" / "activitywatch")
    owned.append(Path(__file__).resolve().parent.parent / "aw_dist" / "activitywatch")
    return exe.parent if exe.parent in owned else None


def update_state() -> dict:
    return dict(_update, managed=managed_dir() is not None)


def _stop_aw() -> None:
    """Stop every ActivityWatch process. aw-qt first: it restarts modules
    that exit."""
    global _launched_proc
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for _ in range(2):
        for img in _AW_IMAGES:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/IM", img], capture_output=True,
                               timeout=15, creationflags=flags)
            except Exception:
                pass
        time.sleep(1.0)
    _launched_proc = None


def _launch_and_wait(exe: Path, wait_seconds: int = 60) -> bool:
    global _launched_proc
    try:
        _launched_proc = subprocess.Popen(
            [str(exe)], cwd=str(exe.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        print(f"[aw_manager] update: launch failed: {exc}")
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(1.5)
    return False


def _rename_retry(src: Path, dst: Path, seconds: int = 20) -> None:
    deadline = time.time() + seconds
    while True:
        try:
            src.rename(dst)
            return
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(1.0)


def _run_update(version: str) -> None:
    import shutil
    import zipfile
    live = managed_dir()                      # ...\activitywatch
    root = live.parent
    zpath, stage, old = root / "_aw_update.zip", root / "_aw_new", root / "activitywatch.old"
    try:
        for p in (stage, old):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        url = (f"https://github.com/ActivityWatch/activitywatch/releases/download/"
               f"{version}/activitywatch-{version}-windows-x86_64.zip")
        _update.update(state="downloading", message=f"Downloading ActivityWatch {version}…")
        got = 0
        with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            with open(zpath, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    pct = f" {got * 100 // total}%" if total else f" {got >> 20} MB"
                    _update["message"] = f"Downloading ActivityWatch {version}…{pct}"
        _update["message"] = "Unpacking…"
        with zipfile.ZipFile(zpath) as z:
            z.extractall(stage)
        new = stage / "activitywatch"
        if not (new / "aw-qt.exe").exists():
            raise RuntimeError("the download does not contain activitywatch\\aw-qt.exe; "
                               "nothing was changed")
        # --- point of no return: stop AW and swap folders ---
        _update.update(state="installing", message="Stopping ActivityWatch and swapping in the new version…")
        _stop_aw()
        _rename_retry(live, old)
        try:
            _rename_retry(new, live, 10)
        except OSError:
            _rename_retry(old, live, 10)
            raise
        _update["message"] = "Starting the new ActivityWatch…"
        if _launch_and_wait(live / "aw-qt.exe"):
            _ver_cache["at"] = 0.0
            _update.update(state="done", message=f"ActivityWatch updated to {version}.")
            print(f"[aw_manager] updated ActivityWatch to {version}")
            shutil.rmtree(old, ignore_errors=True)
        else:
            # New build did not come up: put the old one back.
            _stop_aw()
            bad = root / "activitywatch.failed"
            shutil.rmtree(bad, ignore_errors=True)
            _rename_retry(live, bad)
            _rename_retry(old, live)
            back = _launch_and_wait(live / "aw-qt.exe")
            shutil.rmtree(bad, ignore_errors=True)
            _update.update(state="error", message=(
                f"ActivityWatch {version} did not start, so the previous version was restored"
                + ("." if back else " but it has not come back up yet; restart TimeScribe.")))
            print(f"[aw_manager] update to {version} failed to start; rolled back (running={back})")
    except Exception as exc:
        print(f"[aw_manager] update failed: {exc!r}")
        _update.update(state="error", message=f"Update failed: {exc}")
        try:                                   # never leave AW stopped
            if not is_running():
                exe = find_aw_executable()
                if exe:
                    _launch_and_wait(exe, 45)
        except Exception:
            pass
    finally:
        for p in (zpath,):
            try:
                p.unlink()
            except OSError:
                pass
        shutil.rmtree(stage, ignore_errors=True)


def start_update(version: Optional[str] = None) -> dict:
    """Kick off a background update of the bundled ActivityWatch."""
    import re
    if _update["state"] in ("downloading", "installing"):
        return {"ok": True, **update_state()}
    if not _is_local(aw_base_url()):
        return {"ok": False, "message": "ActivityWatch runs on another machine; update it there."}
    if managed_dir() is None:
        return {"ok": False, "message": "This ActivityWatch was installed separately from TimeScribe; "
                                        "update it with ActivityWatch's own installer."}
    version = version or latest_stable(max_age_s=0)
    if not version or not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        return {"ok": False, "message": "Could not determine the latest ActivityWatch release."}
    _update.update(state="downloading", message="Starting…", version=version)
    threading.Thread(target=_run_update, args=(version,), daemon=True, name="aw-update").start()
    return {"ok": True, **update_state()}
