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
        if entry.private_note:
            item["private_note"] = entry.private_note
        # outcome_id is instance-specific (each Halo instance defines its own
        # outcomes). Configurable via halo_outcome_id; fall back to the
        # generic "Note" outcome, which every instance has.
        outcome_id = cfg.get("halo_outcome_id")
        if outcome_id:
            item["outcome_id"] = str(outcome_id)
        else:
            item["outcome"] = "Note"
        # chargerate is a rate ID (e.g. Tier 3 Labor = 8), not an amount.
        # Priority: explicit on the entry > the ticket's default charge
        # rate > halo_default_chargerate config > omit (outcome default).
        chargerate = entry.charge_rate
        if chargerate is None:
            chargerate = self._ticket_default_chargerate(entry.ticket_id)
        if chargerate is None:
            chargerate = cfg.get("halo_default_chargerate")
        if chargerate is not None:
            # chargerate is an integer rate ID (e.g. 8 = Tier 3, 0 = No
            # Charge). Halo rejects "0.0"/"8.0" -- send a plain integer.
            try:
                item["chargerate"] = str(int(float(chargerate)))
            except (TypeError, ValueError):
                item["chargerate"] = str(chargerate)
            print(f"[halo] ticket {entry.ticket_id}: using chargerate {item['chargerate']}")
        else:
            print(f"[halo] ticket {entry.ticket_id}: no chargerate found; "
                  "outcome default will apply -- check billing on this action")
        resp = self._api_post("Actions", [item])
        rec = resp[0] if isinstance(resp, list) and resp else resp
        # Sanity-check where the time landed: on billable work,
        # actionchargehours/actionprepayhours should be non-zero.
        if isinstance(rec, dict):
            print(f"[halo] action {rec.get('id')}: chargerate={rec.get('chargerate')} "
                  f"chargehours={rec.get('actionchargehours')} "
                  f"prepayhours={rec.get('actionprepayhours')} "
                  f"nonchargehours={rec.get('actionnonchargehours')} "
                  f"chargeamount={rec.get('actionchargeamount')}")
        return str((rec or {}).get("id") or "")

    def get_ticket(self, ticket_id) -> dict:
        """Fetch one ticket's detail for the timesheet click-through panel."""
        t = self._api_get(f"Tickets/{ticket_id}")
        return {
            "id": t.get("id"),
            "url": f"{self.base_url}/ticket?id={t.get('id')}",
            "summary": t.get("summary") or t.get("subject") or "",
            "details": (t.get("details") or t.get("details_html") or "")[:600],
            "client": t.get("client_name") or "",
            "site": t.get("site_name") or "",
            "status": t.get("status_name") or str(t.get("status") or ""),
            "priority": t.get("priority_name") or "",
            "agent": t.get("agent_name") or "",
            "category": t.get("category_1") or "",
        }

    def _ticket_default_chargerate(self, ticket_id):
        """Read the ticket's default charge rate so posted time bills at
        the same tier the ticket is configured for. Halo exposes this
        under slightly different keys per version, so probe a few."""
        try:
            t = self._api_get(f"Tickets/{ticket_id}")
        except Exception as exc:
            print(f"[halo] couldn't read ticket {ticket_id} for chargerate: {exc}")
            return None
        for key in ("defaultchargerate", "default_chargerate", "chargerate",
                    "charge_rate", "actioncode"):
            v = t.get(key)
            if v not in (None, "", -1):
                return v
        # Nothing matched -- log which rate-ish keys exist so the right one
        # can be added to the probe list.
        candidates = {k: v for k, v in t.items()
                      if "charge" in k.lower() or "rate" in k.lower()}
        print(f"[halo] ticket {ticket_id}: no default chargerate found; "
              f"rate-like fields: {candidates}")
        return None

    def get_day_timesheet(self, day_start, day_end) -> dict:
        """The logged-in agent's Halo timesheet for one day, via
        GET /Timesheet/0?date=...&agent_id=...&utcoffset=... -- returns the
        day's events plus Halo's own rollups (target/actual/unlogged hours).
        Event timestamps come back as naive UTC; we convert to local."""
        agent_id = self.current_agent_id
        local_off = datetime.now().astimezone().utcoffset()
        utcoffset_min = int(-local_off.total_seconds() // 60) if local_off else 0
        raw = self._api_get("Timesheet/0", params={
            "date": day_start.strftime("%Y-%m-%dT00:00:00.000Z"),
            "agent_id": str(agent_id),
            "utcoffset": str(utcoffset_min),
        })
        events = raw.get("events") or []
        rows = [r for r in (self._normalize_ts_row(e) for e in events) if r]
        print(f"[halo] Timesheet/0: {len(rows)} events, "
              f"actual={raw.get('actual_hours')} unlogged={raw.get('unlogged_hours')}")
        return {
            "rows": rows,
            "target_hours": raw.get("target_hours"),
            "actual_hours": raw.get("actual_hours"),
            "unlogged_hours": raw.get("unlogged_hours"),
        }

    @staticmethod
    def _normalize_ts_row(r) -> Optional[dict]:
        """Map a Halo timesheet event to {start, end, hours, ticket_id,
        subject, customer, charge_type}. Timestamps are naive UTC ->
        convert to local for display."""
        if not isinstance(r, dict):
            return None
        from datetime import timezone as _tzu

        def _dtparse(val):
            if not val:
                return None
            try:
                dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tzu.utc)    # naive = UTC
                return dt.astimezone().replace(tzinfo=None)   # -> local naive
            except ValueError:
                return None

        start = _dtparse(r.get("start_date"))
        end = _dtparse(r.get("end_date"))
        if start is None and end is None:
            return None
        hours = r.get("timetaken")
        if hours is None and start and end:
            hours = (end - start).total_seconds() / 3600
        tid = r.get("ticket_id")
        if isinstance(tid, (int, float)) and tid <= 0:
            tid = None
        return {
            "start": start.strftime("%H:%M") if start else "",
            "end": end.strftime("%H:%M") if end else "",
            "hours": round(float(hours), 2) if hours is not None else None,
            "ticket_id": tid,
            "subject": (r.get("subject") or r.get("note") or "")[:120],
            "customer": r.get("customer") or "",
            "charge_type": r.get("charge_type_name") or "",
        }

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

    def get_day_meetings(self, since, until) -> list:
        """Calendar appointments for the day, as attribution signals for the
        digest. Halo flags Teams/online meetings and links them to a ticket
        and/or client -- so meeting time can be attributed even when the
        meeting window was never the focused app.

        Returns [{start, end, subject, ticket_id, client, is_teams, online}]
        with local HH:MM times.
        """
        from datetime import timezone as _tzu
        raw = self._api_get("Appointment", params={
            "start_date": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_date":   until.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        rows = raw if isinstance(raw, list) else (raw.get("appointments") or [])

        def _dt(v):
            if not v:
                return None
            try:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=_tzu.utc)
                return d.astimezone().replace(tzinfo=None)
            except ValueError:
                return None

        logged_keys = False
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            s, e = _dt(r.get("start_date")), _dt(r.get("end_date"))
            if s is None:
                continue
            # Teams/online detection: Halo exposes this under a few names
            # across versions; also sniff a join URL / location text.
            blob = " ".join(str(r.get(k) or "") for k in
                            ("online_meeting_url", "teams_url", "location",
                             "appointment_type_name", "subject")).lower()
            is_teams = bool(r.get("teams_meeting") or r.get("is_teams")
                            or "teams.microsoft.com" in blob
                            or "teams meeting" in blob)
            online = bool(r.get("online_meeting") or r.get("is_online")
                          or is_teams or "zoom.us" in blob or "meet.google" in blob)
            tid = r.get("ticket_id")
            if isinstance(tid, (int, float)) and tid <= 0:
                tid = None
            if not logged_keys:
                print(f"[halo] appointment keys sample: {sorted(r.keys())[:20]}")
                logged_keys = True
            out.append({
                "start": s.strftime("%H:%M"),
                "end": e.strftime("%H:%M") if e else "",
                "subject": (r.get("subject") or "")[:150],
                "ticket_id": tid,
                "client": r.get("client_name") or r.get("customer") or "",
                "is_teams": is_teams,
                "online": online,
            })
        out.sort(key=lambda m: m["start"])
        print(f"[halo] {len(out)} calendar appointments for the day")
        return out

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
