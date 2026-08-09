"""Per-request connection settings, so one gateway can serve several historians.

The dashboard is configured once at startup from ``ASPY21_*`` and talks to one
server. That is right for a person exploring one plant, and wrong for an
application whose users configure a connection per project: those settings live
in the calling application, not in this container's environment.

So the machine-facing endpoints accept an optional ``connection`` object. When it
is absent the process-wide settings apply and nothing changes; when it is present
it is used for that request alone. Nothing is stored here -- the caller owns the
configuration, including the password, and this module never writes either to
disk.

``POST /api/v1/connection/test`` exists because "it does not work" is not an
actionable answer. It walks the chain in order -- resolve, socket, endpoint,
credentials, datasource -- and stops at the first thing that fails, so the reply
names the step rather than leaving someone to work backwards from a 401.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from config import (
    AUTH_BASIC,
    AUTH_NONE,
    AUTH_SCHEMES,
    SETTINGS,
    Settings,
    auth_backend_available,
)
from setup_live import candidate_urls, probe, split_host_port, tcp_reachable

logger = logging.getLogger("aspy21-demo.connection")


class ConnectionOverride(BaseModel):
    """Where to read from, for this request only.

    ``host`` accepts whatever someone would reasonably type: a hostname, an IP, a
    ``host:port``, or a full URL. Anything short of a full URL is resolved by
    probing the paths Aspen actually uses, because which one a site exposes
    depends on its IIS setup and is not something a caller should have to know.
    """

    host: str = Field(..., description="Hostname, IP, host:port, or a full base URL.")
    datasource: str = Field("", description="IP.21 datasource name, e.g. IP21.")
    username: str = Field("", description="Blank for anonymous access.")
    password: str = Field("", description="Never logged, never stored, never echoed back.")
    auth_scheme: str = Field(AUTH_BASIC, description="basic | ntlm | negotiate | none.")
    verify_ssl: bool = True
    ca_bundle: str = ""
    timezone: str = Field("UTC", description="The zone IP.21 reports timestamps in.")
    timeout: float = Field(30.0, gt=0)

    model_config = ConfigDict(populate_by_name=True)

    def sanitised(self) -> dict[str, Any]:
        """Everything but the password, for logs and error messages."""
        data = self.model_dump()
        data.pop("password", None)
        data["password_set"] = bool(self.password)
        return data


class ConnectionError_(Exception):
    """A connection could not be established. ``kind`` matches the series error kinds."""

    def __init__(self, kind: str, message: str, step: str):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.step = step


def _validate(override: ConnectionOverride) -> None:
    scheme = (override.auth_scheme or AUTH_BASIC).lower()
    if scheme not in AUTH_SCHEMES:
        raise ConnectionError_(
            "configuration",
            f"auth_scheme={scheme!r} is not valid. Use one of: {', '.join(AUTH_SCHEMES)}.",
            "settings",
        )
    if not auth_backend_available(scheme):
        raise ConnectionError_(
            "configuration",
            f"auth_scheme={scheme!r} needs an optional package this gateway does not have "
            "installed. Rebuild the image with it, or choose basic.",
            "settings",
        )
    if override.password and not override.username:
        raise ConnectionError_(
            "configuration", "A password was supplied without a username.", "settings"
        )
    if not override.host.strip():
        raise ConnectionError_("configuration", "No host was supplied.", "settings")


def _settings_for(override: ConnectionOverride, base_url: str) -> Settings:
    scheme = (override.auth_scheme or AUTH_BASIC).lower()
    anonymous = scheme == AUTH_NONE or not override.username
    return Settings(
        mode="live",
        base_url=base_url.rstrip("/"),
        datasource=override.datasource.strip(),
        username="" if anonymous else override.username.strip(),
        password="" if anonymous else override.password,
        timeout=override.timeout,
        verify_ssl=override.verify_ssl,
        ca_bundle=override.ca_bundle.strip(),
        timezone=(override.timezone or "UTC").strip(),
        auth_scheme=scheme,
        password_source="request" if override.password else "none",
        config_file="",
    )


def resolve(override: ConnectionOverride) -> tuple[Settings, str]:
    """Turn an override into usable settings, discovering the base URL if needed.

    Returns the settings and a note describing how the URL was arrived at, which
    is worth showing: "we found it at /ProcessData" saves the next person the same
    hunt.
    """
    _validate(override)

    raw = override.host.strip()
    candidates = candidate_urls(raw)
    if not candidates:
        raise ConnectionError_("configuration", f"Could not read a host from {raw!r}.", "settings")

    # A socket check first, so "no route to the plant" is never reported as a
    # wrong URL. Every candidate shares a host, so one check covers them all.
    host, port = split_host_port(candidates[0])
    reachable, why = tcp_reachable(host, port, timeout=min(override.timeout, 8.0))
    if not reachable:
        raise ConnectionError_(
            "connection",
            f"Cannot open a connection to {host}:{port} — {why}. With the historian on a "
            "plant network, this is usually access rather than configuration.",
            "network",
        )

    auth = (override.username, override.password) if override.username else None
    verify: bool | str = override.ca_bundle or override.verify_ssl

    # If the caller gave a full URL, trust it and skip probing.
    if "://" in raw and raw.rstrip("/").count("/") > 2:
        return _settings_for(override, raw), f"Using the supplied URL {raw}."

    tls_failure: str | None = None
    for candidate in candidates:
        result = probe(candidate, auth, verify)
        if result.service_found:
            return _settings_for(
                override, candidate
            ), f"Found the ProcessData service at {candidate}."
        detail = (result.detail or "").lower()
        if "certificate" in detail or "ssl" in detail:
            tls_failure = result.detail

    if tls_failure:
        raise ConnectionError_(
            "tls",
            f"The TLS certificate was rejected: {tls_failure}. For an internal CA supply a CA "
            "bundle; a certificate is issued to a hostname, so an IP address never validates.",
            "endpoint",
        )
    raise ConnectionError_(
        "not_found",
        f"{host}:{port} answers, but no ProcessData service was found at any of the usual "
        f"paths ({', '.join(c.split(host, 1)[-1] for c in candidates[:4])}). Check the URL with "
        "whoever administers the historian.",
        "endpoint",
    )


def test(override: ConnectionOverride) -> dict[str, Any]:
    """Walk the whole chain and report the first step that fails.

    Never raises for an expected failure: the caller wants a verdict to show a
    user, not an exception to catch.
    """
    steps: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    try:
        settings, note = resolve(override)
    except ConnectionError_ as exc:
        record(exc.step, False, exc.message)
        return {
            "ok": False,
            "error_kind": exc.kind,
            "failed_step": exc.step,
            "message": exc.message,
            "steps": steps,
        }

    record("network", True, "The host accepted a connection.")
    record("endpoint", True, note)

    # Credentials and datasource in one real browse: a 401 answers the first, and
    # what comes back answers the second.
    from live_backend import LiveBackend

    try:
        with LiveBackend(settings=settings) as backend:
            found = backend.client().search(tag="*", limit=5)
    except Exception as exc:
        from series import classify

        kind, message = classify(exc)
        step = (
            "credentials" if kind == "auth" else "datasource" if kind == "configuration" else "read"
        )
        record(step, False, message)
        return {
            "ok": False,
            "error_kind": kind,
            "failed_step": step,
            "message": message,
            "base_url": settings.base_url,
            "steps": steps,
        }

    record(
        "credentials",
        True,
        "Anonymous access accepted."
        if not settings.username
        else f"{settings.auth_scheme} authentication accepted for {settings.username}.",
    )

    tag_count = len(found or [])
    if tag_count == 0:
        record(
            "datasource",
            False,
            f"Connected, but a tag browse returned nothing. datasource="
            f"{settings.datasource or '<unset>'} is usually the cause — search() needs one.",
        )
        return {
            "ok": False,
            "error_kind": "configuration",
            "failed_step": "datasource",
            "message": "Connected, but no tags came back — check the datasource name.",
            "base_url": settings.base_url,
            "steps": steps,
        }

    record("datasource", True, f"A tag browse returned {tag_count} tag(s).")
    return {
        "ok": True,
        "base_url": settings.base_url,
        "datasource": settings.datasource,
        "auth_scheme": settings.auth_scheme,
        "timezone": settings.timezone,
        "tags_visible": tag_count,
        "message": f"Connected to {settings.base_url}.",
        "steps": steps,
    }


def settings_or_default(override: ConnectionOverride | None) -> Settings:
    """Settings for a request: the override if given, otherwise the process-wide ones."""
    if override is None:
        return SETTINGS
    settings, _ = resolve(override)
    return settings
