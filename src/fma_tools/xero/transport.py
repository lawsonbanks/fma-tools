"""The single seam between this package and the network.

Everything that reaches Xero goes through an object with one method,
`request(method, url, headers, data, timeout) -> Response`. The real one is urllib over
TLS; the tests inject a fake, and conftest replaces the factory with a stub that
raises, so no test can reach the network or a real sign-in by accident.
"""

from __future__ import annotations

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
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()},
                            e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TransportError(type(e).__name__) from None


_FACTORY = UrllibTransport


def default_transport():
    return _FACTORY()
