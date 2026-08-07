"""End-to-end self-test for live mode.

``selftest.py`` covers the mocked backend. This one covers the other half:
that ``ASPY21_MODE=live`` really opens sockets, really sends credentials, and
really logs what came back.

It stands up a localhost server that speaks the IP.21 ProcessData wire format
-- reusing the demo's own payload builders, so the responses are the shapes
aspy21 expects -- and requires HTTP Basic auth. Nothing is patched at the
transport layer: real httpx, real sockets, real Authorization headers. The
only thing that is not real is the historian behind them.

Usage:
    .venv\\Scripts\\python.exe selftest_live.py

Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mock_backend import MockBackend

logging.disable(logging.INFO)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

PORT = 8731
USER = "demo_operator"
PASSWORD = "not-a-real-password"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  [{'pass' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


# --------------------------------------------------------------------------
# A fake historian that is a real HTTP server
# --------------------------------------------------------------------------
engine = MockBackend()
seen_auth: list[str] = []


async def _dispatch(request: Request, handler: str) -> Response:
    header = request.headers.get("authorization", "")
    seen_auth.append(header)
    if header != EXPECTED_AUTH:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.body()
    proxied = httpx.Request(request.method, str(request.url), content=body)
    result = getattr(engine, handler)(proxied)
    return Response(
        result.content,
        status_code=result.status_code,
        media_type=result.headers.get("content-type", "application/json"),
    )


async def sql(request: Request) -> Response:
    return await _dispatch(request, "_handle_sql")


async def browse(request: Request) -> Response:
    return await _dispatch(request, "_handle_browse")


async def xml_root(request: Request) -> Response:
    return await _dispatch(request, "_handle_xml")


fake_ip21 = Starlette(
    routes=[
        Route("/ProcessData/SQL", sql, methods=["POST"]),
        Route("/ProcessData/Browse", browse, methods=["GET"]),
        Route("/ProcessData", xml_root, methods=["POST"]),
    ]
)


def start_server() -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(fake_ip21, host="127.0.0.1", port=PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    sys.exit("the fake IP.21 server did not start")


server = start_server()

# config reads the environment exactly once, at import time, so live mode has
# to be configured before main is imported.
os.environ.update(
    ASPY21_MODE="live",
    ASPY21_BASE_URL=f"http://127.0.0.1:{PORT}/ProcessData",
    ASPY21_DATASOURCE="IP21_PROD",
    ASPY21_USERNAME=USER,
    ASPY21_PASSWORD=PASSWORD,
    ASPY21_TIMEOUT="15",
    # Point at a path that will not exist, so a real saved connection on this
    # machine cannot leak into the test -- or, worse, be queried by it.
    ASPY21_CONFIG=os.path.join(PROJECT_DIR, "aspy21.selftest-isolated.json"),
)

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)


def execute(operation: str, **params: object) -> dict:
    res = client.post("/api/execute", json={"operation": operation, "params": params})
    if res.status_code != 200:
        failures.append(f"{operation} returned HTTP {res.status_code}")
        return {}
    return res.json()


print("configuration comes from the environment")
env = client.get("/api/environment").json()
check(env["mode"] == "live", "environment reports live mode")
check(env["base_url"] == f"http://127.0.0.1:{PORT}/ProcessData", "base_url from ASPY21_BASE_URL")
check(env["datasource"] == "IP21_PROD", "datasource from ASPY21_DATASOURCE")
check(env["auth_configured"] is True, "auth reported as configured")
check(env["demo_tags"] == [], "the mock tag catalogue is withheld in live mode")

print("\ncredentials never leak into any response")
check(PASSWORD not in str(env), "the password is absent from /api/environment")
check(PASSWORD not in client.get("/").text, "the password is absent from the dashboard HTML")
check(PASSWORD not in client.get("/api/catalog").text, "the password is absent from /api/catalog")

print("\nhealth check performs a real call")
health = client.get("/api/health").json()
check(
    health.get("status") == "ok",
    f"health ok (got {health.get('status')}: {health.get('error')})",
)
check(health.get("mode") == "live", "health reports live mode")
check(health.get("tags_visible", 0) >= 1, "health saw at least one tag")

print("\ntag discovery over the socket")
disc = client.get("/api/tags", params={"pattern": "FLOW_*"}).json()
names = sorted(t["name"] for t in disc.get("tags", []))
check(names == ["FLOW_101", "FLOW_102", "FLOW_310"], f"browse returned the flow tags: {names}")
check(disc.get("endpoint") == "Browse", "aspy21 chose the Browse endpoint")
check(len(disc.get("http_calls", [])) == 1, "exactly one HTTP call was logged")
call = (disc.get("http_calls") or [{}])[0]
check(call.get("status") == 200, "the logged call carries the real status code")
check("bytes" in (call.get("response_summary") or ""), "the summary reports real byte counts")
check(f"127.0.0.1:{PORT}" in (call.get("url") or ""), "the logged URL is the real server")

print("\nreads over the socket")
read = execute(
    "read_int",
    tags=["FLOW_101", "FLOW_102"],
    start="2026-08-05 10:00:00",
    end="2026-08-05 11:00:00",
    interval=60,
    output="JSON",
)
check(read.get("ok") is True, f"read succeeded: {read.get('unexpected_error')}")
check(read.get("row_count", 0) > 0, f"read returned rows ({read.get('row_count')})")
check(read.get("http_call_count") == 1, "SQL batched both tags into one call")
body = (read.get("http_calls") or [{}])[0].get("request_body") or ""
check("period=600" in body, "the logged body is the real generated SQL (interval 60 -> period 600)")
check("name in (" in body, "the logged body shows the batched IN clause")

avg = execute(
    "read_aggregate",
    tags=["FLOW_101", "FLOW_102"],
    start="2026-08-05 00:00:00",
    end="2026-08-05 08:00:00",
    interval=600,
    output="JSON",
)
check(
    avg.get("http_call_count") == 2,
    f"AVG issues one call per tag ({avg.get('http_call_count')})",
)

print("\ncredentials actually travelled")
check(bool(seen_auth), "the server saw Authorization headers")
check(all(h == EXPECTED_AUTH for h in seen_auth), "every request carried the configured Basic auth")
rejected = httpx.get(
    f"http://127.0.0.1:{PORT}/ProcessData/Browse?dataSource=IP21_PROD&tag=*",
    auth=("wrong", "wrong"),
)
check(rejected.status_code == 401, "the fake server does reject wrong credentials")

print("\nthe generated snippet targets the real server")
snippet = read.get("code") or ""
check(f"127.0.0.1:{PORT}" in snippet, "snippet points at the live base_url")
check("os.environ" in snippet, "snippet reads credentials from the environment")
check(PASSWORD not in snippet, "snippet never embeds the password")

print("\nmock-only cards are refused, not faked")
res = client.post(
    "/api/execute",
    json={"operation": "error_handling", "params": {"tags": ["FLOW_101"], "fail_status": "500"}},
)
check(res.status_code == 409, f"error_handling returns 409 in live mode (got {res.status_code})")
check("ASPY21_MODE=mock" in res.json().get("detail", ""), "the 409 explains how to run it")

print("\nthe dashboard warns that this is live")
page = client.get("/").text
check("Live mode" in page, "the live banner is shown")
check("banner-live" in page, "the live banner carries its own style")
check("Not available in live mode" in page, "the mock-only card is marked unavailable")
check(USER in page, "the username is shown, so the operator knows who they are connected as")

print("\nmisconfiguration fails at startup, not mid-demo")
BAD_CONFIGS = [
    ({"ASPY21_MODE": "live", "ASPY21_BASE_URL": ""}, "needs a base URL"),
    ({"ASPY21_MODE": "staging"}, "not valid"),
    (
        {"ASPY21_MODE": "live", "ASPY21_BASE_URL": "aspen.local/ProcessData"},
        "must start with http",
    ),
    (
        {
            "ASPY21_MODE": "live",
            "ASPY21_BASE_URL": "https://x/ProcessData",
            "ASPY21_PASSWORD": "p",
            "ASPY21_USERNAME": "",
        },
        "no username is configured",
    ),
    (
        {
            "ASPY21_MODE": "live",
            "ASPY21_BASE_URL": "https://x/ProcessData",
            "ASPY21_TIMEOUT": "soon",
        },
        "is not a number",
    ),
]
for overrides, expected in BAD_CONFIGS:
    child_env = {**os.environ, **overrides}
    proc = subprocess.run(
        # Reading SETTINGS is what main does while importing, so this is the
        # same moment the real server would fail at.
        [sys.executable, "-c", "import config;config.SETTINGS"],
        env=child_env,
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR,
    )
    combined = proc.stdout + proc.stderr
    check(
        proc.returncode != 0 and expected in combined,
        f"{overrides.get('ASPY21_MODE')}/{list(overrides)[-1]} rejected with {expected!r}",
    )

# --------------------------------------------------------------------------
# The settings file setup_live.py writes
# --------------------------------------------------------------------------
print("\nsaved settings file (what setup_live.py writes)")

SCRATCH_CONFIG = os.path.join(PROJECT_DIR, "aspy21.selftest.json")


def read_config_in_child(overrides: dict[str, str], expression: str) -> subprocess.CompletedProcess:
    """Import config in a clean child process and print one expression."""
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("ASPY21_")}
    child_env.update({"ASPY21_CONFIG": SCRATCH_CONFIG, **overrides})
    return subprocess.run(
        [sys.executable, "-c", f"import config;print({expression})"],
        env=child_env,
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR,
    )


saved_file = {
    "mode": "live",
    "base_url": f"http://127.0.0.1:{PORT}/ProcessData",
    "datasource": "IP21_PROD",
    "username": USER,
    "timeout": 20.0,
    "verify_ssl": True,
    "ca_bundle": "",
}
with open(SCRATCH_CONFIG, "w", encoding="utf-8") as fh:
    json.dump(saved_file, fh)

try:
    # The password still has to come from somewhere; the environment stands in
    # for the credential store here, so this runs on machines without a vault.
    proc = read_config_in_child(
        {"ASPY21_PASSWORD": PASSWORD},
        "(config.SETTINGS.mode, config.SETTINGS.base_url, config.SETTINGS.timeout)",
    )
    check(
        f"'{saved_file['base_url']}'" in proc.stdout and "20.0" in proc.stdout,
        f"the file alone configures live mode ({proc.stdout.strip() or proc.stderr[-160:]})",
    )

    proc = read_config_in_child(
        {"ASPY21_MODE": "mock"},
        "config.SETTINGS.mode",
    )
    check(proc.stdout.strip() == "mock", "ASPY21_MODE=mock overrides mode=live in the file")

    proc = read_config_in_child(
        {"ASPY21_PASSWORD": PASSWORD, "ASPY21_DATASOURCE": "OVERRIDDEN"},
        "config.SETTINGS.datasource",
    )
    check(proc.stdout.strip() == "OVERRIDDEN", "an env var overrides the file's datasource")

    proc = read_config_in_child({}, "config.SETTINGS.mode")
    check(
        proc.returncode != 0 and "setup_live.py" in (proc.stdout + proc.stderr),
        "a missing password fails with a pointer to setup_live.py",
    )

    # The repair tool must survive the state it exists to repair: config
    # validates lazily so that importing it for helpers cannot fail.
    repair_env = {k: v for k, v in os.environ.items() if not k.startswith("ASPY21_")}
    repair_env["ASPY21_CONFIG"] = SCRATCH_CONFIG
    proc = subprocess.run(
        [sys.executable, "setup_live.py", "--show"],
        env=repair_env,
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR,
    )
    check(
        proc.returncode == 0 and USER in proc.stdout,
        "setup_live.py --show still runs when the saved connection cannot be loaded",
    )

    with open(SCRATCH_CONFIG, "w", encoding="utf-8") as fh:
        json.dump({**saved_file, "password": "leaked"}, fh)
    proc = read_config_in_child({}, "config.SETTINGS.mode")
    check(
        proc.returncode != 0 and "never stores passwords in files" in (proc.stdout + proc.stderr),
        "a settings file containing a password is refused outright",
    )

    with open(SCRATCH_CONFIG, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    proc = read_config_in_child({}, "config.SETTINGS.mode")
    check(
        proc.returncode != 0 and "could not be read" in (proc.stdout + proc.stderr),
        "a corrupt settings file is reported, not silently ignored",
    )
finally:
    if os.path.exists(SCRATCH_CONFIG):
        os.remove(SCRATCH_CONFIG)

server.should_exit = True

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL LIVE-MODE CHECKS PASSED")
