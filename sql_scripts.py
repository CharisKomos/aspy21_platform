"""Run SQLplus against the historian, from a folder of ``.sql`` files.

``aspy21`` builds SQLplus internally -- ``build_sql_search_query`` emits
``<SQL t="SQLplus">`` and posts it to ``{base_url}/SQL`` -- but exposes no public
way to send your own. This module opens that door, without reimplementing
anything: it wraps the query in the same envelope the library uses and posts it
through the same authenticated ``httpx`` client.

Two things are deliberate.

**Scripts are read from one configured folder and nowhere else.** A name is
resolved inside ``ASPY21_SQL_DIR`` and rejected if it escapes -- ``..`` in a name
is otherwise a way to read any file the process can see.

**Writes are refused unless you opt in.** ``aspy21`` being read-only is a
guarantee, not a missing feature: nothing you call through it can alter the
historian. SQLplus has no such property -- with the right permissions it will
``UPDATE`` and ``DELETE``, and a mistyped ``where`` clause against a production
historian is not recoverable. So anything that is not a ``SELECT`` needs
``ASPY21_SQL_ALLOW_WRITES=true``, and the right place to enforce this properly is
still a read-only account on the historian itself.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

logger = logging.getLogger("aspy21-demo.sql")

# The dataset options aspy21 itself sends: keep numbers and times as text so the
# response parses predictably rather than depending on server locale.
DSO = "CHARINT=N;CHARFLOAT=N;CHARTIME=N;CONVERTERRORS=N"

# Statements that only read. Anything else is a write as far as this is concerned,
# including DDL and the stored-procedure forms, because guessing is worse.
_READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# Stripped before the read-only check so a comment cannot hide a write.
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


class SqlScriptError(Exception):
    """A script could not be read or is not allowed to run."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def strip_comments(sql: str) -> str:
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql)).strip()


def is_read_only(sql: str) -> bool:
    """Whether every statement in the script only reads.

    Conservative on purpose: a script has to be recognisably read-only in full, so
    a trailing ``delete`` after a ``select`` cannot slip through.
    """
    body = strip_comments(sql)
    if not body:
        return False
    for statement in (s for s in body.split(";") if s.strip()):
        if not _READ_ONLY_START.match(statement):
            return False
    return True


def scripts_dir(configured: str) -> Path:
    return Path(configured).expanduser().resolve()


def list_scripts(configured: str) -> list[dict[str, Any]]:
    """Every ``.sql`` file in the folder, newest name-sorted, with its size."""
    folder = scripts_dir(configured)
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.sql")):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "bytes": stat.st_size, "modified": stat.st_mtime})
    return out


def read_script(configured: str, name: str) -> str:
    """The contents of one script, refusing anything outside the folder.

    ``name`` is treated as a bare filename. Resolving and then re-checking the
    parent is what stops ``../../etc/passwd`` and absolute paths alike -- checking
    the string for ``..`` is not enough once symlinks exist.
    """
    folder = scripts_dir(configured)
    candidate = (folder / Path(name).name).resolve()

    if candidate.parent != folder:
        raise SqlScriptError("bad_request", f"{name!r} is not inside the scripts folder.")
    if candidate.suffix.lower() != ".sql":
        raise SqlScriptError("bad_request", "Only .sql files can be run.")
    if not candidate.is_file():
        raise SqlScriptError("not_found", f"No script named {name!r} in {folder}.")

    try:
        return candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise SqlScriptError("bad_request", f"Could not read {name!r}: {exc}") from exc


def build_envelope(
    sql: str,
    datasource: str,
    *,
    max_rows: int = 10000,
    timeout: int = 60,
    group: str = "aspy21_script",
) -> str:
    """Wrap a query in the same ``<SQL>`` envelope aspy21 sends.

    ``t="SQLplus"`` selects the language and ``response="Original"`` returns the
    rows as the server produced them, rather than the record shape the history
    readers ask for.
    """
    return (
        f'<SQL g="{escape(group)}" t="SQLplus" ds="{escape(datasource)}" '
        f'dso="{DSO}" m="{int(max_rows)}" to="{int(timeout)}" '
        f'response="Original" s="1"><![CDATA[{sql.strip()}]]></SQL>'
    )


def rows_from_response(payload: Any) -> list[dict[str, Any]]:
    """Flatten the historian's answer into plain rows.

    The SQL endpoint nests differently depending on the query, so this walks for
    the first list of row-shaped dicts rather than assuming one layout.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    for candidate in (data, payload):
        if isinstance(candidate, list):
            rows = [r for r in candidate if isinstance(r, dict)]
            if rows and "rows" not in rows[0]:
                return rows
            for entry in rows:
                if isinstance(entry.get("rows"), list):
                    return [r for r in entry["rows"] if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return [r for r in data["rows"] if isinstance(r, dict)]
    return []
