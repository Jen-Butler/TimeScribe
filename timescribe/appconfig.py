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
    "llm_provider": "anthropic",
    "openai_model_default": "gpt-4o-mini",
    "digest_interval_minutes": 120,
    "eod_time": "17:15",
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
