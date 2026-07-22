"""Main desktop app: FastAPI server (background thread) + system tray icon.
'Open Dashboard' launches an Edge app-mode window (chromeless, feels native);
falls back to the default browser.
"""
from __future__ import annotations
import shutil
import subprocess
import threading
import webbrowser

import uvicorn
from PIL import Image, ImageDraw
import pystray

from timescribe import appconfig


def _make_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=(56, 189, 248, 255))       # sky blue disc
    d.ellipse([20, 20, 44, 44], fill=(15, 23, 42, 255))       # dark core
    return img


def _dashboard_url() -> str:
    port = appconfig.load().get("ui_port", 8770)
    return f"http://127.0.0.1:{port}"


def open_dashboard(icon=None, item=None):
    url = _dashboard_url()
    edge = (shutil.which("msedge")
            or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    try:
        subprocess.Popen([edge, f"--app={url}", "--window-size=900,760"])
    except (OSError, FileNotFoundError):
        webbrowser.open(url)


def _run_server():
    port = appconfig.load().get("ui_port", 8770)
    from timescribe.server import app as fastapi_app
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


class _Tee:
    """Write to the log file and (if present) the original console stream,
    so app.log is complete no matter how the app was launched."""
    def __init__(self, log_file, console):
        self._log = log_file
        self._console = console

    def write(self, s):
        self._log.write(s)
        if self._console is not None:
            try:
                self._console.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        self._log.flush()
        if self._console is not None:
            try:
                self._console.flush()
            except Exception:
                pass

    # Libraries (uvicorn, click, colorama) probe these on sys.stdout.
    def isatty(self):
        return bool(self._console is not None and self._console.isatty())

    def writable(self):
        return True

    def readable(self):
        return False

    @property
    def encoding(self):
        return getattr(self._log, "encoding", "utf-8")

    def fileno(self):
        # Only meaningful when a real console exists; the log file's fd
        # is the safest fallback for libs that insist on one.
        if self._console is not None:
            try:
                return self._console.fileno()
            except Exception:
                pass
        return self._log.fileno()


def _setup_frozen_logging():
    """ALWAYS write stdout/stderr to app.log. With no console attached
    (PyInstaller --noconsole OR pythonw.exe) the streams are None and the
    first print() would otherwise crash; with a console we tee to both so
    the Logs view in Settings never has blind spots."""
    import sys
    from pathlib import Path
    from platformdirs import user_data_dir
    log_dir = Path(user_data_dir("timescribe")) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(log_file, sys.__stdout__)
    sys.stderr = _Tee(log_file, sys.__stderr__)
    from datetime import datetime
    print(f"\n===== app start {datetime.now().isoformat(timespec='seconds')} =====")


def _already_running() -> bool:
    """True if another TimeScribe instance is serving the dashboard."""
    import httpx
    port = appconfig.load().get("ui_port", 8770)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2,
                      trust_env=False)
        return r.status_code == 200
    except Exception:
        return False


def main():
    _setup_frozen_logging()
    # 0. Single-instance guard: a second launch (startup shortcut + manual
    # click, say) would fail to bind the port and linger as a zombie tray
    # icon. Instead, hand off to the running instance and exit.
    if _already_running():
        print("[app] another instance is already running; opening its dashboard and exiting")
        open_dashboard()
        return
    # 1. Backend in a daemon thread
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()

    # 2. Open dashboard on first launch
    threading.Timer(1.5, open_dashboard).start()

    # 2b. Make sure ActivityWatch is up (launch bundled/installed copy if not)
    def _ensure_aw():
        from timescribe import aw_manager
        result = aw_manager.ensure_running()
        print(f"[app] ActivityWatch: {result}")
    threading.Thread(target=_ensure_aw, daemon=True).start()

    # 3. Tray icon (created first so the scheduler can use it for toasts)
    icon = pystray.Icon(
        "timescribe",
        _make_icon_image(),
        "TimeScribe",
        menu=pystray.Menu(
            pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
            pystray.MenuItem("Run Digest Now", lambda ic, it: threading.Thread(
                target=_digest_now, daemon=True).start()),
            pystray.MenuItem("Quit", lambda ic, it: ic.stop()),
        ),
    )

    # 4. Background scheduler with tray-notification callback
    from timescribe import scheduler
    def _notify(title, message):
        try:
            icon.notify(message, title)
        except Exception:
            pass
    scheduler.start(notify=_notify)

    icon.run()


def _digest_now():
    from datetime import date
    from timescribe import digest
    try:
        digest.run_digest(date.today())
    except Exception as exc:
        print(f"[app] manual digest failed: {exc!r}")


if __name__ == "__main__":
    main()
