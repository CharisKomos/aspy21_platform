"""Interactive setup for connecting the dashboard to a real IP.21 historian.

Run it and answer the questions:

    .venv\\Scripts\\python.exe setup_live.py

It works out the right base URL by probing your server, checks the credentials
actually authenticate, verifies the datasource returns tags, and saves the
result. After that, starting the dashboard normally connects to your historian.

What gets saved where
---------------------
Non-secret settings go to ``aspy21.local.json`` beside this file, which is
git-ignored. The password goes to the operating system's credential store
(Windows Credential Manager), never to that file and never to the repository.

Other commands
--------------
    setup_live.py --test     re-check the saved connection
    setup_live.py --show     print the saved settings
    setup_live.py --reset    delete the saved settings and password
    setup_live.py --mock     switch back to the mocked demo backend
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from config import (
    KEYRING_SERVICE,
    ConfigError,
    config_path,
    get_stored_password,
    load_config_file,
)

# Aspen exposes the ProcessData REST service under a handful of paths depending
# on version and how IIS was set up. Probing beats asking the user to guess.
CANDIDATE_PATHS = (
    "/ProcessData/AtProcessDataREST.dll",
    "/ProcessData",
    "/AspenTech/ProcessData/AtProcessDataREST.dll",
    "/ProcessData/AtProcessDataREST.dll/ProcessData",
)

PROBE_TIMEOUT = 10.0


# --------------------------------------------------------------------------
# console helpers
# --------------------------------------------------------------------------
def say(text: str = "") -> None:
    print(text)


def head(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


_step = 0


def step(text: str) -> None:
    """A numbered wizard heading. Stays contiguous when a step is skipped."""
    global _step
    _step += 1
    head(f"{_step}. {text}")


def ok(text: str) -> None:
    print(f"  [ok]   {text}")


def warn(text: str) -> None:
    print(f"  [!]    {text}")


def bad(text: str) -> None:
    print(f"  [x]    {text}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say("\nCancelled. Nothing was saved.")
        sys.exit(1)
    return answer or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = ask(f"{prompt} {suffix}").lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def ask_password(prompt: str = "  Password: ") -> str:
    """Read a password without echoing it.

    ``getpass`` on Windows reads the console device directly, which hangs when
    input is piped. Fall back to a plain read when there is no terminal, so the
    wizard can also be driven from a script; interactively it still never
    echoes.
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass(prompt)
        say(f"{prompt.rstrip()} (reading from stdin; input is not echoed by the caller)")
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")
    except (EOFError, KeyboardInterrupt):
        say("\nCancelled. Nothing was saved.")
        sys.exit(1)


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
@dataclass
class ProbeResult:
    """What one candidate base URL answered."""

    base_url: str
    reachable: bool
    status: int | None = None
    needs_auth: bool = False
    authenticated: bool = False
    detail: str = ""

    @property
    def service_found(self) -> bool:
        """A service that routes our request, whatever it thinks of our auth."""
        return self.reachable and self.status is not None and self.status != 404


def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Can we open a socket at all? Separates 'no route' from 'bad URL'."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except socket.gaierror as exc:
        return False, f"the name {host!r} does not resolve ({exc.strerror or exc})"
    except TimeoutError:
        return False, f"no answer from {host}:{port} within {timeout:.0f}s"
    except OSError as exc:
        return False, f"{host}:{port} refused the connection ({exc.strerror or exc})"


def probe(base_url: str, auth: tuple[str, str] | None, verify: bool | str) -> ProbeResult:
    """Ask one candidate base URL for a tag browse and classify the answer."""
    try:
        response = httpx.get(
            f"{base_url}/Browse",
            params={"tag": "*", "max": 1},
            auth=auth,
            verify=verify,
            timeout=PROBE_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.ConnectError as exc:
        return ProbeResult(base_url, reachable=False, detail=str(exc)[:200])
    except httpx.TimeoutException:
        return ProbeResult(
            base_url, reachable=False, detail=f"timed out after {PROBE_TIMEOUT:.0f}s"
        )
    except Exception as exc:  # TLS failures land here, and are worth reporting verbatim
        return ProbeResult(base_url, reachable=False, detail=f"{type(exc).__name__}: {exc}"[:200])

    return ProbeResult(
        base_url=base_url,
        reachable=True,
        status=response.status_code,
        needs_auth=response.status_code in (401, 403),
        authenticated=response.status_code == 200,
        detail=f"HTTP {response.status_code}",
    )


def candidate_urls(raw: str) -> list[str]:
    """Turn whatever the user typed into an ordered list of URLs to try."""
    raw = raw.strip().rstrip("/")
    parsed = urlparse(raw if "//" in raw else f"//{raw}", scheme="")

    host = parsed.hostname or raw.split("/")[0]
    port = f":{parsed.port}" if parsed.port else ""
    given_path = parsed.path.rstrip("/")

    schemes = [parsed.scheme] if parsed.scheme in ("http", "https") else ["https", "http"]

    urls: list[str] = []
    for scheme in schemes:
        origin = f"{scheme}://{host}{port}"
        # Whatever the user typed comes first: they may well be right.
        if given_path:
            urls.append(f"{origin}{given_path}")
        for path in CANDIDATE_PATHS:
            urls.append(f"{origin}{path}")
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(urls))


def split_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname or "", port


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def save_config(values: dict[str, Any]) -> None:
    path = config_path()
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    ok(f"settings saved to {path}")


def save_password(username: str, password: str) -> bool:
    """Store the password in the OS credential store. Returns success."""
    try:
        import keyring
    except ImportError:
        bad("the 'keyring' package is not installed, so the password cannot be saved securely.")
        say("        Install it with:  .venv\\Scripts\\python.exe -m pip install keyring")
        say("        Until then, set ASPY21_PASSWORD in your shell before starting the server.")
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, username, password)
    except Exception as exc:
        bad(f"the credential store refused to save the password: {exc}")
        return False
    ok(f"password saved to the OS credential store for user {username!r}")
    return True


def delete_password(username: str) -> None:
    if not username:
        return
    try:
        import keyring
    except ImportError:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, username)
        ok(f"password removed from the credential store for {username!r}")
    except Exception:
        pass  # nothing stored, which is the state we wanted anyway


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_show() -> int:
    saved = load_config_file()
    path = config_path()
    if not saved:
        say(f"No saved settings ({path} does not exist).")
        say("The dashboard runs in mock mode. Run 'python setup_live.py' to connect a server.")
        return 0
    head(f"Saved settings ({path})")
    for key, value in saved.items():
        say(f"  {key:12} {value}")
    username = str(saved.get("username", ""))
    if username:
        stored = "yes" if get_stored_password(username) else "no"
        say(f"  {'password':12} {stored} (in the OS credential store, not in the file)")
    overrides = [n for n in os.environ if n.startswith("ASPY21_")]
    if overrides:
        head("Environment overrides in this shell (these win over the file)")
        for name in sorted(overrides):
            shown = "<set>" if name == "ASPY21_PASSWORD" else os.environ[name]
            say(f"  {name:22} {shown}")
    return 0


def cmd_reset() -> int:
    saved = load_config_file()
    path = config_path()
    username = str(saved.get("username", ""))
    if not saved and not username:
        say("Nothing to reset.")
        return 0
    if not ask_yes(f"Delete {path} and the saved password?", default=False):
        say("Left alone.")
        return 0
    delete_password(username)
    if path.exists():
        path.unlink()
        ok(f"deleted {path}")
    say("\nThe dashboard is back to mock mode.")
    return 0


def cmd_mock() -> int:
    saved = load_config_file()
    saved["mode"] = "mock"
    save_config(saved)
    say("\nThe dashboard will start in mock mode. Your server details are kept,")
    say("so 'python setup_live.py --live' switches back without re-entering them.")
    return 0


def cmd_live() -> int:
    saved = load_config_file()
    if not saved.get("base_url"):
        bad("No server is configured yet. Run 'python setup_live.py' first.")
        return 1
    saved["mode"] = "live"
    save_config(saved)
    say(f"\nLive mode enabled against {saved['base_url']}.")
    return 0


def cmd_test() -> int:
    """Re-check the saved connection, end to end."""
    saved = load_config_file()
    if not saved.get("base_url"):
        bad("No server is configured. Run 'python setup_live.py' first.")
        return 1

    base_url = str(saved["base_url"])
    username = str(saved.get("username", ""))
    datasource = str(saved.get("datasource", ""))
    verify: bool | str = str(saved.get("ca_bundle") or "") or bool(saved.get("verify_ssl", True))

    head(f"Testing {base_url}")

    host, port = split_host_port(base_url)
    reachable, why = tcp_reachable(host, port)
    if not reachable:
        bad(f"cannot reach {host}:{port} -- {why}")
        explain_unreachable(host)
        return 1
    ok(f"{host}:{port} accepts connections")

    auth = None
    if username:
        password = os.getenv("ASPY21_PASSWORD") or get_stored_password(username)
        if not password:
            bad(f"no password stored for {username!r}. Re-run 'python setup_live.py'.")
            return 1
        auth = (username, password)

    result = probe(base_url, auth, verify)
    if not result.reachable:
        bad(f"the request failed: {result.detail}")
        return 1
    if result.needs_auth:
        bad(f"the server rejected the credentials ({result.detail}).")
        say("        Check the username and password, or whether the server wants")
        say("        Windows Integrated authentication rather than Basic.")
        return 1
    if not result.authenticated:
        warn(f"unexpected response: {result.detail}")
        return 1
    ok(f"authenticated as {username or '<anonymous>'}")

    count = browse_count(base_url, auth, verify, datasource)
    if count is None:
        warn("the browse call did not return a tag list. Check the datasource name.")
        return 1
    if count == 0:
        warn(f"connected, but datasource {datasource or '<server default>'!r} returned no tags.")
        return 1
    ok(f"datasource {datasource or '<server default>'} returned {count} tag(s)")

    mode = saved.get("mode", "mock")
    say("")
    if mode == "live":
        say("Connection is good and the dashboard is set to live mode.")
    else:
        say("Connection is good, but mode is 'mock'.")
        say("Run 'python setup_live.py --live' to use it.")
    return 0


def browse_count(
    base_url: str, auth: tuple[str, str] | None, verify: bool | str, datasource: str
) -> int | None:
    """How many tags a plain browse returns, or None if the shape was wrong."""
    params: dict[str, Any] = {"tag": "*", "max": 5}
    if datasource:
        params["dataSource"] = datasource
    try:
        response = httpx.get(
            f"{base_url}/Browse",
            params=params,
            auth=auth,
            verify=verify,
            timeout=PROBE_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    tags = _find_tag_list(payload)
    return None if tags is None else len(tags)


def _find_tag_list(payload: Any, depth: int = 0) -> list[Any] | None:
    """Locate the tag list in a Browse response.

    IP.21 nests it as ``{"data": {"tags": [...]}}``, but the exact wrapper has
    varied between versions, so search the likely keys rather than assuming one.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict) or depth > 4:
        return None
    for key in ("tags", "Tags", "data", "Data", "items", "Items", "result"):
        if key in payload:
            found = _find_tag_list(payload[key], depth + 1)
            if found is not None:
                return found
    return None


def explain_unreachable(host: str) -> None:
    """The remote-desktop case, which is easy to hit and confusing to debug."""
    say("")
    say("  This usually means the historian is not reachable from THIS machine.")
    say("  If you normally reach that server over Remote Desktop, the IP.21 REST")
    say("  service may only be exposed inside the remote network. Options:")
    say("")
    say("    1. Run this dashboard ON the remote machine (copy the project there,")
    say("       or clone it, then run setup and uvicorn from that desktop).")
    say("    2. Ask whoever runs the historian to open the ProcessData REST port")
    say(f"       to your PC, or give you a hostname for {host} that routes from here.")
    say("    3. Use a VPN, if your site has one for plant-network access.")
    say("")
    say("  A quick way to tell: from the Remote Desktop session, open the base URL")
    say("  in a browser there. If it works there but not here, it is network access,")
    say("  not the settings.")


# --------------------------------------------------------------------------
# the wizard
# --------------------------------------------------------------------------
def wizard() -> int:
    saved = load_config_file()

    say("=" * 72)
    say("  Connect the aspy21 dashboard to a real IP.21 historian")
    say("=" * 72)
    say("")
    say("  Answer the questions below. Nothing is saved until the end, and the")
    say("  password goes to the Windows Credential Manager, never to a file.")
    say("  Press Ctrl+C at any point to abandon it.")

    # ---- 1. where the server is -------------------------------------------
    step("Where is the historian?")
    say("  Enter the server name, or a full URL if you already know it.")
    say("  Examples:  aspen-hist01        aspenserver.plant.local")
    say("             https://aspen-hist01/ProcessData")
    say("")
    typed = ask("  Server", str(saved.get("base_url", "")))
    if not typed:
        bad("nothing entered.")
        return 1

    candidates = candidate_urls(typed)
    host, port = split_host_port(candidates[0])

    step("Can this machine reach it?")
    reachable, why = tcp_reachable(host, port)
    if reachable:
        ok(f"{host}:{port} accepts connections")
    else:
        bad(f"cannot reach {host}:{port} -- {why}")
        # Try the other scheme's default port before giving up.
        other = 80 if port == 443 else 443
        alt_reachable, _ = tcp_reachable(host, other)
        if alt_reachable:
            ok(f"but {host}:{other} does answer -- the scheme is probably the other one")
        else:
            explain_unreachable(host)
            if not ask_yes("\n  Carry on and try the HTTP probes anyway?", default=False):
                return 1

    # ---- 3. find the REST root --------------------------------------------
    step("Finding the ProcessData REST endpoint")
    say(f"  Trying {len(candidates)} candidate URL(s)...\n")

    found: list[ProbeResult] = []
    for url in candidates:
        result = probe(url, auth=None, verify=True)
        if result.service_found:
            label = "needs authentication" if result.needs_auth else result.detail
            ok(f"{url}  ->  {label}")
            found.append(result)
        elif result.reachable:
            say(f"  [ ]    {url}  ->  HTTP {result.status} (not the service)")
        elif "certificate" in result.detail.lower() or "SSL" in result.detail:
            warn(f"{url}  ->  TLS problem: {result.detail[:110]}")
            found.append(result)
        else:
            say(f"  [ ]    {url}  ->  {result.detail[:110]}")

    if not found:
        say("")
        bad("None of the candidates looked like a ProcessData REST service.")
        say("        Ask whoever administers the historian for the exact REST URL, then")
        say("        re-run this and paste it in full at the first question.")
        return 1

    base_url = found[0].base_url
    if len(found) > 1:
        say("")
        say("  More than one candidate answered. Pick one:")
        for index, result in enumerate(found, start=1):
            say(f"    {index}. {result.base_url}")
        choice = ask("  Number", "1")
        try:
            base_url = found[int(choice) - 1].base_url
        except (ValueError, IndexError):
            base_url = found[0].base_url
    say("")
    ok(f"using {base_url}")

    # ---- 4. TLS ------------------------------------------------------------
    verify: bool | str = True
    ca_bundle = ""
    verify_ssl = True
    if base_url.startswith("https://"):
        tls_probe = probe(base_url, auth=None, verify=True)
        if not tls_probe.reachable and (
            "certificate" in tls_probe.detail.lower() or "SSL" in tls_probe.detail
        ):
            step("TLS certificate")
            warn(f"the certificate was not trusted: {tls_probe.detail[:150]}")
            say("")
            say("  This is normal for an internal certificate authority. The right fix")
            say("  is to point at your CA bundle; turning verification off leaves the")
            say("  connection open to interception on the plant network.")
            say("")
            ca_bundle = ask("  Path to CA bundle (.pem/.crt), or blank to skip")
            if ca_bundle and not os.path.exists(ca_bundle):
                warn(f"{ca_bundle} does not exist; ignoring it.")
                ca_bundle = ""
            if ca_bundle:
                verify = ca_bundle
            else:
                verify_ssl = not ask_yes(
                    "  Disable certificate verification for this connection?", default=False
                )
                verify = verify_ssl
                if not verify_ssl:
                    warn("certificate verification will be OFF for this connection.")
    elif base_url.startswith("http://"):
        warn("this is a plain http:// URL, so credentials cross the network unencrypted.")
        say("        Use https:// if the server offers it.")

    # ---- 5. credentials ----------------------------------------------------
    step("Credentials")
    needs_auth = any(r.needs_auth for r in found if r.base_url == base_url)
    if needs_auth:
        say("  The server asked for authentication.")
    else:
        say("  The server answered without authentication, but most do require it.")
    say("  Leave the username blank for anonymous access.")
    say("")

    default_user = str(saved.get("username", "")) or os.getenv("USERNAME", "")
    username = ask("  Username", default_user)
    password = ""
    if username:
        say("  (the password is not echoed, and is not written to any file)")
        password = ask_password()
        if not password:
            bad("no password entered.")
            return 1

    auth = (username, password) if username else None

    say("")
    say("  Checking...")
    check = probe(base_url, auth, verify)
    if check.needs_auth:
        bad(f"the server rejected those credentials ({check.detail}).")
        say("")
        say("  Things to check:")
        say("    - the username format: some servers want DOMAIN\\user or user@domain")
        say("    - whether the account has ProcessData access, not just a Windows login")
        say("    - whether the server uses Windows Integrated authentication rather")
        say("      than Basic. aspy21 0.2.0b15 documents Basic only; see the README")
        say("      section on live mode for how to plug in an httpx.Auth object.")
        return 1
    if not check.reachable:
        bad(f"the request failed: {check.detail}")
        return 1
    ok(f"authenticated as {username or '<anonymous>'}")

    # ---- 6. datasource -----------------------------------------------------
    step("Datasource")
    say("  The IP.21 datasource name. Reads work without it (the server default),")
    say("  but the tag browser usually needs it. Your historian administrator")
    say("  knows this; it is often something like IP21 or the plant code.")
    say("")
    datasource = ask("  Datasource", str(saved.get("datasource", "")))

    count = browse_count(base_url, auth, verify, datasource)
    if count is None:
        warn("the browse call did not return a readable tag list.")
        say("        The connection works, but the tag browser may not. You can change")
        say("        the datasource later with 'python setup_live.py' again.")
    elif count == 0:
        warn(f"datasource {datasource or '<server default>'} returned no tags.")
        if not ask_yes("  Save it anyway?", default=True):
            return 1
    else:
        ok(f"browse returned {count} tag(s)")

    # ---- 7. save -----------------------------------------------------------
    step("Saving")
    timeout = ask("  Request timeout in seconds", str(saved.get("timeout", 30)))
    try:
        timeout_value = float(timeout)
    except ValueError:
        timeout_value = 30.0

    values = {
        "mode": "live",
        "base_url": base_url,
        "datasource": datasource,
        "username": username,
        "timeout": timeout_value,
        "verify_ssl": verify_ssl,
        "ca_bundle": ca_bundle,
    }
    save_config(values)
    if username:
        save_password(username, password)

    say("")
    say("=" * 72)
    say("  Done. Start the dashboard as usual:")
    say("")
    say("      .venv\\Scripts\\python.exe -m uvicorn main:app --port 8000")
    say("")
    say(f"  It will connect to {base_url}")
    say("  and the banner at the top of the page will be red to say so.")
    say("")
    say("  Useful later:")
    say("      setup_live.py --test    re-check the connection")
    say("      setup_live.py --mock    go back to the offline demo")
    say("      setup_live.py --live    switch back to your server")
    say("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure the aspy21 dashboard's connection to a real IP.21 historian.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test", action="store_true", help="re-check the saved connection")
    group.add_argument("--show", action="store_true", help="print the saved settings")
    group.add_argument("--reset", action="store_true", help="delete saved settings and password")
    group.add_argument("--mock", action="store_true", help="switch back to the mocked backend")
    group.add_argument("--live", action="store_true", help="switch to the saved live connection")
    args = parser.parse_args(argv)

    try:
        if args.show:
            return cmd_show()
        if args.reset:
            return cmd_reset()
        if args.mock:
            return cmd_mock()
        if args.live:
            return cmd_live()
        if args.test:
            return cmd_test()
        return wizard()
    except ConfigError as exc:
        bad(str(exc))
        return 1
    except KeyboardInterrupt:
        say("\nCancelled. Nothing was saved.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
