"""FastAPI dashboard showcasing every read/search operation in aspy21.

Run it:
    uvicorn main:app --reload

Then open http://127.0.0.1:8000 for the dashboard, or
http://127.0.0.1:8000/docs for the generated OpenAPI docs.

All IP.21 traffic is intercepted by ``mock_backend`` -- no server, no
credentials, no network access.
"""

from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

import aspy21
from mock_backend import BASE_URL, DATASOURCE, TAGS, MockBackend
from operations import (
    AVAILABLE_INCLUDE_FIELDS,
    AVAILABLE_OUTPUT_FORMATS,
    AVAILABLE_READER_TYPES,
    EXECUTORS,
    HAS_CACHE_API,
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
    """Versions and capabilities of the installed stack."""

    aspy21_version: str
    pandas_version: str
    httpx_version: str
    base_url: str
    datasource: str
    reader_types: list[str]
    include_fields: list[str]
    output_formats: list[str]
    has_cache_api: bool
    demo_tags: list[dict[str, Any]]


def environment() -> Environment:
    return Environment(
        aspy21_version=getattr(aspy21, "__version__", "unknown"),
        pandas_version=pd.__version__,
        httpx_version=httpx.__version__,
        base_url=BASE_URL,
        datasource=DATASOURCE,
        reader_types=AVAILABLE_READER_TYPES,
        include_fields=AVAILABLE_INCLUDE_FIELDS,
        output_formats=AVAILABLE_OUTPUT_FORMATS,
        has_cache_api=HAS_CACHE_API,
        demo_tags=[
            {
                "name": spec.name,
                "description": spec.description,
                "unit": spec.unit,
                "nominal": spec.base,
            }
            for spec in TAGS.values()
        ],
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
            detail=f"Unknown operation {payload.operation!r}. "
            f"Known: {sorted(EXECUTORS)}",
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
    with MockBackend(fail_status=fail_status) as backend:
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

    Only the HTTP response is mocked. Ask for ``include=DESCRIPTION`` so the
    picker can show what each tag means.
    """
    started = time.perf_counter()
    with MockBackend() as backend:
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
            raise HTTPException(
                status_code=502,
                detail=f"Tag discovery failed: {type(exc).__name__}: {exc}",
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
    code = (
        "from aspy21 import AspenClient, IncludeFields\n\n"
        f'with AspenClient(\n    base_url="{BASE_URL}",\n'
        f'    datasource="{DATASOURCE}",\n) as client:\n'
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
    """Confirm the mocked backend answers and aspy21 can parse it."""
    with MockBackend() as backend:
        client = backend.client()
        snapshot = client.read(list(TAGS)[:2])
    return {
        "status": "ok",
        "aspy21_version": getattr(aspy21, "__version__", "unknown"),
        "mock_records": len(snapshot) if isinstance(snapshot, list) else 0,
        "real_network_calls": 0,
    }
