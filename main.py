"""FastAPI dashboard showcasing every read/search operation in aspy21.

Run it:
    uvicorn main:app --reload

Then open http://127.0.0.1:8000 for the dashboard, or
http://127.0.0.1:8000/docs for the generated OpenAPI docs.

Two backends
------------
By default (``ASPY21_MODE=mock``) all IP.21 traffic is intercepted by
``mock_backend`` -- no server, no credentials, no network access.

Set ``ASPY21_MODE=live`` plus ``ASPY21_BASE_URL`` and the dashboard talks to a
real historian instead; see ``config.py`` for the full variable list. The
executors are identical in both modes -- only the transport changes.
"""

from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path
from typing import Any

import aspy21
import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from config import SETTINGS
from live_backend import open_backend
from mock_backend import TAGS
from operations import (
    AVAILABLE_INCLUDE_FIELDS,
    AVAILABLE_OUTPUT_FORMATS,
    AVAILABLE_READER_TYPES,
    EXECUTORS,
    HAS_CACHE_API,
    MOCK_ONLY_OPERATIONS,
    build_catalog,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aspy21-demo")

BASE_DIR = Path(__file__).parent

app = FastAPI(
    title="aspy21 operation showcase",
    version="1.0.0",
    description=(
        "Interactive dashboard for the aspy21 IP.21 client. Every card calls "
        "the real aspy21 library against a respx-mocked HTTP backend, so no "
        "AspenTech server or credential is involved."
    ),
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class ExecuteRequest(BaseModel):
    """Parameters for one dashboard card execution."""

    operation: str = Field(..., description="Operation id, e.g. 'read_raw'.")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Form values for that operation."
    )


class Environment(BaseModel):
    """Versions, capabilities and the active backend.

    Never carries the password: only whether one is configured.
    """

    aspy21_version: str
    pandas_version: str
    httpx_version: str
    mode: str
    live: bool
    base_url: str
    datasource: str
    auth_configured: bool
    username: str
    password_source: str = Field(
        "none", description="Where the password came from: environment, credential store, or none."
    )
    config_file: str = Field("", description="Settings file in use, if any.")
    verify_ssl: bool
    timeout: float
    reader_types: list[str]
    include_fields: list[str]
    output_formats: list[str]
    has_cache_api: bool
    demo_tags: list[dict[str, Any]]


def environment() -> Environment:
    settings = SETTINGS.public_dict()
    # The demo catalogue describes the mock's tags. Advertising them against a
    # real historian would be inventing tags the server does not have.
    demo_tags = (
        []
        if SETTINGS.live
        else [
            {
                "name": spec.name,
                "description": spec.description,
                "unit": spec.unit,
                "nominal": spec.base,
            }
            for spec in TAGS.values()
        ]
    )
    return Environment(
        aspy21_version=getattr(aspy21, "__version__", "unknown"),
        pandas_version=pd.__version__,
        httpx_version=httpx.__version__,
        mode=settings["mode"],
        live=settings["live"],
        base_url=settings["base_url"],
        datasource=settings["datasource"],
        auth_configured=settings["auth_configured"],
        username=settings["username"],
        password_source=settings["password_source"],
        config_file=settings["config_file"],
        verify_ssl=settings["verify_ssl"],
        timeout=settings["timeout"],
        reader_types=AVAILABLE_READER_TYPES,
        include_fields=AVAILABLE_INCLUDE_FIELDS,
        output_formats=AVAILABLE_OUTPUT_FORMATS,
        has_cache_api=HAS_CACHE_API,
        demo_tags=demo_tags,
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request) -> HTMLResponse:
    """Render the operation dashboard."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"catalog": build_catalog(), "env": environment().model_dump()},
    )


@app.get("/api/environment", response_model=Environment, tags=["metadata"])
def api_environment() -> Environment:
    """Installed versions, enum members and the demo tag catalogue."""
    return environment()


@app.get("/api/catalog", tags=["metadata"])
def api_catalog() -> list[dict[str, Any]]:
    """The operation cards, including their form fields and defaults."""
    return build_catalog()


@app.post("/api/execute", tags=["operations"])
def api_execute(payload: ExecuteRequest) -> dict[str, Any]:
    """Execute one aspy21 operation against the mocked backend.

    Returns the library's result plus the HTTP calls aspy21 actually made,
    the equivalent Python snippet, and timing.
    """
    executor = EXECUTORS.get(payload.operation)
    if executor is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown operation {payload.operation!r}. Known: {sorted(EXECUTORS)}",
        )

    # Some cards need a server that fails on command. Running them live would
    # either do nothing interesting or fake a failure that never happened.
    if SETTINGS.live and payload.operation in MOCK_ONLY_OPERATIONS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Operation {payload.operation!r} needs the mock backend: it works by "
                "forcing the server to return an error status, which is not something "
                "to ask of a real historian. Restart with ASPY21_MODE=mock to run it."
            ),
        )

    params = dict(payload.params)

    # An explicitly empty tag list is a user mistake, not a reason to quietly
    # substitute defaults -- the card would then report data for tags the user
    # did not choose.
    if "tags" in params and isinstance(params["tags"], list) and not params["tags"]:
        raise HTTPException(
            status_code=400,
            detail="Select at least one tag before executing. "
            "Use the tag browser above to discover tags from IP.21.",
        )

    # Only the error-handling card puts the mock into failure mode.
    fail_status: int | None = None
    if payload.operation == "error_handling":
        try:
            fail_status = int(params.get("fail_status") or 500)
        except (TypeError, ValueError):
            fail_status = 500

    started = time.perf_counter()
    with open_backend(fail_status=fail_status) as backend:
        try:
            result = executor(params, backend)
            ok = True
            failure: dict[str, Any] | None = None
        except Exception as exc:  # unexpected: a bug in the demo, not a demo of a bug
            logger.exception("operation %s failed", payload.operation)
            result = {
                "result_kind": "error",
                "records": None,
                "table": None,
                "row_count": 0,
                "code": None,
                "notes": [],
            }
            ok = False
            failure = {
                "type": f"{type(exc).__module__}.{type(exc).__name__}",
                "message": str(exc)[:800],
                "traceback": traceback.format_exc(limit=8),
            }
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        http_calls = backend.http_calls

    response: dict[str, Any] = {
        "ok": ok,
        "operation": payload.operation,
        "elapsed_ms": elapsed_ms,
        "http_calls": http_calls,
        "http_call_count": len(http_calls),
        "params": params,
        **result,
    }
    if failure is not None:
        response["unexpected_error"] = failure
    return response


class DiscoveredTag(BaseModel):
    """One tag returned by IP.21 tag discovery."""

    name: str
    description: str = ""


class TagDiscovery(BaseModel):
    """Result of a real ``AspenClient.search()`` tag-browse call."""

    tags: list[DiscoveredTag]
    count: int
    pattern: str
    description_filter: str | None
    endpoint: str = Field(
        ..., description="Which IP.21 endpoint aspy21 chose: Browse or SQL search."
    )
    elapsed_ms: float
    code: str
    http_calls: list[dict[str, Any]]


@app.get("/api/tags", response_model=TagDiscovery, tags=["tags"])
def api_tags(
    pattern: str = "*",
    description: str | None = None,
    limit: int = 500,
) -> TagDiscovery:
    """Discover available tags from IP.21.

    This is the endpoint the dashboard's tag picker uses. It performs a real
    ``AspenClient.search(...)`` call -- the same one you would make against a
    live historian -- so aspy21 chooses the endpoint itself:

    * no ``description`` -> ``GET {base}/Browse?dataSource=..&tag=..``
    * with ``description`` -> ``POST {base}/SQL`` with ``g="aspy21_search"``

    In mock mode only the HTTP response is faked; in live mode this really
    browses your historian. Ask for ``include=DESCRIPTION`` so the picker can
    show what each tag means.
    """
    started = time.perf_counter()
    with open_backend() as backend:
        client = backend.client()
        try:
            found = client.search(
                tag=pattern or "*",
                description=description or None,
                limit=limit,
                include=aspy21.IncludeFields.DESCRIPTION,
            )
        except Exception as exc:
            logger.exception("tag discovery failed")
            hint = ""
            if SETTINGS.live:
                hint = (
                    f" Live mode is pointed at {SETTINGS.base_url}"
                    f" (datasource={SETTINGS.datasource or '<unset>'},"
                    f" auth={'basic' if SETTINGS.auth_configured else 'none'})."
                    " Check the base URL, the datasource name and your credentials."
                )
            raise HTTPException(
                status_code=502,
                detail=f"Tag discovery failed: {type(exc).__name__}: {exc}.{hint}",
            ) from exc
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        http_calls = backend.http_calls

    # include=DESCRIPTION yields dicts; other IncludeFields yield bare strings.
    tags: list[DiscoveredTag] = []
    for entry in found or []:
        if isinstance(entry, dict):
            tags.append(
                DiscoveredTag(
                    name=str(entry.get("name", "")),
                    description=str(entry.get("description") or ""),
                )
            )
        else:
            tags.append(DiscoveredTag(name=str(entry)))

    endpoint = http_calls[0]["kind"] if http_calls else "unknown"
    auth_line = ""
    header = "from aspy21 import AspenClient, IncludeFields\n\n"
    if SETTINGS.live and SETTINGS.auth_configured:
        header = "import os\n\nfrom aspy21 import AspenClient, IncludeFields\n\n"
        auth_line = '    auth=(os.environ["ASPY21_USERNAME"], os.environ["ASPY21_PASSWORD"]),\n'
    code = (
        f"{header}"
        f'with AspenClient(\n    base_url="{SETTINGS.base_url}",\n'
        f'    datasource="{SETTINGS.datasource}",\n{auth_line}) as client:\n'
        f"    tags = client.search(\n        tag={(pattern or '*')!r},\n"
        f"        description={description!r},\n        limit={limit!r},\n"
        "        include=IncludeFields.DESCRIPTION,\n    )"
    )

    return TagDiscovery(
        tags=tags,
        count=len(tags),
        pattern=pattern or "*",
        description_filter=description or None,
        endpoint=endpoint,
        elapsed_ms=elapsed,
        code=code,
        http_calls=http_calls,
    )


@app.get("/api/health", tags=["metadata"])
def api_health() -> dict[str, Any]:
    """Confirm the configured backend answers and aspy21 can parse it.

    Mock mode reads two demo tags. Live mode issues the cheapest real call
    there is -- a one-result tag browse -- so a green health check means the
    URL, credentials and datasource are all actually working.

    Always HTTP 200: a failure is reported in the body so the dashboard can
    show why, rather than surfacing as an opaque error page.
    """
    base = {
        "aspy21_version": getattr(aspy21, "__version__", "unknown"),
        "mode": SETTINGS.mode,
        "base_url": SETTINGS.base_url,
        "datasource": SETTINGS.datasource,
        "auth_configured": SETTINGS.auth_configured,
    }

    if not SETTINGS.live:
        with open_backend() as backend:
            client = backend.client()
            snapshot = client.read(list(TAGS)[:2])
        return {
            **base,
            "status": "ok",
            "mock_records": len(snapshot) if isinstance(snapshot, list) else 0,
            "real_network_calls": 0,
        }

    started = time.perf_counter()
    try:
        with open_backend() as backend:
            client = backend.client()
            found = client.search(tag="*", limit=1)
    except Exception as exc:
        logger.exception("live health check failed")
        return {
            **base,
            "status": "error",
            "error": f"{type(exc).__module__}.{type(exc).__name__}: {str(exc)[:400]}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    return {
        **base,
        "status": "ok",
        "tags_visible": len(found or []),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
