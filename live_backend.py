"""A real IP.21 connection, shaped like ``MockBackend``.

``MockBackend`` hands executors an ``AspenClient`` and collects the HTTP calls
that client made. ``LiveBackend`` does the same, except the transport is the
network and the responses come from your historian.

Because both classes expose the same three things -- ``client()``,
``http_calls`` and the context-manager protocol -- every executor in
``operations.py`` works unchanged in either mode. The only card that cannot
work live is the forced-failure one, which needs a server that fails on
demand; ``main`` refuses that card in live mode rather than faking it.

Request logging
---------------
Live calls are recorded with httpx event hooks. Request *bodies* are logged,
because the generated SQL is the point of the dashboard. Request *headers* are
never logged -- that is where the Authorization header lives.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from aspy21 import AspenClient

from config import SETTINGS, Settings
from mock_backend import HttpCall, MockBackend, _extract_g

logger = logging.getLogger("aspy21-demo.live")

# Long SQL is worth showing; a megabyte of it is not.
_MAX_BODY_CHARS = 4000


class Backend(Protocol):
    """What an executor needs from a backend, mocked or real."""

    fail_status: int | None
    calls: list[HttpCall]

    def client(self, **overrides: Any) -> AspenClient: ...

    def close(self) -> None: ...

    def __enter__(self) -> Backend: ...

    def __exit__(self, *exc: object) -> None: ...

    @property
    def http_calls(self) -> list[dict[str, Any]]: ...


def _describe(request: httpx.Request, base_url: str) -> str:
    """Label a request the way the mock labels its routes."""
    path = request.url.path.rstrip("/")
    if path.endswith("/Browse"):
        return "Browse"
    body = request.content.decode("utf-8", "replace") if request.content else ""
    group = _extract_g(body)
    if group:
        return group
    if request.method == "POST" and str(request.url).rstrip("/") == base_url:
        return "XML endpoint"
    return f"{request.method} {path or '/'}"


@dataclass
class LiveBackend:
    """A real ``AspenClient`` against the configured IP.21 server.

    Args:
        settings: Connection settings. Defaults to the process-wide ones.
        fail_status: Accepted for interface parity with ``MockBackend`` and
            ignored -- a real server cannot be told to fail on request.
        seed: Accepted and ignored; live data needs no RNG.
    """

    settings: Settings = SETTINGS
    fail_status: int | None = None
    seed: int | None = None
    calls: list[HttpCall] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.fail_status is not None:
            logger.warning(
                "fail_status=%s ignored: live servers are not asked to fail.", self.fail_status
            )
        self._http_client: httpx.Client | None = None

    # -- plumbing ---------------------------------------------------------
    def client(self, **overrides: Any) -> AspenClient:
        """Build an ``AspenClient`` wired to the real server.

        The auth goes on the ``httpx.Client``, not on ``AspenClient``:
        ``AspenClient`` only applies its own ``auth=`` when it creates the
        HTTP client itself, and here we inject one so the event hooks can log.
        """
        http_client = httpx.Client(
            timeout=self.settings.timeout,
            verify=self.settings.verify,
            auth=self.settings.auth,
            event_hooks={"response": [self._log_response]},
        )
        kwargs: dict[str, Any] = {
            "base_url": self.settings.base_url,
            "datasource": self.settings.datasource,
            "http_client": http_client,
        }
        kwargs.update(overrides)
        self._http_client = http_client
        return AspenClient(**kwargs)

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> LiveBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def http_calls(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.calls]

    # -- logging ----------------------------------------------------------
    def _log_response(self, response: httpx.Response) -> None:
        """Record one real request/response pair for the dashboard."""
        request = response.request
        try:
            # Required before touching .text/.content on a hooked response.
            response.read()
            size = len(response.content)
        except Exception:  # pragma: no cover - a torn-down stream still logs
            size = -1

        body = request.content.decode("utf-8", "replace") if request.content else None
        if body and len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + f"\n... [{len(body) - _MAX_BODY_CHARS} more chars]"

        parts = [f"HTTP {response.status_code}"]
        if size >= 0:
            parts.append(f"{size} bytes")
        content_type = response.headers.get("content-type")
        if content_type:
            parts.append(content_type.split(";")[0])
        elapsed = getattr(response, "elapsed", None)
        if elapsed is not None:
            parts.append(f"{elapsed.total_seconds() * 1000:.0f} ms")

        call = HttpCall(
            method=request.method,
            url=str(request.url),
            kind=_describe(request, self.settings.base_url),
            status=response.status_code,
            request_body=body,
            response_summary=" · ".join(parts),
        )
        with self._lock:
            self.calls.append(call)


def open_backend(fail_status: int | None = None) -> Backend:
    """Return the backend the current configuration calls for."""
    if SETTINGS.live:
        return LiveBackend()
    return MockBackend(fail_status=fail_status)
