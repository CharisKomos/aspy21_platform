# aspy21 operation showcase

A local, runnable dashboard that exercises every read and search operation in
[`aspy21`](https://github.com/bazdalaz/aspy21) — a Python client for AspenTech
InfoPlus.21's ProcessData REST API — against a **fully mocked HTTP backend**.

Each operation is a Swagger-style card with editable parameters and an
**Execute** button. Clicking it calls the real `aspy21.AspenClient` and shows
you the result, the HTTP requests the library actually generated, and the
equivalent Python snippet.

> **No IP.21 server, no credentials, no network access.** `respx` intercepts
> every outbound HTTP call in-process. `http://mock.ip21.local/ProcessData`
> does not resolve to anything.

---

## Setup

Create a virtualenv and install into it:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run it:

```bash
.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

> **Use the interpreter path, not a bare `uvicorn`.** `uvicorn` and the other
> dependencies live inside `.venv\Scripts\` and are not on your PATH, so a bare
> `uvicorn main:app` fails with *"uvicorn is not recognized"*, and a bare
> `python -m uvicorn` picks up the global interpreter and fails with
> *"No module named uvicorn"*.
>
> Alternatively activate the venv first (`.venv\Scripts\Activate.ps1` in
> PowerShell, `.venv\Scripts\activate.bat` in cmd), after which plain
> `uvicorn main:app --reload` works for that shell session.

Run it from the project directory — `main:app` is resolved relative to the
current working directory.

| URL | What it is |
| --- | --- |
| <http://127.0.0.1:8000> | The operation dashboard |
| <http://127.0.0.1:8000/docs> | FastAPI's generated Swagger UI |
| <http://127.0.0.1:8000/api/environment> | Installed versions + enum members |
| <http://127.0.0.1:8000/api/health> | Confirms aspy21 can parse the mock |

### Installing aspy21 from piwheels

`aspy21` wheels are pure Python (`py3-none-any`), so the
[piwheels](https://www.piwheels.org/project/aspy21/) copies install fine on any
platform, not just Raspberry Pi:

```bash
pip install --index-url https://www.piwheels.org/simple --extra-index-url https://pypi.org/simple aspy21==0.2.0b15
```

Keep PyPI as the `--extra-index-url`. piwheels' own `pandas` and `numpy` builds
are ARM-only; the fallback lets pip pick platform-correct wheels for those.

**`0.2.0b15` is a pre-release.** A bare `pip install aspy21` resolves to the
much older stable `0.0.4` — you need the exact `==` pin (as in
`requirements.txt`) or `--pre`.

### Development

```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Adds `ruff` (lint + format) and `pytest` on top of the runtime dependencies.
Tool configuration lives in `pyproject.toml`.

```bash
.venv\Scripts\python.exe -m ruff check .
```

`pyproject.toml` also carries the project metadata, so `pip install -e .` works
if you prefer that to `requirements.txt`. Note `requires-python = ">=3.10"`,
one minor version above aspy21's own 3.9 floor, because the Pydantic models in
`main.py` use PEP 604 unions (`str | None`) that Pydantic resolves at runtime.

### Testing

```bash
.venv\Scripts\python.exe selftest.py
```

Drives every card through the ASGI app in-process — no server required — and
asserts behaviour rather than status codes: the `interval` → `period`
conversion, Browse-vs-SQL endpoint selection, wildcard matching, per-variant
field sets, retry counts, tag batching, and value jitter. Exits non-zero on
failure.

---

## Choosing which tags to use

You don't hardcode tag names. The **Tag browser** at the top of the dashboard
discovers them the way you would against a live historian, and whatever you
select there feeds every card below.

1. Enter a pattern (`*`, `FLOW_*`, `ATI11?`, …) and optionally a description
   filter, then press **Browse tags**.
2. Tick the tags you want — or **Select all**. Multi-select is the default:
   tick as many as you like.
3. Press **Apply selection to all cards**, or **From browser** on an individual
   card.

Each card's tag field is a multi-select chip list with **All** / **None**, and
**shift-click selects a contiguous range**. You can also type names
(comma-separated) to add tags the browser didn't return.

Discovery results accumulate, so you can browse `FLOW_*`, then `PUMP_*`, and
pick from the union.

### The discovery endpoint

```
GET /api/tags?pattern=FLOW_*&description=&limit=500
```

This is a **real `AspenClient.search()` call** — only the HTTP response is
mocked — so aspy21 picks the endpoint itself:

| Query | aspy21 calls |
| --- | --- |
| pattern only | `GET {base}/Browse?dataSource=…&tag=…` |
| with `description` | `POST {base}/SQL` with `g="aspy21_search"` |

The response includes the intercepted HTTP call and the equivalent Python, both
shown in the panel, so you can confirm which endpoint was used. Swapping the
mock for a real IP.21 host is a `base_url` change — the discovery code is
unchanged.

If no tags are selected, execution is refused with a clear message rather than
silently falling back to defaults (`HTTP 400`), so a card can never report data
for tags you didn't choose.

---

## What each card demonstrates

| # | Card | aspy21 call |
| --- | --- | --- |
| 1 | Read — RAW | `read(..., read_type=ReaderType.RAW)` |
| 2 | Read — INT | `read(..., read_type=ReaderType.INT, interval=…)` |
| 3 | Read — SNAPSHOT | `read(..., read_type=ReaderType.SNAPSHOT)`, no time range |
| 4 | Read — AVG | `read(..., read_type=ReaderType.AVG)`, with and without `interval` |
| 5 | Search | `search(tag="REACTOR*", description=…)` with every `IncludeFields` |
| 6 | Search — hybrid | `search(tag=…, start=…)` — search + read in one call |
| 7 | IncludeFields variants | the same read under NONE / STATUS / DESCRIPTION / ALL, diffed |
| 8 | Repeat-call timing | same read twice, timed, with a live cache-API probe |
| 9 | Error handling & retry | forced HTTP failure; shows retry count and exception type |

---

## Two places the brief did not match the library

Both were found by reading the installed source, and both are surfaced
honestly in the UI rather than papered over.

**1. There is no cache in `0.2.0b15`.** The brief asked for a card using
`cache=True` and `client.get_cache_stats()`. This release has no `cache.py`,
`AspenClient.__init__` takes no `cache=` keyword, and none of
`get_cache_stats` / `clear_cache` / `invalidate_cache` exist. Those APIs live on
the project's git `main` branch, not in the published wheel.

Rather than fake cache statistics, **card 8** probes `AspenClient` at runtime,
prints what it finds, runs the identical read twice, and proves both calls
reached HTTP (2 intercepted requests for 2 reads — a cache would have made it
1). The timing delta it reports is warm-up noise, which is why it is tiny and
can go either way.

**2. `ReaderType` has no `MIN`, `MAX` or `RNG`.** This release exposes exactly
`RAW`, `INT`, `SNAPSHOT`, `AVG`. **Card 4** covers `AVG` — the only aggregate —
and says so on the card.

If you need cache + MIN/MAX/RNG, that means a different version of aspy21, and
cards 4 and 8 would need revisiting.

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
    base_url=BASE_URL, datasource="IP21_DEMO", http_client=httpx.Client(transport=transport)
)
```

This means every real aspy21 code path runs end to end — query building, HTTP,
response parsing, DataFrame assembly, tenacity retry — with only the wire
response faked. It also avoids `respx`'s global patching, so the server stays
thread-safe, and a fresh router per execution keeps each card's request log
isolated.

The mock **parses the queries aspy21 really generated** (tag names, time range,
`period=`, `<P>`/`<PU>`, wildcard patterns) and answers accordingly, so the
request bodies shown in the UI are genuine library output.

### Endpoints aspy21 0.2.0b15 actually calls

| Operation | HTTP call |
| --- | --- |
| SNAPSHOT read | `POST {base}/SQL` — body `<SQL g="aspy21_snapshot">` |
| RAW / INT read | `POST {base}/SQL` — body `<SQL g="aspy21_history">` |
| `search(description=…)` | `POST {base}/SQL` — body `<SQL g="aspy21_search">` |
| `search(tag=…)` | `GET {base}/Browse?dataSource=…&tag=…` |
| AVG read | `POST {base}` — body `<Q f="d">`, the XML endpoint |

Two details worth knowing, both visible in the dashboard:

- The three SQL variants **share one URL** and differ only by the `g=`
  attribute in the XML body, so the mock dispatches on body content.
- **Only the XML endpoint retries.** `XmlHistoryReader._fetch` carries
  `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))`.
  Since `AVG` is the only read type routed there, an HTTP 500 on an AVG read
  makes 3 attempts and raises `tenacity.RetryError` (~1.5 s of real backoff),
  while the same failure on RAW/INT/SNAPSHOT raises `httpx.HTTPStatusError`
  after a single attempt. Card 9 lets you flip between the two.

### The `period` conversion

For RAW/INT (the SQL history path), `interval` seconds becomes
`period = interval * 10` in tenths of a second, inside the SQL `WHERE` clause.
Set `interval=60` on card 2 and look for `period=600` in the intercepted body.

The AVG path is different: it emits `<P>{interval}</P><PU>3</PU>`, where `PU=3`
means the period is in seconds. Omitting `interval` drops the `<P>` element
entirely, letting the server choose the bucket size.

---

## Project layout

```
main.py            FastAPI app, routes, templates
operations.py      the 9 card definitions + executors that call real aspy21
mock_backend.py    the ONLY module that fakes anything (respx + demo tags)
templates/
  index.html       server-rendered dashboard
static/
  style.css        Swagger-ish styling, light + dark
  app.js           collect params, POST /api/execute, render the response
requirements.txt
```

`mock_backend.py` is deliberately the single seam: everything else drives the
unmodified library.

## Demo tags

| Tag | Description | Unit | Nominal |
| --- | --- | --- | --- |
| `REACTOR_TEMP` | Reactor R-101 outlet temperature | degC | 252.0 |
| `REACTOR_PRESSURE` | Reactor R-101 head pressure | barg | 3.45 |
| `FLOW_101` | Feed flow to reactor R-101 | m3/h | 118.0 |
| `LEVEL_205` | Buffer tank T-205 level | % | 64.0 |
| `ATI111` | Ambient temperature indicator 111 | degC | 25.5 |

Values are generated with Gaussian jitter plus a slow random walk and clamped
to each instrument's range, so repeated executions return different data.
Quality codes are mostly `0` (Good) with the occasional `1`/`2` so that
`IncludeFields.STATUS` has something to show. Unknown tag names still return
simulated data, so typos and wildcards never produce an empty panel.

---

## License

MIT — see [LICENSE](LICENSE). `aspy21` itself is also MIT licensed.

aspy21 is an independent, unofficial client and is not affiliated with
AspenTech. This project contains no real process data.
