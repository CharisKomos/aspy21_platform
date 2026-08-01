"""Mocked IP.21 ProcessData HTTP backend.

This is the ONLY module that fakes anything. Everything else in this project
drives the real, unmodified ``aspy21`` library.

How the interception works
--------------------------
``aspy21.AspenClient`` accepts an ``http_client=`` argument (dependency
injection). We hand it an ``httpx.Client`` whose transport is a
``respx`` router, so:

* every real aspy21 code path runs end-to-end (query building, HTTP call,
  response parsing, DataFrame assembly, retry/backoff);
* no packet ever leaves the machine, and no credentials are needed.

A fresh router + client is built per dashboard execution, so the request log
is isolated and the server stays thread-safe without any global patching.

What aspy21 0.2.0b15 actually calls
-----------------------------------
Verified by reading the installed source:

===========================  ==============================================
Operation                    HTTP call
===========================  ==============================================
SNAPSHOT read                POST {base}/SQL   body: <SQL g="aspy21_snapshot">
RAW / INT read               POST {base}/SQL   body: <SQL g="aspy21_history">
search(description=...)      POST {base}/SQL   body: <SQL g="aspy21_search">
search(tag=...)              GET  {base}/Browse?dataSource=..&tag=..
AVG read                     POST {base}       body: <Q f="d"> (XML endpoint)
===========================  ==============================================

Note the three SQL variants share one URL and are distinguished only by the
``g=`` attribute in the XML body, so the mock dispatches on body content.
Only the XML endpoint (``AVG``) is wrapped in tenacity retry inside aspy21.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx
from aspy21 import AspenClient

# --------------------------------------------------------------------------
# Demo server identity. Nothing here resolves to a real host.
# --------------------------------------------------------------------------
BASE_URL = "http://mock.ip21.local/ProcessData"
DATASOURCE = "IP21_DEMO"


# --------------------------------------------------------------------------
# Demo tag catalogue
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TagSpec:
    """A fake IP.21 tag with a plausible operating point."""

    name: str
    description: str
    unit: str
    base: float
    noise: float
    low: float
    high: float
    decimals: int = 2

    def sample(self, rng: random.Random, drift: float = 0.0) -> float:
        """One jittered reading, clamped to the instrument range."""
        raw = self.base + drift + rng.gauss(0.0, self.noise)
        return round(max(self.low, min(self.high, raw)), self.decimals)


TAGS: dict[str, TagSpec] = {
    t.name: t
    for t in (
        # Reactor area
        TagSpec(
            "REACTOR_TEMP", "Reactor R-101 outlet temperature", "degC", 252.0, 1.8, 200.0, 300.0
        ),
        TagSpec("REACTOR_PRESSURE", "Reactor R-101 head pressure", "barg", 3.45, 0.09, 0.0, 6.0),
        TagSpec(
            "REACTOR_LEVEL", "Reactor R-101 liquid level", "%", 58.0, 2.1, 0.0, 100.0, decimals=1
        ),
        TagSpec("JACKET_TEMP", "Reactor R-101 jacket temperature", "degC", 180.0, 2.6, 20.0, 250.0),
        # Flow measurements
        TagSpec(
            "FLOW_101", "Feed flow to reactor R-101", "m3/h", 118.0, 3.5, 0.0, 200.0, decimals=1
        ),
        TagSpec(
            "FLOW_102", "Recycle flow to reactor R-101", "m3/h", 42.0, 2.2, 0.0, 120.0, decimals=1
        ),
        TagSpec("FLOW_310", "Product draw-off flow", "m3/h", 76.0, 2.8, 0.0, 150.0, decimals=1),
        # Tank levels
        TagSpec("LEVEL_205", "Buffer tank T-205 level", "%", 64.0, 2.4, 0.0, 100.0, decimals=1),
        TagSpec("LEVEL_206", "Product tank T-206 level", "%", 41.0, 1.9, 0.0, 100.0, decimals=1),
        # Rotating equipment
        TagSpec(
            "PUMP_101_SPEED", "Feed pump P-101 speed", "rpm", 1480.0, 22.0, 0.0, 3000.0, decimals=0
        ),
        TagSpec("PUMP_101_CURRENT", "Feed pump P-101 motor current", "A", 31.5, 1.4, 0.0, 60.0),
        # Utilities
        TagSpec(
            "COOLING_WATER_TEMP", "Cooling water supply temperature", "degC", 18.5, 0.8, 0.0, 40.0
        ),
        TagSpec("STEAM_HEADER_PRESS", "MP steam header pressure", "barg", 11.2, 0.35, 0.0, 20.0),
        # Analysers and final control
        TagSpec("PH_301", "Neutralisation tank pH", "pH", 7.15, 0.18, 0.0, 14.0),
        TagSpec(
            "VALVE_401_POS", "Product valve FV-401 position", "%", 47.0, 3.1, 0.0, 100.0, decimals=1
        ),
        TagSpec("ATI111", "Ambient temperature indicator 111", "degC", 25.5, 0.7, -20.0, 60.0),
        TagSpec("ATI112", "Ambient temperature indicator 112", "degC", 24.8, 0.7, -20.0, 60.0),
    )
}

DEFAULT_TAGS = ["REACTOR_TEMP", "FLOW_101"]

# IP.21 input quality codes. 0 = Good; the mock emits the odd non-zero value
# so that include=STATUS has something interesting to show.
_QUALITY_CODES = [0, 0, 0, 0, 0, 0, 0, 0, 1, 2]


def known_tag(name: str) -> TagSpec:
    """Resolve a tag name, inventing a generic spec for unknown names.

    Unknown names still return data so that wildcard searches and typos in the
    dashboard produce something rather than an empty panel.
    """
    if name in TAGS:
        return TAGS[name]
    return TagSpec(name, f"Simulated tag {name}", "unit", 50.0, 2.0, 0.0, 100.0)


def match_tags(pattern: str) -> list[str]:
    """Resolve an IP.21 wildcard pattern (``*``, ``?``) against the catalogue."""
    if not pattern or pattern == "*":
        return list(TAGS)
    regex = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    compiled = re.compile(regex, re.IGNORECASE)
    hits = [name for name in TAGS if compiled.match(name)]
    if hits:
        return hits
    # Fall back to a substring match, which is how operators usually think.
    needle = pattern.strip("*?").lower()
    return [name for name in TAGS if needle and needle in name.lower()]


# --------------------------------------------------------------------------
# Request log
# --------------------------------------------------------------------------
@dataclass
class HttpCall:
    """One intercepted HTTP request/response pair, shown in the dashboard."""

    method: str
    url: str
    kind: str
    status: int
    request_body: str | None = None
    response_summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "kind": self.kind,
            "status": self.status,
            "request_body": self.request_body,
            "response_summary": self.response_summary,
        }


# --------------------------------------------------------------------------
# Query parsing -- the mock reads the queries aspy21 really generated
# --------------------------------------------------------------------------
_SQL_TS_FMT = "%d-%b-%y %H:%M:%S"


def _extract_sql(body: str) -> str:
    m = re.search(r"<!\[CDATA\[(.*?)\]\]>", body, re.DOTALL)
    return m.group(1) if m else ""


def _extract_g(body: str) -> str:
    m = re.search(r'g="([^"]+)"', body)
    return m.group(1) if m else ""


def _tags_from_sql(sql: str) -> list[str]:
    """Pull tag names out of ``name in ('A','B')`` or ``name='A'``."""
    m = re.search(r"name\s+in\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if m:
        return re.findall(r"'([^']+)'", m.group(1))
    m = re.search(r"name\s*=\s*'([^']+)'", sql, re.IGNORECASE)
    return [m.group(1)] if m else []


def _range_from_sql(sql: str) -> tuple[dt.datetime, dt.datetime]:
    m = re.search(r"ts\s+between\s+'([^']+)'\s+and\s+'([^']+)'", sql, re.IGNORECASE)
    if not m:
        now = dt.datetime.now().replace(microsecond=0)
        return now - dt.timedelta(hours=1), now
    try:
        return (
            dt.datetime.strptime(m.group(1), _SQL_TS_FMT),
            dt.datetime.strptime(m.group(2), _SQL_TS_FMT),
        )
    except ValueError:
        now = dt.datetime.now().replace(microsecond=0)
        return now - dt.timedelta(hours=1), now


def _int_from_sql(sql: str, key: str) -> int | None:
    m = re.search(rf"{key}\s*=\s*(\d+)", sql, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _like_pattern(sql: str, column: str) -> str | None:
    m = re.search(rf"{column}\s+like\s+'([^']*)'", sql, re.IGNORECASE)
    return m.group(1) if m else None


def _xml_tag(body: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", body, re.DOTALL)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Payload builders -- shapes verified against aspy21/readers/response_parser.py
# --------------------------------------------------------------------------
def _timestamps(
    start: dt.datetime,
    end: dt.datetime,
    interval_s: int | None,
    rng: random.Random,
    irregular: bool,
    cap: int,
) -> list[dt.datetime]:
    """Build a timestamp series for the requested window."""
    span = max((end - start).total_seconds(), 1.0)
    step = float(interval_s) if interval_s else max(span / 24.0, 1.0)
    count = max(1, min(cap, int(span // step) + 1))
    out: list[dt.datetime] = []
    for i in range(count):
        offset = i * step
        if irregular:
            # RAW data is stored on value change, so spacing is uneven.
            offset += rng.uniform(-step * 0.35, step * 0.35)
        offset = max(0.0, min(span, offset))
        out.append(start + dt.timedelta(seconds=offset))
    return sorted(set(out))


def sql_history_payload(
    tags: list[str],
    start: dt.datetime,
    end: dt.datetime,
    interval_s: int | None,
    raw: bool,
    with_description: bool,
    include_status: bool,
    rng: random.Random,
    cap: int = 60,
) -> list[dict[str, Any]]:
    """``SqlHistoryResponseParser`` input: a flat list of records."""
    records: list[dict[str, Any]] = []
    for tag_name in tags:
        spec = known_tag(tag_name)
        stamps = _timestamps(start, end, interval_s, rng, irregular=raw, cap=cap)
        drift = 0.0
        for stamp in stamps:
            # A slow random walk on top of the noise looks more like a process.
            drift += rng.gauss(0.0, spec.noise * 0.25)
            drift = max(-spec.noise * 4, min(spec.noise * 4, drift))
            record: dict[str, Any] = {
                "ts": stamp.isoformat(timespec="seconds"),
                "name": tag_name,
                "value": spec.sample(rng, drift),
            }
            if with_description:
                record["name->ip_description"] = spec.description
            if include_status:
                record["status"] = rng.choice(_QUALITY_CODES)
            records.append(record)
    return records


def sql_snapshot_payload(
    tags: list[str], with_description: bool, rng: random.Random
) -> list[dict[str, Any]]:
    """``SqlSnapshotResponseParser`` input: one record per tag."""
    out: list[dict[str, Any]] = []
    for tag_name in tags:
        spec = known_tag(tag_name)
        record: dict[str, Any] = {
            "name": tag_name,
            "name->ip_input_value": spec.sample(rng),
            "name->ip_input_quality": rng.choice(_QUALITY_CODES),
        }
        if with_description:
            record["name->ip_description"] = spec.description
        out.append(record)
    return out


def sql_search_payload(tags: list[str], rng: random.Random) -> dict[str, Any]:
    """SQL search response: ``{"data": [{"cols": .., "rows": [{"fld": ..}]}]}``.

    ``AspenClient._search_by_sql`` reads ``fld[0]`` as name and ``fld[1]`` as
    description, so field order matters here.
    """
    rows = []
    for tag_name in tags:
        spec = known_tag(tag_name)
        rows.append(
            {
                "fld": [
                    {"i": 0, "v": tag_name},
                    {"i": 1, "v": spec.description},
                    {"i": 2, "v": spec.sample(rng)},
                ]
            }
        )
    return {
        "data": [
            {
                "g": "aspy21_search",
                "r": "D",
                "result": {"er": 0, "es": ""},
                "cols": [
                    {"i": 0, "n": "name"},
                    {"i": 1, "n": "d"},
                    {"i": 2, "n": "name->ip_input_value"},
                ],
                "rows": rows,
            }
        ]
    }


def browse_payload(tags: list[str]) -> dict[str, Any]:
    """Browse response: ``{"data": {"tags": [{"t": name, "n": desc}]}}``."""
    return {
        "data": {
            "result": {"er": 0, "es": ""},
            "tags": [
                {"t": name, "n": known_tag(name).description, "m": "", "d": 1} for name in tags
            ],
        }
    }


def xml_history_payload(
    tag_name: str,
    start_ms: int,
    end_ms: int,
    period_s: int | None,
    with_description: bool,
    rng: random.Random,
    cap: int = 60,
) -> dict[str, Any]:
    """``XmlHistoryResponseParser`` input.

    Samples must NOT carry an ``er`` key -- the parser skips any sample that
    has one (see response_parser.py, ``if "er" in sample: continue``).
    """
    spec = known_tag(tag_name)
    span_ms = max(end_ms - start_ms, 1000)
    step_ms = (period_s * 1000) if period_s else max(span_ms // 24, 1000)
    count = max(1, min(cap, span_ms // step_ms + 1))

    samples = []
    drift = 0.0
    for i in range(int(count)):
        drift += rng.gauss(0.0, spec.noise * 0.25)
        samples.append(
            {
                "t": int(start_ms + i * step_ms),
                "v": spec.sample(rng, drift),
                "s": rng.choice(_QUALITY_CODES),
            }
        )

    fields: list[Any] = ["VAL"]
    if with_description:
        fields.append(spec.description)
    return {"data": [{"l": fields, "n": tag_name, "samples": samples}]}


# --------------------------------------------------------------------------
# The mock backend
# --------------------------------------------------------------------------
@dataclass
class MockBackend:
    """A disposable mocked IP.21 server backed by a respx router.

    Args:
        fail_status: When set, every route answers with this HTTP status
            instead of data. Used by the error-handling demo.
        seed: Optional RNG seed for reproducible payloads.
    """

    fail_status: int | None = None
    seed: int | None = None
    calls: list[HttpCall] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._router = respx.Router(assert_all_called=False)

        # Declared respx routes. All three SQL query kinds share one URL, so
        # the SQL route dispatches further on the XML body.
        self._router.post(url=f"{BASE_URL}/SQL").mock(side_effect=self._handle_sql)
        self._router.get(url__startswith=f"{BASE_URL}/Browse").mock(side_effect=self._handle_browse)
        # The bare base URL is aspy21's "XML endpoint" (AVG reads, with retry).
        self._router.post(url=BASE_URL).mock(side_effect=self._handle_xml)

        self._transport = respx.transports.MockTransport(router=self._router)

    # -- plumbing ---------------------------------------------------------
    def client(self, **overrides: Any) -> AspenClient:
        """Build a real ``AspenClient`` wired to this mock."""
        http_client = httpx.Client(transport=self._transport, timeout=10.0)
        kwargs: dict[str, Any] = {
            "base_url": BASE_URL,
            "datasource": DATASOURCE,
            "http_client": http_client,
        }
        kwargs.update(overrides)
        self._http_client = http_client
        return AspenClient(**kwargs)

    def close(self) -> None:
        client = getattr(self, "_http_client", None)
        if client is not None:
            client.close()

    def __enter__(self) -> MockBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def http_calls(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.calls]

    def _log(self, call: HttpCall) -> None:
        with self._lock:
            self.calls.append(call)

    def _maybe_fail(self, request: httpx.Request, kind: str) -> httpx.Response | None:
        """Return an error response when the backend is in failure mode."""
        if self.fail_status is None:
            return None
        body = request.content.decode("utf-8", "replace") if request.content else None
        self._log(
            HttpCall(
                method=request.method,
                url=str(request.url),
                kind=f"{kind} (forced failure)",
                status=self.fail_status,
                request_body=body,
                response_summary=f"HTTP {self.fail_status} returned by mock",
            )
        )
        return httpx.Response(
            self.fail_status,
            json={"error": f"Mocked HTTP {self.fail_status} from IP.21 demo backend"},
        )

    # -- route handlers ---------------------------------------------------
    def _handle_sql(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        group = _extract_g(body) or "aspy21_sql"

        failure = self._maybe_fail(request, group)
        if failure is not None:
            return failure

        sql = _extract_sql(body)

        if group == "aspy21_snapshot":
            tags = _tags_from_sql(sql) or DEFAULT_TAGS
            with_desc = "ip_description" in sql
            payload: Any = sql_snapshot_payload(tags, with_desc, self._rng)
            summary = f"{len(payload)} snapshot record(s)"

        elif group == "aspy21_search":
            desc_pat = _like_pattern(sql, "d") or ""
            name_pat = _like_pattern(sql, "name")
            if name_pat:
                # SQL LIKE wildcards back to IP.21 ones: % -> *, _ -> ?
                candidates = match_tags(name_pat.replace("%", "*").replace("_", "?"))
            else:
                candidates = list(TAGS)
            needle = desc_pat.strip("%").lower()
            if needle:
                candidates = [t for t in candidates if needle in known_tag(t).description.lower()]
            payload = sql_search_payload(candidates, self._rng)
            summary = f"{len(candidates)} tag(s) matched"

        else:  # aspy21_history
            tags = _tags_from_sql(sql) or DEFAULT_TAGS
            start, end = _range_from_sql(sql)
            request_code = _int_from_sql(sql, "request")
            period_tenths = _int_from_sql(sql, "period")
            interval_s = period_tenths // 10 if period_tenths else None
            payload = sql_history_payload(
                tags=tags,
                start=start,
                end=end,
                interval_s=interval_s,
                raw=(request_code == 4),
                with_description="ip_description" in sql,
                include_status="status" in sql.split("from")[0],
                rng=self._rng,
                cap=60,
            )
            kind = "RAW" if request_code == 4 else "INT"
            summary = f"{len(payload)} {kind} record(s) for {len(tags)} tag(s)"
            if period_tenths:
                summary += f"; period={period_tenths} tenths = {interval_s}s"

        self._log(
            HttpCall(
                method=request.method,
                url=str(request.url),
                kind=group,
                status=200,
                request_body=body,
                response_summary=summary,
            )
        )
        return httpx.Response(200, json=payload)

    def _handle_browse(self, request: httpx.Request) -> httpx.Response:
        failure = self._maybe_fail(request, "Browse")
        if failure is not None:
            return failure

        pattern = request.url.params.get("tag", "*")
        matched = match_tags(pattern)
        self._log(
            HttpCall(
                method=request.method,
                url=str(request.url),
                kind="Browse",
                status=200,
                request_body=None,
                response_summary=f"{len(matched)} tag(s) matched pattern {pattern!r}",
            )
        )
        return httpx.Response(200, json=browse_payload(matched))

    def _handle_xml(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")

        failure = self._maybe_fail(request, "XML history")
        if failure is not None:
            return failure

        tag_name = _xml_tag(body, "N") or DEFAULT_TAGS[0]
        start_ms = int(_xml_tag(body, "St") or 0)
        end_ms = int(_xml_tag(body, "Et") or start_ms + 3_600_000)
        period_raw = _xml_tag(body, "P")
        period_s = int(period_raw) if period_raw else None
        rt_code = _xml_tag(body, "RT") or "0"

        payload = xml_history_payload(
            tag_name=tag_name,
            start_ms=start_ms,
            end_ms=end_ms,
            period_s=period_s,
            with_description="IP_DESCRIPTION" in body,
            rng=self._rng,
        )
        n = len(payload["data"][0]["samples"])
        summary = f"{n} sample(s) for {tag_name} (RT={rt_code}"
        summary += f", P={period_s}s)" if period_s else ")"

        self._log(
            HttpCall(
                method=request.method,
                url=str(request.url),
                kind="XML history",
                status=200,
                request_body=body,
                response_summary=summary,
            )
        )
        return httpx.Response(200, json=payload)
