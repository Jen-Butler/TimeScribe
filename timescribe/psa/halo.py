"""HaloPSA adapter. Uses OAuth 2.0 Authorization Code + PKCE (no client
secret needed) to authenticate on behalf of the user. Refresh tokens stored
via keyring (Windows Credential Manager on Windows, macOS Keychain on Mac,
Secret Service on Linux).
"""
from __future__ import annotations
import json
import time
from datetime import datetime
from typing import List, Optional
from urllib.parse import urljoin

import httpx
import keyring

from timescribe.psa.adapter import (
    PSAAdapter, Ticket, TimeEntry, CalendarEvent,
)
from timescribe.oauth.pkce import (
    generate_pkce_pair, generate_state, build_authorize_url,
    start_callback_server, open_browser,
    exchange_code_for_tokens, refresh_access_token,
    CALLBACK_PORT, CALLBACK_PATH,
)


KEYRING_SERVICE = "timescribe.halo"
KEYRING_KEY = "refresh_token"


class HaloPSAAdapter(PSAAdapter):
    def __init__(self, base_url: str, client_id: str, tenant_key: str = "default"):
        """
        base_url:   e.g. 'https://yourcompany.halopsa.com'
        client_id:  from the OAuth app you registered in Halo admin
        tenant_key: identifier for this tenant's stored token
                    (lets one machine hold tokens for multiple Halo instances)
        """
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.tenant_key = tenant_key
        self._access_token: Optional[str] = None
        self._access_token_expires_at: float = 0.0

    @property
    def name(self) -> str:
        return "HaloPSA"

    # --- endpoint helpers ---

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.base_url}/auth/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/auth/token"

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/api"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

    # --- credential storage ---

    def _keyring_key(self) -> str:
        return f"{self.tenant_key}:{KEYRING_KEY}"

    def _load_refresh_token(self) -> Optional[str]:
        return keyring.get_password(KEYRING_SERVICE, self._keyring_key())

    def _save_refresh_token(self, token: str) -> None:
        keyring.set_password(KEYRING_SERVICE, self._keyring_key(), token)

    def _clear_refresh_token(self) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, self._keyring_key())
        except keyring.errors.PasswordDeleteError:
            pass

    # --- PSAAdapter interface ---

    def is_authenticated(self) -> bool:
        # Either we have a live access_token, or we have a refresh_token we can use.
        if self._access_token and time.time() < self._access_token_expires_at - 30:
            return True
        return self._load_refresh_token() is not None

    def connect(self) -> None:
        """Full OAuth PKCE flow. Opens the user's browser, waits for callback."""
        if self.is_authenticated():
            try:
                self._ensure_access_token()
                return
            except Exception as exc:
                # Stored refresh token is stale/revoked (Halo rotates them,
                # and re-registering the OAuth app invalidates old ones).
                # Fall through to a fresh browser login instead of dying.
                print(f"[halo] stored token refresh failed ({exc}); starting fresh login")
                self._clear_refresh_token()
                self._access_token = None

        from timescribe import appconfig as _appconfig
        verifier, challenge = generate_pkce_pair()
        state = generate_state()
        url = build_authorize_url(
            authorize_endpoint=self.authorize_endpoint,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            code_challenge=challenge,
            state=state,
            scope="all",
            tenant=_appconfig.load().get("halo_tenant", ""),
        )
        print(f"[halo] Opening browser for Halo authentication...")
        print(f"[halo] If the browser doesn't open, paste this URL: {url}")
        open_browser(url)
        result = start_callback_server(timeout_seconds=300)
        if result.error:
            raise RuntimeError(f"OAuth failed: {result.error} — {result.error_description}")
        if result.state != state:
            raise RuntimeError("OAuth state mismatch (possible CSRF); refusing to continue")
        if not result.code:
            raise RuntimeError("OAuth returned no code")

        tokens = exchange_code_for_tokens(
            token_endpoint=self.token_endpoint,
            client_id=self.client_id,
            code=result.code,
            code_verifier=verifier,
            redirect_uri=self.redirect_uri,
        )
        self._access_token = tokens.get("access_token")
        self._access_token_expires_at = time.time() + int(tokens.get("expires_in", 3600))
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise RuntimeError("Halo did not return a refresh_token; check OAuth app config")
        self._save_refresh_token(refresh)
        print(f"[halo] Authenticated. Access token expires in {tokens.get('expires_in')}s.")

    def _ensure_access_token(self) -> str:
        """Return a valid access_token, refreshing if needed."""
        if self._access_token and time.time() < self._access_token_expires_at - 30:
            return self._access_token
        refresh = self._load_refresh_token()
        if not refresh:
            raise RuntimeError("No stored refresh token; call connect() first")
        try:
            tokens = refresh_access_token(
                token_endpoint=self.token_endpoint,
                client_id=self.client_id,
                refresh_token=refresh,
            )
        except Exception as exc:
            # Refresh token revoked/expired (e.g. the Halo application's
            # permissions were changed, which invalidates all its tokens).
            # Clear it so the UI shows "not connected" instead of endless
            # background failures.
            print(f"[halo] refresh token rejected ({exc}); clearing stored token")
            self._clear_refresh_token()
            self._access_token = None
            raise RuntimeError(
                "Halo session expired - reconnect via Settings > Connect to Halo") from exc
        self._access_token = tokens.get("access_token")
        self._access_token_expires_at = time.time() + int(tokens.get("expires_in", 3600))
        # Halo may or may not rotate the refresh_token; save if present
        if tokens.get("refresh_token"):
            self._save_refresh_token(tokens["refresh_token"])
        return self._access_token

    def _api_get(self, path: str, params: dict = None) -> dict:
        token = self._ensure_access_token()
        url = urljoin(self.api_base + "/", path.lstrip("/"))
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _api_post(self, path: str, body: list | dict) -> dict:
        import json as _json
        token = self._ensure_access_token()
        url = urljoin(self.api_base + "/", path.lstrip("/"))
        print(f"[halo] POST {path}: {_json.dumps(body)[:800]}")
        resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"},
                          json=body, timeout=30)
        if resp.status_code >= 400:
            detail = (resp.text or "")[:500]
            print(f"[halo] POST {path} -> {resp.status_code}: {detail}")
            raise RuntimeError(f"Halo {path} returned {resp.status_code}: "
                               f"{detail or 'no detail'}")
        print(f"[halo] POST {path} -> {resp.status_code}")
        return resp.json() if resp.content else {}

    # --- Concrete implementations (stubs -- filled in Phase 2) ---

    def get_current_agent(self) -> dict:
        """Return the agent record for whoever authenticated via OAuth.
        Caches the result for the lifetime of the adapter instance."""
        if getattr(self, "_current_agent", None) is None:
            self._current_agent = self._api_get("Agent/me")
        return self._current_agent

    @property
    def current_agent_id(self) -> int:
        return int(self.get_current_agent().get("id"))

    def list_open_tickets(self, agent_id=None, include_recent_actions=True) -> List[Ticket]:
        if agent_id is None:
            agent_id = self.current_agent_id
            print(f"[halo] using logged-in agent id {agent_id} "
                  f"({self.get_current_agent().get('name', '?')})")
        params = {"open_only": True, "pageinate": False}
        if agent_id is not None:
            params["agent_id"] = agent_id
        raw = self._api_get("Tickets", params=params)
        # Halo API shape: {"tickets": [...]} or a bare list depending on version
        rows = raw.get("tickets") if isinstance(raw, dict) else raw
        out: List[Ticket] = []
        for r in rows or []:
            out.append(Ticket(
                id=r.get("id"),
                client=r.get("client_name") or "",
                subject=r.get("summary") or r.get("subject") or "",
                status=str(r.get("status_name") or r.get("status") or ""),
                priority=str(r.get("priority_name") or ""),
                project_id=r.get("projectid"),
                raw=r,
            ))
        return out

    def create_time_entry(self, entry: TimeEntry) -> str:
        """Create a real Halo time entry: an Action on the ticket
        (POST /Actions) with timetaken + chargerate. This is what shows in
        the timesheet and billing -- an /Appointment is only a calendar
        item and does NOT count as logged time.

        Halo quirk: Action datetimes are stored as UTC and displayed in the
        agent's local timezone, so we convert the local completion time to
        UTC before sending. timetaken is decimal hours; chargerate goes as
        a string; the body is an array even for a single action.

        Entries without a ticket can't be Actions (ticket_id is required);
        those go to /TimesheetEvent, matching Halo's own Quick Time UI.
        """
        from datetime import timezone as _tz

        if not entry.ticket_id:
            return self._create_quick_time_appointment(entry)

        from timescribe import appconfig as _appconfig
        cfg = _appconfig.load()

        hours = round((entry.end_local - entry.start_local).total_seconds() / 3600, 4)
        end_utc = entry.end_local.astimezone(_tz.utc)   # naive = assume local tz
        item = {
            "ticket_id": str(entry.ticket_id),
            "datetime": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "timetaken": hours,
            "note_html": f"<p>{entry.note}</p>",
            "hiddenfromuser": False,
            "sendemail": False,
        }
        # outcome_id is instance-specific (each Halo instance defines its own
        # outcomes). Configurable via halo_outcome_id; fall back to the
        # generic "Note" outcome, which every instance has.
        outcome_id = cfg.get("halo_outcome_id")
        if outcome_id:
            item["outcome_id"] = str(outcome_id)
        else:
            item["outcome"] = "Note"
        if entry.charge_rate is not None:
            item["chargerate"] = str(entry.charge_rate)
        resp = self._api_post("Actions", [item])
        rec = resp[0] if isinstance(resp, list) and resp else resp
        return str((rec or {}).get("id") or "")

    def _create_quick_time_appointment(self, entry: TimeEntry) -> str:
        """Entries with no ticket -> a Halo timesheet event
        (POST /TimesheetEvent), which is how Halo's own UI logs
        'Quick Time'. Dates go up as UTC with a Z suffix.

        client_id/site_id for quick time are instance-specific; set
        quicktime_client_id / quicktime_site_id in config (usually your
        internal client + main site)."""
        from datetime import timezone as _tz
        from timescribe import appconfig as _appconfig
        cfg = _appconfig.load()

        agent = self.get_current_agent()
        agent_id = int(agent.get("id"))
        agent_name = agent.get("name", "")
        d = entry.start_local
        hour12 = d.hour % 12 or 12
        ampm = "AM" if d.hour < 12 else "PM"
        item = {
            "start_date": entry.start_local.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end_date":   entry.end_local.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "ticket_id":  None,
            "tickettype_id": None,
            "lognewticket": False,
            "agent_id":   agent_id,
            "agents":     [{"id": agent_id, "name": agent_name}],
            "event_type": 0,
            "user_name":  agent_name,
            "charge_rate": entry.charge_rate or 0,
            "note": entry.note,
            "subject": (f"Quick Time - {agent_name} - "
                        f"{d.month}/{d.day}/{d.year} {hour12}:{d.minute:02d} {ampm}"),
        }
        if cfg.get("quicktime_client_id"):
            item["client_id"] = int(cfg["quicktime_client_id"])
        if cfg.get("quicktime_site_id"):
            item["site_id"] = int(cfg["quicktime_site_id"])
        resp = self._api_post("TimesheetEvent", [item])
        rec = resp[0] if isinstance(resp, list) and resp else resp
        return str((rec or {}).get("id") or "")

    def list_calendar_events(self, from_dt, to_dt) -> List[CalendarEvent]:
        raw = self._api_get("Appointment", params={
            "start_date": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_date":   to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        rows = raw if isinstance(raw, list) else raw.get("appointments") or []
        out: List[CalendarEvent] = []
        for r in rows:
            out.append(CalendarEvent(
                id=str(r.get("id")),
                start_local=datetime.fromisoformat(r["start_date"].rstrip("Z")),
                end_local=datetime.fromisoformat(r["end_date"].rstrip("Z")),
                subject=r.get("subject") or "",
                all_day=bool(r.get("allday")),
                ticket_id=r.get("ticket_id") if r.get("ticket_id", -1) > 0 else None,
                is_private=bool(r.get("is_private")),
            ))
        return out

    def create_calendar_event(self, event: CalendarEvent) -> str:
        # Same endpoint as time_entry -- Halo doesn't strictly separate them.
        payload = [{
            "ticket_id": event.ticket_id or -1,
            "start_date": event.start_local.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "end_date":   event.end_local.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "subject":   event.subject,
            "allday":    event.all_day,
        }]
        resp = self._api_post("Appointment", payload)
        return str(resp.get("id") or "")
