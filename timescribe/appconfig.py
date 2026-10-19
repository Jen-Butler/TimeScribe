"""App config stored at %APPDATA%/timescribe/config.json.
Secrets (Anthropic key) go to Windows Credential Manager via keyring,
never into this file.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict

import keyring
from platformdirs import user_config_dir

APP_NAME = "timescribe"
KEYRING_SERVICE = "timescribe"

DEFAULTS: Dict[str, Any] = {
    "psa_type": "halo",
    "halo_base_url": "",
    "halo_client_id": "",
    # How TimeScribe signs in to Halo:
    #   "pkce"               browser sign-in as you (Authorisation Code +
    #                        PKCE, loopback redirect). Default.
    #   "client_credentials" API application + secret; no browser and no
    #                        redirect URI. For tenants whose CDN/WAF blocks
    #                        loopback redirects. Secret -> credential store.
    "halo_auth_mode": "pkce",
    # client_credentials only: the token is an application, not a person, so
    # the agent to act as (timesheet to read, who time is attributed to).
    "halo_agent_id": 0,
    "anthropic_model_default": "claude-haiku-4-5",
    "anthropic_model_planner": "claude-sonnet-4-6",
    "timezone": "America/New_York",
    "work_start": "09:00",
    "work_end": "17:00",
    "lunch_start": "12:00",
    "lunch_end": "13:00",
    "edge_user_data_dir": "",
    "exclude_profiles": [],
    "ui_port": 8770,
    "auto_digest_enabled": True,
    "aw_host": "http://127.0.0.1:5600",
    "llm_provider": "anthropic",   # anthropic | openai | halo_org | mcp
    "openai_model_default": "gpt-4o-mini",
    "openai_model_planner": "gpt-4o",
    # Org-shared AI: admin stores the key in an ENCRYPTED custom field on a
    # Halo client (the org's own/house client). Connected clients read it
    # back via the authenticated API. Never plaintext, never in this file.
    "org_ai_client_id": 0,             # Halo client id holding the field
    "org_ai_field_name": "CFTimeScribeAIKey",   # encrypted custom field name
    "org_ai_provider": "openai",       # underlying provider of the org key
    "org_ai_model_default": "gpt-4o-mini",
    "org_ai_model_planner": "gpt-4o",
    "digest_interval_minutes": 120,
    # Global noise floor: app-focus windows shorter than this are dropped
    # from the activity view, the digest feed and ticket matching alike.
    "min_focus_seconds": 10,
    # Mobile access: second listener on the LAN for paired phones only.
    "mobile_enabled": False,
    "mobile_port": 8771,          # HTTP (pairing/setup); HTTPS uses port+1
    "mobile_https": True,         # local-CA HTTPS so the phone can "Install app"
    "mobile_devices": [],
    "eod_time": "17:15",
    # Which weekdays the scheduler runs (0=Mon .. 6=Sun). Default Mon-Fri.
    "digest_weekdays": [0, 1, 2, 3, 4],
    # Combined pass: build ticket-matched drafts during periodic digests,
    # not just at EOD. One LLM call does digest + matching together.
    "combined_digest_drafts": True,
    # Which browser profiles to read. "browserkey::folder" -> bool. Absent
    # source defaults to enabled (unless in legacy exclude_profiles).
    "browser_profiles": {},
    # Edge profile-directory (folder name, e.g. "Profile 1") that ticket
    # links open in. "" = the system default browser/profile.
    "ticket_browser_profile": "",
    # Default sort for the draft list: smart | start_asc | start_desc |
    # end_asc | end_desc.
    "draft_sort_default": "smart",
    # Pomodoro durations (minutes), set in Settings.
    "pomo_focus_min": 25,
    "pomo_break_min": 5,
    "pomo_long_min": 15,
    # --- Appearance (Settings > Appearance) ---
    # Font family applied to the whole app. "" = default (Barlow). A curated
    # value like "Georgia, serif" or the literal "custom" (use the uploaded
    # font). See ui_font_custom.
    "ui_font": "",
    # Filename of a user-uploaded font stored under <configdir>/fonts.
    # Served at /api/font/current and referenced via an injected @font-face.
    "ui_font_custom": "",
    # Per-variable colour overrides, keyed WITHOUT the leading dashes, e.g.
    # {"accent": "#5980a6", "bg": "#f2f2f3"}. Applied on top of the active
    # light/dark base. {} = no overrides (pure default theme).
    "ui_colors": {},
    # User overrides for the LLM system prompts, keyed by prompt id
    # (digest | ticket_match | combined | gap_fill). Absent/empty = use the
    # built-in default. See timescribe/prompts.py.
    "prompts": {},
    # --- Local screenshot recorder (Settings > Screenshots). OPT-IN; images
    # stay on this machine and are never sent to Halo. ---
    "screenshots_enabled": False,
    "screenshots_interval_min": 2,        # periodic shot cadence while active
    "screenshots_retention_days": 14,     # auto-purge older day folders
    "screenshots_max_width": 0,           # 0 = full resolution (retention keeps disk bounded)
    "screenshots_quality": 80,            # JPEG quality
    "screenshots_idle_skip_s": 60,        # no input this long -> skip capture
    "screenshots_exclude_apps": [],       # exe names never to capture
}


def _path() -> Path:
    return Path(user_config_dir(APP_NAME)) / "config.json"


def load() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    p = _path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save(cfg: Dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Never persist secrets even if a caller sneaks one in
    clean = {k: v for k, v in cfg.items() if "api_key" not in k}
    p.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def set_secret(name: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, name, value)


def get_secret(name: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, name)


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass
