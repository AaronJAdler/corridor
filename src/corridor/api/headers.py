"""The headers every response carries, whatever it says and whoever sends it.

The API answers JSON to programs. None of its responses is meant to be rendered, framed,
cached by something in between, or to load anything, and these headers say so to a
browser that is handed one anyway.
"""

from typing import Final, Protocol

SECURITY_HEADERS: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    # Honoured only over HTTPS, which is how a deployment is reached.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}

CONTENT_SECURITY_POLICY: Final = "default-src 'none'; frame-ancestors 'none'"

# The one page that is a page: the interactive documentation, served outside production
# only. It loads its script and its stylesheet from elsewhere, which the policy forbids.
_RENDERED_PATHS: Final = frozenset({"/docs"})


class _Headers(Protocol):
    """What can have a header set on it: a plain dict, or Starlette's own headers."""

    def __setitem__(self, key: str, value: str, /) -> None: ...

    def __contains__(self, key: object, /) -> bool: ...


def apply_security_headers(headers: _Headers, *, path: str) -> None:
    """Set the security headers on a response to a request for ``path``."""
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value
    if path not in _RENDERED_PATHS:
        headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    # Balances and tokens are for the client that asked. A handler that has a reason to
    # let its answer be cached says so itself, and is left alone.
    if "Cache-Control" not in headers:
        headers["Cache-Control"] = "no-store"
