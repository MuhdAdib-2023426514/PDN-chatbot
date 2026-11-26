"""Deterministic offline Malay/English language tagging for chunks and documents."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-zA-Z]{2,}")

_MALAY_MARKERS: frozenset[str] = frozenset(
    {
        "yang",
        "dan",
        "untuk",
        "dengan",
        "darah",
        "penderma",
        "pendermaan",
        "derma",
        "pusat",
        "negara",
        "waktu",
        "operasi",
        "isnin",
        "selasa",
        "rabu",
        "khamis",
        "jumaat",
        "sabtu",
        "ahad",
        "pagi",
        "petang",
        "malam",
        "cuti",
        "umum",
        "tidak",
        "boleh",
        "sila",
        "anda",
        "atau",
        "pada",
        "dari",
        "daripada",
        "dalam",
        "kepada",
        "adalah",
        "akan",
        "hari",
        "bulan",
        "tahun",
        "kelayakan",
        "syarat",
        "talian",
        "utama",
        "hubungi",
        "kami",
        "alamat",
        "bekalan",
        "stok",
        "paras",
        "selamat",
        "kumpulan",
        "kaunter",
        "temujanji",
        "bahagian",
        "pengarah",
        "pegawai",
        "perubatan",
        "kementerian",
        "kesihatan",
        "malaysia",
        "maklumat",
        "soalan",
        "lazim",
        "kerana",
        "sebelum",
        "selepas",
        "berat",
        "badan",
        "umur",
        "berumur",
        "keistimewaan",
    }
)

_ENGLISH_MARKERS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "blood",
        "donor",
        "donors",
        "donation",
        "donate",
        "national",
        "centre",
        "center",
        "operating",
        "hours",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "everyday",
        "public",
        "holiday",
        "eligibility",
        "criteria",
        "deferral",
        "months",
        "weeks",
        "days",
        "years",
        "before",
        "after",
        "must",
        "should",
        "please",
        "contact",
        "address",
        "phone",
        "email",
        "guideline",
        "guidelines",
        "clinical",
        "transfusion",
        "practice",
        "patient",
        "patients",
        "procedure",
        "procedures",
        "needle",
        "tattoo",
        "piercing",
        "acupuncture",
        "safe",
        "supply",
        "level",
        "suite",
        "shopping",
        "basement",
        "health",
        "ministry",
        "service",
        "services",
        "department",
        "information",
        "frequently",
        "asked",
        "questions",
        "who",
        "what",
        "when",
        "where",
        "how",
        "are",
        "you",
        "your",
        "from",
        "this",
        "that",
        "have",
        "been",
    }
)


def detect_chunk_language(text: str, *, default: str = "ms") -> str:
    """Classify text deterministically as 'ms', 'en', or 'ms+en' (bilingual)."""
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    if not tokens:
        return default

    ms_hits = sum(1 for t in tokens if t in _MALAY_MARKERS)
    en_hits = sum(1 for t in tokens if t in _ENGLISH_MARKERS)

    if ms_hits == 0 and en_hits == 0:
        return default
    if ms_hits > 0 and en_hits == 0:
        return "ms"
    if en_hits > 0 and ms_hits == 0:
        return "en"

    total = ms_hits + en_hits
    ms_ratio = ms_hits / total
    en_ratio = en_hits / total
    if ms_ratio >= 0.25 and en_ratio >= 0.25 and min(ms_hits, en_hits) >= 2:
        return "ms+en"
    return "ms" if ms_hits >= en_hits else "en"


def language_filter_values(lang: str) -> list[str] | None:
    """Return allowed stored language values for API query filter ('ms', 'en', or 'any')."""
    normalized = lang.strip().lower()
    if normalized in {"", "any", "all"}:
        return None
    if normalized == "ms":
        return ["ms", "ms+en"]
    if normalized == "en":
        return ["en", "ms+en"]
    return [normalized]
