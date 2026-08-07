"""End-to-end self-test for the aspy21 dashboard.

Runs every operation card through the real ASGI app (no server needed) and
asserts the interesting behaviours, not just HTTP 200s.

Usage:
    .venv\\Scripts\\python.exe selftest.py

Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import logging
import os
import sys

# Force the mocked backend before main is imported. Without this, a saved live
# connection (aspy21.local.json) would send this test's reads to a real
# historian.
os.environ["ASPY21_MODE"] = "mock"

from fastapi.testclient import TestClient

from main import app
from operations import default_window

# main.py configures INFO logging for the app; keep the test output readable.
logging.disable(logging.INFO)

client = TestClient(app)
START, END = default_window(1)
DAY_START, DAY_END = default_window(8)

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [pass] {label}")
    else:
        print(f"  [FAIL] {label}")
        failures.append(label)


def execute(operation: str, **params: object) -> dict:
    res = client.post("/api/execute", json={"operation": operation, "params": params})
    if res.status_code != 200:
        failures.append(f"{operation} returned HTTP {res.status_code}")
        return {}
    body = res.json()
    if body.get("unexpected_error"):
        failures.append(f"{operation} crashed: {body['unexpected_error']['type']}")
        print(body["unexpected_error"].get("traceback", ""))
    return body


print("pages and metadata")
for path in (
    "/",
    "/docs",
    "/openapi.json",
    "/api/environment",
    "/api/catalog",
    "/api/health",
    "/static/style.css",
    "/static/app.js",
):
    check(client.get(path).status_code == 200, f"GET {path}")

print("\ntag discovery (/api/tags -> real client.search())")


def discover(**query: object) -> dict:
    res = client.get("/api/tags", params=query)
    if res.status_code != 200:
        failures.append(f"/api/tags {query} returned HTTP {res.status_code}")
        return {}
    return res.json()


d = discover()
names = [t["name"] for t in d.get("tags", [])]
check(d.get("count", 0) > 10, f"default browse returns the full catalogue ({d.get('count')})")
check(d.get("endpoint") == "Browse", "name-only discovery uses the Browse endpoint")
check(all(t["description"] for t in d.get("tags", [])), "every tag carries a description")
check(len(d.get("http_calls", [])) == 1, "discovery makes exactly one HTTP call")

d = discover(pattern="FLOW_*")
check(
    sorted(t["name"] for t in d.get("tags", [])) == ["FLOW_101", "FLOW_102", "FLOW_310"],
    "the * wildcard matches a prefix group",
)

d = discover(pattern="ATI11?")
check(
    sorted(t["name"] for t in d.get("tags", [])) == ["ATI111", "ATI112"],
    "the ? wildcard matches exactly one character",
)

d = discover(description="temperature")
check(
    d.get("endpoint") == "aspy21_search",
    "a description filter switches discovery to the SQL search endpoint",
)
check(d.get("count", 0) >= 4, "description search finds the temperature tags")

d = discover(pattern="REACTOR*", description="level")
check(
    [t["name"] for t in d.get("tags", [])] == ["REACTOR_LEVEL"],
    "pattern and description filters combine server-side",
)

d = discover(pattern="NO_SUCH_TAG_*")
check(d.get("count") == 0, "an unmatched pattern returns an empty list, not an error")

print("\ndiscovered tags are usable in a read")
d = discover(pattern="PUMP_*")
pump_tags = [t["name"] for t in d.get("tags", [])]
check(len(pump_tags) == 2, "found both pump tags")
r = execute("read_int", tags=pump_tags, start=START, end=END, interval=60, output="JSON")
returned = {rec["tag"] for rec in (r.get("records") or [])}
check(
    returned == set(pump_tags),
    f"reading the discovered tags returns exactly those tags: {sorted(returned)}",
)

print("\nan empty tag selection is refused, not silently defaulted")
res = client.post(
    "/api/execute",
    json={"operation": "read_raw", "params": {"tags": [], "start": START, "end": END}},
)
check(res.status_code == 400, "empty tag list returns HTTP 400")
check("at least one tag" in res.json().get("detail", "").lower(), "the 400 explains what to do")

print("\nreads return data and hit the mocked transport")
# The SQL reader batches every tag into one query; the XML reader (AVG) loops
# and issues one request per tag. Both are aspy21's own behaviour.
for read_type, extra, calls_for_two_tags in (
    ("RAW", {}, 1),
    ("INT", {"interval": 60}, 1),
    ("AVG", {"interval": 600}, 2),
):
    op = {"RAW": "read_raw", "INT": "read_int", "AVG": "read_aggregate"}[read_type]
    for output in ("JSON", "DATAFRAME"):
        d = execute(
            op, tags=["REACTOR_TEMP", "FLOW_101"], start=START, end=END, output=output, **extra
        )
        check(d.get("row_count", 0) > 0, f"{read_type}/{output} returned rows")
        check(
            d.get("http_call_count") == calls_for_two_tags,
            f"{read_type}/{output} made {calls_for_two_tags} HTTP call(s) for 2 tags",
        )

print("\nbatching: SQL batches tags, the XML endpoint does not")
d = execute(
    "read_int",
    tags=["REACTOR_TEMP", "FLOW_101", "LEVEL_205"],
    start=START,
    end=END,
    interval=60,
    output="JSON",
)
check(d.get("http_call_count") == 1, "INT batches 3 tags into a single SQL query")
body = (d.get("http_calls") or [{}])[0].get("request_body") or ""
check("name in (" in body, "batched query uses a SQL IN clause")

d = execute(
    "read_aggregate",
    tags=["REACTOR_TEMP", "FLOW_101", "LEVEL_205"],
    start=DAY_START,
    end=DAY_END,
    interval=600,
    output="JSON",
)
check(d.get("http_call_count") == 3, "AVG issues one XML request per tag")

d = execute("read_snapshot", tags=["REACTOR_TEMP", "ATI111"], include="ALL", output="JSON")
check(d.get("row_count") == 2, "SNAPSHOT returned one record per tag")
check("description" in (d.get("records") or [{}])[0], "SNAPSHOT include=ALL adds description")

print("\ninterval -> period conversion (tenths of a second)")
d = execute("read_int", tags=["REACTOR_TEMP"], start=START, end=END, interval=60, output="JSON")
body = (d.get("http_calls") or [{}])[0].get("request_body") or ""
check("period=600" in body, "interval=60 becomes period=600 in the SQL body")
check("request=1" in body, "INT maps to request=1")

d = execute("read_int", tags=["REACTOR_TEMP"], start=START, end=END, interval=None, output="JSON")
body = (d.get("http_calls") or [{}])[0].get("request_body") or ""
check("period=" not in body, "no interval means no period= clause")

d = execute("read_raw", tags=["REACTOR_TEMP"], start=START, end=END, output="JSON")
body = (d.get("http_calls") or [{}])[0].get("request_body") or ""
check("request=4" in body, "RAW maps to request=4")

print("\nsearch: endpoint selection and return shapes")
d = execute("search", tag="REACTOR*", description="", include="NONE", limit=100)
check(d.get("result_kind") == "list", "include=NONE returns bare tag names")
check(
    (d.get("http_calls") or [{}])[0].get("kind") == "Browse",
    "no description filter uses the Browse endpoint",
)

d = execute("search", tag="*", description="temperature", include="DESCRIPTION", limit=100)
check(
    (d.get("http_calls") or [{}])[0].get("kind") == "aspy21_search",
    "description filter switches to the SQL search endpoint",
)
check(d.get("result_kind") == "records", "include=DESCRIPTION returns dicts")

d = execute("search", tag="ATI???", description="", include="NONE", limit=100)
check(
    sorted(d.get("records") or []) == ["ATI111", "ATI112"],
    "ATI??? matches both ambient indicators, one character per ?",
)

d = execute("search", tag="ATI11?", description="", include="NONE", limit=100)
check(sorted(d.get("records") or []) == ["ATI111", "ATI112"], "ATI11? matches both too")

d = execute("search", tag="ATI1111", description="", include="NONE", limit=100)
check(d.get("row_count") == 0, "a too-long literal name matches nothing")

d = execute(
    "search_hybrid",
    tag="REACTOR*",
    description="",
    start=START,
    end=END,
    interval=300,
    read_type="INT",
    include="NONE",
    output="DATAFRAME",
)
check(d.get("http_call_count") == 2, "hybrid mode makes 2 calls (discover, then read)")
check(d.get("row_count", 0) > 0, "hybrid mode returned data")

print("\nIncludeFields changes the returned field set")
d = execute(
    "include_fields",
    tags=["REACTOR_TEMP"],
    start=START,
    end=END,
    variants=["NONE", "STATUS", "DESCRIPTION", "ALL"],
    output="JSON",
)
by_name = {c["include"]: c["keys"] for c in d.get("comparisons", [])}
check(by_name.get("NONE") == ["timestamp", "tag", "value"], "NONE gives 3 fields")
check("status" in by_name.get("STATUS", []), "STATUS adds a status field")
check("description" in by_name.get("DESCRIPTION", []), "DESCRIPTION adds a description")
check({"status", "description"} <= set(by_name.get("ALL", [])), "ALL adds both")

print("\nretry behaviour differs by read path")
d = execute(
    "error_handling",
    tags=["REACTOR_TEMP"],
    start=START,
    end=END,
    read_type="AVG",
    fail_status="500",
)
check(d.get("attempts") == 3, "AVG retries 3 times (tenacity on the XML endpoint)")
check("RetryError" in (d.get("error") or {}).get("type", ""), "AVG surfaces tenacity.RetryError")

for read_type in ("RAW", "INT", "SNAPSHOT"):
    d = execute(
        "error_handling",
        tags=["REACTOR_TEMP"],
        start=START,
        end=END,
        read_type=read_type,
        fail_status="503",
    )
    check(d.get("attempts") == 1, f"{read_type} does not retry")
    check(
        "HTTPStatusError" in (d.get("error") or {}).get("type", ""),
        f"{read_type} surfaces httpx.HTTPStatusError",
    )

print("\ncache probe reflects reality for this aspy21 build")
d = execute("repeat_timing", tags=["REACTOR_TEMP"], start=START, end=END)
summary = d.get("summary", {})
check(summary.get("total_http_calls") == 2, "two identical reads make two HTTP calls (no caching)")
check(
    summary.get("accepts_cache_kwarg") is False,
    "AspenClient takes no cache= keyword in this release",
)
check(not any(summary.get("cache_api_probe", {}).values()), "no cache methods exist on AspenClient")

print("\nmocked values carry jitter")
values = []
for _ in range(2):
    d = execute("read_snapshot", tags=["REACTOR_TEMP"], include="NONE", output="JSON")
    values.append((d.get("records") or [{}])[0].get("value"))
check(values[0] != values[1], f"repeat snapshots differ: {values}")

print("\nerror handling for bad input")
res = client.post("/api/execute", json={"operation": "does_not_exist", "params": {}})
check(res.status_code == 404, "unknown operation returns 404")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
