"""Malay and English source_effective_date parser (never fabricates valid_until)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

_MONTHS: dict[str, int] = {
    "januari": 1,
    "january": 1,
    "jan": 1,
    "februari": 2,
    "february": 2,
    "feb": 2,
    "mac": 3,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mei": 5,
    "may": 5,
    "jun": 6,
    "june": 6,
    "julai": 7,
    "july": 7,
    "jul": 7,
    "ogos": 8,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "disember": 12,
    "december": 12,
    "dis": 12,
    "dec": 12,
}

_MONTH_ALT = "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))

# e.g., 9December2024, 5-Ogos-2024, 15 Mac 2026
_DAY_MONTH_YEAR_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})[\s_\-]*({_MONTH_ALT})[\s_\-]*(20[2-3]\d)(?!\d)",
    flags=re.IGNORECASE,
)

# e.g., 1stockseptember2026, WaktuOperasiApril2024, SEP_2026, Julai_2024
_MONTH_YEAR_RE = re.compile(
    rf"({_MONTH_ALT})[\s_\-]*(20[2-3]\d)(?!\d)",
    flags=re.IGNORECASE,
)

# e.g., 2026-09-15 or 2026/09/15
_ISO_DATE_RE = re.compile(r"(?<!\d)(20[2-3]\d)[\-/](0[1-9]|1[0-2])[\-/](0[1-9]|[12]\d|3[01])(?!\d)")

# e.g., 08112026 (DDMMYYYY) in filenames like FINAL_-_HARI_DEEPAVALI_08112026.png
_DDMMYYYY_RE = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])(20[2-3]\d)(?!\d)")


def extract_date_from_text(text: str) -> str | None:
    """Return an ISO date (YYYY-MM-DD) or year-month (YYYY-MM) if explicitly present."""
    iso_m = _ISO_DATE_RE.search(text)
    if iso_m:
        return f"{iso_m.group(1)}-{iso_m.group(2)}-{iso_m.group(3)}"

    dmy_m = _DAY_MONTH_YEAR_RE.search(text)
    if dmy_m:
        day = int(dmy_m.group(1))
        month = _MONTHS[dmy_m.group(2).lower()]
        year = int(dmy_m.group(3))
        if 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"

    ddmm_m = _DDMMYYYY_RE.search(text)
    if ddmm_m:
        day = int(ddmm_m.group(1))
        month = int(ddmm_m.group(2))
        year = int(ddmm_m.group(3))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}-{day:02d}"

    my_m = _MONTH_YEAR_RE.search(text)
    if my_m:
        month = _MONTHS[my_m.group(1).lower()]
        year = int(my_m.group(2))
        return f"{year:04d}-{month:02d}"

    return None


def extract_source_effective_date(
    *,
    source_url: str,
    text_candidates: list[str] | None = None,
    text_provenance: str = "page_text",
) -> tuple[str | None, str | None]:
    """Extract source_effective_date with provenance ('filename' or text_provenance)."""
    filename = PurePosixPath(urlsplit(source_url).path).name
    if filename:
        from_filename = extract_date_from_text(filename)
        if from_filename:
            return from_filename, "filename"

    if text_candidates:
        for snippet in text_candidates:
            from_text = extract_date_from_text(snippet)
            if from_text:
                return from_text, text_provenance

    return None, None
