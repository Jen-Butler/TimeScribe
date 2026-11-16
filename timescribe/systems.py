"""Known systems: tell the AI what the domains in the activity feed ARE.

A custom Halo hostname (service.example.net) means nothing to the model, so
when another product (Freshservice, say) is prominent in the day's context it
starts calling Halo tickets by the wrong name. This module builds a legend of
domain -> system label from the configured Halo URL plus a few well-known
products, and the user can add their own in Settings (one `domain = label`
per line). The legend is stated in every prompt and each browser visit in
the feed is tagged with its system.
"""
from __future__ import annotations

from urllib.parse import urlparse

from timescribe import appconfig

# Built-in knowledge of common MSP tooling (substring of the host -> label).
BUILTIN = [
    ("halopsa.com", "HaloPSA (a client's HaloPSA instance)"),
    ("haloitsm.com", "HaloITSM"),
    ("freshservice.com", "Freshservice (a client's ITSM -- NOT our PSA)"),
    ("freshdesk.com", "Freshdesk"),
    ("servicenow.com", "ServiceNow"),
    ("service-now.com", "ServiceNow"),
    ("connectwise.com", "ConnectWise"),
    ("myconnectwise.net", "ConnectWise Manage"),
    ("autotask.net", "Autotask"),
    ("rewst.io", "Rewst (automation platform)"),
    ("ninjarmm.com", "NinjaOne RMM"),
    ("ninjaone.com", "NinjaOne RMM"),
    ("n-able.com", "N-able"),
    ("portal.azure.com", "Azure portal"),
    ("entra.microsoft.com", "Microsoft Entra admin"),
    ("admin.microsoft.com", "Microsoft 365 admin"),
    ("github.com", "GitHub"),
    ("teams.microsoft.com", "Microsoft Teams"),
    ("outlook.office.com", "Outlook web"),
    ("claude.ai", "Claude (AI assistant)"),
    ("chatgpt.com", "ChatGPT"),
]


def _halo_host() -> str:
    url = (appconfig.load().get("halo_base_url") or "").strip()
    if not url:
        return ""
    try:
        return (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except Exception:
        return ""


def legend() -> list[tuple[str, str]]:
    """Ordered (host-substring, label). Most specific first: the user's own
    Halo host, then user-defined entries, then built-ins."""
    out: list[tuple[str, str]] = []
    h = _halo_host()
    if h:
        out.append((h, "HaloPSA -- OUR OWN PSA (ticket numbers / '#NNNN' seen here are HaloPSA tickets)"))
    cfg = appconfig.load()
    for k, v in (cfg.get("known_systems") or {}).items():
        k = (k or "").strip().lower()
        if k and v:
            out.append((k, str(v).strip()))
    seen = {k for k, _ in out}
    for k, v in BUILTIN:
        if k not in seen:
            out.append((k, v))
    return out


def label_for(domain: str) -> str:
    d = (domain or "").lower()
    if not d:
        return ""
    for k, v in legend():
        if k in d:
            return v
    return ""


def legend_text() -> str:
    """Prompt block stating what each domain is. Short labels only."""
    rows = legend()
    if not rows:
        return ""
    lines = [f"- {k} = {v}" for k, v in rows[:40]]
    aw = aw_rules() if appconfig.load().get("aw_categories_enabled") else []
    aw_note = ("\n- {AW:<category>} tags are the technician's OWN ActivityWatch categories "
               "(their regex rules on app/title/url); treat them as authoritative." if aw else "")
    return ("KNOWN SYSTEMS (what the domains in the feed are; name systems by THESE labels, "
            "never guess from surrounding context):\n" + "\n".join(lines) + aw_note +
            "\n- A ticket number seen on a HaloPSA page is a HaloPSA ticket. Call a ticket a "
            "Freshservice/ServiceNow/other-system ticket ONLY when the URL is that system's domain.")


# ---------- ActivityWatch categories ----------
#
# AW's web UI lets the user classify activity with regex rules (Settings ->
# Categorization). Those rules live in aw-server's settings as `classes`:
# [{"name": ["Work", "Programming"], "rule": {"type": "regex", "regex": "...",
# "ignore_case": bool}}]. We read them live (cached a few minutes) and tag
# feed lines with the deepest matching category, the same way AW does, so a
# rule like  HaloPSA -> service.risingtidegroup.net|halopsa  classifies the
# feed for the AI without any TimeScribe-side config.

import re as _re
import time as _time

_aw_cache = {"at": 0.0, "rules": []}


def aw_rules(max_age_s: int = 300) -> list:
    """[(name:'Work > Programming', compiled_regex)] deepest-first."""
    if _time.time() - _aw_cache["at"] < max_age_s:
        return _aw_cache["rules"]
    rules = []
    try:
        import httpx
        base = appconfig.load().get("aw_url", "http://localhost:5600").rstrip("/")
        r = httpx.get(f"{base}/api/0/settings", timeout=3, trust_env=False)
        for c in (r.json().get("classes") or []):
            rule = c.get("rule") or {}
            if rule.get("type") != "regex" or not rule.get("regex"):
                continue
            flags = _re.IGNORECASE if rule.get("ignore_case") else 0
            try:
                rx = _re.compile(rule["regex"], flags)
            except _re.error:
                continue
            name = " > ".join(c.get("name") or [])
            rules.append((name, rx, len(c.get("name") or [])))
        rules.sort(key=lambda t: -t[2])          # deepest category wins
    except Exception as exc:
        print(f"[systems] AW categories unavailable: {exc}")
    _aw_cache.update(at=_time.time(), rules=[(n, rx) for n, rx, _ in rules])
    return _aw_cache["rules"]


def aw_category(*texts: str) -> str:
    """Deepest AW category whose regex matches any of the texts (app, title,
    url, domain), or ''."""
    hay = " ".join(t for t in texts if t)
    if not hay:
        return ""
    for name, rx in aw_rules():
        if rx.search(hay):
            return name
    return ""


def tags_for(domain: str = "", app: str = "", title: str = "", url: str = "") -> str:
    """The `{...}` suffix for a feed line: system label from the legend and/or
    the user's ActivityWatch category."""
    parts = []
    sysl = label_for(domain)
    if sysl:
        parts.append(sysl.split(" -- ")[0].split(" (")[0])
    if appconfig.load().get("aw_categories_enabled"):
        cat = aw_category(app, title, url, domain)
        if cat:
            parts.append("AW:" + cat)
    return " {" + "; ".join(parts) + "}" if parts else ""


# ---------- browser profiles: clients vs the home profile ----------

def _norm(x: str) -> str:
    return _re.sub(r"[^a-z0-9]", "", (x or "").lower())


def home_profiles(names: list) -> list:
    """Profiles that are the technician's OWN company rather than a client.
    Configured in Settings (home_profiles), else auto-detected: a profile
    whose name appears in the Halo hostname ('Rising Tide' ->
    service.risingtidegroup.net)."""
    cfg = appconfig.load()
    conf = [p.strip() for p in (cfg.get("home_profiles") or []) if p and p.strip()]
    if conf:
        low = {c.lower() for c in conf}
        return [n for n in names if n.lower() in low] or conf
    host = _norm(_halo_host())
    return [n for n in names if len(_norm(n)) >= 4 and _norm(n) in host]


def profiles_text() -> str:
    """Prompt block: which browser profiles are clients and which is home."""
    try:
        from timescribe import history
        cfg = appconfig.load()
        enabled = cfg.get("browser_profiles", {})
        excl = {x.strip().lower() for x in cfg.get("exclude_profiles", []) if x.strip()}
        names = []
        for key, _name, folder, friendly, _hp in history.discover_all_sources(cfg.get("edge_user_data_dir") or None):
            default = not (folder.lower() in excl or friendly.lower() in excl)
            if enabled.get(f"{key}::{folder}", default) and friendly not in names:
                names.append(friendly)
    except Exception as exc:
        print(f"[systems] profile list unavailable: {exc}")
        return ""
    if not names:
        return ""
    home = home_profiles(names)
    generic = _re.compile(r"^(profile \d+|person \d+|default.*|me|work|personal|guest)$", _re.I)
    other = [n for n in names if n not in home and generic.match(n.strip())]
    clients = [n for n in names if n not in home and n not in other]
    out = "BROWSER PROFILES (the [Profile] tag on browser visits):\n"
    if home:
        out += ("- HOME (the technician's own company, used for general work): " + ", ".join(home) +
                ". Activity here is NOT automatically internal -- if it names a client, it is that client's work.\n")
    if clients:
        out += "- CLIENT profiles (each is one client): " + ", ".join(clients[:60]) + "\n"
    if other:
        out += "- Unnamed/personal profiles (no client implied): " + ", ".join(other) + "\n"
    return out.rstrip()
