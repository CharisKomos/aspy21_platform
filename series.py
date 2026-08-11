"""A narrow time-series endpoint for machine ingestion.

``/api/execute`` is shaped for a person: every response carries explanatory
notes, a generated code snippet, and the full body of each HTTP call aspy21
made. That is the point of the dashboard, and exactly the wrong thing to send
an ingest client several times an hour.

``POST /api/v1/series`` is the same library call with the teaching material
removed -- tags in, a window, rows out. It is the contract the DMAIC service
polls. Nothing here reimplements aspy21; it builds arguments, calls
``client.read()``, and normalises what comes back.

Timestamps
----------
This is the one place a quiet mistake stays expensive. IP.21 answers in the
historian machine's *local* wall time with no zone attached, while the consumer
stores UTC. A plant on UTC+2 whose readings are stored verbatim is two hours
wrong forever, and no chart makes that obvious.

So the boundary is explicit in both directions. Request bounds are read as aware
instants (a token with no zone is taken as UTC), converted into the historian's
zone to build the query, and every timestamp on the way out is converted back
and rendered with an explicit offset. ``ASPY21_TIMEZONE`` names that zone.

The window is half-open, ``(from, to]``
---------------------------------------
The caller passes the newest timestamp it already holds as ``from``, so a
reading exactly on that bound is one it already has. Excluding it is what makes
a poll idempotent: a failed pull can be retried with the same cursor and no row
arrives twice.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import ssl
import time
from typing import Any

import httpx
from aspy21 import IncludeFields, OutputFormat, ReaderType
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from config import SETTINGS
from connection import ConnectionError_, ConnectionOverride, settings_or_default, test
from live_backend import open_backend
from sql_scripts import (
    SqlScriptError,
    build_envelope,
    is_read_only,
    list_scripts,
    read_script,
    rows_from_response,
    scripts_dir,
)

logger = logging.getLogger("aspy21-demo.series")

router = APIRouter(prefix="/api/v1", tags=["ingest"])

# What aspy21 wants for start/end -- naive local wall time, the same format the
# dashboard's own default_window() produces.
IP21_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# A backstop, not the real control: the caller sizes its own window. This only
# stops one mistaken request from returning a month of one-second data.
DEFAULT_MAX_ROWS = 200_000


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
class SeriesRequest(BaseModel):
    """One windowed read for a set of tags."""

    tags: list[str] = Field(..., min_length=1, description="Tag names to read.")
    from_: dt.datetime = Field(
        ...,
        alias="from",
        description="Exclusive lower bound. Pass the newest timestamp you already hold.",
    )
    to: dt.datetime = Field(..., description="Inclusive upper bound.")
    interval_seconds: int | None = Field(
        None,
        ge=0,
        description=(
            "Grid spacing for an interpolated (INT) read. Omit or send 0 for raw stored "
            "points. Interpolated is usually what a control chart wants, because raw "
            "points are unevenly spaced."
        ),
    )
    include_status: bool = Field(True, description="Ask for the per-reading quality code.")
    max_rows: int = Field(DEFAULT_MAX_ROWS, gt=0, description="Refuse rather than return more.")
    connection: ConnectionOverride | None = Field(
        None,
        description=(
            "Read from this historian instead of the one configured at startup. Lets one "
            "gateway serve several plants; nothing about it is stored."
        ),
    )
    simulate_failure: int | None = Field(
        None,
        ge=400,
        le=599,
        description=(
            "Mock mode only: make the historian return this status, so a client can be "
            "tested against every failure path without breaking a real server. Refused "
            "with 409 in live mode."
        ),
    )

    model_config = ConfigDict(populate_by_name=True)


class SeriesRow(BaseModel):
    """One reading. ``timestamp`` always carries an explicit UTC offset."""

    tag: str
    timestamp: dt.datetime
    value: float
    status: int | None = None


class SeriesResponse(BaseModel):
    """Rows plus enough context to tell a good pull from a suspicious one."""

    ok: bool = True
    mode: str = Field(..., description="'mock' or 'live'. Simulated data is never silent.")
    live: bool
    rows: list[SeriesRow]
    row_count: int
    tags_requested: int
    tags_returned: int = Field(..., description="Distinct tags that actually returned data.")
    from_: dt.datetime = Field(..., alias="from")
    to: dt.datetime
    interval_seconds: int | None
    read_type: str
    timezone: str = Field(..., description="The zone the historian's timestamps were read in.")
    skipped_non_numeric: int = Field(..., description="Dropped: no value, NaN, or unparseable.")
    skipped_out_of_window: int = Field(..., description="Rows dropped: outside (from, to].")
    duplicates_dropped: int
    elapsed_ms: float
    http_call_count: int

    model_config = ConfigDict(populate_by_name=True)


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------
def historian_tz(settings: Any = None) -> dt.tzinfo:
    """The zone IP.21 reports its timestamps in.

    ``UTC`` (the default) and ``local`` need no tz database. Any other value is
    an IANA name and needs one -- present on Linux, and on Windows only if the
    ``tzdata`` package is installed.
    """
    name = (getattr(settings or SETTINGS, "timezone", "") or "UTC").strip()
    if name.upper() == "UTC":
        return dt.timezone.utc
    if name.lower() == "local":
        # The mock generates naive local timestamps, so mock mode round-trips
        # correctly whatever zone the host is in.
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_kind": "configuration",
                "message": (
                    f"ASPY21_TIMEZONE={name!r} is not a known time zone: {exc}. "
                    "Use 'UTC', 'local', or an IANA name like 'Europe/Athens'. On Windows "
                    "an IANA name also needs the 'tzdata' package installed."
                ),
            },
        ) from exc


def as_utc(value: dt.datetime) -> dt.datetime:
    """A bare datetime is taken as UTC; an aware one is converted to it."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def to_query_text(value: dt.datetime, tz: dt.tzinfo) -> str:
    """Render an instant the way IP.21 wants it: local wall time, no zone."""
    return value.astimezone(tz).strftime(IP21_TIME_FORMAT)


def row_timestamp(raw: Any, tz: dt.tzinfo) -> dt.datetime | None:
    """Turn one parsed aspy21 timestamp into an aware UTC instant, or None."""
    value = raw
    if value is None:
        return None
    if isinstance(value, str):
        try:
            # Python 3.10's fromisoformat rejects a trailing 'Z'; 3.11+ accepts it.
            value = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        # pandas.Timestamp subclasses datetime, so this is for the oddities.
        converter = getattr(value, "to_pydatetime", None)
        if not callable(converter):
            return None
        try:
            value = converter()
        except Exception:
            return None
        if not isinstance(value, dt.datetime):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)  # historian local time
    return value.astimezone(dt.timezone.utc)


def finite(raw: Any) -> float | None:
    """A real number, or None for anything a chart cannot use."""
    if isinstance(raw, bool):  # bool is an int; a boolean is not a reading
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def quality(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# failure
# --------------------------------------------------------------------------
def unwrap(exc: BaseException) -> BaseException:
    """Peel tenacity's RetryError down to the failure that actually happened."""
    attempt = getattr(exc, "last_attempt", None)
    if attempt is not None:
        try:
            inner = attempt.exception()
        except Exception:
            inner = None
        if inner is not None and inner is not exc:
            return unwrap(inner)
    return exc


def _is_tls_failure(exc: BaseException) -> bool:
    """Whether a connection failure was really a certificate problem.

    httpx wraps TLS errors in ConnectError, so they arrive looking like "could
    not reach the server" -- which sends people to check firewalls when the host
    is answering fine and only the certificate is wrong. Worth separating: an
    internal CA needs ASPY21_CA_BUNDLE, not a network ticket.
    """
    seen: list[BaseException] = []
    cursor: BaseException | None = exc
    while cursor is not None and cursor not in seen and len(seen) < 10:
        if isinstance(cursor, ssl.SSLError):
            return True
        seen.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    return "certificate verify failed" in str(exc).lower() or "ssl" in type(exc).__name__.lower()


def classify(exc: BaseException, settings: Any = None) -> tuple[str, str]:
    """``(error_kind, message)``.

    The kind is the part a caller branches on -- "connection lost" and "the
    password is wrong" want different words on a status badge and different
    retry behaviour, and neither is recoverable by trying harder immediately.

    ``settings`` names the server in the message. It matters once a request can
    carry its own connection: reporting the process-wide URL would send someone to
    check a machine the failing read never touched.
    """
    active = settings or SETTINGS
    root = unwrap(exc)
    if isinstance(root, (httpx.ConnectError, httpx.ConnectTimeout)) and _is_tls_failure(root):
        return "tls", (
            f"The TLS certificate for {active.base_url} was rejected: {root}. If the "
            "historian uses an internal CA, supply a CA bundle; a certificate is issued "
            "to a hostname, so an IP address never validates."
        )
    if isinstance(root, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connection", f"Could not reach the historian at {active.base_url}: {root}"
    if isinstance(root, httpx.TimeoutException):
        return "timeout", f"The historian did not answer within {active.timeout}s: {root}"
    if isinstance(root, httpx.HTTPStatusError):
        code = root.response.status_code
        if code in (401, 403):
            return "auth", (
                f"The historian rejected the credentials (HTTP {code}). If the site uses "
                "Windows Integrated authentication, try auth_scheme=ntlm with a "
                "domain-qualified username."
            )
        if code == 404:
            return "not_found", f"No ProcessData endpoint at {active.base_url} (HTTP {code})."
        return "upstream", f"The historian returned HTTP {code}."
    if isinstance(root, httpx.HTTPError):
        return "connection", f"{type(root).__name__}: {root}"
    if isinstance(root, ValueError):
        # aspy21 raises this for things like a missing datasource.
        return "configuration", str(root)
    return "unknown", f"{type(root).__module__}.{type(root).__name__}: {root}"


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------
class TagBrowseRequest(BaseModel):
    """Discover tags, optionally against a caller-supplied historian."""

    pattern: str = Field("*", description="Tag name pattern. * matches any run, ? exactly one.")
    limit: int = Field(500, gt=0, le=10000)
    connection: ConnectionOverride | None = None


class ConnectionTestRequest(BaseModel):
    connection: ConnectionOverride


@router.post("/tags", tags=["ingest"])
def api_v1_tags(payload: TagBrowseRequest) -> dict[str, Any]:
    """Tag names only — the machine-facing counterpart of ``GET /api/tags``.

    A POST because it carries a connection: the browse itself is a read.
    """
    try:
        active = settings_or_default(payload.connection)
    except ConnectionError_ as exc:
        raise HTTPException(
            status_code=502,
            detail={"error_kind": exc.kind, "message": exc.message, "failed_step": exc.step},
        ) from exc

    started = time.perf_counter()
    with open_backend(settings=active) as backend:
        try:
            found = backend.client().search(
                tag=payload.pattern or "*", limit=payload.limit, include=IncludeFields.DESCRIPTION
            )
        except Exception as exc:
            kind, message = classify(exc, active)
            logger.warning("tag browse failed (%s): %s", kind, exc)
            raise HTTPException(
                status_code=502,
                detail={"error_kind": kind, "message": message, "base_url": active.base_url},
            ) from exc

    tags = []
    for entry in found or []:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            description = str(entry.get("description") or "")
        else:
            name, description = str(entry).strip(), ""
        if name:
            tags.append({"name": name, "description": description})

    return {
        "tags": tags,
        "count": len(tags),
        "mode": active.mode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


class SqlRequest(BaseModel):
    """Run SQLplus. Either inline ``sql`` or a ``script`` from the scripts folder."""

    sql: str | None = Field(None, description="SQLplus to run. Ignored when 'script' is given.")
    script: str | None = Field(None, description="Filename in the scripts folder, e.g. daily.sql.")
    datasource: str | None = Field(None, description="Overrides the configured datasource.")
    max_rows: int = Field(10_000, gt=0, le=1_000_000)
    timeout: int = Field(60, gt=0, le=3600)
    connection: ConnectionOverride | None = None


@router.get("/sql/scripts", tags=["sql"])
def api_sql_scripts() -> dict[str, Any]:
    """The .sql files available to run, and where they are read from."""
    folder = getattr(SETTINGS, "sql_dir", "sql")
    return {
        "scripts": list_scripts(folder),
        "directory": str(scripts_dir(folder)),
        "exists": scripts_dir(folder).is_dir(),
        "allow_writes": getattr(SETTINGS, "sql_allow_writes", False),
    }


@router.get("/sql/scripts/{name}", tags=["sql"])
def api_sql_script(name: str) -> dict[str, Any]:
    """One script's contents, so it can be reviewed before it is run."""
    try:
        body = read_script(getattr(SETTINGS, "sql_dir", "sql"), name)
    except SqlScriptError as exc:
        raise HTTPException(
            status_code=404 if exc.kind == "not_found" else 400,
            detail={"error_kind": exc.kind, "message": exc.message},
        ) from exc
    return {"name": name, "sql": body, "read_only": is_read_only(body)}


@router.post("/sql", tags=["sql"])
def api_sql(payload: SqlRequest) -> dict[str, Any]:
    """Run SQLplus against the historian and return the rows.

    aspy21 builds SQLplus internally but exposes no way to send your own. This
    wraps a query in the same ``<SQL t="SQLplus">`` envelope the library uses and
    posts it through the same authenticated client, so auth, TLS and the request
    log all behave as they do everywhere else.
    """
    if payload.script:
        try:
            sql = read_script(getattr(SETTINGS, "sql_dir", "sql"), payload.script)
        except SqlScriptError as exc:
            raise HTTPException(
                status_code=404 if exc.kind == "not_found" else 400,
                detail={"error_kind": exc.kind, "message": exc.message},
            ) from exc
    else:
        sql = payload.sql or ""

    if not sql.strip():
        raise HTTPException(
            status_code=400,
            detail={"error_kind": "bad_request", "message": "No SQL to run."},
        )

    # aspy21 cannot alter the historian; SQLplus can. Refusing writes by default
    # keeps that guarantee unless it is knowingly given up.
    if not is_read_only(sql) and not getattr(SETTINGS, "sql_allow_writes", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error_kind": "write_refused",
                "message": (
                    "This script is not a plain SELECT, and writes are disabled. Set "
                    "ASPY21_SQL_ALLOW_WRITES=true to permit it — and prefer a read-only "
                    "account on the historian, which no setting here can override."
                ),
            },
        )

    try:
        active = settings_or_default(payload.connection)
    except ConnectionError_ as exc:
        raise HTTPException(
            status_code=502,
            detail={"error_kind": exc.kind, "message": exc.message, "failed_step": exc.step},
        ) from exc

    datasource = (payload.datasource or active.datasource or "").strip()
    if not datasource:
        raise HTTPException(
            status_code=400,
            detail={
                "error_kind": "configuration",
                "message": ("SQLplus needs a datasource — set one on the connection, or here."),
            },
        )

    xml = build_envelope(sql, datasource, max_rows=payload.max_rows, timeout=payload.timeout)

    started = time.perf_counter()
    with open_backend(settings=active) as backend:
        client = backend.client()
        try:
            # Reuse the library's own authenticated client rather than a new one, so
            # credentials, TLS and the dashboard's request log all still apply.
            response = client._client.post(  # the library exposes no public SQL path
                f"{active.base_url}/SQL", content=xml, headers={"Content-Type": "text/xml"}
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            kind, message = classify(exc, active)
            logger.warning("sql script failed (%s): %s", kind, exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "error_kind": kind,
                    "message": message,
                    "base_url": active.base_url,
                    "sql": sql[:500],
                },
            ) from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        http_calls = backend.http_calls

    rows = rows_from_response(body)
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        raise HTTPException(
            status_code=502,
            detail={
                "error_kind": "upstream",
                "message": f"The historian rejected the query: {body['error']}",
            },
        )

    return {
        "ok": True,
        "mode": active.mode,
        "script": payload.script,
        "sql": sql,
        "datasource": datasource,
        "rows": rows,
        "row_count": len(rows),
        "columns": sorted({k for r in rows for k in r}) if rows else [],
        "read_only": is_read_only(sql),
        "request_xml": xml,
        "elapsed_ms": elapsed_ms,
        "http_call_count": len(http_calls),
    }


@router.post("/connection/test", tags=["ingest"])
def api_connection_test(payload: ConnectionTestRequest) -> dict[str, Any]:
    """Check a connection before saving it, and say which step failed.

    Always HTTP 200: the caller wants a verdict to show a user, not an exception
    to catch. Look at ``ok`` and, when false, ``failed_step`` and ``error_kind``.
    """
    return test(payload.connection)


@router.post("/series", response_model=SeriesResponse)
def api_series(payload: SeriesRequest) -> SeriesResponse:
    """Read one window of history for a set of tags.

    Returns rows only -- no notes, no code snippet, no request bodies. Failures
    carry an ``error_kind`` so the caller can tell a lost connection from a bad
    password without parsing prose.
    """
    tags = [t.strip() for t in payload.tags if t and t.strip()]
    if not tags:
        raise HTTPException(
            status_code=400,
            detail={"error_kind": "bad_request", "message": "Ask for at least one tag."},
        )

    start = as_utc(payload.from_)
    end = as_utc(payload.to)
    if start >= end:
        raise HTTPException(
            status_code=400,
            detail={
                "error_kind": "bad_request",
                "message": (
                    f"'from' ({start.isoformat()}) must come before 'to' ({end.isoformat()})."
                ),
            },
        )

    # Forcing a failure is a testing affordance, not a feature of the data path.
    # Refused live for the same reason the error-handling card is: asking a real
    # historian to fail is not a thing to do, and faking it would prove nothing.
    if payload.simulate_failure is not None and (SETTINGS.live or payload.connection is not None):
        raise HTTPException(
            status_code=409,
            detail={
                "error_kind": "bad_request",
                "message": (
                    "simulate_failure only works against the mock backend. Restart with "
                    "ASPY21_MODE=mock to exercise the failure paths."
                ),
            },
        )

    # A per-request connection makes this gateway multi-tenant: the calling
    # application owns the settings, so it can point each of its projects at a
    # different plant without a container per historian.
    try:
        active = settings_or_default(payload.connection)
    except ConnectionError_ as exc:
        raise HTTPException(
            status_code=502,
            detail={"error_kind": exc.kind, "message": exc.message, "failed_step": exc.step},
        ) from exc

    tz = historian_tz(active)
    interval = payload.interval_seconds or None
    read_type = ReaderType.INT if interval else ReaderType.RAW
    include = IncludeFields.STATUS if payload.include_status else IncludeFields.NONE

    started = time.perf_counter()
    with open_backend(fail_status=payload.simulate_failure, settings=active) as backend:
        client = backend.client()
        try:
            result = client.read(
                tags,
                start=to_query_text(start, tz),
                end=to_query_text(end, tz),
                interval=interval,
                read_type=read_type,
                include=include,
                output=OutputFormat.JSON,
            )
        except Exception as exc:
            kind, message = classify(exc, active)
            logger.warning("series read failed (%s): %s", kind, exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "error_kind": kind,
                    "message": message,
                    "mode": active.mode,
                    "base_url": active.base_url,
                    "exception": f"{type(unwrap(exc)).__module__}.{type(unwrap(exc)).__name__}",
                },
            ) from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        http_call_count = len(backend.http_calls)

    rows: list[SeriesRow] = []
    seen: set[tuple[str, dt.datetime]] = set()
    skipped_non_numeric = 0
    skipped_out_of_window = 0
    duplicates_dropped = 0

    for record in result if isinstance(result, list) else []:
        if not isinstance(record, dict):
            skipped_non_numeric += 1
            continue
        tag = str(record.get("tag") or "").strip()
        stamp = row_timestamp(record.get("timestamp"), tz)
        value = finite(record.get("value"))
        if not tag or stamp is None or value is None:
            skipped_non_numeric += 1
            continue
        if stamp <= start or stamp > end:
            # Half-open: the caller already holds the reading on the lower bound.
            skipped_out_of_window += 1
            continue
        key = (tag, stamp)
        if key in seen:
            duplicates_dropped += 1
            continue
        seen.add(key)
        rows.append(
            SeriesRow(
                tag=tag,
                timestamp=stamp,
                value=value,
                status=quality(record.get("status")),
            )
        )

    if len(rows) > payload.max_rows:
        raise HTTPException(
            status_code=413,
            detail={
                "error_kind": "too_many_rows",
                "message": (
                    f"{len(rows)} rows exceeds max_rows={payload.max_rows}. Ask for a shorter "
                    "window (lower max_chunk), a coarser interval, or fewer tags per call."
                ),
                "row_count": len(rows),
                "max_rows": payload.max_rows,
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )

    rows.sort(key=lambda r: (r.tag, r.timestamp))

    return SeriesResponse(
        ok=True,
        mode=active.mode,
        live=active.live,
        rows=rows,
        row_count=len(rows),
        tags_requested=len(tags),
        tags_returned=len({r.tag for r in rows}),
        from_=start,
        to=end,
        interval_seconds=interval,
        read_type=read_type.name,
        timezone=getattr(active, "timezone", "UTC") or "UTC",
        skipped_non_numeric=skipped_non_numeric,
        skipped_out_of_window=skipped_out_of_window,
        duplicates_dropped=duplicates_dropped,
        elapsed_ms=elapsed_ms,
        http_call_count=http_call_count,
    )
