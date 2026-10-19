"""Mobile access: a second listener on the LAN, locked to paired devices.

Design
- The dashboard keeps binding 127.0.0.1 only. When mobile access is enabled
  a second uvicorn server serves the SAME FastAPI app on the LAN (0.0.0.0,
  mobile_port). Nothing is exposed beyond the local network; the gate below
  also refuses any client that is not on a private/link-local address.
- Pairing: the PC mints a one-time code (10 min) shown as a QR + URL. The
  phone opens /m/pair?code=... once; the server answers with a long-lived
  device credential in an HttpOnly cookie and remembers only its SHA-256.
  From then on the phone is recognised until revoked from Settings. No app
  data is stored on the phone -- just that opaque key.
- Remote (non-loopback) requests are allow-listed to the endpoints the
  phone page needs. Settings, secrets, logs, update/restart, screenshots
  and the mobile admin endpoints are loopback-only regardless of cookie.
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import socket
import threading
import time
from datetime import datetime
from typing import Optional

from timescribe import appconfig

COOKIE = "ts_device"
PAIR_TTL_S = 600            # a pairing code lives 10 minutes
TOKEN_MAX_AGE_S = 365 * 24 * 3600

_pending: dict[str, float] = {}          # code -> expiry epoch
_lock = threading.Lock()
_server = None                           # uvicorn.Server for the LAN listener
_server_thread: Optional[threading.Thread] = None
_last_seen_write: dict[str, float] = {}


# ---------- helpers ----------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_loopback(host: str) -> bool:
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def is_private(host: str) -> bool:
    """LAN / VPN addresses only. Refuses public internet sources even if a
    router were misconfigured to forward the port."""
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback


def lan_ip() -> str:
    """Best-guess LAN IPv4: the interface the OS would use to reach the
    internet (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def port() -> int:
    return int(appconfig.load().get("mobile_port", 8771) or 8771)


def enabled() -> bool:
    return bool(appconfig.load().get("mobile_enabled", False))


def base_url() -> str:
    return f"http://{lan_ip()}:{port()}"


# ---------- pairing ----------

def new_pairing_code() -> dict:
    """Mint a one-time pairing code. Human-typable (no 0/O/1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    exp = time.time() + PAIR_TTL_S
    with _lock:
        # keep the table small: drop expired codes
        for c, e in list(_pending.items()):
            if e < time.time():
                _pending.pop(c, None)
        _pending[code] = exp
    url = f"{base_url()}/m/pair?code={code}"
    return {"code": code, "url": url, "expires_in": PAIR_TTL_S, "qr_png": _qr_svg(url)}


def _qr_svg(text: str) -> str:
    """QR as a data: URI (PNG, 4-module quiet zone, big modules). A PNG with a
    generous quiet zone scans as a link reliably; small vector QRs on screen
    tend to be read by phone cameras as a 'document'."""
    try:
        import base64
        import io
        import segno
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="png", scale=10, border=4,
                                         dark="#000000", light="#ffffff")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:              # segno missing -> type the URL instead
        print(f"[mobile] QR unavailable: {exc}")
        return ""


def redeem_pairing_code(code: str, device_name: str) -> Optional[str]:
    """Consume a pairing code; returns the new device token (plaintext, to
    be set as the cookie) or None if the code is unknown/expired."""
    code = (code or "").strip().upper().replace("-", "")
    with _lock:
        exp = _pending.pop(code, None)
    if not exp or exp < time.time():
        return None
    token = secrets.token_urlsafe(32)
    cfg = appconfig.load()
    devices = list(cfg.get("mobile_devices") or [])
    devices.append({
        "id": secrets.token_hex(4),
        "name": (device_name or "Phone")[:80],
        "hash": _hash(token),
        "created": datetime.now().isoformat(timespec="seconds"),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    })
    cfg["mobile_devices"] = devices
    appconfig.save(cfg)
    print(f"[mobile] paired new device: {devices[-1]['name']} ({devices[-1]['id']})")
    return token


def verify_token(token: Optional[str]) -> Optional[dict]:
    """Return the device record for a valid cookie token, else None.
    Touches last_seen at most once a minute per device."""
    if not token:
        return None
    h = _hash(token)
    cfg = appconfig.load()
    for d in cfg.get("mobile_devices") or []:
        if secrets.compare_digest(d.get("hash", ""), h):
            now = time.time()
            if now - _last_seen_write.get(d["id"], 0) > 60:
                _last_seen_write[d["id"]] = now
                d["last_seen"] = datetime.now().isoformat(timespec="seconds")
                try:
                    appconfig.save(cfg)
                except Exception:
                    pass
            return d
    return None


def list_devices() -> list[dict]:
    return [{k: v for k, v in d.items() if k != "hash"}
            for d in (appconfig.load().get("mobile_devices") or [])]


def revoke_device(device_id: str) -> bool:
    cfg = appconfig.load()
    before = cfg.get("mobile_devices") or []
    after = [d for d in before if d.get("id") != device_id]
    if len(after) == len(before):
        return False
    cfg["mobile_devices"] = after
    appconfig.save(cfg)
    print(f"[mobile] revoked device {device_id}")
    return True


# ---------- the LAN listener ----------

def listening() -> bool:
    return bool(_server is not None and _server_thread is not None
                and _server_thread.is_alive() and not _server.should_exit)


def start_listener(fastapi_app) -> None:
    """Start the LAN listener (idempotent). Runs uvicorn in a daemon thread
    so it can be stopped/restarted when the setting or port changes."""
    global _server, _server_thread
    if listening():
        return
    import uvicorn
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port(),
                            log_level="warning")
    _server = uvicorn.Server(config)
    _server.install_signal_handlers = lambda: None     # not the main thread

    def _run():
        try:
            _server.run()
        except Exception as exc:
            print(f"[mobile] LAN listener stopped: {exc}")

    _server_thread = threading.Thread(target=_run, daemon=True, name="mobile-listener")
    _server_thread.start()
    print(f"[mobile] listening on {base_url()}")


def stop_listener() -> None:
    global _server, _server_thread
    if _server is not None:
        _server.should_exit = True
        if _server_thread is not None:
            _server_thread.join(timeout=5)
        print("[mobile] LAN listener stopped")
    _server = None
    _server_thread = None


def apply(fastapi_app) -> None:
    """Reconcile the listener with the current setting (called at startup
    and after Settings are saved)."""
    if enabled():
        if listening() and _server.config.port != port():
            stop_listener()
        start_listener(fastapi_app)
    else:
        stop_listener()


# ---------- Windows Firewall ----------

FW_RULE = "TimeScribe Mobile"


_fw_cache: dict = {"at": 0.0, "value": None, "running": False}


def _fw_query() -> Optional[bool]:
    """Parse `netsh advfirewall firewall show rule name=all dir=in`: fast,
    unprivileged, and -- unlike Get-NetFirewallPortFilter -- doesn't abort
    with 'Access is denied' on protected rules. A rule counts if it is
    enabled, Allow, TCP or Any protocol, and its LocalPort explicitly lists
    the mobile port (a single port, a list, or a range). Program rules with
    LocalPort 'Any' are ignored: they don't open the port for this process."""
    import re
    import subprocess
    p = port()
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                            "name=all", "dir=in"], capture_output=True, text=True,
                           timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        print(f"[mobile] firewall check failed: {exc}")
        return None
    text = r.stdout or ""
    if not text.strip():
        return None

    def covers(ports: str) -> bool:
        for part in ports.replace(" ", "").split(","):
            if not part or part.lower() == "any":
                continue
            if part.isdigit() and int(part) == p:
                return True
            m = re.fullmatch(r"(\d+)-(\d+)", part)
            if m and int(m.group(1)) <= p <= int(m.group(2)):
                return True
        return False

    for block in re.split(r"\n\s*\n", text):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip().lower()] = v.strip()
        if not fields.get("rule name"):
            continue
        if fields.get("enabled", "").lower() != "yes":
            continue
        if fields.get("action", "").lower() != "allow":
            continue
        if fields.get("protocol", "").upper() not in ("TCP", "ANY"):
            continue
        if covers(fields.get("localport", "")):
            return True
    return False


def firewall_rule_exists(force: bool = False) -> Optional[bool]:
    """True if an enabled inbound Allow rule explicitly covers the mobile
    port (any name -- a hand-made rule counts); False if none; None while
    the check is still running. The PowerShell query takes several seconds,
    so it runs in a background thread and the result is cached for 5 min."""
    import sys
    if not sys.platform.startswith("win"):
        return None
    fresh = time.time() - _fw_cache["at"] < 300
    if (fresh and not force) or _fw_cache["running"]:
        return _fw_cache["value"] if fresh else None

    def _run():
        try:
            _fw_cache["value"] = _fw_query()
            _fw_cache["at"] = time.time()
        finally:
            _fw_cache["running"] = False

    _fw_cache["running"] = True
    threading.Thread(target=_run, daemon=True, name="fw-check").start()
    return _fw_cache["value"] if fresh else None


def open_firewall() -> str:
    """Add an inbound allow rule for the mobile port on PRIVATE networks
    only, via an elevated PowerShell (Windows shows a UAC prompt)."""
    import subprocess
    p = port()
    inner = (f"netsh advfirewall firewall delete rule name=\"{FW_RULE}\" | Out-Null; "
             f"netsh advfirewall firewall add rule name=\"{FW_RULE}\" dir=in action=allow "
             f"protocol=TCP localport={p} profile=private")
    ps = f"Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','{inner}'"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, text=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    firewall_rule_exists(force=True)
    return "requested"
