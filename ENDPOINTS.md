# Endpoint reference

Two layers of HTTP live in this project, and it helps to keep them apart:

| Layer | What it is | Who calls it |
| --- | --- | --- |
| **[Platform API](#part-1--platform-api)** | The dashboard's own REST API (FastAPI) | Your browser, `curl`, any HTTP client |
| **[IP.21 API](#part-2--ip21-endpoints-aspy21-calls)** | The AspenTech ProcessData endpoints | `aspy21` itself, intercepted by `respx` in mock mode |

A click in the dashboard travels down both: the browser calls the Platform API,
which calls `aspy21`, which calls the IP.21 API, which the mock answers — or,
in [live mode](README.md#live-mode-connecting-to-a-real-historian), which your
own historian answers over the network. The request shapes are identical either
way; only the last hop changes.

```
browser ──► POST /api/execute ──► aspy21.AspenClient.read()
                                        │
                                        ▼
                             POST http://mock.ip21.local/ProcessData/SQL
                                        │
                                        ▼
                             mock_backend.py (respx router)
```

Everything below was verified against the installed `aspy21 0.2.0b15` source,
not its documentation.

---

# Part 1 — Platform API

Base URL when running locally: `http://127.0.0.1:8000`.

All of these are also browsable, with live "Try it out" forms, at
<http://127.0.0.1:8000/docs>.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The dashboard HTML |
| `GET` | `/docs` | Generated Swagger UI |
| `GET` | `/openapi.json` | OpenAPI schema |
| `GET` | `/api/environment` | Installed versions, enum members, demo tags |
| `GET` | `/api/catalog` | Card definitions and their form fields |
| `GET` | `/api/tags` | **Tag discovery** — runs a real `client.search()` |
| `POST` | `/api/execute` | **Run one operation** — runs a real `client.read()` / `search()` |
| `POST` | `/api/v1/series` | **Ingest contract** — a windowed read, rows only, for machine callers |
| `GET` | `/api/health` | Liveness plus a round-trip through the mock |

## `GET /api/tags`

Discovers available tags. This is what the dashboard's tag browser calls.

**Query parameters**

| Name | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pattern` | string | `*` | Tag name pattern. `*` = any run of characters, `?` = exactly one |
| `description` | string | *(none)* | Optional description filter. **Changes which IP.21 endpoint is used** |
| `limit` | int | `500` | Maximum tags to return |

**Example**

```bash
curl "http://127.0.0.1:8000/api/tags?pattern=FLOW_*"
```

```json
{
  "tags": [
    { "name": "FLOW_101", "description": "Feed flow to reactor R-101" },
    { "name": "FLOW_102", "description": "Recycle flow to reactor R-101" },
    { "name": "FLOW_310", "description": "Product draw-off flow" }
  ],
  "count": 3,
  "pattern": "FLOW_*",
  "description_filter": null,
  "endpoint": "Browse",
  "elapsed_ms": 4.1,
  "code": "…equivalent Python…",
  "http_calls": [ { "method": "GET", "url": "…", "kind": "Browse", "status": 200 } ]
}
```

The `endpoint` field reports which IP.21 endpoint `aspy21` chose — `Browse` or
`aspy21_search`. You do not choose it; the library does, based on whether you
passed a `description`.

## `POST /api/execute`

Runs one operation card.

**Body**

```json
{ "operation": "read_int", "params": { "tags": ["REACTOR_TEMP"], "interval": 60 } }
```

`operation` is one of:

```
read_raw   read_int   read_snapshot   read_aggregate
search     search_hybrid              include_fields
repeat_timing                         error_handling
```

`params` mirrors the card's form fields — see `GET /api/catalog` for each
card's field names, types and defaults.

**Response** (fields vary by `result_kind`)

| Field | Meaning |
| --- | --- |
| `ok` | `false` only if the dashboard itself errored |
| `result_kind` | `records` \| `dataframe` \| `list` \| `comparison` \| `timing` \| `error` |
| `records` | JSON rows, when `result_kind` is `records` or `list` |
| `table` | `{columns, rows, index_name, descriptions}` for DataFrame output |
| `row_count` | Number of rows returned |
| `http_calls` | **Every IP.21 request `aspy21` made**, with real request bodies |
| `http_call_count` | How many — useful for spotting batching and retries |
| `elapsed_ms` | Server-side duration |
| `notes` | Plain-English explanation of what the library did |
| `code` | The equivalent standalone `aspy21` snippet |
| `error` | Present when `aspy21` raised: type, message, chain, underlying cause |
| `attempts` | Retry attempts observed (error-handling card) |

**Errors**

| Status | When |
| --- | --- |
| `400` | `tags` was supplied but empty — pick at least one tag |
| `404` | Unknown `operation` |
| `409` | The operation needs the mock backend and live mode is active |

An empty tag list is refused rather than silently defaulted, so a card can
never report data for tags you did not choose. Likewise `error_handling`, which
works by forcing the server to return an error status, is refused in live mode
rather than faked.

## `GET /api/environment`

Versions and capabilities, probed at runtime rather than hardcoded. Useful for
confirming what your install actually supports:

```json
{
  "aspy21_version": "0.2.0b15",
  "mode": "mock",
  "live": false,
  "base_url": "http://mock.ip21.local/ProcessData",
  "datasource": "IP21_DEMO",
  "auth_configured": false,
  "reader_types": ["RAW", "INT", "SNAPSHOT", "AVG"],
  "include_fields": ["NONE", "STATUS", "DESCRIPTION", "ALL"],
  "output_formats": ["JSON", "DATAFRAME"],
  "has_cache_api": false
}
```

`auth_configured` reports only *whether* credentials are set. The password is
never included in this or any other response.

## `GET /api/health`

Performs a real call through whichever backend is configured, so a green result
proves the whole chain works — not merely that the process is up. In mock mode
that is a snapshot read; in live mode it is a one-result tag browse, which also
confirms the URL, credentials and datasource are correct.

Always returns HTTP `200`; failures are reported in the body so the reason is
visible rather than hidden behind an error page.

```json
{ "status": "ok", "aspy21_version": "0.2.0b15", "mode": "mock", "mock_records": 2, "real_network_calls": 0 }
```

```json
{ "status": "error", "mode": "live", "base_url": "https://aspen.plant.local/ProcessData",
  "error": "httpx.HTTPStatusError: Client error '401 Unauthorized' ...", "elapsed_ms": 84.2 }
```

## `POST /api/v1/series`

The **ingest contract**: the same `client.read()` the cards make, with the
teaching material removed. `/api/execute` answers a person — notes, a code
snippet, every request body. This answers a poller, several times an hour, and
returns rows only.

**Request**

```json
{
  "tags": ["FLOW_101", "REACTOR_TEMP"],
  "from": "2026-08-08T00:00:00Z",
  "to": "2026-08-08T06:00:00Z",
  "interval_seconds": 60,
  "include_status": true,
  "max_rows": 200000
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `tags` | *(required)* | At least one. An empty list is a `422`, never a silent default |
| `from` | *(required)* | **Exclusive** lower bound — pass the newest timestamp you already hold |
| `to` | *(required)* | Inclusive upper bound |
| `interval_seconds` | `null` | Grid spacing for an interpolated (`INT`) read. Omit or `0` for raw (`RAW`) points |
| `include_status` | `true` | Ask for the per-reading quality code |
| `max_rows` | `200000` | Refuse rather than return more |

**The window is half-open, `(from, to]`.** A reading exactly on the lower bound
is one the caller already has, so excluding it makes a poll idempotent: a failed
pull is retried with the same cursor and no row arrives twice.

**Response**

```json
{
  "ok": true, "mode": "live", "live": true,
  "rows": [ { "tag": "FLOW_101", "timestamp": "2026-08-08T00:01:00Z", "value": 42.1, "status": 0 } ],
  "row_count": 1, "tags_requested": 2, "tags_returned": 1,
  "from": "2026-08-08T00:00:00Z", "to": "2026-08-08T06:00:00Z",
  "interval_seconds": 60, "read_type": "INT", "timezone": "Europe/Athens",
  "skipped_non_numeric": 0, "skipped_out_of_window": 0, "duplicates_dropped": 0,
  "elapsed_ms": 812.4, "http_call_count": 1
}
```

Rows are sorted by `(tag, timestamp)` and de-duplicated on that pair. Readings
that are missing, `NaN`, or unparseable are dropped and counted rather than
passed on as nulls — a chart cannot use them and a database column may refuse
them. `mode` is always reported, so simulated data can never be mistaken for
plant data.

### Timestamps

IP.21 answers in the historian machine's **local wall time with no zone
attached**. A consumer that stores UTC and takes those values verbatim is wrong
by a fixed offset, forever, in a way no chart reveals.

So the boundary is explicit both ways. `from`/`to` are read as aware instants (a
token with no zone is taken as UTC), converted into the historian's zone to
build the query, and every returned timestamp is converted back and rendered
with an explicit offset. Set **`ASPY21_TIMEZONE`** to the historian's zone —
`UTC` (default), `local`, or an IANA name like `Europe/Athens`. Live mode logs a
warning at startup while it is still `UTC`, because that is rarely right for a
real plant.

### Failures

Every failure carries a machine-readable `error_kind`, so a caller can tell a
lost connection from a wrong password without parsing prose:

```json
{ "detail": { "error_kind": "auth", "message": "The historian rejected the credentials (HTTP 401).",
              "mode": "live", "base_url": "…", "exception": "httpx.HTTPStatusError" } }
```

| Status | `error_kind` | Cause |
| --- | --- | --- |
| `400` | `bad_request` | No usable tags, or `from` is not before `to` |
| `422` | *(FastAPI validation)* | Malformed body — empty `tags`, unparseable datetime |
| `413` | `too_many_rows` | The result exceeds `max_rows`. Ask for a shorter window, a coarser interval, or fewer tags |
| `500` | `configuration` | `ASPY21_TIMEZONE` names a zone this machine cannot resolve |
| `502` | `connection` | Could not reach the historian |
| `502` | `timeout` | It did not answer in time |
| `502` | `auth` | Credentials rejected (401/403) |
| `502` | `not_found` | No ProcessData endpoint at that URL (404) |
| `502` | `upstream` | Any other HTTP status from the historian |
| `502` | `configuration` | aspy21 raised `ValueError` — typically a missing datasource |
| `502` | `unknown` | Anything else |

`tenacity.RetryError` is unwrapped before classification, so a retried failure
reports the reason it actually failed rather than the retry wrapper.

**One limit worth knowing:** `max_rows` caps the *response*, not memory. The
library materialises the full result before this endpoint can count it, so an
enormous window is still fetched once before being refused. Size the window on
the caller's side.

Works in mock mode, so an ingest client can be developed and tested end to end
with no historian, no credentials and no network.

---

# Part 2 — IP.21 endpoints aspy21 calls

`aspy21 0.2.0b15` issues HTTP from exactly **five call sites**, resolving to
**three distinct URLs**. There is no write path, no separate aggregates
endpoint, and no async client — the library is read-only.

Given `base_url = "https://aspen.example.com/ProcessData"`:

| # | URL | Method | Body |
| --- | --- | --- | --- |
| 1 | `{base_url}/SQL` | `POST` | `<SQL g="…">` XML, `Content-Type: text/xml` |
| 2 | `{base_url}/Browse` | `GET` | none — parameters in the query string |
| 3 | `{base_url}` *(bare)* | `POST` | `<Q f="d">` XML, `Content-Type: text/xml` |

### Which call your code triggers

| You write | HTTP | Distinguished by |
| --- | --- | --- |
| `read(..., read_type=SNAPSHOT)`, or no `start`/`end` | `POST /SQL` | `g="aspy21_snapshot"` |
| `read(..., read_type=RAW)` | `POST /SQL` | `g="aspy21_history"`, `request=4` |
| `read(..., read_type=INT)` | `POST /SQL` | `g="aspy21_history"`, `request=1` |
| `read(..., read_type=AVG)` | `POST {base_url}` | `<RT>10</RT>` |
| `search(tag=…)` | `GET /Browse` | — |
| `search(tag=…, description=…)` | `POST /SQL` | `g="aspy21_search"` |
| `search(tag=…, start=…)` | *both* | discovery, then the matching read |

Note that **three logically different queries share the `/SQL` URL** and are
told apart only by the `g=` attribute inside the XML body. Anything inspecting
this traffic — a proxy, a mock, a firewall rule — must read the body, not just
the path.

## 1. `POST {base_url}/SQL`

Wraps an Aspen SQLplus statement in XML. The wrapper is identical across the
three variants; only `g=` and the statement differ.

```xml
<SQL g="aspy21_history" t="SQLplus" ds="IP21"
     dso="CHARINT=N;CHARFLOAT=N;CHARTIME=N;CONVERTERRORS=N"
     m="100000" to="30" response="Record" s="1">
<![CDATA[Select ts, name, value from history(80)
         where name in ('REACTOR_TEMP','FLOW_101')
           and ts between '20-Jun-25 08:00:00' and '20-Jun-25 09:00:00'
           and request=1 and period=600]]>
</SQL>
```

| Variant | `g=` | Table | `response=` |
| --- | --- | --- | --- |
| History | `aspy21_history` | `history(80)` | `Record` |
| Snapshot | `aspy21_snapshot` | `all_records` | `Record` |
| Search | `aspy21_search` | `all_records` | `Original` |

**Timestamps** are formatted `DD-MMM-YY HH:MM:SS`.

**`request=`** selects the retrieval mode: `4` for RAW, `1` for INT.

**`period=`** appears only when you pass `interval`, and is **in tenths of a
second**: `interval=60` becomes `period=600`. This conversion is specific to
this SQL path.

**Tag batching:** multiple tags become one `name in (…)` clause, so N tags cost
**one** request.

**Response shapes**

History (`response="Record"`) — a flat JSON array:

```json
[
  { "ts": "2025-06-20T08:00:00", "name": "REACTOR_TEMP", "value": 251.4, "status": 0 }
]
```

Snapshot — one record per tag, with arrow-syntax field names:

```json
[
  { "name": "REACTOR_TEMP",
    "name->ip_input_value": 252.1,
    "name->ip_input_quality": 0,
    "name->ip_description": "Reactor R-101 outlet temperature" }
]
```

Search (`response="Original"`) — a column/row envelope, where `fld[0]` is the
tag name and `fld[1]` the description:

```json
{ "data": [ { "result": { "er": 0, "es": "" },
              "cols": [ { "i": 0, "n": "name" }, { "i": 1, "n": "d" } ],
              "rows": [ { "fld": [ { "i": 0, "v": "REACTOR_TEMP" },
                                   { "i": 1, "v": "Reactor R-101 outlet temperature" } ] } ] } ] }
```

An `er` value other than `0` in `result` raises `ValueError`.

## 2. `GET {base_url}/Browse`

Tag discovery by name pattern.

```
GET /ProcessData/Browse?dataSource=IP21&tag=FLOW_*&max=500&getTrendable=0
```

| Parameter | Meaning |
| --- | --- |
| `dataSource` | Datasource name — **required**, `search()` raises `ValueError` without one |
| `tag` | Pattern; `*` and `?` are passed through unescaped |
| `max` | Row limit |
| `getTrendable` | Always `0` |

**Response** — `t` is the tag name, `n` the description:

```json
{ "data": { "result": { "er": 0, "es": "" },
            "tags": [ { "t": "FLOW_101", "n": "Feed flow to reactor R-101" } ] } }
```

## 3. `POST {base_url}` — the XML endpoint

Used **only** for `AVG` reads, and the only path with retry.

```xml
<Q f="d" allQuotes="1"><Tag>
  <N><![CDATA[REACTOR_TEMP]]></N>
  <D><![CDATA[IP21]]></D>
  <F><![CDATA[VAL]]></F>
  <HF>0</HF>
  <St>1750406400000</St>
  <Et>1750410000000</Et>
  <RT>10</RT>
  <X>100000</X>
  <P>600</P><PU>3</PU>
</Tag></Q>
```

| Element | Meaning |
| --- | --- |
| `N` | Tag name — **one tag per request** |
| `F` | Field; `IP_DESCRIPTION` is added when descriptions are requested |
| `St` / `Et` | Start / end, **milliseconds since epoch** |
| `RT` | Reader type: `0` RAW, `1` INT, `2` SNAPSHOT, `10` AVG |
| `X` | Max rows |
| `P` / `PU` | Period and unit — `PU=3` means **seconds**, added only when `interval` is set and `RT >= 10` |

**No batching:** this reader loops over tags, so N tags cost **N** requests —
unlike the SQL path.

**Response** — `t` is epoch milliseconds, `v` the value, `s` the status:

```json
{ "data": [ { "l": ["VAL", "Reactor R-101 outlet temperature"],
              "samples": [ { "t": 1750406400000, "v": 251.4, "s": 0 } ] } ] }
```

A sample carrying an `er` key is skipped by the parser.

---

## Cross-cutting behaviour

### Retry is not uniform

`XmlHistoryReader._fetch` — and nothing else — is decorated with:

```python
@retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
```

Consequently, on a persistent HTTP 500:

| Read type | Endpoint | Attempts | Exception raised |
| --- | --- | --- | --- |
| `AVG` | `POST {base_url}` | **3** (~1.5 s of backoff) | `tenacity.RetryError`, wrapping the last `httpx.HTTPStatusError` |
| `RAW` / `INT` / `SNAPSHOT` | `POST /SQL` | **1** | `httpx.HTTPStatusError` |
| `search` | `/Browse` or `/SQL` | **1** | `httpx.HTTPStatusError` |

`aspy21` never swallows transport errors: it logs and re-raises, so callers
always receive the original `httpx` exception (directly, or via
`RetryError.last_attempt.exception()`).

Card 9 in the dashboard lets you flip between these paths and watch the
difference.

### Request cost summary

| Call | Requests |
| --- | --- |
| `read()` with RAW / INT / SNAPSHOT, any number of tags | 1 |
| `read()` with AVG over N tags | N |
| `search()` search-only | 1 |
| `search()` hybrid over N tags | 1 + (1 or N, per the rules above) |

### Authentication

Basic auth via `auth=("user", "password")`, or any `httpx.Auth` object, applied
to every request. In mock mode this demo passes no credentials — the mock needs
none.

Pointing the code at a real historian is a `base_url` and `auth` change and
nothing else; every request shape above is what your server would receive. The
dashboard does exactly that in
[live mode](README.md#live-mode-connecting-to-a-real-historian): set
`ASPY21_MODE=live`, `ASPY21_BASE_URL` and the credential variables, and
`live_backend.py` builds the same `AspenClient` over a real socket.

One implementation detail worth knowing if you copy that code: `AspenClient`
applies its own `auth=` only when it creates the HTTP client itself. If you
inject `http_client=`, put the auth on the `httpx.Client` instead — otherwise
it is silently dropped.

---

## Coverage in this project

Every endpoint, variant and response shape on this page is exercised by the
dashboard and asserted by `selftest.py`, and again over real sockets by
`selftest_live.py`. See the
[README](README.md#what-each-card-does) for the card-by-card guide.
