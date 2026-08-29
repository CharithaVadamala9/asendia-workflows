"""Payload handling for JobDiva's untyped BI endpoints.

Every fact encoded here was discovered by calling the live API, not read from the
Swagger — 260 of 431 V2 endpoints declare their response as an object with no
properties. `scripts/discover_jobdiva.py` produced these findings and
`docs/jobdiva-shapes.md` records them.

What the real API does, all of which has bitten us at least once:

  - Responses are enveloped: ``{"message": "Query ... completed successfully",
    "data": [ ... ]}``.
  - Keys are UPPERCASE, sometimes with underscores: ``CANDIDATEID``, ``JOBTITLE``,
    ``SECURITY_CLEARANCE``, ``REQUIRED_DEGREE``.
  - **Every scalar is a string.** ``ID`` is ``"28097080"``, ``SECURITY_CLEARANCE`` is
    ``"0"`` — which is truthy in Python, so a naive ``bool(value)`` is always True.
  - **Empty fields are sometimes the four-character string ``"Null"``.** A real job in
    this tenant has ``SKILLS == "Null"``, which a naive parser turns into a required
    skill literally named "Null".
  - ``CANDIDATEID`` exceeds 32 bits (``19535651872847``).
  - ``RESUMEID`` is **not** numeric: ``"19535651872847_2950_11"``.
  - Dates come back ISO (``2026-05-06T11:45:55``) but must be *sent* as
    ``MM/dd/yyyy HH:mm:ss``.
  - ``JOBDESCRIPTION`` is HTML with entities (``&mdash;``, ``&nbsp;``, ``<br />``).
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

# Values JobDiva uses to mean "no value". Checked case-insensitively.
NULLISH = {"", "null", "none", "n/a", "na", "-"}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]{2,}")


def rows(body: Any) -> list[dict]:
    """Extract the row list from a BI response envelope."""
    if body is None:
        return []
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        # Some endpoints return the object directly rather than a one-row list.
        if data is None and any(k.isupper() for k in body):
            return [body]
        for key in ("records", "rows", "result", "results", "items"):
            if isinstance(body.get(key), list):
                return [r for r in body[key] if isinstance(r, dict)]
    return []


def find(row: dict, *names: str) -> Any:
    """Look up a key ignoring case, spaces, and underscores.

    JobDiva spells the same concept ``SECURITY_CLEARANCE`` in one payload and
    ``"security clearance"`` in another, so all three separators are normalized away.
    """
    lookup = {_norm(k): v for k, v in row.items()}
    for name in names:
        if (value := lookup.get(_norm(name))) is not None:
            return value
    return None


def _norm(key: str) -> str:
    return key.lower().replace(" ", "").replace("_", "")


def text(row: dict, *names: str) -> str:
    """A string field, with JobDiva's several spellings of "empty" mapped to ''."""
    value = find(row, *names)
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in NULLISH else s


def integer(row: dict, *names: str) -> int | None:
    """An int64 field. Values arrive as strings and may be nullish."""
    s = text(row, *names)
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def boolean(row: dict, *names: str) -> bool:
    """A flag. JobDiva sends "0"/"1" as strings, and "0" is truthy in Python."""
    s = text(row, *names).lower()
    return s in {"1", "true", "yes", "y", "opt_in", "optin"}


def timestamp(row: dict, *names: str) -> datetime | None:
    s = text(row, *names)
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def plain_text(value: str | None) -> str:
    """Strip HTML from a description field.

    Job descriptions are stored as HTML. Feeding raw markup to the scoring model
    wastes tokens and dilutes the signal, so tags become whitespace and entities are
    unescaped before it goes anywhere near a prompt.
    """
    if not value:
        return ""
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", value, flags=re.I)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def skills(value: Any) -> list[str]:
    """Split a skills field, discarding JobDiva's nullish placeholders.

    Guards the trap that a real job in this tenant carries ``SKILLS == "Null"``.
    """
    if not value:
        return []
    items = value if isinstance(value, list) else re.split(r"[,;|\n]", str(value))
    out = []
    for item in items:
        s = str(item).strip()
        if s and s.lower() not in NULLISH:
            out.append(s)
    return out
