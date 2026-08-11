"""Runtime configuration: mocked demo backend, or a live IP.21 historian.

The dashboard ships in ``mock`` mode, where ``mock_backend`` answers every
request in-process. Live mode swaps in a real ``AspenClient`` pointed at your
own ProcessData REST endpoint.

The easy way to set that up is ``python setup_live.py``, which probes your
server, tests the connection, and saves the answers. This module is what reads
them back.

Where settings come from
------------------------
Three layers, highest priority first:

1. **Environment variables** -- always win, so you can override a saved
   connection for one run without editing anything.
2. **``aspy21.local.json``** -- what ``setup_live.py`` writes. Non-secret
   settings only. Git-ignored.
3. **Built-in defaults** -- which mean mock mode.

The password is never in any of those. It lives in the OS credential store
(Windows Credential Manager via ``keyring``), or in ``ASPY21_PASSWORD`` if you
prefer to pass it per-shell.

Environment variables
---------------------
==========================  ===============================================
``ASPY21_MODE``             ``mock`` or ``live``
``ASPY21_BASE_URL``         ProcessData REST root, e.g.
                            ``https://aspen.plant.local/ProcessData``
``ASPY21_DATASOURCE``       IP.21 datasource name. Required for search().
``ASPY21_AUTH_SCHEME``      ``basic`` (default), ``ntlm``, ``negotiate`` or
                            ``none``. Sites that front IP.21 with IIS Windows
                            Integrated authentication need ``ntlm`` (or
                            ``negotiate`` for Kerberos); Basic simply fails
                            there with 401.
``ASPY21_USERNAME``         Username. Omit for anonymous access. NTLM wants it
                            domain-qualified: ``DOMAIN\\user``.
``ASPY21_PASSWORD``         Password. Overrides the credential store when set.
``ASPY21_TIMEOUT``          Request timeout in seconds (default 30).
``ASPY21_TIMEZONE``         The zone IP.21 reports timestamps in: ``UTC``
                            (default), ``local``, or an IANA name such as
                            ``Europe/Athens``. IP.21 answers in the historian
                            machine's local wall time with no zone attached, so
                            getting this wrong shifts every reading by a fixed
                            offset -- silently. Only ``/api/v1/series`` uses it.
``ASPY21_VERIFY_SSL``       ``false`` disables certificate verification.
``ASPY21_CA_BUNDLE``        Path to a CA bundle, for internal CAs. Preferred
                            over disabling verification.
``ASPY21_CONFIG``           Path to the settings file (default
                            ``aspy21.local.json`` beside this module).
``ASPY21_SQL_DIR``          Folder of ``.sql`` scripts the SQL card can run
                            (default ``sql`` beside this module).
``ASPY21_SQL_ALLOW_WRITES`` ``true`` lets non-SELECT scripts run. Off by
                            default: aspy21 itself cannot alter the historian,
                            and SQLplus can.
==========================  ===============================================

Credentials are never written to the settings file, never logged, and never
returned by any API endpoint -- ``public_dict`` reports only *whether* auth is
configured, and from where.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mock_backend import BASE_URL as MOCK_BASE_URL
from mock_backend import DATASOURCE as MOCK_DATASOURCE

logger = logging.getLogger("aspy21-demo.config")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# Identifies our entry in the OS credential store.
KEYRING_SERVICE = "aspy21-dashboard"

DEFAULT_CONFIG_PATH = Path(__file__).with_name("aspy21.local.json")

# Keys setup_live.py is allowed to write. Anything else in the file is ignored
# rather than silently trusted.
_FILE_KEYS = frozenset(
    {
        "mode",
        "base_url",
        "datasource",
        "username",
        "timeout",
        "verify_ssl",
        "ca_bundle",
        "timezone",
        "auth_scheme",
    }
)

# How to authenticate. IP.21 is usually fronted by IIS, and which of these a site
# accepts is a deployment decision made long before this code runs.
AUTH_BASIC = "basic"
AUTH_NTLM = "ntlm"
AUTH_NEGOTIATE = "negotiate"
AUTH_NONE = "none"
AUTH_SCHEMES = (AUTH_BASIC, AUTH_NTLM, AUTH_NEGOTIATE, AUTH_NONE)

# Windows Integrated auth needs a library httpx does not bundle, and each brings
# its own baggage: httpx-ntlm is pure Python, httpx-gssapi needs system Kerberos
# libraries. Both are optional, so a Basic-only install stays small -- but asking
# for a scheme whose library is absent must fail loudly at startup rather than as
# a puzzling 401 on the first read.
_AUTH_PACKAGES = {
    AUTH_NTLM: ("httpx_ntlm", "httpx-ntlm"),
    AUTH_NEGOTIATE: ("httpx_gssapi", "httpx-gssapi"),
}


def auth_backend_available(scheme: str) -> bool:
    """Whether the library a scheme needs is importable."""
    module = _AUTH_PACKAGES.get(scheme)
    if module is None:
        return True
    try:
        __import__(module[0])
    except Exception:
        return False
    return True


class ConfigError(RuntimeError):
    """Raised at import time when live mode is selected but misconfigured."""


def config_path() -> Path:
    """Where the saved settings live."""
    override = (os.getenv("ASPY21_CONFIG") or "").strip()
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    """Read the saved settings, tolerating absence but not corruption."""
    target = path or config_path()
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"{target} could not be read: {exc}. Fix or delete it, or re-run setup_live.py."
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} should contain a JSON object. Re-run setup_live.py.")
    if "password" in raw:
        raise ConfigError(
            f"{target} contains a 'password' key. This project never stores passwords in "
            "files -- remove it and re-run setup_live.py to save it to the credential store."
        )
    return {k: v for k, v in raw.items() if k in _FILE_KEYS}


def get_stored_password(username: str) -> str:
    """Read the password from the OS credential store, if one is saved there."""
    if not username:
        return ""
    try:
        import keyring
    except ImportError:
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE, username) or ""
    except Exception as exc:  # a locked or unavailable vault is not fatal
        logger.warning("Could not read the credential store: %s", exc)
        return ""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    """Where the dashboard sends its IP.21 traffic."""

    mode: str
    base_url: str
    datasource: str
    username: str
    password: str
    timeout: float
    verify_ssl: bool
    ca_bundle: str
    # The zone IP.21's naive timestamps are in. Only /api/v1/series needs it:
    # the dashboard displays whatever the historian said, but an ingest client
    # stores UTC and cannot guess the offset.
    timezone: str = "UTC"
    # How to authenticate: basic | ntlm | negotiate | none. See AUTH_SCHEMES.
    auth_scheme: str = AUTH_BASIC
    # Folder of .sql scripts the SQL card lists and runs.
    sql_dir: str = "sql"
    # Whether a script that is not a SELECT may run. Off unless asked for.
    sql_allow_writes: bool = False
    # Where the password came from, for display and troubleshooting. Never the
    # value itself.
    password_source: str = "none"
    config_file: str = ""

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def credentials(self) -> tuple[str, str] | None:
        """Username and password, or None when going out anonymous."""
        if self.live and self.auth_scheme != AUTH_NONE and self.username:
            return (self.username, self.password)
        return None

    @property
    def auth_configured(self) -> bool:
        """Whether credentials are set, without exposing them."""
        return self.credentials is not None

    @property
    def verify(self) -> str | bool:
        """The value httpx wants for ``verify=``."""
        if self.ca_bundle:
            return self.ca_bundle
        return self.verify_ssl

    def public_dict(self) -> dict[str, Any]:
        """Everything the UI may see. Deliberately excludes the password."""
        return {
            "mode": self.mode,
            "live": self.live,
            "base_url": self.base_url,
            "datasource": self.datasource,
            "auth_configured": self.auth_configured,
            "auth_scheme": self.auth_scheme,
            "username": self.username if self.live else "",
            "password_source": self.password_source,
            "timeout": self.timeout,
            "verify_ssl": self.verify_ssl,
            "ca_bundle": self.ca_bundle,
            "timezone": self.timezone,
            "sql_dir": self.sql_dir,
            "sql_allow_writes": self.sql_allow_writes,
            "config_file": self.config_file,
        }


def load_settings(config: dict[str, Any] | None = None) -> Settings:
    """Resolve settings from the environment and the saved config file.

    Environment variables win over the file, so a saved connection can be
    overridden for a single run without editing anything.

    Validation happens here rather than on the first request, so a typo in the
    base URL surfaces at startup, not three cards into a demo.
    """
    saved = load_config_file() if config is None else config
    path = str(config_path())

    def setting(env_name: str, key: str, default: str = "") -> str:
        """Environment first, then the saved file, then the default."""
        from_env = _env(env_name)
        if from_env:
            return from_env
        value = saved.get(key, default)
        return str(value).strip() if value is not None else default

    mode = setting("ASPY21_MODE", "mode", "mock").lower()
    if mode not in {"mock", "live"}:
        raise ConfigError(
            f"mode={mode!r} is not valid (from "
            f"{'ASPY21_MODE' if _env('ASPY21_MODE') else path}). Use 'mock' or 'live'."
        )

    if mode == "mock":
        return Settings(
            mode="mock",
            base_url=MOCK_BASE_URL,
            datasource=MOCK_DATASOURCE,
            username="",
            password="",
            timeout=10.0,
            verify_ssl=True,
            ca_bundle="",
            # The mock generates naive timestamps in the host's own zone, so
            # 'local' is what makes a mock read round-trip on any machine.
            timezone=_env("ASPY21_TIMEZONE") or "local",
            sql_dir=_env("ASPY21_SQL_DIR") or str(Path(__file__).with_name("sql")),
            sql_allow_writes=_env("ASPY21_SQL_ALLOW_WRITES").lower() in _TRUE,
            config_file=path if saved else "",
        )

    base_url = setting("ASPY21_BASE_URL", "base_url").rstrip("/")
    if not base_url:
        raise ConfigError(
            "Live mode needs a base URL. Run 'python setup_live.py' to configure one, "
            "or set ASPY21_BASE_URL, e.g. https://aspen.yourplant.local/ProcessData"
        )
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"base_url={base_url!r} must start with http:// or https://")

    auth_scheme = setting("ASPY21_AUTH_SCHEME", "auth_scheme", AUTH_BASIC).lower()
    if auth_scheme not in AUTH_SCHEMES:
        raise ConfigError(
            f"auth_scheme={auth_scheme!r} is not valid. Use one of: {', '.join(AUTH_SCHEMES)}."
        )
    if not auth_backend_available(auth_scheme):
        _, package = _AUTH_PACKAGES[auth_scheme]
        raise ConfigError(
            f"auth_scheme={auth_scheme!r} needs the optional {package} package, which is not "
            f"installed here. Run 'pip install {package}'"
            + (
                " (it also needs system Kerberos libraries: libkrb5 on Debian/Ubuntu)."
                if auth_scheme == AUTH_NEGOTIATE
                else "."
            )
        )

    username = setting("ASPY21_USERNAME", "username")

    # The password is the one setting that never comes from the file.
    password = os.getenv("ASPY21_PASSWORD") or ""
    password_source = "environment" if password else "none"
    if username and not password:
        password = get_stored_password(username)
        password_source = "credential store" if password else "none"

    # Negotiate can use an existing Kerberos ticket, so it is the one scheme where
    # a username without a password is a legitimate configuration.
    if username and not password and auth_scheme != AUTH_NEGOTIATE:
        raise ConfigError(
            f"No password found for user {username!r}. Run 'python setup_live.py' to save "
            "one to the credential store, or set ASPY21_PASSWORD for this shell."
        )
    if password and not username:
        raise ConfigError("ASPY21_PASSWORD is set but no username is configured.")
    if auth_scheme == AUTH_NONE and username:
        raise ConfigError(
            "auth_scheme='none' sends requests anonymously, but a username is configured. "
            "Remove the username, or pick basic/ntlm/negotiate."
        )

    ca_bundle = setting("ASPY21_CA_BUNDLE", "ca_bundle")
    if ca_bundle and not os.path.exists(ca_bundle):
        raise ConfigError(f"ca_bundle={ca_bundle!r} does not exist.")

    verify_raw = setting("ASPY21_VERIFY_SSL", "verify_ssl", "true").lower()
    if verify_raw in _TRUE:
        verify_ssl = True
    elif verify_raw in _FALSE:
        verify_ssl = False
    else:
        raise ConfigError(f"verify_ssl={verify_raw!r} is not a boolean. Use true/false.")

    timeout_raw = setting("ASPY21_TIMEOUT", "timeout", "30")
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise ConfigError(f"timeout={timeout_raw!r} is not a number.") from exc
    if timeout <= 0:
        raise ConfigError(f"timeout={timeout_raw!r} must be greater than zero.")

    settings = Settings(
        mode="live",
        base_url=base_url,
        datasource=setting("ASPY21_DATASOURCE", "datasource"),
        username=username,
        password=password,
        timeout=timeout,
        verify_ssl=verify_ssl,
        ca_bundle=ca_bundle,
        timezone=setting("ASPY21_TIMEZONE", "timezone", "UTC"),
        auth_scheme=auth_scheme,
        sql_dir=setting("ASPY21_SQL_DIR", "sql_dir", str(Path(__file__).with_name("sql"))),
        sql_allow_writes=_env("ASPY21_SQL_ALLOW_WRITES").lower() in _TRUE,
        password_source=password_source,
        config_file=path if saved else "",
    )

    logger.warning(
        "LIVE MODE: talking to %s (datasource=%r, auth=%s, verify=%s). Reads hit a real historian.",
        settings.base_url,
        settings.datasource or "<server default>",
        settings.auth_scheme if settings.auth_configured else "anonymous",
        settings.verify,
    )
    if settings.auth_scheme == AUTH_NTLM and "\\" not in settings.username:
        logger.warning(
            "NTLM usually wants a domain-qualified user, e.g. DOMAIN\\%s. A bare username "
            "is a common cause of a 401 that looks like a wrong password.",
            settings.username or "user",
        )
    if base_url.startswith("http://"):
        logger.warning(
            "The base URL is plain http:// -- credentials would cross the network "
            "unencrypted. Use https:// where the server supports it."
        )
    if not verify_ssl and not ca_bundle:
        logger.warning(
            "verify_ssl is off -- TLS certificates are NOT checked. Prefer a CA bundle "
            "(ASPY21_CA_BUNDLE) with your internal CA."
        )
    if not settings.datasource:
        logger.warning(
            "No datasource configured. Reads use the server default, but "
            "client.search() (the tag browser) usually requires a datasource."
        )
    if settings.timezone.upper() == "UTC":
        logger.warning(
            "ASPY21_TIMEZONE is UTC. IP.21 answers in the historian machine's local wall "
            "time, so if that machine is not on UTC every reading /api/v1/series returns "
            "is shifted by a fixed offset. Set it to the plant's zone, e.g. Europe/Athens."
        )

    return settings


_settings_cache: Settings | None = None


def __getattr__(name: str) -> Any:
    """Resolve ``config.SETTINGS`` on first access rather than at import.

    The app still fails at startup, because ``main`` reads ``SETTINGS`` while
    importing. But ``setup_live.py`` -- the tool you reach for when the saved
    connection is broken -- can import this module for its helpers without
    tripping over the very configuration it exists to repair.
    """
    if name != "SETTINGS":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache
