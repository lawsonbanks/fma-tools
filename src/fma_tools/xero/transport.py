"""The single seam between this package and the network.

Everything that reaches Xero goes through an object with one method,
`request(method, url, headers, data, timeout) -> Response`. The real one is urllib over
TLS; the tests inject a fake, and conftest replaces the factory with a stub that
raises, so no test can reach the network or a real sign-in by accident.
"""

from __future__ import annotations

import http.client
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import __version__

# Named, so Xero's side can tell this tool from an anonymous script; some gateways
# turn the standard library's default agent away.
USER_AGENT = f"fma-tools/{__version__} (xero; read-only)"


@dataclass
class Response:
    status: int
    headers: dict          # keys lower-cased
    body: bytes


class TransportError(Exception):
    """The request never produced an HTTP response. Carries the kind of failure only:
    never the address's query, a header or a body."""


_TUNNEL_REFUSED = re.compile(r"Tunnel connection failed: \d{3}")


def _kind(e: BaseException) -> str:
    """The kind of failure and, for a URLError, the kind beneath it. A proxy that
    refuses the tunnel says so in a fixed phrase with a status code, and that phrase is
    kept: it is how a sandbox's network rule looks from inside, and it is not cured by
    waiting. Nothing else of any message is kept."""
    name = type(e).__name__
    reason = getattr(e, "reason", None)
    if isinstance(reason, BaseException):
        refused = _TUNNEL_REFUSED.search(str(reason))
        return f"{name}: {refused.group(0) if refused else type(reason).__name__}"
    return name


class UrllibTransport:
    """HTTPS through the standard library, verified against certifi's bundle so the
    same roots are trusted on every Mac and in CI."""

    def __init__(self):
        import certifi
        self._ctx = ssl.create_default_context(cafile=certifi.where())

    def request(self, method: str, url: str, headers: dict, data: bytes | None = None,
                timeout: float = 30) -> Response:
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()},
                                r.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except (http.client.HTTPException, OSError):
                body = b""
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, body)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                OSError) as e:
            # http.client's own failures (a response cut off mid-body, a status line
            # that is not one) are not OSErrors; left to escape they would read as a
            # bug in this tool rather than a connection that failed.
            raise TransportError(_kind(e)) from None


_FACTORY = UrllibTransport


def default_transport():
    return _FACTORY()
