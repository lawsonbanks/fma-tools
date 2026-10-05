"""Sign in to Xero as an installed app: authorization code with PKCE, no client secret.

The app is registered at developer.xero.com as "Auth Code with PKCE". That type has no
secret to keep, which is why it suits a tool that lives on a laptop: the only standing
credential is the rotating refresh token, held in the store.

What a person does: sees Xero's own sign-in page in their browser, picks ONE
organisation, clicks Allow. Xero grants one organisation per pass, so connecting four
is four passes; the same sign-in then covers all of them.

Two ways to bring the answer home:
  * listen on http://localhost:<port>/callback (the redirect registered with the app);
  * or print the link, let it be opened on another device, and take the address it
    lands on pasted back. The address carries a one-use code that is worthless without
    the verifier this process kept.

Scopes are granular and read-only. An app created after 2 March 2026 cannot request
the old broad scopes at all, and no scope here can change a ledger.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import urllib.parse

from .transport import TransportError

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
DEFAULT_PORT = 8976

# offline_access is what yields a refresh token; openid + email say who authorised.
SCOPES = (
    "offline_access",
    "openid",
    "email",
    "accounting.settings.read",                  # organisation, chart of accounts
    "accounting.contacts.read",
    "accounting.invoices.read",
    "accounting.payments.read",
    "accounting.budgets.read",
    "accounting.reports.trialbalance.read",
    "accounting.reports.balancesheet.read",
    "accounting.reports.profitandloss.read",
    "accounting.reports.banksummary.read",
    "accounting.reports.aged.read",
)

# What the token endpoint says when a sign-in will never work again. Retrying cannot
# revive any of these; a person has to authorise afresh.
_TERMINAL = {"invalid_grant", "invalid_client", "unauthorized_client",
             "invalid_request", "unsupported_grant_type", "invalid_scope"}


class TokenRejected(Exception):
    """Terminal: Xero will not honour this code or refresh token."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TokenUnavailable(Exception):
    """Transient: Xero could not be reached or answered with a server error."""


# -- PKCE ----------------------------------------------------------------------------

def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """(verifier, challenge). The verifier never leaves this Mac."""
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, challenge_for(verifier)


def redirect_uri(port: int) -> str:
    # Xero accepts http only for the literal host "localhost".
    return f"http://localhost:{port}/callback"


def consent_url(client_id: str, port: int, state: str, challenge: str,
                scopes=SCOPES) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(port),
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # Spaces must travel as %20. Xero reads a "+" as part of the scope's own name,
    # which turns the whole list into one unknown scope.
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params,
                                                        quote_via=urllib.parse.quote)


# -- the redirect --------------------------------------------------------------------

def parse_redirect(text: str) -> dict:
    """The answer Xero put on the redirect, from a full address or a bare query."""
    text = (text or "").strip().strip("'\"")
    query = urllib.parse.urlparse(text).query if "?" in text else text
    q = urllib.parse.parse_qs(query)
    return {k: (q.get(k) or [None])[0]
            for k in ("code", "state", "error", "error_description")}


_PAGE = (b"<!doctype html><meta charset='utf-8'><title>Connected</title>"
         b"<body style='font-family:system-ui;margin:3em'><h2>That is all Xero needed."
         b"</h2><p>You can close this tab and go back to the session that asked.</p>")


class _V6Server(http.server.HTTPServer):
    address_family = socket.AF_INET6


class CallbackListener:
    """Hears one redirect on the loopback interface, both address families, because a
    browser may resolve "localhost" to either. Bound before the browser is opened."""

    def __init__(self, port: int):
        self._answer: dict = {}
        self._heard = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802 -- stdlib's name
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                if not outer._heard.is_set():
                    outer._answer = parse_redirect(parsed.query)
                    outer._heard.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_PAGE)

            def log_message(self, *args):          # stdout is the JSON channel
                return

        self._servers = []
        try:
            v4 = http.server.HTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            raise OSError(f"port {port} on this Mac is in use ({e.strerror})") from None
        self._servers.append(v4)
        self.port = v4.server_address[1]
        try:
            self._servers.append(_V6Server(("::1", self.port), Handler))
        except OSError:
            pass                                    # no IPv6 loopback; v4 suffices
        self._threads = [threading.Thread(target=s.serve_forever,
                                          kwargs={"poll_interval": 0.05}, daemon=True)
                         for s in self._servers]
        for t in self._threads:
            t.start()

    def wait(self, timeout: float) -> dict:
        """The redirect's answer, or {} when nobody came back in time."""
        self._heard.wait(timeout)
        return dict(self._answer)

    def close(self) -> None:
        for s in self._servers:
            s.shutdown()
            s.server_close()
        for t in self._threads:
            t.join(timeout=2)


# -- the token endpoint --------------------------------------------------------------

def _post_form(transport, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode("ascii")
    try:
        r = transport.request("POST", TOKEN_URL,
                              {"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"}, data, 30)
    except TransportError as e:
        raise TokenUnavailable(str(e)) from None
    # The body of a token response is never echoed: on success it IS the sign-in.
    try:
        body = json.loads(r.body.decode("utf-8", "replace"))
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if r.status == 200 and body.get("access_token"):
        return body
    reason = str(body.get("error") or f"HTTP {r.status}")[:60]
    if r.status in (400, 401) or reason in _TERMINAL:
        raise TokenRejected(reason)
    raise TokenUnavailable(reason)


def exchange_code(transport, client_id: str, code: str, port: int, verifier: str) -> dict:
    return _post_form(transport, {
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": redirect_uri(port), "code_verifier": verifier})


def refresh(transport, client_id: str, refresh_token: str) -> dict:
    return _post_form(transport, {
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": refresh_token})


def stamp(token_response: dict, now: float) -> dict:
    """The fields the store keeps from a token response, with absolute times."""
    return {
        "access_token": token_response["access_token"],
        "refresh_token": token_response.get("refresh_token"),
        "access_expires_at": now + float(token_response.get("expires_in", 0)),
        "refresh_issued_at": now,
        "scope": token_response.get("scope", ""),
    }


def claims(jwt: str | None) -> dict:
    """The claims inside a token Xero issued, read without verifying the signature:
    it came straight from Xero over TLS, and it is read only to learn which login and
    which consent it belongs to."""
    try:
        payload = (jwt or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        out = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return out if isinstance(out, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}
