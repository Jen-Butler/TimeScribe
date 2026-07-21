"""PKCE-based OAuth 2.0 Authorization Code flow for desktop apps.

Why PKCE for desktop:
- Desktop apps can't safely store a client_secret (users could extract it
  from the binary). PKCE (RFC 7636) provides equivalent security via a
  one-time cryptographic challenge, so we're a "public client" with no
  shared secret.

Flow:
  1. generate_pkce_pair()  -> (verifier, challenge)
  2. build_authorize_url() -> URL to open in the user's browser
  3. start_callback_server() -> spins up localhost:8765, waits for the redirect
  4. exchange_code_for_tokens() -> POSTs code + verifier to token endpoint
  5. store the refresh_token via keyring; access_token is short-lived
"""
from __future__ import annotations
import base64
import hashlib
import http.server
import secrets
import socketserver
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Optional

import httpx


CALLBACK_PORT = 8765
CALLBACK_PATH = "/oauth/callback"


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge).

    Verifier is a URL-safe random 43-char string.
    Challenge is SHA256(verifier), base64url-encoded, no padding.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def generate_state() -> str:
    """CSRF-protection nonce embedded in the authorize URL."""
    return secrets.token_urlsafe(24)


def build_authorize_url(*,
                        authorize_endpoint: str,
                        client_id: str,
                        redirect_uri: str,
                        code_challenge: str,
                        state: str,
                        scope: str = "all") -> str:
    """Construct the URL to open in the user's browser."""
    params = {
        "response_type":  "code",
        "client_id":      client_id,
        "redirect_uri":   redirect_uri,
        "scope":          scope,
        "state":          state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return authorize_endpoint + "?" + urllib.parse.urlencode(params)


@dataclass
class CallbackResult:
    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    error_description: Optional[str] = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the redirect from the PSA back to localhost."""
    result: CallbackResult = None
    ready_event: threading.Event = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404); self.end_headers(); return
        params = urllib.parse.parse_qs(parsed.query)
        self.__class__.result = CallbackResult(
            code=(params.get("code") or [None])[0],
            state=(params.get("state") or [None])[0],
            error=(params.get("error") or [None])[0],
            error_description=(params.get("error_description") or [None])[0],
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.__class__.result.error:
            body = (b"<h2>Authentication failed</h2>"
                    b"<p>" + (self.__class__.result.error_description or "").encode() + b"</p>"
                    b"<p>You can close this tab and check the app.</p>")
        else:
            body = (b"<h2>Authentication successful</h2>"
                    b"<p>You can close this tab and return to the app.</p>")
        self.wfile.write(body)
        self.__class__.ready_event.set()

    def log_message(self, *args, **kwargs):
        pass  # silence stderr


def start_callback_server(timeout_seconds: int = 300) -> CallbackResult:
    """Spin up a one-shot localhost server, wait for the redirect, tear it down.

    Blocks up to timeout_seconds. Returns whatever the PSA sent back.
    """
    ready = threading.Event()
    _CallbackHandler.result = CallbackResult()
    _CallbackHandler.ready_event = ready

    server = socketserver.TCPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        got = ready.wait(timeout=timeout_seconds)
        if not got:
            return CallbackResult(error="timeout", error_description=f"No redirect after {timeout_seconds}s")
        return _CallbackHandler.result
    finally:
        server.shutdown()
        server.server_close()


def open_browser(url: str) -> None:
    """Open the user's default browser to the authorize URL."""
    webbrowser.open(url)


def exchange_code_for_tokens(*,
                             token_endpoint: str,
                             client_id: str,
                             code: str,
                             code_verifier: str,
                             redirect_uri: str) -> dict:
    """POST to the PSA's token endpoint. Returns the JSON response
    (typically {access_token, refresh_token, expires_in, token_type, scope})."""
    data = {
        "grant_type":    "authorization_code",
        "client_id":     client_id,
        "code":          code,
        "code_verifier": code_verifier,
        "redirect_uri":  redirect_uri,
    }
    resp = httpx.post(token_endpoint, data=data,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=30)
    _raise_with_body(resp)
    return resp.json()


def _raise_with_body(resp: "httpx.Response") -> None:
    """raise_for_status, but include the OAuth error body -- a bare
    '400 Bad Request' hides the actual reason (invalid_client,
    invalid_grant, redirect_uri mismatch...)."""
    if resp.status_code < 400:
        return
    detail = ""
    try:
        j = resp.json()
        detail = j.get("error_description") or j.get("error") or ""
    except Exception:
        detail = (resp.text or "")[:300]
    raise RuntimeError(
        f"Token endpoint returned {resp.status_code}: {detail or 'no detail in response'}")


def refresh_access_token(*,
                         token_endpoint: str,
                         client_id: str,
                         refresh_token: str) -> dict:
    """Exchange a refresh_token for a new access_token."""
    data = {
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "refresh_token": refresh_token,
    }
    resp = httpx.post(token_endpoint, data=data,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=30)
    _raise_with_body(resp)
    return resp.json()
