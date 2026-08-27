"""Parser for the CMS-required ``cms-hpt.txt`` discovery file.

45 CFR 180 requires each hospital to publish ``cms-hpt.txt`` in the root of its
website domain, pointing at the machine-readable file (MRF) of standard charges.
CMS specifies the required elements but not a strict grammar, so real files vary:
key casing, hyphen vs underscore separators, JSON instead of key/value lines, and
several location blocks in a single file.

Parsing is deliberately tolerant. Nothing here raises on malformed input -- an
unrecognised line becomes a warning so the row can be quarantined with a reason
code rather than failing the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: Normalised key -> canonical field name. Keys are normalised by stripping every
#: non-alphanumeric character, which collapses ``mrf-url``, ``mrf_url``,
#: ``MRF URL`` and ``mrfUrl`` onto one entry.
_ALIASES: dict[str, str] = {
    "locationname": "location_name",
    "location": "location_name",
    "hospitalname": "location_name",
    "hospitallocationname": "location_name",
    "sourcepageurl": "source_page_url",
    "sourcepage": "source_page_url",
    "sourceurl": "source_page_url",
    "mrfurl": "mrf_url",
    "mrf": "mrf_url",
    "machinereadableurl": "mrf_url",
    "machinereadablefileurl": "mrf_url",
    "contactname": "contact_name",
    "hospitalcontactname": "contact_name",
    "pointofcontactname": "contact_name",
    "contactemail": "contact_email",
    "hospitalcontactemail": "contact_email",
    "pointofcontactemail": "contact_email",
    "email": "contact_email",
}


@dataclass(frozen=True)
class CmsHptRecord:
    """One hospital location block from a ``cms-hpt.txt`` file."""

    location_name: str | None = None
    source_page_url: str | None = None
    mrf_url: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """True when the record carries the one field the pipeline needs."""
        return bool(self.mrf_url)


@dataclass(frozen=True)
class CmsHptDocument:
    """Parse result: the records found, plus why anything was skipped."""

    records: tuple[CmsHptRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    source_format: str = "unknown"

    @property
    def mrf_urls(self) -> tuple[str, ...]:
        return tuple(r.mrf_url for r in self.records if r.mrf_url)


def parse_cms_hpt(text: str) -> CmsHptDocument:
    """Parse ``cms-hpt.txt`` content in either key/value or JSON form."""
    stripped = text.lstrip("﻿").strip()
    if not stripped:
        return CmsHptDocument(warnings=("file is empty",))
    if stripped[0] in "{[":
        return _parse_json(stripped)
    return _parse_key_value(stripped)


def _parse_key_value(text: str) -> CmsHptDocument:
    records: list[CmsHptRecord] = []
    warnings: list[str] = []
    known: dict[str, str] = {}
    extra: dict[str, str] = {}

    def flush() -> None:
        nonlocal known, extra
        if known or extra:
            records.append(_build(known, extra))
            known = {}
            extra = {}

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            # A blank line separates location blocks in multi-location files.
            flush()
            continue
        if line.startswith("#"):
            continue
        if ":" not in line:
            warnings.append(f"line {lineno}: no key/value separator: {_clip(line)!r}")
            continue

        raw_key, raw_value = line.split(":", 1)
        value = raw_value.strip().strip('"').strip()
        canonical = _ALIASES.get(_normalise_key(raw_key))
        if canonical is None:
            extra[_normalise_key(raw_key)] = value
            warnings.append(f"line {lineno}: unrecognised key {raw_key.strip()!r}")
            continue
        if canonical in known:
            # A repeated key without a blank line between blocks: treat it as
            # the start of the next record.
            flush()
        if value:
            known[canonical] = value

    flush()
    return CmsHptDocument(tuple(records), tuple(warnings), "key-value")


def _parse_json(text: str) -> CmsHptDocument:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return CmsHptDocument(
            warnings=(f"looks like JSON but failed to parse: {exc}",),
            source_format="json",
        )

    warnings: list[str] = []
    objects = _json_objects(payload)
    records: list[CmsHptRecord] = []

    for obj in objects:
        known: dict[str, str] = {}
        extra: dict[str, str] = {}
        for raw_key, raw_value in obj.items():
            if not isinstance(raw_value, str | int | float):
                continue
            value = str(raw_value).strip()
            canonical = _ALIASES.get(_normalise_key(str(raw_key)))
            if canonical is None:
                extra[_normalise_key(str(raw_key))] = value
                warnings.append(f"unrecognised key {raw_key!r}")
            elif value:
                known[canonical] = value
        if known or extra:
            records.append(_build(known, extra))

    if not records:
        warnings.append("no recognisable records found in JSON payload")
    return CmsHptDocument(tuple(records), tuple(warnings), "json")


def _json_objects(payload: object) -> list[dict[str, object]]:
    """Flatten the JSON shapes seen in the wild into a list of record objects."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if any(_normalise_key(str(k)) in _ALIASES for k in payload):
            return [payload]
        # A wrapper object such as {"locations": [...]}.
        for value in payload.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _build(known: dict[str, str], extra: dict[str, str]) -> CmsHptRecord:
    return CmsHptRecord(
        location_name=known.get("location_name"),
        source_page_url=known.get("source_page_url"),
        mrf_url=known.get("mrf_url"),
        contact_name=known.get("contact_name"),
        contact_email=known.get("contact_email"),
        extra=dict(extra),
    )


def _normalise_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", raw.strip().lower())


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
