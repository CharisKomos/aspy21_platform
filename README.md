# aspy21 platform — user manual

[![CI](https://github.com/CharisKomos/aspy21_platform/actions/workflows/ci.yml/badge.svg)](https://github.com/CharisKomos/aspy21_platform/actions/workflows/ci.yml)

An interactive dashboard for [`aspy21`](https://github.com/bazdalaz/aspy21), the
Python client for AspenTech InfoPlus.21's ProcessData REST API. Every operation
the library offers is a card you can fill in and run, and every run shows you
the data, the HTTP the library generated, and the Python you would write to do
the same thing yourself.

> **Nothing here touches a real historian.** `respx` intercepts every outbound
> HTTP call inside the process. `http://mock.ip21.local/ProcessData` does not
> resolve to anything, and no credentials are needed or accepted.

Use it to learn the library, to see exactly what it puts on the wire before you
point it at a production server, or as a reference implementation to copy from.

**See also:** [ENDPOINTS.md](ENDPOINTS.md) — a full reference for both this
platform's REST API and the IP.21 endpoints `aspy21` calls underneath.

---

## Contents

1. [Install and run](#install-and-run)
2. [Your first minute](#your-first-minute)
3. [The dashboard, top to bottom](#the-dashboard-top-to-bottom)
4. [Choosing tags](#choosing-tags)
5. [What each card does](#what-each-card-does)
6. [Reading the results](#reading-the-results)
7. [Using the API directly](#using-the-api-directly)
8. [Troubleshooting](#troubleshooting)
9. [Testing and development](#testing-and-development)
10. [How the mocking works](#how-the-mocking-works)
11. [Version caveats](#version-caveats)
12. [Reference](#reference)

---

## Install and run

Requires **Python 3.10 or newer**.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

Open <http://127.0.0.1:8000>.

On macOS or Linux the interpreter is at `.venv/bin/python` instead.

> **Use the interpreter path, not a bare `uvicorn`.** The dependencies live
> inside the virtualenv and are not on your `PATH`. A bare `uvicorn main:app`
> fails with *"uvicorn is not recognized"*; a bare `python -m uvicorn` picks up
> your system Python and fails with *"No module named uvicorn"*.
>
> If you prefer short commands, activate the environment first
> (`.venv\Scripts\Activate.ps1`, or `source .venv/bin/activate`), after which
> plain `uvicorn main:app --reload` works for that shell.

Run it from the project directory — `main:app` is resolved relative to your
working directory.

### Installing aspy21 from piwheels

The wheels are pure Python (`py3-none-any`), so the
[piwheels](https://www.piwheels.org/project/aspy21/) copies install anywhere,
not just on a Raspberry Pi:

```bash
pip install --index-url https://www.piwheels.org/simple --extra-index-url https://pypi.org/simple aspy21==0.2.0b15
```

Keep PyPI as the `--extra-index-url`: piwheels' own `pandas` and `numpy` builds
are ARM-only, and the fallback lets pip choose platform-correct wheels.

> **`0.2.0b15` is a pre-release.** A bare `pip install aspy21` silently gives
> you the far older stable `0.0.4`. You need the exact `==` pin — as
> `requirements.txt` has — or `--pre`.

---

## Your first minute

1. Start the server and open <http://127.0.0.1:8000>.
2. Card 1, **Read — RAW**, is already open with sensible defaults. Press
   **Execute**.
3. You get process data back, plus the exact SQL `aspy21` generated.

Now try the three things that show it is genuinely running the library:

- **Card 2**, set *Interval* to `60` and Execute. Expand *Intercepted HTTP* and
  find `period=600` in the request body — `aspy21` converted seconds to tenths
  of a second. Change the interval to `30` and it becomes `period=300`. Clear
  the field and the clause disappears entirely.
- **Card 9**, leave *ReaderType* on `AVG` and Execute. It takes about 1.5
  seconds, makes **3** HTTP attempts, and raises `tenacity.RetryError`. Switch
  to `RAW` and Execute again: instant, **1** attempt,
  `httpx.HTTPStatusError`. That difference is real library behaviour.
- **Card 3**, press Execute twice. The values move, because the mock adds
  jitter.

---

## The dashboard, top to bottom

| Section | What it is |
| --- | --- |
| **Header** | Installed `aspy21`, `pandas` and `httpx` versions, plus a link to the generated Swagger docs |
| **Simulated-data banner** | The mock's base URL and datasource |
| **Tag browser** | Discover tags from IP.21 and choose which ones the cards use |
| **Cards 1–9** | One per operation. Click a title to expand or collapse |
| **Footer** | Attribution |

Under the tag browser, a fine-print line reports the `ReaderType` and
`IncludeFields` members **your installed version actually has**, and whether a
cache API is present. These are probed at runtime, so they stay honest if you
change versions.

---

## Choosing tags

You never have to hardcode tag names. The **Tag browser** discovers them the
same way you would against a live historian.

### Discovering

1. Enter a **tag pattern**. `*` matches any run of characters, `?` matches
   exactly one. Try `*`, `FLOW_*`, `*_205`, `ATI11?`.
2. Optionally enter a **description filter** — free text matched against tag
   descriptions, for example `temperature`.
3. Press **Browse tags**.

The results panel reports how many tags matched, **which IP.21 endpoint was
used**, the intercepted HTTP call, and the equivalent Python. The endpoint is
not your choice — `aspy21` decides:

| What you entered | Endpoint `aspy21` used |
| --- | --- |
| Pattern only | `GET /Browse` |
| Anything in the description filter | `POST /SQL` with `g="aspy21_search"` |

Discovery results **accumulate**, so you can browse `FLOW_*`, then `PUMP_*`,
and end up able to pick from both.

### Selecting

Tick the tags you want in the results table, or press **Select all**. Then
either:

- **Apply selection to all cards** — pushes your selection to every card at
  once, or
- **From browser** on an individual card — applies it to just that card.

### The per-card picker

Every card's *Tags* field is a multi-select chip list:

| Control | Effect |
| --- | --- |
| Click a chip | Toggle that tag |
| **Shift-click** a chip | Select or deselect the whole range from your last click |
| **All** / **None** | Everything or nothing, on this card |
| **From browser** | Copy the tag browser's current selection |
| Text box + **Add** | Type names manually, comma-separated, for tags discovery did not return |

A counter shows `n of m selected`. If you select nothing, **Execute** is
disabled-looking and refuses to run — deliberately, so a card can never quietly
fall back to defaults and report data for tags you did not pick.

---

## What each card does

### 1. Read — RAW

Raw stored points over a time range, exactly as the historian holds them, so
timestamps are **unevenly spaced**.

*Set:* tags, start, end, `IncludeFields`, `OutputFormat`.
*Look for:* irregular gaps between timestamps; `request=4` in the SQL.

### 2. Read — INT (interpolated)

Values on a fixed interval grid.

*Set:* tags, start, end, **interval in seconds**, `IncludeFields`,
`OutputFormat`.
*Look for:* `period=600` in the request body when interval is `60` — the
tenths-of-a-second conversion. Leaving interval blank omits the clause
altogether.

### 3. Read — SNAPSHOT

The current value of each tag. **No time range needed.**

*Set:* tags, `IncludeFields`, `OutputFormat`.
*Look for:* a single row per tag; the timestamp is generated client-side, so it
changes each run. `aspy21` also switches to SNAPSHOT automatically whenever you
omit both start and end, whatever `read_type` you asked for.

### 4. Read — AVG (aggregate)

Averaged buckets. **`AVG` is the only aggregate this release has** — there is
no `MIN`, `MAX` or `RNG`.

*Set:* tags, start, end, interval (optional), `OutputFormat`.
*Look for:* this one goes to a **different endpoint** — a POST to the bare base
URL with `<RT>10</RT>`, not `/SQL`. With an interval you get `<P>600</P><PU>3</PU>`
(`PU=3` means seconds). Select several tags and watch the HTTP count: this
reader loops, so N tags means N requests.

### 5. Search (wildcards)

Find tags by name pattern, optionally filtered by description.

*Set:* tag pattern, description filter, `IncludeFields`, limit, case
sensitivity.
*Look for:* `IncludeFields.NONE` or `STATUS` returns a plain list of **names**;
`DESCRIPTION` or `ALL` returns **objects** with name and description. Adding a
description filter switches the endpoint from `Browse` to SQL search.

### 6. Search — hybrid (search + read)

Resolve a pattern and read the matching tags in one call.

*Set:* pattern, description, start, end, interval, `ReaderType`,
`IncludeFields`, `OutputFormat`.
*Look for:* **two** HTTP calls — one to discover tags, one to read them. Adding
`start` is what flips `search()` from search-only into hybrid mode.

### 7. IncludeFields variants

Runs the same read under several `IncludeFields` values and diffs the results.

*Set:* tags, start, end, which variants to run, `OutputFormat`.
*Look for:* the field-name pills. Green ones are fields that variant adds over
`NONE`. `STATUS` adds `status`, `DESCRIPTION` adds `description`, `ALL` adds
both. In DataFrame output they arrive as `<tag>_status` and `<tag>_description`
columns.

### 8. Repeat-call timing (cache probe)

Runs one identical read twice and times both.

*Set:* tags, start, end.
*Look for:* **2 HTTP calls for 2 reads** — proof there is no caching. The card
also probes `AspenClient` live and reports that `get_cache_stats`,
`clear_cache` and `invalidate_cache` are all absent, and that `__init__` takes
no `cache=` keyword. The timing difference is warm-up noise, not a cache hit,
which is why it is tiny and can go either way. See
[Version caveats](#version-caveats).

### 9. Error handling and retry

Forces the mock to fail and shows you what surfaces.

*Set:* tags, start, end, `ReaderType`, and the HTTP status to force.
*Look for:* the contrast.

| `ReaderType` | Attempts | Exception |
| --- | --- | --- |
| `AVG` | **3**, with visible backoff | `tenacity.RetryError` wrapping the last `httpx.HTTPStatusError` |
| `RAW`, `INT`, `SNAPSHOT` | **1** | `httpx.HTTPStatusError` |

Only the XML endpoint used by `AVG` carries `aspy21`'s tenacity retry
decorator. Everything else fails on the first attempt.

---

## Reading the results

Every execution returns the same five blocks.

**Metrics** — total time, HTTP call count, row count, result kind, and either
an `ok` badge or an exception badge. The HTTP call count is the most
informative number on the page: it reveals batching (many tags, one call),
looping (many tags, many calls) and retries (one tag, three calls).

**Result** — JSON for `OutputFormat.JSON`, or a rendered table for
`DATAFRAME`. DataFrame tables also show the index name and any
`df.attrs["tag_descriptions"]` metadata the library attached.

**What happened** — plain-English notes explaining which endpoint was chosen,
which parameters changed the generated query, and any behaviour worth knowing.

**Intercepted HTTP** — every request `aspy21` made, with method, URL, a kind
label, status, and **the genuine request body**. This is unedited library
output, which is what makes the dashboard useful as a reference: what you see
here is what a real server would receive.

**Equivalent aspy21 code** — a standalone snippet reproducing the call outside
the dashboard. Copy it into your own script and only `base_url` and `auth` need
to change.

---

## Using the API directly

Everything the dashboard does is available over HTTP, documented interactively
at <http://127.0.0.1:8000/docs>.

```bash
curl "http://127.0.0.1:8000/api/tags?pattern=FLOW_*"
```

```bash
curl -X POST http://127.0.0.1:8000/api/execute -H "Content-Type: application/json" -d "{\"operation\":\"read_snapshot\",\"params\":{\"tags\":[\"REACTOR_TEMP\"],\"include\":\"ALL\"}}"
```

Full parameter and response documentation: **[ENDPOINTS.md](ENDPOINTS.md)**.

---

## Troubleshooting

**"uvicorn is not recognized" / "No module named uvicorn"**
You are not using the virtualenv's interpreter. Use
`.venv\Scripts\python.exe -m uvicorn …`, or activate the environment first.

**The page will not load / connection refused**
Check something is listening:

```bash
curl http://127.0.0.1:8000/api/health
```

**Edits are not taking effect**
Look for more than one server process holding port 8000. A stale worker will
keep serving old code even after you restart. On Windows:

```bash
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

Stop everything on that port and start once, with `--reload`.

**A card returns no rows**
Check the time range is in the past and start precedes end. The mock generates
data for whatever window you ask for, so an inverted or zero-length range
yields nothing.

**"Select at least one tag before executing"**
Working as intended — the card has no tags selected. Use **All** on the card,
or pick some in the tag browser.

**Card 9 takes about 1.5 seconds**
Also intended. That is `aspy21`'s exponential backoff between three real retry
attempts.

---

## Testing and development

```bash
.venv\Scripts\python.exe selftest.py
```

Drives every card through the ASGI app in-process — no server needed — and
asserts behaviour rather than status codes: endpoint selection, wildcard
semantics, the interval-to-period conversion, tag batching, retry counts,
per-variant field sets, value jitter, and the empty-selection guard. Exits
non-zero on failure.

```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```bash
.venv\Scripts\python.exe -m ruff check .
```

```bash
.venv\Scripts\python.exe -m ruff format .
```

Tool configuration is in `pyproject.toml`, which also carries project metadata,
so `pip install -e .` works if you prefer it to `requirements.txt`.
`requires-python` is `>=3.10`, one minor above `aspy21`'s own 3.9 floor,
because the Pydantic models in `main.py` use PEP 604 unions (`str | None`) that
Pydantic resolves at runtime.

CI runs `ruff check`, `ruff format --check`, and `selftest.py` on Python 3.10,
3.11 and 3.12 for every push and pull request — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## How the mocking works

`AspenClient` accepts an `http_client=` argument, so `mock_backend.py` hands it
an `httpx.Client` whose transport is a `respx` router:

```python
router = respx.Router(assert_all_called=False)
router.post(url=f"{BASE_URL}/SQL").mock(side_effect=self._handle_sql)
router.get(url__startswith=f"{BASE_URL}/Browse").mock(side_effect=self._handle_browse)
router.post(url=BASE_URL).mock(side_effect=self._handle_xml)

transport = respx.transports.MockTransport(router=router)
client = AspenClient(
    base_url=BASE_URL,
    datasource="IP21_DEMO",
    http_client=httpx.Client(transport=transport),
)
```

Every real `aspy21` code path therefore runs end to end — query building, HTTP,
response parsing, DataFrame assembly, tenacity retry — with only the wire
response faked. Injecting a transport also avoids `respx`'s process-wide global
patching, which keeps the server thread-safe, and a fresh router per execution
keeps each card's request log isolated.

The mock **parses the queries `aspy21` actually generated** — tag names, time
range, `period=`, `<P>`/`<PU>`, wildcard patterns — and answers accordingly. The
request bodies in the UI are genuine library output, not canned strings.

`mock_backend.py` is the single seam. Everything else drives the unmodified
library. For the full endpoint and payload reference, see
[ENDPOINTS.md](ENDPOINTS.md).

---

## Version caveats

This dashboard reports what **your installed version** supports, probed at
runtime. Against `aspy21 0.2.0b15`, two things differ from what the project's
GitHub `main` branch documents:

**There is no cache layer.** No `cache.py`, no `cache=` constructor keyword, and
no `get_cache_stats` / `clear_cache` / `invalidate_cache`. Card 8 therefore
measures and reports the real situation instead of displaying invented cache
statistics.

**`ReaderType` has only `RAW`, `INT`, `SNAPSHOT`, `AVG`.** No `MIN`, `MAX` or
`RNG`. Card 4 covers `AVG`, the only aggregate.

Both features exist on the project's git `main` branch but not in the published
wheel. If you move to a version that has them, cards 4 and 8 are where to look.

---

## Reference

### Project layout

```
main.py               FastAPI app and routes
operations.py         the 9 card definitions and their executors
mock_backend.py       the ONLY module that fakes anything
selftest.py           end-to-end self-test
templates/index.html  server-rendered dashboard
static/               style.css, app.js
ENDPOINTS.md          endpoint reference
```

### Demo tags

Seventeen simulated tags across a small plant: reactor (`REACTOR_TEMP`,
`REACTOR_PRESSURE`, `REACTOR_LEVEL`, `JACKET_TEMP`), flows (`FLOW_101`,
`FLOW_102`, `FLOW_310`), levels (`LEVEL_205`, `LEVEL_206`), rotating equipment
(`PUMP_101_SPEED`, `PUMP_101_CURRENT`), utilities (`COOLING_WATER_TEMP`,
`STEAM_HEADER_PRESS`), analysers and final control (`PH_301`, `VALVE_401_POS`),
and ambient indicators (`ATI111`, `ATI112`).

Values use Gaussian jitter plus a slow random walk, clamped to each
instrument's range, so repeated executions differ. Quality codes are mostly `0`
(Good) with occasional `1`/`2` so `IncludeFields.STATUS` has something to show.
Unknown tag names still return simulated data, so a typo never produces an
empty panel.

### License

MIT — see [LICENSE](LICENSE). `aspy21` itself is also MIT licensed.

`aspy21` is an independent, unofficial client and is not affiliated with
AspenTech. This project contains no real process data.
