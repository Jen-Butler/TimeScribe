"""Local FastAPI backend. Binds 127.0.0.1 only."""
from __future__ import annotations
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from timescribe import appconfig
from timescribe.psa.halo import HaloPSAAdapter

app = FastAPI(title="TimeScribe Desktop", docs_url=None, redoc_url=None)

_adapter: Optional[HaloPSAAdapter] = None
_oauth_lock = threading.Lock()


def get_adapter() -> Optional[HaloPSAAdapter]:
    global _adapter
    cfg = appconfig.load()
    if not cfg.get("halo_base_url") or not cfg.get("halo_client_id"):
        return None
    mode = (cfg.get("halo_auth_mode") or "pkce").lower()
    agent = int(cfg.get("halo_agent_id") or 0) or None
    if (_adapter is None
            or _adapter.base_url != cfg["halo_base_url"].rstrip("/")
            or _adapter.client_id != cfg["halo_client_id"]
            or _adapter.auth_mode != mode
            or _adapter.agent_id_override != agent):
        _adapter = HaloPSAAdapter(
            base_url=cfg["halo_base_url"],
            client_id=cfg["halo_client_id"],
            auth_mode=mode,
            agent_id=agent,
        )
    return _adapter


# ---------- UI ----------

def _ui_path() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        # PyInstaller: data files land under sys._MEIPASS (onefile) or the
        # exe dir (onedir). We add ui/ via the spec's datas.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "ui" / "index.html"
    return Path(__file__).parent / "ui" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index():
    return _ui_path().read_text(encoding="utf-8")


@app.get("/favicon.ico")
def favicon():
    from fastapi.responses import FileResponse, Response
    ico = _ui_path().parent / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/manifest.webmanifest")
def manifest():
    from fastapi.responses import FileResponse, Response
    m = _ui_path().parent / "manifest.webmanifest"
    if m.exists():
        return FileResponse(str(m), media_type="application/manifest+json")
    return Response(status_code=404)


@app.get("/sw.js")
def service_worker():
    """Serve the service worker at the ROOT scope. A registered service
    worker with a fetch handler is what makes the dashboard installable as
    a PWA (Edge/Chrome won't offer 'Install as app' without one).
    Service-Worker-Allowed lets it control the whole origin."""
    from fastapi.responses import FileResponse, Response
    sw = _ui_path().parent / "sw.js"
    if sw.exists():
        return FileResponse(str(sw), media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/",
                                     "Cache-Control": "no-cache"})
    return Response(status_code=404)


@app.get("/{asset:path}.png")
def ui_png(asset: str):
    """Serve PWA icon PNGs from the ui/ folder (icon-192, icon-512)."""
    from fastapi.responses import FileResponse, Response
    p = (_ui_path().parent / f"{asset}.png").resolve()
    # keep it inside the ui dir
    if p.exists() and str(p).startswith(str(_ui_path().parent.resolve())):
        return FileResponse(str(p), media_type="image/png")
    return Response(status_code=404)


# ---------- API ----------

class ConfigUpdate(BaseModel):
    halo_base_url: Optional[str] = None
    halo_client_id: Optional[str] = None
    halo_auth_mode: Optional[str] = None        # "pkce" | "client_credentials"
    halo_agent_id: Optional[int] = None         # client_credentials: agent to act as
    timezone: Optional[str] = None
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    exclude_profiles: Optional[list] = None
    llm_provider: Optional[str] = None      # "anthropic" | "openai"
    aw_host: Optional[str] = None
    digest_weekdays: Optional[list] = None      # [0..6], Mon=0
    digest_interval_minutes: Optional[int] = None
    combined_digest_drafts: Optional[bool] = None
    browser_profiles: Optional[dict] = None     # "browserkey::folder" -> bool
    ticket_browser_profile: Optional[str] = None  # Edge profile-directory
    draft_sort_default: Optional[str] = None    # default draft-list sort
    pomo_focus_min: Optional[int] = None
    pomo_break_min: Optional[int] = None
    pomo_long_min: Optional[int] = None
    ui_font: Optional[str] = None               # font family, "" = default
    ui_font_custom: Optional[str] = None         # uploaded font filename, "" clears
    ui_colors: Optional[dict] = None             # {var(no --): value} overrides
    screenshots_enabled: Optional[bool] = None
    screenshots_interval_min: Optional[int] = None
    screenshots_retention_days: Optional[int] = None
    screenshots_exclude_apps: Optional[list] = None


class SecretUpdate(BaseModel):
    name: str          # "anthropic_api_key"
    value: str


@app.get("/api/status")
def status():
    cfg = appconfig.load()
    a = get_adapter()
    halo_connected = False
    agent = None
    if a and a.is_authenticated():
        halo_connected = True
        try:
            rec = a.get_current_agent()
            agent = {"id": rec.get("id"), "name": rec.get("name")}
        except Exception:
            agent = None
    return {
        "halo_configured": bool(cfg.get("halo_base_url") and cfg.get("halo_client_id")),
        "halo_connected": halo_connected,
        "agent": agent,
        "anthropic_key_set": appconfig.get_secret("anthropic_api_key") is not None,
        "openai_key_set": appconfig.get_secret("openai_api_key") is not None,
        "org_ai_key_set": appconfig.get_secret("org_ai_key") is not None,
        "llm_provider": cfg.get("llm_provider", "anthropic"),
        "halo_auth_mode": (cfg.get("halo_auth_mode") or "pkce"),
        "halo_secret_set": bool(a and a.load_client_secret()),
        "ai_health": __import__("timescribe.llm", fromlist=["ai_health"]).ai_health(),
        "screenshots": __import__("timescribe.screenshots", fromlist=["status"]).status(),
        "config": {k: v for k, v in cfg.items() if "key" not in k.lower()},
    }


class HaloSecret(BaseModel):
    secret: str


@app.post("/api/halo/secret")
def halo_secret(body: HaloSecret):
    """Store the Halo API application's client secret (client_credentials
    mode). Credential store only -- never written to the config file."""
    a = get_adapter()
    if a is None:
        raise HTTPException(400, "Set Halo base URL and Client ID first")
    s = (body.secret or "").strip()
    if not s:
        a.clear_client_secret()
        return {"ok": True, "set": False}
    a.save_client_secret(s)
    a._access_token = None   # force a fresh token with the new secret
    return {"ok": True, "set": True}


@app.post("/api/config")
def update_config(body: ConfigUpdate):
    cfg = appconfig.load()
    for k, v in body.model_dump(exclude_none=True).items():
        cfg[k] = v
    appconfig.save(cfg)
    return {"ok": True}


@app.post("/api/secret")
def set_secret(body: SecretUpdate):
    if body.name not in ("anthropic_api_key", "openai_api_key"):
        raise HTTPException(400, "unknown secret name")
    appconfig.set_secret(body.name, body.value.strip())
    return {"ok": True}


@app.post("/api/org_ai/fetch")
def org_ai_fetch():
    """Pull the org-shared AI key from the encrypted Halo custom field and
    cache it locally (Credential Manager). Techs run this once after
    connecting; the key is never shown."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    cfg = appconfig.load()
    cid = int(cfg.get("org_ai_client_id") or 0)
    if not cid:
        raise HTTPException(400, "No org AI client id configured yet")
    key = a.get_org_ai_key(cid, cfg.get("org_ai_field_name", "CFTimeScribeAIKey"))
    if not key:
        raise HTTPException(404, "No org AI key found on that client "
                            "(admin hasn't published one, or it's not readable)")
    appconfig.set_secret("org_ai_key", key)
    return {"ok": True, "provider": cfg.get("org_ai_provider", "openai")}


class OrgAIPublish(BaseModel):
    client_id: int
    field_name: Optional[str] = None
    provider: Optional[str] = None
    key: str


@app.post("/api/org_ai/publish")
def org_ai_publish(body: OrgAIPublish):
    """Admin helper: store the org AI key into the encrypted custom field on
    the given Halo client, and record the client/provider in config."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    cfg = appconfig.load()
    field = body.field_name or cfg.get("org_ai_field_name", "CFTimeScribeAIKey")
    try:
        a.publish_org_ai_key(body.client_id, field, body.key.strip())
    except Exception as exc:
        raise HTTPException(502, str(exc))
    cfg["org_ai_client_id"] = body.client_id
    cfg["org_ai_field_name"] = field
    if body.provider:
        cfg["org_ai_provider"] = body.provider
    appconfig.save(cfg)
    return {"ok": True}


@app.post("/api/halo/connect")
def halo_connect():
    a = get_adapter()
    if a is None:
        raise HTTPException(400, "Set Halo base URL and Client ID first")
    # A retry supersedes a stuck attempt (e.g. the browser opened in the
    # wrong profile) instead of refusing with "already in progress": cancel
    # the pending callback wait so it unblocks and frees the port, then take
    # the lock ourselves.
    if not _oauth_lock.acquire(blocking=False):
        from timescribe.oauth.pkce import cancel_pending
        cancel_pending()
        if not _oauth_lock.acquire(timeout=10):
            raise HTTPException(409, "Previous sign-in is still shutting down -- try once more")
    try:
        a.connect()   # opens browser, blocks until callback or timeout
        rec = a.get_current_agent()
        return {"ok": True, "agent": {"id": rec.get("id"), "name": rec.get("name")}}
    except Exception as exc:
        msg = str(exc)
        if "cancelled" in msg.lower() or "superseded" in msg.lower():
            # This attempt was replaced by a newer click; nothing to report.
            raise HTTPException(409, "Sign-in restarted in a new attempt")
        raise HTTPException(500, f"OAuth failed: {msg}")
    finally:
        _oauth_lock.release()


@app.post("/api/install_window")
def install_window():
    """Open the dashboard in a normal Edge window so the user can install
    it as a PWA (the default --app window hides Edge's install UI)."""
    try:
        from timescribe import app as _app
        launched = _app.open_install_window()
        return {"ok": True, "launched": launched}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/create_shortcut")
def create_shortcut():
    """Fallback: drop a Desktop shortcut that launches TimeScribe with its
    own icon, for anyone who'd rather not install the PWA."""
    try:
        from timescribe import app as _app
        path = _app.create_desktop_shortcut()
        return {"ok": True, "path": path}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/halo/signout")
def halo_signout():
    """Sign out of Halo: clears the stored refresh token and cached
    session so the next Connect runs a fresh OAuth login (same Halo,
    different user — or change the URL in Settings for another Halo)."""
    global _adapter
    a = get_adapter()
    if a is not None:
        a.sign_out()
    _adapter = None
    return {"ok": True}


@app.get("/api/tickets")
def tickets(limit: int = 25):
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    items = a.list_open_tickets()
    return {"count": len(items),
            "tickets": [
                {"id": t.id, "client": t.client, "subject": t.subject,
                 "status": t.status}
                for t in items[:limit]
            ]}


@app.get("/api/browsers")
def browsers_list():
    """Installed Chromium browsers and their profiles, with enabled state,
    for the Settings browser-source picker."""
    from timescribe import history as _history
    cfg = appconfig.load()
    edge_override = cfg.get("edge_user_data_dir") or None
    enabled = cfg.get("browser_profiles", {})
    excl = {s.strip().lower() for s in cfg.get("exclude_profiles", []) if s.strip()}
    grouped = {}
    for key, name, folder, friendly, hp in _history.discover_all_sources(edge_override):
        sid = f"{key}::{folder}"
        default = not (folder.lower() in excl or friendly.lower() in excl)
        grouped.setdefault(key, {"key": key, "browser": name, "profiles": []})
        grouped[key]["profiles"].append({
            "sid": sid, "folder": folder, "name": friendly,
            "enabled": enabled.get(sid, default),
        })
    return {"browsers": list(grouped.values())}


class OpenUrl(BaseModel):
    url: str


@app.post("/api/open-ticket")
def open_ticket_url(body: OpenUrl):
    """Open a Halo ticket URL in the configured Edge profile (so it lands in
    the window the tech actually works in). Restricted to the configured
    Halo host for safety."""
    import shutil, subprocess, os, webbrowser
    from urllib.parse import urlparse
    cfg = appconfig.load()
    base = (cfg.get("halo_base_url") or "").rstrip("/")
    u = urlparse(body.url)
    if u.scheme not in ("http", "https"):
        print(f"[open-ticket] rejected bad url: {body.url!r}")
        raise HTTPException(400, "bad url")
    if base and not body.url.startswith(base):
        print(f"[open-ticket] url {body.url!r} not under Halo base {base!r}; opening anyway")
    prof = (cfg.get("ticket_browser_profile") or "").strip()
    edge = shutil.which("msedge")
    if not edge:
        for p in (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(p):
                edge = p
                break
    print(f"[open-ticket] url={body.url} profile={prof!r} edge={edge!r}")
    if edge:
        args = [edge]
        if prof:
            args.append(f"--profile-directory={prof}")
        # No --new-window: Edge opens the URL as a new TAB in that profile's
        # existing window and raises it to the foreground.
        args.append(body.url)
        try:
            subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            print(f"[open-ticket] launched: {' '.join(args)}")
            return {"ok": True, "opened_with": "edge", "profile": prof}
        except Exception as exc:
            print(f"[open-ticket] edge launch failed: {exc}; falling back to default browser")
    # Fallback: default browser (no profile control).
    try:
        webbrowser.open(body.url)
        print("[open-ticket] opened with default browser")
        return {"ok": True, "opened_with": "default"}
    except Exception as exc:
        print(f"[open-ticket] failed entirely: {exc}")
        raise HTTPException(500, f"couldn't open browser: {exc}")


class OpenAnyUrl(BaseModel):
    url: str
    profile: Optional[str] = None   # Edge profile-directory folder


@app.post("/api/open-url")
def open_any_url(body: OpenAnyUrl):
    """Open any captured http(s) URL, in a specific Edge profile if given
    (used by the activity peek to reopen a visit in the same profile)."""
    import shutil, subprocess, os, webbrowser
    from urllib.parse import urlparse
    if urlparse(body.url).scheme not in ("http", "https"):
        raise HTTPException(400, "bad url")
    prof = (body.profile or "").strip()
    edge = shutil.which("msedge")
    if not edge:
        for p in (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(p):
                edge = p; break
    print(f"[open-url] url={body.url} profile={prof!r} edge={edge!r}")
    if edge:
        args = [edge]
        if prof:
            args.append(f"--profile-directory={prof}")
        args.append(body.url)
        try:
            subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": True}
        except Exception as exc:
            print(f"[open-url] edge launch failed: {exc}")
    try:
        webbrowser.open(body.url)
        return {"ok": True, "opened_with": "default"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/tickets/search")
def tickets_search(q: str, limit: int = 20):
    """Free-text ticket search for relinking a draft to the right ticket."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    if not (q or "").strip():
        return {"tickets": []}
    try:
        return {"tickets": a.search_tickets(q.strip(), limit)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/tickets/mine")
def tickets_mine(refresh: bool = False):
    """All tickets assigned to the logged-in agent, for the relink picker
    to filter locally (no per-keystroke API calls, no /Client or /Site).
    Served from cache (warmed hourly by the scheduler); refresh=true forces
    a fresh pull from Halo."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    try:
        return {"tickets": a.list_agent_tickets(force=refresh)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


_FONT_MIME = {".ttf": "font/ttf", ".otf": "font/otf",
              ".woff": "font/woff", ".woff2": "font/woff2"}


@app.post("/api/font/upload")
async def font_upload(file: UploadFile = File(...)):
    """Store a user-uploaded font under <configdir>/fonts and select it.
    Served back at /api/font/current for an injected @font-face."""
    import re as _re
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _FONT_MIME:
        raise HTTPException(400, "Font must be .ttf, .otf, .woff, or .woff2")
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(400, "Font file too large (max 12 MB)")
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename).name) or ("custom" + ext)
    fonts_dir = appconfig._path().parent / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    (fonts_dir / safe).write_bytes(data)
    cfg = appconfig.load()
    cfg["ui_font_custom"] = safe
    cfg["ui_font"] = "custom"
    appconfig.save(cfg)
    return {"ok": True, "name": safe}


@app.get("/api/font/current")
def font_current():
    """Serve the selected custom font file, if one is uploaded."""
    from fastapi.responses import FileResponse, Response
    cfg = appconfig.load()
    name = cfg.get("ui_font_custom") or ""
    fonts_root = (appconfig._path().parent / "fonts").resolve()
    p = (fonts_root / name).resolve()
    if not name or not str(p).startswith(str(fonts_root)) or not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p), media_type=_FONT_MIME.get(p.suffix.lower(),
                                                          "application/octet-stream"))


class PromptUpdate(BaseModel):
    key: str
    value: str = ""


@app.get("/api/prompts")
def prompts_get():
    """List the editable LLM prompts: their default text and any user
    override (empty string when none)."""
    from timescribe import prompts as _p
    overrides = appconfig.load().get("prompts") or {}
    return {"prompts": [{"key": k, "label": _p.LABELS[k],
                         "default": _p.editable_default(k),
                         "locked": _p.locked(k),
                         "value": overrides.get(k, "")}
                        for k in _p.ORDER]}


@app.post("/api/prompts")
def prompts_set(body: PromptUpdate):
    """Save (or clear) an override for one prompt. Empty text, or text equal
    to the default, clears the override so the built-in default is used."""
    from timescribe import prompts as _p
    if body.key not in _p.ORDER:
        raise HTTPException(400, "unknown prompt key")
    cfg = appconfig.load()
    overrides = dict(cfg.get("prompts") or {})
    v = body.value or ""
    if not v.strip() or v.strip() == _p.editable_default(body.key).strip():
        overrides.pop(body.key, None)
    else:
        overrides[body.key] = v
    cfg["prompts"] = overrides
    appconfig.save(cfg)
    return {"ok": True, "customized": body.key in overrides}


# ---------- Charge rate audit ----------

@app.get("/api/audit/chargerates")
def audit_chargerates(days: int = 90):
    """Read-only: compare the charge rate on every entry TimeScribe posted
    (last N days) against the manual timed entries on the same tickets, and
    flag mismatches. Runs in-process on the app's own Halo session."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    posted = [p for p in _drafts.posted_log(days_back=days) if p.get("ticket_id")]
    try:
        out = a.audit_chargerates(posted)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    out["days"] = days
    out["posted_considered"] = len(posted)
    return out


# ---------- Local screenshot recorder ----------

@app.get("/api/screenshots")
def screenshots_list(day: str = None):
    """The day's screenshot index (time, file, app, title) plus recorder
    status. Optional from_min/to_min filtering is done client-side."""
    from timescribe import screenshots as _shots
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "items": _shots.load_index(target),
            "status": _shots.status()}


@app.get("/api/screenshots/file")
def screenshots_file(day: str, name: str):
    """Serve one screenshot. Only files present in that day's index resolve
    (path-traversal safe)."""
    from fastapi.responses import FileResponse, Response
    from timescribe import screenshots as _shots
    target = _date.fromisoformat(day)
    p = _shots.file_path(target, name)
    if p is None:
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


class ShotsPurge(BaseModel):
    all: bool = False


@app.post("/api/screenshots/purge")
def screenshots_purge(body: ShotsPurge):
    """Delete screenshots: everything (all=true) or just those past the
    retention window."""
    from timescribe import screenshots as _shots
    if body.all:
        _shots.purge_all()
        return {"ok": True, "removed": "all"}
    days = int(appconfig.load().get("screenshots_retention_days", 14))
    return {"ok": True, "removed": _shots.purge_old(days)}


class ShotsPause(BaseModel):
    paused: bool


@app.post("/api/screenshots/pause")
def screenshots_pause(body: ShotsPause):
    """Temporarily pause/resume capture without changing the setting."""
    from timescribe import screenshots as _shots
    _shots.set_paused(body.paused)
    return {"ok": True, "paused": body.paused}


@app.get("/api/ticket")
def ticket_detail(id: int):
    """One ticket's detail, for the timesheet click-through panel."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    try:
        return a.get_ticket(id)
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ---------- Activity / digest ----------

from datetime import date as _date
from timescribe import digest as _digest
from timescribe import activitywatch as _aw


@app.get("/api/activity/status")
def activity_status():
    return {
        "activitywatch": _aw.is_available(),
    }


@app.post("/api/digest/run")
def digest_run(day: str = None, start: str = None, end: str = None):
    """Run a digest. Optional start/end (HH:MM) restrict to a window
    within the day; otherwise the whole day is digested."""
    from datetime import datetime as _dtt, time as _time
    target = _date.fromisoformat(day) if day else _date.today()
    since = until = None
    if start or end:
        s = _dtt.strptime(start or "00:00", "%H:%M").time()
        e = _dtt.strptime(end or "23:59", "%H:%M").time()
        since = _dtt.combine(target, s)
        until = _dtt.combine(target, e)
    try:
        entries = _digest.run_digest(target, since=since, until=until)
        return {"ok": True, "count": len(entries), "entries": entries}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/digest")
def digest_get(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "entries": _digest.load_digest(target)}


@app.post("/api/digest/rebuild")
def digest_rebuild(day: str = None):
    """Purge the day's stored digest AND unposted drafts, then rebuild
    from scratch (combined digest+drafts when Halo is connected and
    combined mode is on; plain digest otherwise). Posted drafts are
    always preserved."""
    from timescribe import drafts as _dr, inference as _inf
    target = _date.fromisoformat(day) if day else _date.today()
    existing = _dr.load(target)
    posted = [x for x in existing if x.get("status") == "posted"]
    # IMPORTANT: don't purge the digest or drafts until the rebuild actually
    # succeeds. Both paths below replace the day's digest (and build_combined
    # replaces unposted drafts, keeping posted) only on success -- so a
    # failure (e.g. AI credits exhausted) leaves the existing day untouched
    # instead of wiping it.
    try:
        cfg = appconfig.load()
        a = get_adapter()
        if (cfg.get("combined_digest_drafts", True)
                and a is not None and a.is_authenticated()):
            result = _inf.build_combined(a, target, replace_drafts=True)  # explicit rebuild
            entries = [e.model_dump() if hasattr(e, "model_dump") else e
                       for e in result.get("entries", [])]
        else:
            entries = _digest.run_digest(target)     # full-day: replaces digest
            _dr.save(target, posted)                 # drop unposted only after success
        return {"ok": True, "count": len(entries), "entries": entries,
                "preserved_posted": len(posted)}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------- Time entry drafts ----------

from datetime import datetime as _dt
from timescribe import inference as _inference
from timescribe import drafts as _drafts
from timescribe.psa.adapter import TimeEntry as _TimeEntry


class DraftAction(BaseModel):
    index: int
    action: str = ""     # "approve" | "reject" (unused for split)


@app.post("/api/drafts/generate")
def drafts_generate(day: str = None):
    """Import the day's digest entries directly as drafts -- NO second LLM
    pass. The digest summaries become the notes; ticket assignment is the
    technician's call (or use /api/drafts/suggest for AI matching)."""
    target = _date.fromisoformat(day) if day else _date.today()
    entries = _digest.load_digest(target)
    if not entries:
        return {"drafts": [], "error": "No digest for this day. Run the digest first."}
    existing = _drafts.load(target)
    posted = [x for x in existing if x.get("status") == "posted"]
    new_items = []
    for e in entries:
        tr = (e.get("time_range") or "").split("-")
        if len(tr) != 2:
            continue
        new_items.append({
            "ticket_id": None,
            "start_time": tr[0].strip(),
            "end_time": tr[1].strip(),
            "note": e.get("summary", ""),
            "confidence": 0.0,
            "client": "", "subject": "(unassigned)",
            "status": "draft",
        })
    new_items = _drafts.filter_ignored(target, new_items)
    # Don't re-import windows already posted / on the Halo timesheet.
    ts_rows = []
    a = get_adapter()
    if a is not None and a.is_authenticated():
        try:
            from datetime import datetime as _dtc, time as _tmc
            ts_rows = a.get_day_timesheet(
                _dtc.combine(target, _tmc.min),
                _dtc.combine(target, _tmc.max)).get("rows", [])
        except Exception:
            pass
    new_items = _drafts.filter_covered(
        new_items, _drafts.covered_windows(target, ts_rows))
    _drafts.save(target, posted + new_items)
    return {"drafts": new_items, "preserved_posted": len(posted)}


@app.post("/api/drafts/fill-gaps")
def drafts_fill_gaps(day: str = None):
    """Read the Halo timesheet, find uncovered work-hour windows, and
    propose factual draft entries from activity to fill them (AFK gaps
    become away-time entries). Appends to existing drafts."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    try:
        return _inference.fill_timesheet_gaps(a, target)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/drafts/suggest")
def drafts_suggest(day: str = None):
    """AI ticket match: rebuild the day's drafts from the EXISTING digest,
    ticket-matched in one LLM pass -- WITHOUT regenerating the digest (that's
    Purge & rebuild's job). Explicit action, so it replaces the current
    unposted drafts (posted are kept). Errors if there's no digest yet."""
    target = _date.fromisoformat(day) if day else _date.today()
    if not _digest.load_digest(target):
        return {"drafts": [], "error": "No digest for this day yet — run the "
                "digest first, or use Purge & rebuild."}
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    try:
        return _inference.generate_drafts(a, target)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/drafts/add")
def drafts_add(day: str = None):
    """Add a blank manual draft (works in every mode, incl. MCP-only)."""
    from datetime import datetime as _now_dt
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    now = _now_dt.now()
    start_m = (now.hour * 60 + now.minute) // 5 * 5
    end_m = min(start_m + 30, 23 * 60 + 55)
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    items.append({
        "ticket_id": None,
        "start_time": fmt(start_m),
        "end_time": fmt(end_m),
        "note": "",
        "confidence": 0.0,
        "client": "", "subject": "(manual entry)",
        "status": "draft",
    })
    _drafts.save(target, items)
    return {"ok": True, "index": len(items) - 1}


@app.post("/api/drafts/split")
def drafts_split(body: DraftAction, day: str = None):
    """Split a draft into two halves at its midpoint (rounded to 5 min)."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") == "posted":
        raise HTTPException(400, "cannot split a posted entry")
    try:
        sh, sm = map(int, item["start_time"].split(":"))
        eh, em = map(int, item["end_time"].split(":"))
    except (ValueError, KeyError):
        raise HTTPException(400, "draft has malformed times")
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if end_m - start_m < 10:
        raise HTTPException(400, "entry too short to split (needs >=10 min)")
    mid_m = start_m + round((end_m - start_m) / 2 / 5) * 5
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    first = dict(item, end_time=fmt(mid_m), status="draft")
    second = dict(item, start_time=fmt(mid_m), status="draft")
    items[body.index] = first
    items.insert(body.index + 1, second)
    _drafts.save(target, items)
    return {"ok": True, "count": len(items)}


@app.get("/api/drafts")
def drafts_get(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "drafts": _drafts.load(target)}


@app.post("/api/drafts/action")
def drafts_action(body: DraftAction, day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    status = {"approve": "approved", "reject": "rejected",
              "unapprove": "draft"}.get(body.action)
    if not status:
        raise HTTPException(400, "action must be approve, unapprove, or reject")
    return _drafts.set_status(target, body.index, status)


@app.post("/api/drafts/delete")
def drafts_delete(body: DraftAction, day: str = None):
    """Remove an entry from the local list. Does NOT touch anything
    already created in the PSA -- delete that in Halo if needed."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    if items[body.index].get("status") == "posted":
        raise HTTPException(400, "posted entries can't be deleted; they live in the Posted tab")
    removed = items.pop(body.index)
    _drafts.save(target, items)
    return {"ok": True, "removed": removed.get("note", "")[:60], "count": len(items)}


class DraftMerge(BaseModel):
    anchor_index: int      # entry that absorbs (keeps its ticket id)
    other_index: int       # entry merged in and removed


@app.post("/api/drafts/merge")
def drafts_merge(body: DraftMerge, day: str = None):
    """Merge `other` into `anchor`: append its note, union the time span,
    keep the anchor's ticket/client. The anchor keeps its identity; the
    other entry is removed. Neither may be posted."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    ai, oi = body.anchor_index, body.other_index
    if not (0 <= ai < len(items)) or not (0 <= oi < len(items)) or ai == oi:
        raise HTTPException(400, "bad merge indices")
    anchor, other = items[ai], items[oi]
    if anchor.get("status") == "posted" or other.get("status") == "posted":
        raise HTTPException(400, "can't merge posted entries")

    def _m(hm):
        try:
            h, m = map(int, (hm or "").split(":")); return h * 60 + m
        except (ValueError, AttributeError):
            return None
    def _fmt(x):
        return f"{x // 60:02d}:{x % 60:02d}"

    starts = [v for v in (_m(anchor.get("start_time")), _m(other.get("start_time"))) if v is not None]
    ends = [v for v in (_m(anchor.get("end_time")), _m(other.get("end_time"))) if v is not None]
    if starts:
        anchor["start_time"] = _fmt(min(starts))
    if ends:
        anchor["end_time"] = _fmt(max(ends))
    # Append notes (skip duplicates / empties).
    def _join(a, b):
        a, b = (a or "").strip(), (b or "").strip()
        if not b or b in a:
            return a
        return (a + "\n" + b).strip()
    anchor["note"] = _join(anchor.get("note"), other.get("note"))
    anchor["private_note"] = _join(anchor.get("private_note"), other.get("private_note"))
    # Absorbing means this is now hand-curated: full confidence, keep draft.
    anchor["confidence"] = max(anchor.get("confidence", 0) or 0,
                               other.get("confidence", 0) or 0)
    anchor["status"] = "draft"
    items.pop(oi)
    _drafts.save(target, items)
    # anchor index shifts down by 1 if the removed item was before it
    new_anchor = ai - 1 if oi < ai else ai
    return {"ok": True, "anchor_index": new_anchor, "count": len(items)}


@app.post("/api/drafts/ignore")
def drafts_ignore(body: DraftAction, day: str = None):
    """Remove an entry AND remember it, so future digests don't resurface
    the same activity. Only for unposted entries."""
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    if items[body.index].get("status") == "posted":
        raise HTTPException(400, "can't ignore a posted entry")
    ignored = items.pop(body.index)
    _drafts.add_ignored(target, ignored)
    _drafts.save(target, items)
    return {"ok": True, "count": len(items)}


class DraftBulk(BaseModel):
    indexes: list[int]
    action: str          # "approve" | "reject" | "delete" | "ignore"


@app.post("/api/drafts/bulk")
def drafts_bulk(body: DraftBulk, day: str = None):
    """Apply one action to many drafts at once. Indexes are positions in
    the day's draft list. Posted entries are skipped, never modified.
    Removals (delete/ignore) are processed in descending index order so
    earlier removals don't shift later indexes."""
    target = _date.fromisoformat(day) if day else _date.today()
    if body.action not in ("approve", "reject", "delete", "ignore"):
        raise HTTPException(400, "action must be approve, reject, delete or ignore")
    items = _drafts.load(target)
    idxs = sorted({i for i in body.indexes if 0 <= i < len(items)}, reverse=True)
    done = skipped = 0
    for i in idxs:
        if items[i].get("status") == "posted":
            skipped += 1
            continue
        if body.action in ("approve", "reject"):
            items[i]["status"] = "approved" if body.action == "approve" else "rejected"
        elif body.action == "delete":
            items.pop(i)
        else:  # ignore
            _drafts.add_ignored(target, items.pop(i))
        done += 1
    _drafts.save(target, items)
    return {"ok": True, "done": done, "skipped_posted": skipped,
            "count": len(items)}


@app.get("/api/drafts/ignored")
def drafts_ignored_list(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(), "ignored": _drafts.load_ignored(target)}


@app.post("/api/drafts/unignore")
def drafts_unignore(body: DraftAction, day: str = None):
    """Remove an entry from the day's ignore list (index into that list)."""
    target = _date.fromisoformat(day) if day else _date.today()
    ig = _drafts.load_ignored(target)
    if not (0 <= body.index < len(ig)):
        raise HTTPException(404, "ignored index out of range")
    ig.pop(body.index)
    _drafts._ignored_path(target).write_text(__import__("json").dumps(ig, indent=2),
                                             encoding="utf-8")
    return {"ok": True, "count": len(ig)}


class DraftUpdate(BaseModel):
    index: int
    ticket_id: Optional[int] = None      # None/blank -> Quick Time
    note: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    no_charge: Optional[bool] = None     # override ticket rate -> no charge
    private_note: Optional[str] = None   # agent-only note on the action


@app.post("/api/drafts/update")
def drafts_update(body: DraftUpdate, day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") == "posted":
        raise HTTPException(400, "cannot edit a posted entry")
    # Editing invalidates a prior approval: an approved entry drops back to
    # draft so stale approved text can't be posted without a fresh approve.
    if item.get("status") == "approved":
        item["status"] = "draft"
    item["ticket_id"] = body.ticket_id          # explicit None clears it
    if body.note is not None:
        item["note"] = body.note
    if body.no_charge is not None:
        item["no_charge"] = body.no_charge
    if body.private_note is not None:
        item["private_note"] = body.private_note
    if body.start_time:
        item["start_time"] = body.start_time
    if body.end_time:
        item["end_time"] = body.end_time
    # Re-resolve display fields for the assigned ticket
    a = get_adapter()
    item["client"] = ""
    item["subject"] = "(quick time)" if not body.ticket_id else "?"
    if body.ticket_id and a and a.is_authenticated():
        try:
            for t in a.list_open_tickets():
                if t.id == body.ticket_id:
                    item["client"], item["subject"] = t.client, t.subject
                    break
        except Exception:
            pass
    _drafts.save(target, items)
    return item


@app.post("/api/drafts/post")
def drafts_post(day: str = None):
    """Post all APPROVED drafts to the PSA."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    results = []
    for i, item in enumerate(items):
        if item.get("status") != "approved":
            continue
        try:
            posted_id = a.create_time_entry(_entry_from_item(item, target))
            _drafts.set_status(target, i, "posted", posted_id=posted_id)
            results.append({"index": i, "ok": True, "posted_id": posted_id})
        except Exception as exc:
            results.append({"index": i, "ok": False, "error": str(exc)})
    return {"results": results}


class DraftIndex(BaseModel):
    index: int


@app.post("/api/drafts/post-one")
def drafts_post_one(body: DraftIndex, day: str = None):
    """Post a SINGLE approved draft by index. Drives the client-side posting
    modal so each entry can show success/failure as it goes; a bad entry
    returns ok:false rather than aborting the whole run."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") == "posted":
        return {"ok": True, "posted_id": item.get("posted_id"), "already": True}
    if item.get("status") != "approved":
        raise HTTPException(400, "draft is not approved")
    try:
        posted_id = a.create_time_entry(_entry_from_item(item, target))
        _drafts.set_status(target, body.index, "posted", posted_id=posted_id)
        return {"ok": True, "posted_id": posted_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _entry_from_item(item: dict, target) -> "_TimeEntry":
    """Build a TimeEntry from a stored draft item, applying the no-charge
    override (chargerate 0, configurable via halo_nocharge_rate) and any
    private note."""
    cfg = appconfig.load()
    tid = item.get("ticket_id")
    charge_rate = None
    if item.get("no_charge"):
        charge_rate = float(cfg.get("halo_nocharge_rate", 0))
    return _TimeEntry(
        ticket_id=int(tid) if tid else None,
        start_local=_dt.combine(target, _dt.strptime(item["start_time"], "%H:%M").time()),
        end_local=_dt.combine(target, _dt.strptime(item["end_time"], "%H:%M").time()),
        note=item["note"],
        charge_rate=charge_rate,
        billable=not item.get("no_charge"),
        private_note=item.get("private_note") or None,
    )


@app.post("/api/drafts/repost")
def drafts_repost(body: DraftUpdate, day: str = None):
    """Re-post an already-posted entry to the PSA, applying any edits sent
    with the request first. Creates a NEW entry in the PSA and records its
    id; it does not delete the previous one."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    items = _drafts.load(target)
    if not (0 <= body.index < len(items)):
        raise HTTPException(404, "draft index out of range")
    item = items[body.index]
    if item.get("status") != "posted":
        raise HTTPException(400, "only posted entries can be re-posted")
    # Apply edits from the request (posted entries are otherwise locked).
    item["ticket_id"] = body.ticket_id
    if body.note is not None:
        item["note"] = body.note
    if body.no_charge is not None:
        item["no_charge"] = body.no_charge
    if body.private_note is not None:
        item["private_note"] = body.private_note
    if body.start_time:
        item["start_time"] = body.start_time
    if body.end_time:
        item["end_time"] = body.end_time
    # Refresh client/subject display for the (possibly new) ticket.
    item["subject"] = "(quick time)" if not body.ticket_id else item.get("subject", "?")
    if body.ticket_id:
        try:
            for t in a.list_open_tickets():
                if t.id == body.ticket_id:
                    item["client"], item["subject"] = t.client, t.subject
                    break
        except Exception:
            pass
    # Was the previous Halo action still there, or had it been deleted in
    # Halo before this repost? Reporting this avoids surprise duplicates.
    prev_id = item.get("posted_id")
    prev_exists = None
    try:
        prev_exists = a.action_exists(prev_id)
    except Exception:
        prev_exists = None
    try:
        posted_id = a.create_time_entry(_entry_from_item(item, target))
    except Exception as exc:
        # Pass Halo's actual complaint to the UI instead of a bare 500.
        raise HTTPException(502, str(exc))
    item["posted_id"] = posted_id
    item["status"] = "posted"
    _drafts.save(target, items)
    return {"ok": True, "posted_id": posted_id,
            "previous_id": prev_id, "previous_exists": prev_exists}


class ActionRef(BaseModel):
    action_id: str


@app.post("/api/halo/delete_action")
def halo_delete_action(body: ActionRef):
    """Delete a Halo action by id -- used to remove a copy posted to the
    wrong ticket after reposting it to the right one."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    try:
        a.delete_action(body.action_id)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True}


@app.get("/api/timesheet")
def timesheet(day: str = None):
    """What Halo already has on the logged-in agent's timesheet for a day."""
    a = get_adapter()
    if a is None or not a.is_authenticated():
        raise HTTPException(401, "Halo not connected")
    target = _date.fromisoformat(day) if day else _date.today()
    from datetime import time as _time
    try:
        ts = a.get_day_timesheet(_dt.combine(target, _time.min),
                                 _dt.combine(target, _time.max))
    except Exception as exc:
        raise HTTPException(502, str(exc))
    ts["rows"].sort(key=lambda r: r.get("start") or "99")
    # Split logged time into billable vs no-charge from each row's charge
    # type. Derive no-charge as (logged - billable) so the parts always sum
    # to the displayed logged total -- avoids independent-rounding drift.
    billable = sum((r.get("hours") or 0) for r in ts["rows"]
                   if "no charge" not in (r.get("charge_type") or "").lower())
    logged = ts.get("actual_hours")
    if logged is None:
        logged = sum((r.get("hours") or 0) for r in ts["rows"])
    ts["billable_hours"] = round(billable, 2)
    ts["nonbillable_hours"] = round(max(0.0, round(logged, 2) - ts["billable_hours"]), 2)
    base = (appconfig.load().get("halo_base_url") or "").rstrip("/")
    return {"day": target.isoformat(), "halo_base_url": base, **ts}


@app.get("/api/timeline")
def timeline(day: str = None):
    """Raw activity data for one day as minutes-from-midnight, for the
    timeline overlay: window focus, browser visits, AFK, and the digest."""
    from datetime import date as _d, datetime as _dtm, time as _time, timedelta as _timedelta
    from timescribe import history as _history
    target = _d.fromisoformat(day) if day else _d.today()
    since = _dtm.combine(target, _time.min)
    until = _dtm.combine(target, _time.max)

    def mins(dt):
        return round(dt.hour * 60 + dt.minute + dt.second / 60, 2)

    cfg = appconfig.load()
    try:
        visits = _history.read_enabled_history(
            since=since, until=until,
            ignore_prefixes=("chrome-extension://", "edge-extension://",
                             "moz-extension://", "chrome://", "edge://", "about:"),
            enabled_map=cfg.get("browser_profiles", {}),
            exclude_profiles=cfg.get("exclude_profiles", []),
            edge_override=cfg.get("edge_user_data_dir") or None)
    except Exception as exc:
        print(f"[timeline] history read failed: {exc}")
        visits = []
    windows = _aw.get_window_events(since, until)
    afk = _aw.get_inactivity_periods(since, until)

    win_out = []
    for w in windows:
        end = w["time"] + _timedelta(seconds=w["duration_s"])
        win_out.append({"start": mins(w["time"]), "end": mins(end),
                        "app": w["app"], "title": w["title"],
                        "clock": w["time"].strftime("%H:%M")})
    vis_out = [{"t": mins(v["time"]), "domain": v["domain"],
                "title": v["title"], "profile": v["profile"],
                "url": v.get("url", ""),
                "profile_folder": v.get("profile_folder", ""),
                "browser_key": v.get("browser_key", ""),
                "clock": v["time"].strftime("%H:%M")} for v in visits]
    afk_out = [{"start": mins(a["start"]), "end": mins(a["end"]),
                "minutes": a["minutes"]} for a in afk]

    digest_out = []
    for e in _digest.load_digest(target):
        tr = (e.get("time_range") or "").split("-")
        if len(tr) == 2:
            try:
                sh, sm = map(int, tr[0].strip().split(":"))
                eh, em = map(int, tr[1].strip().split(":"))
                digest_out.append({"start": sh * 60 + sm, "end": eh * 60 + em,
                                   "summary": e.get("summary", "")})
            except ValueError:
                pass

    span = ([w["start"] for w in win_out] + [w["end"] for w in win_out]
            + [v["t"] for v in vis_out])
    lo = min(span) if span else 8 * 60
    hi = max(span) if span else 18 * 60
    return {"day": target.isoformat(),
            "start_min": max(0, int(lo // 60 * 60)),
            "end_min": min(1440, int(-(-hi // 60) * 60)),
            "windows": win_out, "visits": vis_out,
            "afk": afk_out, "digest": digest_out}


# ---------- Logs ----------

@app.get("/api/logs")
def get_logs(lines: int = 200):
    """Tail of app.log for the Settings > Logs view."""
    from pathlib import Path
    from platformdirs import user_data_dir
    log_path = Path(user_data_dir("timescribe")) / "logs" / "app.log"
    if not log_path.exists():
        return {"path": str(log_path), "lines": ["(no log file yet)"]}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return {"path": str(log_path), "lines": [f"(could not read log: {exc})"]}
    lines = max(10, min(lines, 2000))
    return {"path": str(log_path), "lines": [l.rstrip("\n") for l in all_lines[-lines:]]}


# ---------- MCP config helper ----------

@app.get("/api/mcp/config")
def mcp_config():
    """Ready-to-paste MCP registration for Claude Desktop / Cowork / Claude Code."""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        command = _sys.executable
        args = ["mcp"]
    else:
        # The tray app runs under pythonw.exe (no console window), but MCP
        # servers must use python.exe: Claude Desktop talks to the server
        # over stdio, and pythonw detaches from it -> "Server disconnected".
        command = _sys.executable
        if command.lower().endswith("pythonw.exe"):
            command = command[:-len("pythonw.exe")] + "python.exe"
        args = ["-m", "timescribe.mcp_server"]
    entry = {"command": command, "args": args}
    import json as _json
    return {
        "server_name": "timescribe-activity",
        "entry": entry,
        "claude_desktop_json": _json.dumps(
            {"mcpServers": {"timescribe-activity": entry}}, indent=2),
        "claude_code_cmd": ("claude mcp add timescribe-activity --scope user -- "
                            + command + " " + " ".join(args)),
    }


# ---------- Posted log + summary ----------

@app.get("/api/posted")
def posted(days: int = 14):
    cfg = appconfig.load()
    base = (cfg.get("halo_base_url") or "").rstrip("/")
    items = _drafts.posted_log(days_back=days)
    for it in items:
        tid = it.get("ticket_id")
        it["halo_url"] = f"{base}/tickets?id={tid}" if (base and tid) else None
    return {"days": days, "count": len(items), "entries": items}


@app.get("/api/summary")
def summary(day: str = None):
    target = _date.fromisoformat(day) if day else _date.today()
    return {"day": target.isoformat(),
            **_drafts.day_summary(target, _digest.load_digest(target))}


# ---------- Scheduler ----------

from timescribe import scheduler as _scheduler


# ---------- Self-update (source/git installs) ----------

def _repo_dir():
    """The git working tree, or None for frozen/installer builds."""
    import sys
    if getattr(sys, "frozen", False):
        return None
    d = Path(__file__).resolve().parent.parent
    return d if (d / ".git").exists() else None


def _git(args, cwd, timeout=60):
    import subprocess
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


@app.get("/api/update/status")
def update_status(fetch: bool = True):
    """Report whether the local checkout is behind its remote branch."""
    repo = _repo_dir()
    if repo is None:
        return {"git": False,
                "note": "Not a source install — update via the installer/GitHub release."}
    try:
        _, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        if fetch:
            _git(["fetch", "--quiet", "origin"], repo, timeout=90)
        _, local, _ = _git(["rev-parse", "HEAD"], repo)
        rc, remote, _ = _git(["rev-parse", f"origin/{branch}"], repo)
        _, behind, _ = _git(["rev-list", "--count", f"HEAD..origin/{branch}"], repo)
        _, latest, _ = _git(["log", "-1", "--format=%h %s", f"origin/{branch}"], repo)
        _, curmsg, _ = _git(["log", "-1", "--format=%h %s"], repo)
        _, dirty, _ = _git(["status", "--porcelain"], repo)
        n = int(behind) if behind.isdigit() else 0
        return {"git": True, "branch": branch, "behind": n,
                "current": curmsg, "latest": latest,
                "dirty": bool(dirty.strip()),
                "up_to_date": n == 0 and rc == 0}
    except Exception as exc:
        return {"git": True, "error": str(exc)}


@app.post("/api/update/apply")
def update_apply():
    """Fast-forward pull the current branch and refresh dependencies.
    A restart is required afterward to load the new backend code."""
    import sys
    repo = _repo_dir()
    if repo is None:
        raise HTTPException(400, "Not a source install")
    _, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    _, dirty, _ = _git(["status", "--porcelain"], repo)
    if dirty.strip():
        raise HTTPException(409, "Local changes present — resolve them before updating.")
    rc, out, err = _git(["pull", "--ff-only", "origin", branch], repo, timeout=120)
    if rc != 0:
        raise HTTPException(502, f"git pull failed: {err or out}")
    # Pick up any new/updated dependencies (best-effort, non-fatal).
    dep_note = ""
    try:
        import subprocess
        pr = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(repo),
                             "--quiet"], capture_output=True, text=True, timeout=300,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if pr.returncode != 0:
            dep_note = " (dependency refresh reported an issue; check logs)"
            print(f"[update] pip install issue: {pr.stderr[:400]}")
    except Exception as exc:
        dep_note = " (couldn't refresh dependencies automatically)"
        print(f"[update] pip step failed: {exc}")
    print(f"[update] pulled {branch}: {out}")
    return {"ok": True, "output": out,
            "message": "Updated. Quit and relaunch TimeScribe to apply." + dep_note}


@app.post("/api/restart")
def restart_app():
    """Relaunch TimeScribe: spawn a detached helper that waits for this
    process to exit (freeing the port), then starts a fresh instance."""
    import sys, subprocess, os, threading
    py = sys.executable  # pythonw for the tray app
    try:
        # cmd helper: wait ~2s for the old process to die, then relaunch.
        subprocess.Popen(
            ["cmd", "/c", f'timeout /t 2 /nobreak >nul & start "" "{py}" -m timescribe app'],
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           | getattr(subprocess, "DETACHED_PROCESS", 0)),
        )
    except Exception as exc:
        raise HTTPException(500, f"couldn't schedule relaunch: {exc}")
    # Give the HTTP response time to return, then hard-exit the whole process.
    threading.Timer(0.7, lambda: os._exit(0)).start()
    return {"ok": True, "message": "Restarting TimeScribe…"}


@app.post("/api/open-dashboard")
def open_dashboard_ep():
    """Let a second launch ask the running instance to show its dashboard
    window (reusing the existing one) instead of spawning another."""
    try:
        from timescribe import app as _app
        _app.open_dashboard()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return {"ok": True}


@app.get("/api/scheduler/status")
def scheduler_status():
    return _scheduler.status()
