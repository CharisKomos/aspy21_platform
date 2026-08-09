# aspy21 platform — user manual

[![CI](https://github.com/CharisKomos/aspy21_platform/actions/workflows/ci.yml/badge.svg)](https://github.com/CharisKomos/aspy21_platform/actions/workflows/ci.yml)

A dashboard **and a gateway** for [`aspy21`](https://github.com/bazdalaz/aspy21),
the Python client for AspenTech InfoPlus.21's ProcessData REST API.

It does two jobs from the same code:

- **For a person** — every operation the library offers is a card you can fill in
  and run, and every run shows you the data, the HTTP the library generated, and
  the Python you would write to do the same thing yourself.
- **For a program** — [`POST /api/v1/series`](#the-ingest-api) is a narrow ingest
  contract: tags in, a time window, rows out. It is what lets a service in another
  language read IP.21 through a library that only exists in Python.

> **Out of the box, nothing here touches a real historian.** `respx` intercepts
> every outbound HTTP call inside the process. `http://mock.ip21.local/ProcessData`
> does not resolve to anything, and no credentials are needed or accepted. That
> makes it useful as a test double: a client can be built and tested end to end
> with no server, no credentials and no network.

Use it to learn the library, to see exactly what it puts on the wire before you
point it at a production server, as a reference implementation to copy from, or as
the historian gateway for your own application. When you are ready,
[live mode](#live-mode-connecting-to-a-real-historian) points all of it at your own
IP.21 server.

**See also:** [ENDPOINTS.md](ENDPOINTS.md) — a full reference for both this
platform's REST API and the IP.21 endpoints `aspy21` calls underneath.

---

## Contents

1. [Install and run](#install-and-run)
2. [Your first minute](#your-first-minute)
3. [The ingest API](#the-ingest-api)
4. [Live mode: connecting to a real historian](#live-mode-connecting-to-a-real-historian)
5. [Authentication](#authentication)
6. [Time zones](#time-zones)
7. [The dashboard, top to bottom](#the-dashboard-top-to-bottom)
8. [Choosing tags](#choosing-tags)
9. [What each card does](#what-each-card-does)
10. [Reading the results](#reading-the-results)
11. [Using the API directly](#using-the-api-directly)
12. [Troubleshooting](#troubleshooting)
13. [Testing and development](#testing-and-development)
14. [How the mocking works](#how-the-mocking-works)
15. [Version caveats](#version-caveats)
16. [Reference](#reference)

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

That gets you the offline demo, which needs no server and no credentials. To
point it at your own historian instead, run the setup wizard once:

```bash
.venv\Scripts\python.exe setup_live.py
```

See [live mode](#live-mode-connecting-to-a-real-historian) for what it asks and
where to run it.

### Or run it as a container

There is a `Dockerfile`, so the gateway can run as a service alongside whatever
consumes it. The image ships in mock mode, so this works with no historian:

```bash
docker build -t aspy21-gateway .
```

```bash
docker run --rm -p 8000:8000 aspy21-gateway
```

Point it at a real plant with environment variables rather than the wizard — a
container has no credential store to save into:

```bash
docker run --rm -p 8000:8000 -e ASPY21_MODE=live -e ASPY21_BASE_URL=https://aspen-hist01/ProcessData -e ASPY21_DATASOURCE=IP21 -e ASPY21_USERNAME=DOMAIN\\svc-user -e ASPY21_PASSWORD="$ASPY21_PASSWORD" -e ASPY21_TIMEZONE=Europe/Athens aspy21-gateway
```

The image runs unprivileged and carries a `HEALTHCHECK` that calls `/api/health`,
so it reports healthy only once the whole chain answers — not merely once the
process is up. Pass the password from your shell or a secret; never bake it into
an image.

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

## The ingest API

The cards answer a person. `POST /api/v1/series` answers a program, several times
an hour, and returns rows and nothing else — no notes, no generated snippet, no
request bodies.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/series -H "Content-Type: application/json" -d "{\"tags\":[\"FLOW_101\"],\"from\":\"2026-08-08T00:00:00Z\",\"to\":\"2026-08-08T06:00:00Z\",\"interval_seconds\":60}"
```

Four things about it are worth knowing before you build against it:

**The window is half-open, `(from, to]`.** Pass the newest timestamp you already
hold as `from`. A reading exactly on that bound is one you have, so excluding it
makes a poll idempotent — a failed pull can be retried with the same cursor and no
row arrives twice.

**Timestamps always carry an explicit offset**, and rows are sorted by
`(tag, timestamp)` and de-duplicated on that pair. Readings that are missing, `NaN`
or unparseable are dropped and counted rather than passed on as nulls.

**Failures carry a machine-readable `error_kind`** — `connection`, `timeout`,
`auth`, `tls`, `not_found`, `upstream`, `configuration`, `too_many_rows` — so a
caller can tell a dropped connection from a wrong password without parsing prose.
`tenacity.RetryError` is unwrapped first, so a retried failure reports the reason it
actually failed rather than the retry wrapper.

**Oversized results are refused, not truncated.** A silently short answer would
advance the caller's cursor past real data. Ask for a shorter window instead.

### More than one historian

`/api/v1/series` and `/api/v1/tags` accept an optional `connection` object. Without
one they use the settings this process started with; with one, that request reads
the historian described — so a single gateway can serve an application whose users
configure a connection per project, rather than needing a container per plant.

`POST /api/v1/connection/test` checks such a connection before anything is saved
against it, walking `settings → network → endpoint → credentials → datasource` and
stopping at the first failure, so the answer names the step rather than leaving
someone to work backwards from a 401.

Nothing about a supplied connection is stored here — it belongs to the caller,
password included. That also means credentials cross the network on every call, so
keep the gateway on a private network or behind TLS.

Full request/response reference, including the error table:
[ENDPOINTS.md](ENDPOINTS.md#post-apiv1series).

### Testing a client against it

Every failure path can be forced, so a client never meets one for the first time in
production. In **mock mode only**, add `simulate_failure` with the status you want
the historian to return:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/series -H "Content-Type: application/json" -d "{\"tags\":[\"FLOW_101\"],\"from\":\"2026-08-08T00:00:00Z\",\"to\":\"2026-08-08T01:00:00Z\",\"simulate_failure\":401}"
```

That returns `502` with `error_kind: "auth"`. It is refused with `409` in live mode,
for the same reason the error-handling card is: asking a production historian to
fail is not a thing to do, and faking it would prove nothing.

---

## Live mode: connecting to a real historian

In live mode the dashboard stops mocking: the same cards, the same executors,
the same `AspenClient` — but the transport is the network and the responses come
from your IP.21 server.

### The easy way: run the setup wizard

#### Why run it

Connecting to a real historian needs five things to line up: the REST base URL,
the datasource name, a username, a password, and a TLS decision. Getting any
one of them wrong produces the same unhelpful symptom — a dashboard that will
not load data — and there is nothing in the error to tell you which one it was.

The wizard exists so you do not have to guess. It checks each thing in order
and **stops at the first one that fails**, telling you what is wrong rather
than leaving you to work backwards from a 401. In particular it finds the base
URL for you: Aspen exposes the ProcessData REST service under different paths
depending on version and how IIS was configured, and the wizard probes the
known ones instead of asking you to know which applies.

It also means you never put a password in a file, a script, or a shell
variable — see [where the credentials go](#where-the-credentials-go).

You do not have to use it. Every setting has an environment variable, covered
under [doing it by hand](#doing-it-by-hand). The wizard just checks your
answers as you give them.

#### Where to run it

**From the project folder**, using the virtualenv's interpreter:

```bash
cd "C:\path\to\Demo aspy21"
```

```bash
.venv\Scripts\python.exe setup_live.py
```

Both parts matter. The path `.venv\Scripts\python.exe` is relative to the
project folder, and the settings file is written next to `setup_live.py`, so
running it from elsewhere either fails to start or configures a copy the
dashboard will not read. A plain `python setup_live.py` uses your system Python
and fails on the missing dependencies. On macOS or Linux the interpreter is
`.venv/bin/python`.

**On the machine that will run the dashboard.** The wizard tests the connection
from wherever it is running, so its answers only apply there. If you reach the
historian through Remote Desktop and the plant network is not visible from your
own PC, run the project — setup and `uvicorn` both — inside the Remote Desktop
session, and open `http://127.0.0.1:8000` in a browser there. See
[if it cannot reach the server](#if-it-cannot-reach-the-server).

**Any shell will do** — PowerShell, Command Prompt or a terminal in your
editor. Nothing needs to be activated first, and nothing needs administrator
rights: the settings file is written into the project folder and the password
into your own user account's credential store.

#### What it does

It asks for your server, then does the work itself:

1. **Checks the machine can reach it** at the TCP level, so "wrong URL" and
   "no network route" are told apart before anything else.
2. **Finds the REST endpoint** by probing the paths Aspen actually uses
   (`/ProcessData`, `/ProcessData/AtProcessDataREST.dll`, and others) over both
   `https` and `http`. You do not have to know which one your site uses.
3. **Handles the certificate** if it is issued by an internal CA, offering to
   use a CA bundle before it offers to skip verification.
4. **Verifies the credentials** against the server and, if they are rejected,
   says what is usually wrong (username format, Basic vs Windows auth).
5. **Verifies the datasource** by doing a real tag browse and counting what
   comes back.
6. **Saves it** — settings to `aspy21.local.json`, password to Windows
   Credential Manager.

#### What you need to hand

Three answers. Everything else the wizard works out or defaults sensibly.

| It asks for | At the step called | Notes |
|---|---|---|
| **Server** | *Where is the historian?* | A hostname or an IP is enough — `aspen-hist01`, `192.168.10.25`, `192.168.10.25:8080`, or a full URL if you know it |
| **Username** and **password** | *Credentials* | The password prompt does not echo. This is the only place you ever type it |
| **Datasource** | *Datasource* | The one thing the wizard cannot guess. Your historian administrator knows it; often `IP21` or a plant code |

(Steps are numbered as you go, but the numbers shift: the TLS step only appears
when a certificate actually needs a decision. Go by the names.)

If you give it an IP and the server uses HTTPS, expect a certificate warning:
certificates are issued to hostnames, so one will not validate against an IP
even when the server is perfectly healthy. The wizard offers you a CA bundle or
to skip verification. Prefer the hostname where you have one — run `hostname`
inside the Remote Desktop session to find it.

After that, start the dashboard the ordinary way and it connects to your
historian:

```bash
.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

Later:

```bash
.venv\Scripts\python.exe setup_live.py --test
```

| Command | What it does |
|---|---|
| `setup_live.py` | Run (or re-run) the wizard |
| `setup_live.py --test` | Re-check the saved connection and report where it breaks |
| `setup_live.py --show` | Print the saved settings, and any env vars overriding them |
| `setup_live.py --mock` | Switch back to the offline demo, keeping your server details |
| `setup_live.py --live` | Switch back to your server |
| `setup_live.py --reset` | Delete the saved settings and the stored password |

### Where the credentials go

**The password is never written to a file.** The wizard stores it in the
Windows Credential Manager through `keyring`, and `config.py` reads it back at
startup. `aspy21.local.json` holds only non-secret settings, and is git-ignored
because it names your internal server. If a `password` key ever appears in that
file, the app refuses to start rather than load it.

If you would rather not use the credential store, set `ASPY21_PASSWORD` in your
shell instead — it takes priority when present. Avoid `setx`, which writes the
value to the registry in plain text.

### If it cannot reach the server

You reach the historian over Remote Desktop, so this is worth checking first:
**the IP.21 REST service may only be reachable from inside the remote
network.** If the wizard reports that it cannot open a socket, the settings are
probably fine and the network is the problem. The quickest test is to open the
base URL in a browser *inside* the Remote Desktop session — if it works there
and not from your PC, it is access, not configuration.

Three ways round it:

1. **Run the dashboard on the remote machine.** Copy or clone the project
   there, create the virtualenv, and run the wizard and `uvicorn` from that
   desktop. Then browse to `http://127.0.0.1:8000` inside the session.
2. **Ask for the port to be opened** to your PC, or for a hostname that routes
   from your network.
3. **Use the site VPN**, if there is one for plant-network access.

### Doing it by hand

Every setting also has an environment variable, and **environment variables
always win over the saved file** — handy for overriding one run without
changing anything:

| Variable | Meaning |
|---|---|
| `ASPY21_MODE` | `mock` or `live` |
| `ASPY21_BASE_URL` | ProcessData REST root, e.g. `https://aspen.yourplant.local/ProcessData` |
| `ASPY21_DATASOURCE` | IP.21 datasource name. Reads fall back to the server default; `search()` usually needs it |
| `ASPY21_AUTH_SCHEME` | `basic` (default), `ntlm`, `negotiate` or `none` — see [Authentication](#authentication) |
| `ASPY21_USERNAME` | Username. NTLM wants it domain-qualified: `DOMAIN\user` |
| `ASPY21_PASSWORD` | Password. Overrides the credential store |
| `ASPY21_TIMEZONE` | The zone IP.21 reports in: `UTC` (default), `local`, or an IANA name — see [Time zones](#time-zones) |
| `ASPY21_TIMEOUT` | Request timeout in seconds (default `30`) |
| `ASPY21_VERIFY_SSL` | `false` disables certificate checks. Prefer `ASPY21_CA_BUNDLE` |
| `ASPY21_CA_BUNDLE` | Path to a CA bundle, for internal certificate authorities |
| `ASPY21_CONFIG` | Path to the settings file (default `aspy21.local.json`) |

Environment variables are how you configure a **container**, which has no credential
store to read from.

`aspy21` appends `/SQL` and `/Browse` to the base URL itself, so give it the
root — not a full endpoint path.

### What changes

- The banner turns red and names the server, datasource, user, where the
  password came from, TLS state and timeout you are actually connected to.
- The built-in demo tag catalogue disappears, and the tag pickers start empty —
  your historian's tags are unknown until the **tag browser** has searched.
  Browse first, then **Apply selection to all cards**.
- **Card 9 (error handling) is disabled.** It works by forcing the server to
  return HTTP 500, which is not a thing to ask of a production historian, and
  faking the failure would prove nothing. Run it in mock mode.
- The *Intercepted HTTP* panel becomes a real request log: real URLs, real
  status codes, real byte counts and round-trip times. Request bodies are still
  shown, because the generated SQL is the point; request headers are never
  logged, which is where the `Authorization` header lives.
- The generated Python snippet targets your server and reads the credentials
  from `os.environ`, so it is safe to copy out of the page.

Misconfiguration fails at startup rather than mid-demo: a missing base URL, a
scheme-less URL, a password without a username, or a non-numeric timeout all
raise `ConfigError` before the server binds.

> **Live mode reads real process data.** Start with narrow time ranges. A RAW
> read across many tags over a long window can return a great deal of data and
> put real load on the historian.

### If it does not connect

Run `setup_live.py --test` first — it walks the same chain and stops at the
step that fails. `GET /api/health` does the same from the running app and
reports the exception type. Common causes:

- **Cannot open a socket** — network access, not settings. See
  [above](#if-it-cannot-reach-the-server); with Remote Desktop this is the
  likeliest cause by far.
- **401** — wrong credentials, or the server wants Windows Integrated auth rather
  than Basic. Set `ASPY21_AUTH_SCHEME=ntlm` (see [Authentication](#authentication)),
  and check the username is domain-qualified.
- **404** — the base URL is wrong. Re-run the wizard and let it probe.
- **SSL errors** — an internal CA; give the wizard your CA bundle path. Through
  `/api/v1/series` these report as `error_kind: "tls"` rather than `connection`,
  because the host is answering fine and only the certificate is being rejected.
- **Empty tag browse** — the datasource is unset or wrong.
- **Timestamps off by a constant** — `ASPY21_TIMEZONE`. See
  [Time zones](#time-zones).

A 401 does not always mean the credentials are wrong — it often means the server
wanted a different **scheme**. See [Authentication](#authentication).

---

## Authentication

`aspy21 0.2.0b15` documents HTTP Basic only, but plenty of sites front IP.21 with
IIS Windows Integrated authentication, where Basic simply returns 401 no matter how
correct the password is. Set `ASPY21_AUTH_SCHEME`:

| Scheme | When to use it | Needs |
|---|---|---|
| `basic` *(default)* | The server accepts HTTP Basic | nothing |
| `ntlm` | IIS Windows Integrated (NTLM) | `httpx-ntlm` — installed by default |
| `negotiate` | Kerberos / SPNEGO | `httpx-gssapi` — **install it yourself** |
| `none` | Anonymous access | nothing |

Neither Windows scheme is a header you can just set: both are multi-leg
challenge/response handshakes, so they need an `httpx.Auth` that can drive the
exchange. `AspenClient` accepts one, which is how they plug in.

`negotiate` is deliberately not installed by default — `httpx-gssapi` needs system
Kerberos libraries (`libkrb5`), which is a lot to impose for a scheme most sites do
not use:

```bash
pip install httpx-gssapi
```

Selecting a scheme whose library is missing makes the app **refuse to start, naming
the package**, rather than failing later as an unexplained 401.

Two things that cause a 401 that looks like a wrong password:

- **NTLM usually wants a domain-qualified user** — `DOMAIN\user`, not `user`. Live
  mode logs a warning at startup if yours is bare.
- **Kerberos authenticates against a ticket**, not a password, so `negotiate` is the
  one scheme where a username with no password is a valid configuration.

---

## Time zones

**This is the setting most likely to be wrong without anyone noticing.**

IP.21 answers in the historian machine's *local wall time*, with no zone attached.
A consumer that stores UTC and takes those values verbatim is wrong by a fixed
offset — forever, silently, on every reading. No chart reveals it; the data just
sits at the wrong time of day.

So `/api/v1/series` makes the boundary explicit in both directions: request bounds
are read as aware instants (a token with no zone is taken as UTC), converted into
the historian's zone to build the query, and every returned timestamp is converted
back and rendered with an offset. `ASPY21_TIMEZONE` names that zone:

| Value | Meaning |
|---|---|
| `UTC` *(default)* | The historian machine runs on UTC |
| `local` | Whatever zone this machine is in — what mock mode uses, so it round-trips anywhere |
| an IANA name | e.g. `Europe/Athens`. On Windows this also needs `tzdata`, which is in `requirements.txt` |

Live mode logs a warning at startup while it is still `UTC`, because that is rarely
right for a real plant. **Verify it on the first connection**: read a tag you can
also see in Aspen's own client and compare a few timestamps.

---

## The dashboard, top to bottom

| Section | What it is |
| --- | --- |
| **Header** | Installed `aspy21`, `pandas` and `httpx` versions, plus a link to the generated Swagger docs. `/api/environment` additionally reports the active mode, auth scheme and time zone |
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

`/api/execute` mirrors the cards, so its responses carry the notes, the generated
snippet and every request body — useful to read, wasteful to poll. If you are
writing a client rather than exploring, use [`/api/v1/series`](#the-ingest-api)
instead.

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

**`ConfigError` on startup**
Live mode is misconfigured; the message names the setting. This is deliberate —
the check runs before the server binds, so a typo cannot surface three cards
into a demo. `setup_live.py --show` prints what is currently configured, and
`--test` checks it against the server.

**Live mode: the tag pickers are empty**
Expected. The demo catalogue describes the mock's tags, not yours. Browse in the
tag browser, then **Apply selection to all cards**.

**Live mode: card 9 is greyed out**
Also expected — it needs a server that fails on command. See
[live mode](#live-mode-connecting-to-a-real-historian).

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

It also covers the ingest contract: the half-open window, sorting and
de-duplication, offset-bearing timestamps, and **every failure path** — each
`error_kind` forced through the mock, plus the ones that never reach the wire (TLS,
connection, timeouts, `tenacity.RetryError` unwrapping) asserted directly against
the classifier.

```bash
.venv\Scripts\python.exe selftest_live.py
```

Covers the other half: that live mode really opens sockets. It stands up a
localhost server speaking the IP.21 wire format, requires HTTP Basic auth, and
points `ASPY21_MODE=live` at it — real httpx, real sockets, real
`Authorization` headers, loopback only. It asserts that credentials travel,
that the password never reaches any response or the rendered page, that the
HTTP log records real statuses and byte counts, that the mock-only card is
refused with a 409 rather than faked, and that each way of misconfiguring live
mode fails at startup.

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

CI runs `ruff check`, `ruff format --check`, `selftest.py` and
`selftest_live.py` on Python 3.10, 3.11 and 3.12 for every push and pull
request — see
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

Because the seam is exactly one method — `client()`, which returns an
`AspenClient` — swapping it for a real connection is all that
[live mode](#live-mode-connecting-to-a-real-historian) does.
`live_backend.LiveBackend` exposes the same `client()`, `http_calls` and
context-manager surface, so every executor runs unchanged; only the transport
differs. `config.SETTINGS` decides which one `open_backend()` hands out.

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
series.py             POST /api/v1/series — the machine-facing ingest contract
config.py             settings: environment > aspy21.local.json > defaults
setup_live.py         interactive setup for a real historian
mock_backend.py       the ONLY module that fakes anything
live_backend.py       the real connection, same shape as the mock
selftest.py           end-to-end self-test (mock mode)
selftest_live.py      end-to-end self-test (live mode, against localhost)
templates/index.html  server-rendered dashboard
static/               style.css, app.js
Dockerfile            runs the gateway as a service
ENDPOINTS.md          endpoint reference

aspy21.local.json     your saved connection (git-ignored, never has a password)
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
