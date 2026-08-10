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
        # Serializes token refresh across the scheduler / UI / MCP threads.
        # Halo rotates refresh tokens (one-time use), so two concurrent
        # refreshes with the same token make the second fail invalid_grant;
        # without this lock that failure used to wipe the good rotated token.
        self._token_lock = __import__("threading").Lock()

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

    def sign_out(self) -> None:
        """Forget all credentials for this tenant: live access token,
        cached agent record, and the stored refresh token. The next
        connect() runs a fresh OAuth flow (any user, any instance)."""
        self._access_token = None
        self._access_token_expires_at = 0.0
        self._current_agent = None
        self._clear_refresh_token()

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

    def _fresh_enough(self) -> bool:
        return bool(self._access_token
                    and time.time() < self._access_token_expires_at - 30)

    def _ensure_access_token(self) -> str:
        """Return a valid access_token, refreshing if needed.

        Refresh is serialized: the scheduler, UI, and MCP threads all call
        this, and Halo rotates refresh tokens (one-time use). Without a lock,
        two threads refresh with the same token at once -- one wins and
        rotates, the other gets invalid_grant. We double-check inside the
        lock (so only ONE network refresh happens per expiry) and, if a
        refresh is rejected, RELOAD the stored token in case a sibling call
        just rotated it, retrying once before declaring the session dead."""
        if self._fresh_enough():
            return self._access_token

        with self._token_lock:
            # Another thread may have refreshed while we waited for the lock.
            if self._fresh_enough():
                return self._access_token

            refresh = self._load_refresh_token()
            if not refresh:
                raise RuntimeError("No stored refresh token; call connect() first")

            try:
                tokens = self._do_refresh(refresh)
            except Exception as exc:
                # Could be a genuine revocation OR a benign race: a sibling
                # thread rotated the token microseconds ago. Re-read the
                # stored token; if it changed, retry once with the new one.
                current = self._load_refresh_token()
                if current and current != refresh:
                    print("[halo] refresh raced with another call; retrying "
                          "with the rotated token")
                    try:
                        tokens = self._do_refresh(current)
                    except Exception as exc2:
                        exc = exc2
                        current = None   # fall through to the failure path
                    else:
                        return self._apply_tokens(tokens)
                # If a sibling already produced a live access token, use it
                # rather than tearing down a working session.
                if self._fresh_enough():
                    return self._access_token
                print(f"[halo] refresh token rejected ({exc}); clearing stored token")
                self._clear_refresh_token()
                self._access_token = None
                raise RuntimeError(
                    "Halo session expired - reconnect via Settings > Connect to Halo") from exc

            return self._apply_tokens(tokens)

    def _do_refresh(self, refresh: str) -> dict:
        return refresh_access_token(
            token_endpoint=self.token_endpoint,
            client_id=self.client_id,
            refresh_token=refresh,
        )

    def _apply_tokens(self, tokens: dict) -> str:
        self._access_token = tokens.get("access_token")
        self._access_token_expires_at = time.time() + int(tokens.get("expires_in", 3600))
        # Halo rotates the refresh_token on each use; persist the new one.
        if tokens.get("refresh_token"):
            self._save_refresh_token(tokens["refresh_token"])
        return self._access_token

    def _api_get(self, path: str, params: dict = None) -> dict:
        token = self._ensure_access_token()
        url = urljoin(self.api_base + "/", path.lstrip("/"))
        print(f"[halo] GET {path} params={params or {}}")
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=30)
        if resp.status_code >= 400:
            detail = (resp.text or "")[:500]
            print(f"[halo] GET {path} -> {resp.status_code}: {detail}")
            raise RuntimeError(f"Halo {path} returned {resp.status_code}: "
                               f"{detail or 'no detail'}")
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

    def delete_action(self, action_id) -> None:
        """Delete an action in Halo (e.g. one posted to the wrong ticket).
        Used to complete a 'move': repost to the right ticket, then remove
        the original."""
        if not action_id:
            raise RuntimeError("no action id to delete")
        token = self._ensure_access_token()
        url = urljoin(self.api_base + "/", f"Actions/{action_id}")
        print(f"[halo] DELETE Actions/{action_id}")
        resp = httpx.delete(url, headers={"Authorization": f"Bearer {token}"},
                            timeout=30)
        if resp.status_code >= 400:
            detail = (resp.text or "")[:400]
            raise RuntimeError(f"Halo delete action {action_id} returned "
                               f"{resp.status_code}: {detail or 'no detail'}")

    def action_exists(self, action_id) -> Optional[bool]:
        """Does a previously-posted action still exist in Halo?
        True = still there, False = gone/deleted, None = couldn't tell
        (don't block a repost on an inconclusive check)."""
        if not action_id:
            return None
        try:
            r = self._api_get(f"Actions/{action_id}")
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return False
            return None
        if isinstance(r, dict) and r.get("id"):
            return not bool(r.get("deleted") or r.get("isdeleted")
                            or r.get("actisdeleted"))
        return False

    @staticmethod
    def _find_customfield(rec: dict, name: str):
        """Return a client record's custom field value by field name."""
        cfs = rec.get("customfields") or rec.get("customFields") or []
        for cf in cfs if isinstance(cfs, list) else []:
            if not isinstance(cf, dict):
                continue
            if (cf.get("name") or "").lower() == name.lower():
                return cf.get("value")
        return None

    def get_org_ai_key(self, client_id: int, field_name: str) -> Optional[str]:
        """Read the org-shared AI key from an ENCRYPTED custom field on a
        Halo client. Halo returns the decrypted value only to authorized
        agents; returns None if unset or not permitted."""
        if not client_id:
            return None
        try:
            rec = self._api_get(f"Client/{client_id}")
        except Exception as exc:
            print(f"[halo] org AI key read failed (client {client_id}): {exc}")
            return None
        val = self._find_customfield(rec, field_name)
        if val:
            print(f"[halo] loaded org AI key from client {client_id} field '{field_name}'")
            return str(val)
        print(f"[halo] client {client_id} has no value in field '{field_name}' "
              "(unset, or the field isn't readable by this agent)")
        return None

    def publish_org_ai_key(self, client_id: int, field_name: str, key: str) -> None:
        """Admin helper: write the org AI key into the encrypted custom
        field on the client. Requires the field to exist (created once in
        Halo config as an encrypted custom field on the Client entity)."""
        if not client_id:
            raise RuntimeError("no org_ai_client_id configured")
        item = {"id": client_id,
                "customfields": [{"name": field_name, "value": key}]}
        self._api_post("Client", [item])
        print(f"[halo] published org AI key to client {client_id} field '{field_name}'")

    def _ticket_row(self, r: dict) -> dict:
        return {
            "id": r.get("id"),
            "client": (r.get("client_name") or r.get("clientname")
                       or r.get("client") or ""),
            "site": (r.get("site_name") or r.get("sitename") or ""),
            "subject": (r.get("summary") or r.get("subject") or r.get("details") or "")[:120],
            "status": str(r.get("status_name") or r.get("statusname")
                          or r.get("status") or ""),
        }

    def _match_client_ids(self, q: str, limit: int = 5) -> list:
        """Client ids whose name matches q. Halo's /Client `search` does
        case-insensitive substring matching on the client name."""
        try:
            raw = self._api_get("Client", params={"search": q, "count": 20,
                                                  "page_size": 20, "pageinate": True})
        except Exception as exc:
            print(f"[halo] client search '{q}' failed: {exc}")
            return []
        rows = raw.get("clients") if isinstance(raw, dict) else raw
        ids = [r.get("id") for r in (rows or [])
               if isinstance(r, dict) and r.get("id")]
        print(f"[halo] client name match '{q}': {len(ids)} -> {ids[:limit]}")
        return ids[:limit]

    def _match_site_ids(self, q: str, limit: int = 8) -> list:
        """Site ids whose name matches q. This is what makes brand /
        trading names findable -- e.g. 'BornGood' is a Halo *site* under a
        client named something else, so a client- or summary-only search
        never surfaces it. /Site `search` substring-matches the site name."""
        try:
            raw = self._api_get("Site", params={"search": q, "count": 20,
                                                "page_size": 20, "pageinate": True})
        except Exception as exc:
            print(f"[halo] site search '{q}' failed: {exc}")
            return []
        rows = raw.get("sites") if isinstance(raw, dict) else raw
        ids = [r.get("id") for r in (rows or [])
               if isinstance(r, dict) and r.get("id")]
        print(f"[halo] site name match '{q}': {len(ids)} -> {ids[:limit]}")
        return ids[:limit]

    def _collect_tickets(self, params: dict, out: list, seen: set, limit: int):
        """Run a /Tickets query and append normalized rows (skipping
        quick-time entries and dupes)."""
        try:
            raw = self._api_get("Tickets", params=params)
        except Exception as exc:
            print(f"[halo] ticket query {params} failed: {exc}")
            return
        rows = raw.get("tickets") if isinstance(raw, dict) else raw
        print(f"[halo] ticket query {params}: {len(rows or []) if isinstance(rows, list) else '?'} rows")
        for r in (rows or []):
            if not isinstance(r, dict) or r.get("id") in seen:
                continue
            subj = (r.get("summary") or r.get("subject") or "")
            if subj.strip().lower().startswith("quick time"):
                continue
            out.append(self._ticket_row(r)); seen.add(r.get("id"))
            if len(out) >= limit:
                return

    def search_tickets(self, query: str, limit: int = 25) -> list:
        """Find tickets by number, ticket text, company (client) name, OR
        site / trading name.

        Halo's ticket `search` only matches ticket text (summary/details),
        so a term that only appears as a company or brand name never
        surfaces. We therefore resolve matching clients AND sites and pull
        their tickets too. The site branch is what makes brand names like
        'BornGood' (a Halo site under a differently-named client) findable.
        All branches include closed tickets (open_only=false) and dedupe."""
        out, seen = [], set()
        q = query.strip()
        if not q:
            return out

        def pull(params):
            params.update({"count": limit, "page_size": limit, "page_no": 1,
                           "pageinate": True, "open_only": False})
            self._collect_tickets(params, out, seen, limit)

        # 1) Exact ticket number.
        if q.isdigit():
            try:
                t = self._api_get(f"Tickets/{q}")
                if isinstance(t, dict) and t.get("id"):
                    out.append(self._ticket_row(t)); seen.add(t.get("id"))
            except Exception as exc:
                print(f"[halo] direct ticket #{q} lookup failed: {exc}")

        # 2) Ticket text search (summary / details / reference).
        pull({"search": q})

        # 3) Company (client) name -> that client's tickets.
        if len(out) < limit:
            for cid in self._match_client_ids(q):
                if len(out) >= limit:
                    break
                pull({"client_id": cid})

        # 4) Site / trading name -> that site's tickets.
        if len(out) < limit:
            for sid in self._match_site_ids(q):
                if len(out) >= limit:
                    break
                pull({"site_id": sid})

        print(f"[halo] search '{q}': {len(out)} ticket(s) total")
        return out[:limit]

    def list_agent_tickets(self, max_tickets: int = 2000) -> list:
        """Every ticket assigned to the logged-in agent (open AND closed),
        trimmed to the fields the relink picker needs and cached ~5 min.

        This backs the relink search: the UI fetches this list once and
        filters it locally as the user types, so there are no per-keystroke
        API calls -- and it never touches /Client or /Site, sidestepping
        the 403 on those endpoints entirely."""
        import time as _t
        now = _t.time()
        if (getattr(self, "_mytix_cache", None) is not None
                and now - getattr(self, "_mytix_ts", 0) < 300):
            return self._mytix_cache

        try:
            agent_id = self.current_agent_id
        except Exception as exc:
            print(f"[halo] can't resolve agent id for my-tickets: {exc}")
            return []

        out, seen, page = [], set(), 1
        while len(out) < max_tickets:
            try:
                raw = self._api_get("Tickets", params={
                    "agent_id": agent_id, "open_only": False,
                    "count": 100, "page_size": 100, "page_no": page,
                    "pageinate": True})
            except Exception as exc:
                print(f"[halo] my-tickets page {page} failed: {exc}")
                break
            rows = raw.get("tickets") if isinstance(raw, dict) else raw
            if not rows:
                break
            for r in rows:
                if not isinstance(r, dict) or r.get("id") in seen:
                    continue
                if (r.get("summary") or "").strip().lower().startswith("quick time"):
                    continue
                out.append(self._ticket_row(r)); seen.add(r.get("id"))
            if len(rows) < 100:
                break
            page += 1

        out.sort(key=lambda t: t.get("id") or 0, reverse=True)
        self._mytix_cache = out
        self._mytix_ts = now
        print(f"[halo] my-tickets: {len(out)} for agent {agent_id}")
        return out

    def get_ticket(self, ticket_id) -> dict:
        """Fetch one ticket's detail for the timesheet click-through panel."""
        t = self._api_get(f"Tickets/{ticket_id}")
        return {
            "id": t.get("id"),
            "url": f"{self.base_url}/ticket?id={t.get('id')}",
            "summary": t.get("summary") or t.get("subject") or "",
            "details": (t.get("details") or t.get("details_html") or "")[:1500],
            "client": t.get("client_name") or "",
            "site": t.get("site_name") or "",
            "status": t.get("status_name") or str(t.get("status") or ""),
            "priority": t.get("priority_name") or "",
            "agent": t.get("agent_name") or "",
            "category": t.get("category_1") or "",
            "actions": self.get_ticket_actions(t.get("id")),
        }

    def get_ticket_actions(self, ticket_id, limit: int = 100) -> list:
        """All actions/notes on a ticket, newest first, for the in-app
        ticket viewer. Strips HTML from note bodies."""
        import re as _re
        if not ticket_id:
            return []
        try:
            raw = self._api_get("Actions", params={"ticket_id": ticket_id,
                                                    "count": limit})
        except Exception as exc:
            print(f"[halo] ticket {ticket_id} actions fetch failed: {exc}")
            return []
        rows = raw.get("actions") if isinstance(raw, dict) else raw
        out = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            body = (r.get("note") or r.get("note_html") or "")
            body = _re.sub(r"<[^>]+>", " ", body)
            body = _re.sub(r"\s+", " ", body).strip()
            when = (r.get("datetime") or r.get("actiondatecreated") or "")
            out.append({
                "id": r.get("id"),
                "when": str(when).replace("T", " ")[:16],
                "who": r.get("who") or "",
                "outcome": r.get("outcome") or "",
                "timetaken": r.get("timetaken"),
                "note": body[:2000],
            })
        # Newest first if we can sort on the datetime string.
        out.sort(key=lambda a: a.get("when") or "", reverse=True)
        print(f"[halo] ticket {ticket_id}: {len(out)} actions")
        return out

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

    def _fetch_my_appointments(self, from_dt, to_dt) -> list:
        """GET /Appointment scoped to the logged-in agent ONLY.

        Without an agent filter Halo returns every agent's appointments,
        which polluted digests with other technicians' meetings. Params
        mirror the query Halo's own scheduler UI issues: `agents=<id>`,
        UTC Z-timestamps + utcoffset, appointments only (no shifts /
        recurring masters). We still post-filter defensively.
        """
        from datetime import timezone as _tzu
        my_id = self.current_agent_id
        # JS getTimezoneOffset convention: minutes to ADD to local to get
        # UTC (EDT -> 240).
        local_off = datetime.now().astimezone().utcoffset()
        utcoffset = -int(local_off.total_seconds() // 60) if local_off else 0

        def _utc(d: datetime) -> str:
            if d.tzinfo is None:
                d = d.astimezone()          # naive = local
            return d.astimezone(_tzu.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        raw = self._api_get("Appointment", params={
            "start_date": _utc(from_dt),
            "end_date":   _utc(to_dt),
            "agents":     str(my_id),
            "utcoffset":  utcoffset,
            "appointmentsonly":       "true",
            "excluderecurringmaster": "true",
            "showshifts":             "false",
        })
        rows = raw if isinstance(raw, list) else (raw.get("appointments") or [])

        def _mine(r: dict) -> bool:
            attributed = False
            for k in ("agent_id", "agentid", "assigned_agent_id"):
                v = r.get(k)
                if v not in (None, "", 0, -1):
                    attributed = True
                    try:
                        if int(v) == my_id:
                            return True
                    except (TypeError, ValueError):
                        pass
            agents = r.get("agents")
            if isinstance(agents, list) and agents:
                attributed = True
                for a in agents:
                    aid = a.get("id") if isinstance(a, dict) else a
                    try:
                        if int(aid) == my_id:
                            return True
                    except (TypeError, ValueError):
                        pass
            # No agent attribution at all -> keep (can't misattribute).
            return not attributed

        mine = [r for r in rows if isinstance(r, dict) and _mine(r)]
        dropped = len([r for r in rows if isinstance(r, dict)]) - len(mine)
        if dropped:
            print(f"[halo] dropped {dropped} appointments belonging to other agents")
        return mine

    def get_day_meetings(self, since, until) -> list:
        """Calendar appointments for the day, as attribution signals for the
        digest. Halo flags Teams/online meetings and links them to a ticket
        and/or client -- so meeting time can be attributed even when the
        meeting window was never the focused app.

        Returns [{start, end, subject, ticket_id, client, is_teams, online}]
        with local HH:MM times. Scoped to the logged-in agent.
        """
        from datetime import timezone as _tzu
        rows = self._fetch_my_appointments(since, until)

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

        # Only meetings that have already ENDED count as signals -- a
        # future or in-progress appointment may not happen as scheduled,
        # so it must never turn into a digest entry or draft.
        cutoff = until
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone().replace(tzinfo=None)
        cutoff = min(cutoff, datetime.now())

        logged_keys = False
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            s, e = _dt(r.get("start_date")), _dt(r.get("end_date"))
            if s is None:
                continue
            if e is None or e > cutoff:
                continue    # not finished yet -> skip until it has ended
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
        from datetime import timezone as _tzu

        def _local(v: str) -> datetime:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_tzu.utc)   # Halo returns UTC
            return d.astimezone().replace(tzinfo=None)

        rows = self._fetch_my_appointments(from_dt, to_dt)
        out: List[CalendarEvent] = []
        for r in rows:
            out.append(CalendarEvent(
                id=str(r.get("id")),
                start_local=_local(r["start_date"]),
                end_local=_local(r["end_date"]),
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
