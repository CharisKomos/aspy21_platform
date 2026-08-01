"""The dashboard's operation catalogue and executors.

Every executor here calls the real ``aspy21.AspenClient``. None of them
reimplement library logic -- they only build arguments, invoke the library,
and shape the result for display.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections.abc import Callable
from typing import Any

import pandas as pd
from aspy21 import AspenClient, IncludeFields, OutputFormat, ReaderType

from mock_backend import DATASOURCE, TAGS, MockBackend

# Reader types this installed version actually offers. 0.2.0b15 ships
# RAW/INT/SNAPSHOT/AVG only -- there is no MIN, MAX or RNG.
AVAILABLE_READER_TYPES = [r.name for r in ReaderType]
AVAILABLE_INCLUDE_FIELDS = [i.name for i in IncludeFields]
AVAILABLE_OUTPUT_FORMATS = [o.name for o in OutputFormat]

# Cache API presence, probed rather than assumed.
CACHE_METHODS = ("get_cache_stats", "clear_cache", "invalidate_cache")
HAS_CACHE_API = any(hasattr(AspenClient, name) for name in CACHE_METHODS)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def default_window(hours: int = 1) -> tuple[str, str]:
    """A sensible pre-filled time range: the last ``hours`` hour(s)."""
    end = dt.datetime.now().replace(second=0, microsecond=0)
    start = end - dt.timedelta(hours=hours)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def _jsonable(value: Any) -> Any:
    """Convert pandas/numpy scalars into plain JSON-safe Python values."""
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (pd.Timestamp, dt.datetime, dt.date)):
        return value.isoformat()
    if value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return None
    # numpy scalars expose .item()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except Exception:  # pragma: no cover - defensive
            return str(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _records(data: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in row.items()} for row in data]


def df_to_table(df: pd.DataFrame) -> dict[str, Any]:
    """Render a DataFrame as ``{columns, rows}`` for the HTML table."""
    if df.empty:
        return {"columns": [], "rows": [], "index_name": None, "descriptions": {}}
    index_name = df.index.name or "index"
    columns = [index_name] + [str(c) for c in df.columns]
    rows = []
    for idx, row in df.iterrows():
        rows.append([_jsonable(idx)] + [_jsonable(row[c]) for c in df.columns])
    return {
        "columns": columns,
        "rows": rows,
        "index_name": index_name,
        "descriptions": dict(df.attrs.get("tag_descriptions", {})),
    }


def _tag_list(raw: Any, fallback: list[str]) -> list[str]:
    if isinstance(raw, list):
        tags = [str(t).strip() for t in raw if str(t).strip()]
    elif isinstance(raw, str):
        tags = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
    else:
        tags = []
    return tags or fallback


def _opt_int(raw: Any) -> int | None:
    if raw in (None, "", "none", "None"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _enum(enum_cls: Any, raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return enum_cls[str(raw).upper()]
    except KeyError:
        return default


def _shape_result(result: Any, output: OutputFormat) -> dict[str, Any]:
    """Normalise an aspy21 return value into a displayable payload."""
    if isinstance(result, pd.DataFrame):
        return {
            "result_kind": "dataframe",
            "table": df_to_table(result),
            "row_count": len(result),
            "records": None,
        }
    if isinstance(result, list):
        if result and isinstance(result[0], dict):
            return {
                "result_kind": "records",
                "records": _records(result),
                "row_count": len(result),
                "table": None,
            }
        return {
            "result_kind": "list",
            "records": [_jsonable(v) for v in result],
            "row_count": len(result),
            "table": None,
        }
    return {
        "result_kind": "scalar",
        "records": _jsonable(result),
        "row_count": 1,
        "table": None,
    }


def _code(call: str) -> str:
    return (
        "from aspy21 import AspenClient, IncludeFields, OutputFormat, ReaderType\n\n"
        f'with AspenClient(\n    base_url="{"http://mock.ip21.local/ProcessData"}",\n'
        f'    datasource="{DATASOURCE}",\n) as client:\n    {call}'
    )


# --------------------------------------------------------------------------
# executors
# --------------------------------------------------------------------------
def op_read_raw(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    tags = _tag_list(p.get("tags"), ["REACTOR_TEMP", "FLOW_101"])
    start, end = p.get("start"), p.get("end")
    include = _enum(IncludeFields, p.get("include"), IncludeFields.NONE)
    output = _enum(OutputFormat, p.get("output"), OutputFormat.JSON)

    client = mb.client()
    result = client.read(
        tags,
        start=start,
        end=end,
        read_type=ReaderType.RAW,
        include=include,
        output=output,
    )
    return {
        **_shape_result(result, output),
        "code": _code(
            f"data = client.read(\n        {tags!r},\n        start={start!r},\n"
            f"        end={end!r},\n        read_type=ReaderType.RAW,\n"
            f"        include=IncludeFields.{include.name},\n"
            f"        output=OutputFormat.{output.name},\n    )"
        ),
        "notes": [
            "RAW returns points as stored by the historian, so timestamps are "
            "unevenly spaced (the mock jitters them deliberately).",
            'Routed to POST /SQL with g="aspy21_history" and request=4.',
        ],
    }


def op_read_int(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    tags = _tag_list(p.get("tags"), ["REACTOR_TEMP", "FLOW_101"])
    start, end = p.get("start"), p.get("end")
    interval = _opt_int(p.get("interval"))
    include = _enum(IncludeFields, p.get("include"), IncludeFields.NONE)
    output = _enum(OutputFormat, p.get("output"), OutputFormat.DATAFRAME)

    client = mb.client()
    result = client.read(
        tags,
        start=start,
        end=end,
        interval=interval,
        read_type=ReaderType.INT,
        include=include,
        output=output,
    )
    notes = [
        "INT asks IP.21 for interpolated values on a fixed grid.",
        'Routed to POST /SQL with g="aspy21_history" and request=1.',
    ]
    if interval is not None:
        notes.append(
            f"aspy21 converts interval={interval}s to period={interval * 10} "
            "(tenths of a second) in the SQL WHERE clause -- visible in the "
            "request body below."
        )
    else:
        notes.append("No interval supplied, so no period= clause is added.")
    return {
        **_shape_result(result, output),
        "code": _code(
            f"df = client.read(\n        {tags!r},\n        start={start!r},\n"
            f"        end={end!r},\n        interval={interval!r},\n"
            f"        read_type=ReaderType.INT,\n"
            f"        include=IncludeFields.{include.name},\n"
            f"        output=OutputFormat.{output.name},\n    )"
        ),
        "notes": notes,
    }


def op_read_snapshot(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    tags = _tag_list(p.get("tags"), list(TAGS))
    include = _enum(IncludeFields, p.get("include"), IncludeFields.ALL)
    output = _enum(OutputFormat, p.get("output"), OutputFormat.JSON)

    client = mb.client()
    result = client.read(
        tags,
        read_type=ReaderType.SNAPSHOT,
        include=include,
        output=output,
    )
    return {
        **_shape_result(result, output),
        "code": _code(
            f"latest = client.read(\n        {tags!r},\n"
            f"        read_type=ReaderType.SNAPSHOT,\n"
            f"        include=IncludeFields.{include.name},\n"
            f"        output=OutputFormat.{output.name},\n    )"
        ),
        "notes": [
            "No start/end needed. aspy21 also auto-selects SNAPSHOT whenever "
            "both start and end are omitted, whatever read_type you pass.",
            'Routed to POST /SQL with g="aspy21_snapshot", querying '
            "all_records for ip_input_value / ip_input_quality.",
            "The timestamp is generated client-side at request time, so it "
            "changes on every execution.",
        ],
    }


def op_read_aggregate(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    tags = _tag_list(p.get("tags"), ["REACTOR_TEMP"])
    start, end = p.get("start"), p.get("end")
    interval = _opt_int(p.get("interval"))
    output = _enum(OutputFormat, p.get("output"), OutputFormat.JSON)

    client = mb.client()
    result = client.read(
        tags,
        start=start,
        end=end,
        interval=interval,
        read_type=ReaderType.AVG,
        output=output,
    )

    notes = [
        "AVG is the only aggregate in aspy21 "
        + _version_label()
        + f" -- ReaderType exposes exactly {AVAILABLE_READER_TYPES}. "
        "MIN, MAX and RNG do not exist in this release.",
        "AVG does not match the SQL history reader, so it falls through to "
        "the XML endpoint: POST to the bare base URL with <RT>10</RT>.",
    ]
    if interval is not None:
        notes.append(
            f"With interval={interval}s the XML query carries <P>{interval}</P>"
            "<PU>3</PU> (PU=3 means seconds). The tenths-of-a-second period "
            "conversion belongs to the SQL history path (RAW/INT), not here."
        )
    else:
        notes.append(
            "Without an interval the <P> element is omitted entirely "
            "(aspy21 only adds it when interval is set and RT >= 10), so the "
            "server picks its own bucket size."
        )
    notes.append("This is the only read path wrapped in tenacity retry inside aspy21.")
    return {
        **_shape_result(result, output),
        "code": _code(
            f"avg = client.read(\n        {tags!r},\n        start={start!r},\n"
            f"        end={end!r},\n        interval={interval!r},\n"
            f"        read_type=ReaderType.AVG,\n"
            f"        output=OutputFormat.{output.name},\n    )"
        ),
        "notes": notes,
    }


def op_search(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    tag = p.get("tag") or "*"
    description = p.get("description") or None
    include = _enum(IncludeFields, p.get("include"), IncludeFields.NONE)
    limit = _opt_int(p.get("limit")) or 10_000
    case_sensitive = bool(p.get("case_sensitive"))

    client = mb.client()
    result = client.search(
        tag=tag,
        description=description,
        case_sensitive=case_sensitive,
        limit=limit,
        include=include,
    )
    notes = [
        "Search-only mode: no start given, so aspy21 returns tag metadata rather than data.",
        "include=NONE or STATUS returns a plain list of tag-name strings; "
        "DESCRIPTION or ALL returns dicts with name + description.",
    ]
    if description:
        notes.append(
            "Because description was supplied, aspy21 uses the SQL endpoint "
            '(g="aspy21_search") and filters server-side, translating * to '
            "the SQL wildcard %."
        )
    else:
        notes.append(
            "With no description filter, aspy21 uses GET /Browse and passes "
            "the wildcard pattern straight through."
        )
    return {
        **_shape_result(result, OutputFormat.JSON),
        "code": _code(
            f"tags = client.search(\n        tag={tag!r},\n"
            f"        description={description!r},\n"
            f"        case_sensitive={case_sensitive!r},\n"
            f"        limit={limit!r},\n"
            f"        include=IncludeFields.{include.name},\n    )"
        ),
        "notes": notes,
    }


def op_search_hybrid(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    tag = p.get("tag") or "*"
    description = p.get("description") or None
    start, end = p.get("start"), p.get("end")
    interval = _opt_int(p.get("interval"))
    read_type = _enum(ReaderType, p.get("read_type"), ReaderType.INT)
    include = _enum(IncludeFields, p.get("include"), IncludeFields.NONE)
    output = _enum(OutputFormat, p.get("output"), OutputFormat.DATAFRAME)

    client = mb.client()
    result = client.search(
        tag=tag,
        description=description,
        start=start,
        end=end,
        interval=interval,
        read_type=read_type,
        include=include,
        output=output,
    )
    return {
        **_shape_result(result, output),
        "code": _code(
            f"df = client.search(\n        tag={tag!r},\n"
            f"        description={description!r},\n        start={start!r},\n"
            f"        end={end!r},\n        interval={interval!r},\n"
            f"        read_type=ReaderType.{read_type.name},\n"
            f"        include=IncludeFields.{include.name},\n"
            f"        output=OutputFormat.{output.name},\n    )"
        ),
        "notes": [
            "Passing start switches search() into hybrid mode: it resolves the "
            "pattern, then calls read() internally with the matched tags.",
            "Expect two HTTP calls below -- one to discover tags, one (or more) "
            "to fetch their data.",
            "One call does both, so you never round-trip the tag list yourself.",
        ],
    }


def op_include_fields(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    """Run the same read under several IncludeFields values and compare."""
    tags = _tag_list(p.get("tags"), ["REACTOR_TEMP"])
    start, end = p.get("start"), p.get("end")
    output = _enum(OutputFormat, p.get("output"), OutputFormat.JSON)
    variants = p.get("variants") or ["NONE", "STATUS", "DESCRIPTION", "ALL"]
    if isinstance(variants, str):
        variants = [variants]

    comparisons = []
    for name in variants:
        include = _enum(IncludeFields, name, IncludeFields.NONE)
        client = mb.client()
        started = time.perf_counter()
        result = client.read(
            tags,
            start=start,
            end=end,
            interval=600,
            read_type=ReaderType.INT,
            include=include,
            output=output,
        )
        elapsed = (time.perf_counter() - started) * 1000

        shaped = _shape_result(result, output)
        if shaped["result_kind"] == "records" and shaped["records"]:
            keys = list(shaped["records"][0].keys())
        elif shaped["result_kind"] == "dataframe":
            keys = shaped["table"]["columns"]
        else:
            keys = []
        comparisons.append(
            {
                "include": include.name,
                "keys": keys,
                "row_count": shaped["row_count"],
                "elapsed_ms": round(elapsed, 1),
                "sample": (shaped["records"][:1] if shaped["records"] else None),
                "table": shaped["table"],
            }
        )

    return {
        "result_kind": "comparison",
        "comparisons": comparisons,
        "records": None,
        "table": None,
        "row_count": sum(c["row_count"] for c in comparisons),
        "code": _code(
            "for include in (IncludeFields.NONE, IncludeFields.STATUS,\n"
            "                IncludeFields.DESCRIPTION, IncludeFields.ALL):\n"
            f"        data = client.read(\n            {tags!r},\n"
            f"            start={start!r},\n            end={end!r},\n"
            "            interval=600,\n            read_type=ReaderType.INT,\n"
            "            include=include,\n        )"
        ),
        "notes": [
            "IncludeFields changes both the generated SQL SELECT list and the "
            "shape of the returned records -- compare the key lists below.",
            "STATUS adds ip_input_quality / status; DESCRIPTION adds "
            "name->ip_description; ALL adds both.",
            "In DataFrame output the extra fields arrive as <tag>_status and "
            "<tag>_description columns.",
        ],
    }


def op_repeat_timing(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    """Run one identical read twice and report timings + HTTP call count.

    The brief asked for a cache demo using ``cache=True`` and
    ``get_cache_stats()``. Those do not exist in this release, so rather than
    fake them this card measures what actually happens on a repeat call and
    reports the introspection result.
    """
    tags = _tag_list(p.get("tags"), ["REACTOR_TEMP", "FLOW_101"])
    start, end = p.get("start"), p.get("end")

    timings = []
    for label in ("first call", "second call"):
        client = mb.client()
        started = time.perf_counter()
        result = client.read(
            tags,
            start=start,
            end=end,
            interval=60,
            read_type=ReaderType.INT,
        )
        elapsed = (time.perf_counter() - started) * 1000
        timings.append(
            {
                "label": label,
                "elapsed_ms": round(elapsed, 2),
                "row_count": len(result) if isinstance(result, list) else 0,
                "http_calls_so_far": len(mb.calls),
            }
        )

    first, second = timings[0]["elapsed_ms"], timings[1]["elapsed_ms"]
    delta = round(second - first, 2)
    probe = {name: hasattr(AspenClient, name) for name in CACHE_METHODS}
    accepts_cache_kwarg = "cache" in AspenClient.__init__.__code__.co_varnames

    return {
        "result_kind": "timing",
        "timings": timings,
        "records": None,
        "table": None,
        "row_count": 0,
        "summary": {
            "first_ms": first,
            "second_ms": second,
            "delta_ms": delta,
            "total_http_calls": len(mb.calls),
            "cache_api_probe": probe,
            "accepts_cache_kwarg": accepts_cache_kwarg,
            "has_cache_api": HAS_CACHE_API,
        },
        "code": _code(
            f"for _ in range(2):\n        data = client.read(\n            {tags!r},\n"
            f"            start={start!r},\n            end={end!r},\n"
            "            interval=60,\n            read_type=ReaderType.INT,\n        )"
        ),
        "notes": [
            f"aspy21 {_version_label()} has no caching layer. Probing "
            f"AspenClient gives {probe}, and its __init__ "
            f"{'does' if accepts_cache_kwarg else 'does not'} accept a cache= "
            "keyword.",
            f"Both calls therefore hit HTTP: {len(mb.calls)} requests were "
            "intercepted for 2 reads. A cache would have made that 1.",
            "The timing difference here is warm-up noise (imports, pandas "
            "dtype paths), not a cache hit -- which is exactly why the number "
            "is small and can go either way.",
            "cache=True, get_cache_stats() and CacheConfig exist on the "
            "project's git main branch, not in this published release.",
        ],
    }


def op_error_handling(p: dict[str, Any], mb: MockBackend) -> dict[str, Any]:
    """Deliberately fail the backend and report what surfaces."""
    tags = _tag_list(p.get("tags"), ["REACTOR_TEMP"])
    start, end = p.get("start"), p.get("end")
    read_type = _enum(ReaderType, p.get("read_type"), ReaderType.AVG)

    client = mb.client()
    started = time.perf_counter()
    error: dict[str, Any] | None = None
    result: Any = None
    try:
        result = client.read(
            tags,
            start=start,
            end=end,
            read_type=read_type,
        )
    # Deliberately broad: this card exists to report whatever aspy21 raises,
    # including tenacity.RetryError, so nothing may be filtered out here.
    except BaseException as exc:
        chain = []
        cursor: BaseException | None = exc
        while cursor is not None and len(chain) < 5:
            chain.append(f"{type(cursor).__module__}.{type(cursor).__name__}")
            cursor = cursor.__cause__ or cursor.__context__
        last_attempt = getattr(exc, "last_attempt", None)
        underlying = None
        if last_attempt is not None:
            try:
                underlying = repr(last_attempt.exception())
            except Exception:  # pragma: no cover
                underlying = None
        error = {
            "type": f"{type(exc).__module__}.{type(exc).__name__}",
            "message": str(exc)[:600],
            "chain": chain,
            "underlying": underlying,
        }
    elapsed = (time.perf_counter() - started) * 1000

    attempts = len(mb.calls)
    retried = attempts > 1
    notes = [
        f"The mock answered every request with HTTP {mb.fail_status}.",
        f"aspy21 made {attempts} HTTP attempt(s) in {elapsed:.0f} ms.",
    ]
    if read_type is ReaderType.AVG:
        notes.append(
            "AVG uses XmlHistoryReader._fetch, decorated with tenacity "
            "@retry(stop=stop_after_attempt(3), "
            "wait=wait_exponential(multiplier=0.5, min=0.5, max=8)) -- hence "
            "3 attempts and a visible backoff delay, surfacing as "
            "tenacity.RetryError."
        )
    else:
        notes.append(
            f"{read_type.name} uses the SQL reader, which has no retry "
            "decorator. response.raise_for_status() fails on the first "
            "attempt and httpx.HTTPStatusError propagates unchanged."
        )
    notes.append(
        "Either way aspy21 does not swallow transport errors: it logs and "
        "re-raises, so callers keep the original httpx exception."
    )

    shaped = (
        _shape_result(result, OutputFormat.JSON)
        if error is None
        else {"result_kind": "error", "records": None, "table": None, "row_count": 0}
    )
    return {
        **shaped,
        "error": error,
        "retry_observed": retried,
        "attempts": attempts,
        "code": _code(
            "try:\n        data = client.read(\n"
            f"            {tags!r},\n            start={start!r},\n"
            f"            end={end!r},\n"
            f"            read_type=ReaderType.{read_type.name},\n        )\n"
            "    except httpx.HTTPStatusError as exc:\n"
            "        print(exc.response.status_code)\n"
            "    except RetryError as exc:\n"
            "        print(exc.last_attempt.exception())"
        ),
        "notes": notes,
    }


def _version_label() -> str:
    import aspy21

    return getattr(aspy21, "__version__", "unknown")


EXECUTORS: dict[str, Callable[[dict[str, Any], MockBackend], dict[str, Any]]] = {
    "read_raw": op_read_raw,
    "read_int": op_read_int,
    "read_snapshot": op_read_snapshot,
    "read_aggregate": op_read_aggregate,
    "search": op_search,
    "search_hybrid": op_search_hybrid,
    "include_fields": op_include_fields,
    "repeat_timing": op_repeat_timing,
    "error_handling": op_error_handling,
}


# --------------------------------------------------------------------------
# catalogue (drives the rendered form controls)
# --------------------------------------------------------------------------
def _tags_field(default: list[str], label: str = "Tags") -> dict[str, Any]:
    return {
        "name": "tags",
        "label": label,
        "type": "tagpicker",
        "default": default,
        "options": list(TAGS),
        "help": "Pick from tags discovered via client.search(), or type names.",
    }


def _select(
    name: str, label: str, options: list[str], default: str, help_text: str = ""
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": "select",
        "options": options,
        "default": default,
        "help": help_text,
    }


def build_catalog() -> list[dict[str, Any]]:
    """Build the card definitions with freshly computed default timestamps."""
    start, end = default_window(1)
    day_start, day_end = default_window(8)

    return [
        {
            "id": "read_raw",
            "index": 1,
            "title": "Read - RAW",
            "verb": "READ",
            "summary": "Raw stored data points for one or more tags over a time range.",
            "detail": (
                "Returns points exactly as the historian stored them, so the "
                "spacing is irregular. Routed to POST /SQL with request=4."
            ),
            "fields": [
                _tags_field(["REACTOR_TEMP", "FLOW_101"]),
                {"name": "start", "label": "Start", "type": "text", "default": start},
                {"name": "end", "label": "End", "type": "text", "default": end},
                _select("include", "IncludeFields", AVAILABLE_INCLUDE_FIELDS, "NONE"),
                _select("output", "OutputFormat", AVAILABLE_OUTPUT_FORMATS, "JSON"),
            ],
        },
        {
            "id": "read_int",
            "index": 2,
            "title": "Read - INT (interpolated)",
            "verb": "READ",
            "summary": "Interpolated values on a fixed interval grid.",
            "detail": (
                "aspy21 converts interval seconds into period tenths-of-seconds "
                "for the SQL WHERE clause. Set interval=60 and look for "
                "period=600 in the intercepted request body."
            ),
            "fields": [
                _tags_field(["REACTOR_TEMP", "FLOW_101"]),
                {"name": "start", "label": "Start", "type": "text", "default": start},
                {"name": "end", "label": "End", "type": "text", "default": end},
                {
                    "name": "interval",
                    "label": "Interval (s)",
                    "type": "number",
                    "default": 60,
                    "help": "Leave blank to omit the period= clause.",
                },
                _select("include", "IncludeFields", AVAILABLE_INCLUDE_FIELDS, "NONE"),
                _select("output", "OutputFormat", AVAILABLE_OUTPUT_FORMATS, "DATAFRAME"),
            ],
        },
        {
            "id": "read_snapshot",
            "index": 3,
            "title": "Read - SNAPSHOT",
            "verb": "READ",
            "summary": "Current value of each tag. No start or end required.",
            "detail": (
                "Queries all_records for ip_input_value and ip_input_quality. "
                "aspy21 also falls back to SNAPSHOT automatically whenever both "
                "start and end are omitted."
            ),
            "fields": [
                _tags_field(list(TAGS)),
                _select("include", "IncludeFields", AVAILABLE_INCLUDE_FIELDS, "ALL"),
                _select("output", "OutputFormat", AVAILABLE_OUTPUT_FORMATS, "JSON"),
            ],
        },
        {
            "id": "read_aggregate",
            "index": 4,
            "title": "Read - AVG (aggregate)",
            "verb": "READ",
            "summary": "Time-weighted average buckets, with or without an interval.",
            "detail": (
                f"ReaderType in this build exposes {AVAILABLE_READER_TYPES}. "
                "AVG is the only aggregate; MIN, MAX and RNG are not in this "
                "release. AVG routes to the XML endpoint (RT=10), the one path "
                "with tenacity retry."
            ),
            "fields": [
                _tags_field(["REACTOR_TEMP"]),
                {"name": "start", "label": "Start", "type": "text", "default": day_start},
                {"name": "end", "label": "End", "type": "text", "default": day_end},
                {
                    "name": "interval",
                    "label": "Interval (s)",
                    "type": "number",
                    "default": 600,
                    "help": "Blank omits <P>, so the server picks the bucket size.",
                },
                _select("output", "OutputFormat", AVAILABLE_OUTPUT_FORMATS, "JSON"),
            ],
        },
        {
            "id": "search",
            "index": 5,
            "title": "Search (wildcards)",
            "verb": "SEARCH",
            "summary": "Find tags by name pattern, optionally filtered by description.",
            "detail": (
                "* matches any run of characters, ? exactly one. Adding a "
                "description switches aspy21 from GET /Browse to the SQL "
                "endpoint for server-side filtering."
            ),
            "fields": [
                {
                    "name": "tag",
                    "label": "Tag pattern",
                    "type": "text",
                    "default": "REACTOR*",
                    "help": "Try REACTOR*, *_101, ATI???, or * for everything.",
                },
                {
                    "name": "description",
                    "label": "Description filter",
                    "type": "text",
                    "default": "",
                    "help": "Optional. Non-empty forces the SQL search path.",
                },
                _select(
                    "include",
                    "IncludeFields",
                    AVAILABLE_INCLUDE_FIELDS,
                    "DESCRIPTION",
                    "NONE/STATUS return names; DESCRIPTION/ALL return dicts.",
                ),
                {"name": "limit", "label": "Limit", "type": "number", "default": 100},
                {
                    "name": "case_sensitive",
                    "label": "Case sensitive",
                    "type": "checkbox",
                    "default": False,
                },
            ],
        },
        {
            "id": "search_hybrid",
            "index": 6,
            "title": "Search - hybrid (search + read)",
            "verb": "SEARCH",
            "summary": "Resolve a wildcard pattern and read the matched tags in one call.",
            "detail": (
                "Supplying start flips search() into hybrid mode: it discovers "
                "tags, then calls read() internally. Watch for two HTTP calls."
            ),
            "fields": [
                {"name": "tag", "label": "Tag pattern", "type": "text", "default": "REACTOR*"},
                {
                    "name": "description",
                    "label": "Description filter",
                    "type": "text",
                    "default": "",
                },
                {"name": "start", "label": "Start", "type": "text", "default": start},
                {"name": "end", "label": "End", "type": "text", "default": end},
                {"name": "interval", "label": "Interval (s)", "type": "number", "default": 300},
                _select("read_type", "ReaderType", AVAILABLE_READER_TYPES, "INT"),
                _select("include", "IncludeFields", AVAILABLE_INCLUDE_FIELDS, "NONE"),
                _select("output", "OutputFormat", AVAILABLE_OUTPUT_FORMATS, "DATAFRAME"),
            ],
        },
        {
            "id": "include_fields",
            "index": 7,
            "title": "IncludeFields variants",
            "verb": "COMPARE",
            "summary": "The same read under NONE, STATUS, DESCRIPTION and ALL, side by side.",
            "detail": (
                "Runs one read per selected variant and diffs the returned "
                "field names, so you can see exactly what each flag adds."
            ),
            "fields": [
                _tags_field(["REACTOR_TEMP"]),
                {"name": "start", "label": "Start", "type": "text", "default": start},
                {"name": "end", "label": "End", "type": "text", "default": end},
                {
                    "name": "variants",
                    "label": "Variants to run",
                    "type": "multiselect",
                    "default": ["NONE", "STATUS", "DESCRIPTION", "ALL"],
                    "options": AVAILABLE_INCLUDE_FIELDS,
                },
                _select("output", "OutputFormat", AVAILABLE_OUTPUT_FORMATS, "JSON"),
            ],
        },
        {
            "id": "repeat_timing",
            "index": 8,
            "title": "Repeat-call timing (cache probe)",
            "verb": "DIAGNOSTIC",
            "summary": "Runs one read twice, times both, and probes for a cache API.",
            "detail": (
                f"aspy21 {_version_label()} ships no caching layer, so this "
                "card reports the live introspection result and proves both "
                "calls reached HTTP instead of faking cache statistics."
            ),
            "fields": [
                _tags_field(["REACTOR_TEMP", "FLOW_101"]),
                {"name": "start", "label": "Start", "type": "text", "default": start},
                {"name": "end", "label": "End", "type": "text", "default": end},
            ],
        },
        {
            "id": "error_handling",
            "index": 9,
            "title": "Error handling and retry",
            "verb": "DIAGNOSTIC",
            "summary": "Force an HTTP failure and watch aspy21's retry and exceptions.",
            "detail": (
                "AVG goes through XmlHistoryReader._fetch, which carries "
                "tenacity retry (3 attempts, exponential backoff) and raises "
                "RetryError. RAW/INT/SNAPSHOT have no retry and raise "
                "httpx.HTTPStatusError on the first failure."
            ),
            "fields": [
                _tags_field(["REACTOR_TEMP"]),
                {"name": "start", "label": "Start", "type": "text", "default": start},
                {"name": "end", "label": "End", "type": "text", "default": end},
                _select(
                    "read_type",
                    "ReaderType",
                    AVAILABLE_READER_TYPES,
                    "AVG",
                    "AVG retries 3x; the others fail immediately.",
                ),
                _select(
                    "fail_status", "Mocked HTTP status", ["500", "502", "503", "404", "401"], "500"
                ),
            ],
        },
    ]
