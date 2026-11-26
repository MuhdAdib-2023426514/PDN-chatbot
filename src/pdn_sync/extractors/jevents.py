"""Phase 4 dedicated Joomla JEvents blood donation schedule parser & normalizer."""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from pdn_sync.crawling.url_policy import is_in_allowed_scope, normalize_url
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.extractors.contracts import ExtractedTable, ExtractionResult
from pdn_sync.staging.semantic_hash import (
    SemanticStructuredEvent,
    SemanticTextBlock,
    normalize_semantic_text,
)

JEVENTS_EXTRACTOR_NAME = "jevents_html_bs4"
JEVENTS_EXTRACTOR_VERSION = "1.1.0"

_KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

_MONTH_NAME_MAP: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "januari": 1,
    "feb": 2,
    "february": 2,
    "februari": 2,
    "mar": 3,
    "march": 3,
    "mac": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "julai": 7,
    "aug": 8,
    "august": 8,
    "ogos": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "okt": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
    "dis": 12,
    "disember": 12,
}

_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b")
_DMY_WORD_DATE_RE = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])\s+([A-Za-z]+)\.?\s*,?\s*(20\d{2})\b",
    flags=re.IGNORECASE,
)
_MDY_WORD_DATE_RE = re.compile(
    r"\b([A-Za-z]+)\.?\s+(0?[1-9]|[12]\d|3[01])\s*,?\s*(20\d{2})\b",
    flags=re.IGNORECASE,
)
_DMY_NUM_DATE_RE = re.compile(r"\b(0?[1-9]|[12]\d|3[01])[/\-](0?[1-9]|1[0-2])[/\-](20\d{2})\b")
_URL_YMD_RE = re.compile(r"/(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/|$)")
_UPSTREAM_ID_RE = re.compile(r"/eventdetail/(\d+)(?:/|$)", flags=re.IGNORECASE)

# Handles spacing typos like "10.00 PAG I", "1.45 T/ HARI", "4.30 PTG", "9:00 AM"
_TIME_TOKEN_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[:.]\s*(\d{2}))?\s*"
    r"(p\s*a\s*g\s*i|p\s*g|a\s*\.?\s*m\.?|"
    r"t\s*/\s*h\s*a\s*r\s*i|t\s*e\s*n\s*g\s*a\s*h\s*a\s*r\s*i|t\s*g\s*h|n\s*o\s*o\s*n|"
    r"p\s*e\s*t\s*a\s*n\s*g|p\s*t\s*g|m\s*a\s*l\s*a\s*m|m\s*l\s*m|p\s*\.?\s*m\.?)?",
    flags=re.IGNORECASE,
)
_SESSION_SPLIT_RE = re.compile(
    r"(sesi\s*\d+|session\s*\d+)\s*[:\-\u2013\u2014]?\s*(.*?)(?=(?:sesi\s*\d+|session\s*\d+)|$)",
    flags=re.IGNORECASE | re.DOTALL,
)
_RANGE_SEP_RE = re.compile(
    r"\s*(?:-|\u2013|\u2014|\bhingga\b|\bsampai\b|\bto\b)\s*",
    flags=re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"\b(?:dibatalkan|batal|cancelled|canceled)\b",
    flags=re.IGNORECASE,
)
_POSTCODE_RE = re.compile(r"\b(\d{5})\b")

_EXPLICIT_STATES: tuple[tuple[str, str], ...] = (
    ("wilayah persekutuan kuala lumpur", "Kuala Lumpur"),
    ("w.p. kuala lumpur", "Kuala Lumpur"),
    ("wp kuala lumpur", "Kuala Lumpur"),
    ("kuala lumpur", "Kuala Lumpur"),
    ("wilayah persekutuan putrajaya", "Putrajaya"),
    ("w.p. putrajaya", "Putrajaya"),
    ("wp putrajaya", "Putrajaya"),
    ("putrajaya", "Putrajaya"),
    ("wilayah persekutuan labuan", "Labuan"),
    ("labuan", "Labuan"),
    ("negeri sembilan", "Negeri Sembilan"),
    ("n. sembilan", "Negeri Sembilan"),
    ("pulau pinang", "Pulau Pinang"),
    ("penang", "Pulau Pinang"),
    ("selangor", "Selangor"),
    ("melaka", "Melaka"),
    ("malacca", "Melaka"),
    ("johor", "Johor"),
    ("perak", "Perak"),
    ("pahang", "Pahang"),
    ("kedah", "Kedah"),
    ("perlis", "Perlis"),
    ("kelantan", "Kelantan"),
    ("terengganu", "Terengganu"),
    ("sabah", "Sabah"),
    ("sarawak", "Sarawak"),
)

_KNOWN_CITIES_TO_STATE: tuple[tuple[str, str, str], ...] = (
    ("petaling jaya", "Petaling Jaya", "Selangor"),
    ("shah alam", "Shah Alam", "Selangor"),
    ("subang jaya", "Subang Jaya", "Selangor"),
    ("subang", "Subang", "Selangor"),
    ("puchong", "Puchong", "Selangor"),
    ("cyberjaya", "Cyberjaya", "Selangor"),
    ("kajang", "Kajang", "Selangor"),
    ("bandar baru bangi", "Bangi", "Selangor"),
    ("bangi", "Bangi", "Selangor"),
    ("klang", "Klang", "Selangor"),
    ("ampang", "Ampang", "Selangor"),
    ("rawang", "Rawang", "Selangor"),
    ("gombak", "Gombak", "Selangor"),
    ("sepang", "Sepang", "Selangor"),
    ("seri kembangan", "Seri Kembangan", "Selangor"),
    ("damansara", "Damansara", "Selangor"),
    ("1 utama", "Petaling Jaya", "Selangor"),
    ("one utama", "Petaling Jaya", "Selangor"),
    ("sunway pyramid", "Subang Jaya", "Selangor"),
    ("sunway", "Subang Jaya", "Selangor"),
    ("batu caves", "Batu Caves", "Selangor"),
    ("selayang", "Selayang", "Selangor"),
    ("sungai buloh", "Sungai Buloh", "Selangor"),
    ("setia alam", "Shah Alam", "Selangor"),
    ("mid valley", "Kuala Lumpur", "Kuala Lumpur"),
    ("klcc", "Kuala Lumpur", "Kuala Lumpur"),
    ("bukit bintang", "Kuala Lumpur", "Kuala Lumpur"),
    ("wangsa maju", "Kuala Lumpur", "Kuala Lumpur"),
    ("setapak", "Kuala Lumpur", "Kuala Lumpur"),
    ("kepong", "Kuala Lumpur", "Kuala Lumpur"),
    ("bangsar", "Kuala Lumpur", "Kuala Lumpur"),
    ("sentul", "Kuala Lumpur", "Kuala Lumpur"),
    ("jalan tun razak", "Kuala Lumpur", "Kuala Lumpur"),
    ("ioi city mall", "Putrajaya", "Putrajaya"),
    ("presint ", "Putrajaya", "Putrajaya"),
    ("seremban", "Seremban", "Negeri Sembilan"),
    ("nilai", "Nilai", "Negeri Sembilan"),
    ("port dickson", "Port Dickson", "Negeri Sembilan"),
    ("ipoh", "Ipoh", "Perak"),
    ("kuantan", "Kuantan", "Pahang"),
    ("bentong", "Bentong", "Pahang"),
    ("johor bahru", "Johor Bahru", "Johor"),
    ("kota kinabalu", "Kota Kinabalu", "Sabah"),
    ("kuching", "Kuching", "Sarawak"),
)


def build_monthcalendar_urls(
    now_kl: datetime | None = None,
    *,
    months_ahead: int = 3,
    base_url: str = "https://pdn.gov.my/v2",
) -> list[tuple[int, int, str]]:
    """Build bounded JEvents monthcalendar URLs for current + next N months (Asia/Kuala_Lumpur)."""
    ref = now_kl.astimezone(_KL_TZ) if now_kl is not None else datetime.now(_KL_TZ)
    base = base_url.rstrip("/")
    out: list[tuple[int, int, str]] = []
    year, month = ref.year, ref.month
    for offset in range(months_ahead + 1):
        m_total = (month - 1) + offset
        y = year + (m_total // 12)
        m = (m_total % 12) + 1
        out.append((y, m, f"{base}/index.php/component/jevents/monthcalendar/{y}/{m:02d}/-"))
    return out


def extract_upstream_event_id(url: str) -> str:
    """Extract numeric JEvents upstream event ID from an eventdetail URL."""
    m = _UPSTREAM_ID_RE.search(urlsplit(url).path)
    if m:
        return m.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def extract_eventdetail_links_from_monthcalendar(
    raw_html: bytes | str,
    *,
    page_url: str,
    allowed_host: str = "pdn.gov.my",
    allowed_path_prefix: str = "/v2",
) -> list[str]:
    """Extract deduplicated canonical eventdetail URLs from a JEvents monthcalendar HTML page."""
    soup = BeautifulSoup(raw_html, "lxml")
    seen_event_ids: set[str] = set()
    urls: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        joined = urljoin(page_url, href)
        try:
            normalized = normalize_url(
                joined,
                allowed_host=allowed_host,
                allowed_path_prefix=allowed_path_prefix,
            )
        except Exception:
            continue
        if not is_in_allowed_scope(
            normalized,
            allowed_host=allowed_host,
            allowed_path_prefix=allowed_path_prefix,
        ):
            continue
        path_l = urlsplit(normalized).path.lower()
        if "/index.php/component/jevents/eventdetail/" not in path_l:
            continue
        ev_id = extract_upstream_event_id(normalized)
        if ev_id in seen_event_ids:
            continue
        seen_event_ids.add(ev_id)
        urls.append(normalized)

    return urls


def normalize_malay_time(raw: str | None) -> str | None:
    """Normalize Malay/English time strings (handling spacing typos) to 24h 'HH:MM'."""
    if not raw:
        return None
    cleaned = normalize_semantic_text(raw)
    if not cleaned:
        return None
    # Repair spaced two-digit hours/minutes like "1 1 .00 PAGI" or "1 0 .00 PAGI"
    cleaned = re.sub(r"(?<=\d)\s+(?=\d)", "", cleaned)

    m = _TIME_TOKEN_RE.search(cleaned)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) is not None else 0
    meridiem_raw = (m.group(3) or "").lower()
    meridiem = re.sub(r"[\s.]", "", meridiem_raw)

    if minute > 59:
        return None

    if meridiem in {"pagi", "pg", "am"}:
        if hour > 12:
            return None
        if hour == 12:
            hour = 0
    elif meridiem in {"t/hari", "thari", "tgh", "tengahari", "noon"}:
        if hour > 12:
            return None
        if 1 <= hour <= 6:
            hour += 12
    elif meridiem in {"petang", "ptg", "malam", "mlm", "pm"}:
        if hour > 12:
            return None
        if 1 <= hour <= 11:
            hour += 12
    else:
        if hour > 23:
            return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_time_range(raw: str) -> tuple[str | None, str | None]:
    parts = _RANGE_SEP_RE.split(normalize_semantic_text(raw), maxsplit=1)
    if len(parts) != 2:
        t = normalize_malay_time(raw)
        return t, None

    left_raw, right_raw = parts[0].strip(), parts[1].strip()
    t_end = normalize_malay_time(right_raw)
    t_start = normalize_malay_time(left_raw)

    # If left lacks meridiem (e.g. "2.30 - 5.00 PETANG"), disambiguate using end hour
    if t_start and t_end:
        left_has_meridiem = bool(
            re.search(r"pag|pg|am|t\s*/?h|tgh|noon|pet|ptg|mal|mlm|pm", left_raw, re.IGNORECASE)
        )
        if not left_has_meridiem:
            sh, sm = (int(x) for x in t_start.split(":"))
            eh, _ = (int(x) for x in t_end.split(":"))
            if eh >= 13 and 1 <= sh <= 7 and sh + 12 <= eh:
                t_start = f"{sh + 12:02d}:{sm:02d}"
    return t_start, t_end


def parse_session_cells(
    mula_raw: str,
    tamat_raw: str,
) -> list[tuple[str | None, str | None, str | None]]:
    """Parse MULA/TAMAT cells into one or more (session_label, time_start, time_end) tuples.

    Handles both single-session rows and PDN's multi-session layout where MULA holds
    'SESI 1: 10.00 PAG I - 1.45 T/ HARI' and TAMAT holds 'SESI 2: 2.30 PTG - 5.00 PTG'.
    """
    combined_sessions: list[tuple[str | None, str | None, str | None]] = []
    for cell in (mula_raw, tamat_raw):
        matches = list(_SESSION_SPLIT_RE.finditer(cell or ""))
        if matches:
            for match in matches:
                lbl = re.sub(r"\s+", " ", match.group(1).strip()).upper()
                body = match.group(2).strip()
                t_start, t_end = _parse_time_range(body)
                combined_sessions.append((lbl, t_start, t_end))

    if combined_sessions:
        return combined_sessions

    # Standard single-session row (also check if MULA itself contains a full range)
    mula_clean = normalize_semantic_text(mula_raw or "")
    tamat_clean = normalize_semantic_text(tamat_raw or "")
    if mula_clean and not tamat_clean and _RANGE_SEP_RE.search(mula_clean):
        ts, te = _parse_time_range(mula_clean)
        return [(None, ts, te)]

    ts = normalize_malay_time(mula_clean)
    te = normalize_malay_time(tamat_clean)
    return [(None, ts, te)]


def normalize_target_donors(raw: str | None) -> int | None:
    """Repair spacing typos inside target donor integers (e.g., '7 0' -> 70, '1 5 0' -> 150)."""
    if not raw:
        return None
    cleaned = normalize_semantic_text(raw)
    if not cleaned:
        return None
    # Collapse spaces strictly between digits ("7 0" -> "70", "1 5 0" -> "150")
    repaired = re.sub(r"(?<=\d)\s+(?=\d)", "", cleaned)
    m = re.search(r"\b(\d{1,5})\b", repaired)
    if not m:
        return None
    val = int(m.group(1))
    if val <= 0 or val > 50000:
        return None
    return val


def parse_malay_or_iso_date(text: str) -> str | None:
    """Parse an explicit YYYY-MM-DD date from Malay/English or numeric date strings."""
    cleaned = normalize_semantic_text(text)
    if not cleaned:
        return None

    m_iso = _ISO_DATE_RE.search(cleaned)
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return datetime(y, mo, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    for m_word in _DMY_WORD_DATE_RE.finditer(cleaned):
        d = int(m_word.group(1))
        mo_name = m_word.group(2).lower().rstrip(".")
        y = int(m_word.group(3))
        mo_opt = _MONTH_NAME_MAP.get(mo_name)
        if mo_opt is not None:
            try:
                return datetime(y, mo_opt, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")
            except ValueError:
                continue

    for m_word in _MDY_WORD_DATE_RE.finditer(cleaned):
        mo_name = m_word.group(1).lower().rstrip(".")
        d = int(m_word.group(2))
        y = int(m_word.group(3))
        mo_opt = _MONTH_NAME_MAP.get(mo_name)
        if mo_opt is not None:
            try:
                return datetime(y, mo_opt, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")
            except ValueError:
                continue

    m_num = _DMY_NUM_DATE_RE.search(cleaned)
    if m_num:
        d, mo, y = int(m_num.group(1)), int(m_num.group(2)), int(m_num.group(3))
        try:
            return datetime(y, mo, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def parse_all_dates_in_text(text: str) -> list[str]:
    """Return ordered deduplicated YYYY-MM-DD dates found in text (preserving appearance order)."""
    cleaned = normalize_semantic_text(text)
    if not cleaned:
        return []

    found: list[tuple[int, str]] = []
    for m_iso in _ISO_DATE_RE.finditer(cleaned):
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        with contextlib.suppress(ValueError):
            found.append((m_iso.start(), datetime(y, mo, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")))
    for m_word in _DMY_WORD_DATE_RE.finditer(cleaned):
        d = int(m_word.group(1))
        mo_opt = _MONTH_NAME_MAP.get(m_word.group(2).lower().rstrip("."))
        y = int(m_word.group(3))
        if mo_opt is not None:
            with contextlib.suppress(ValueError):
                found.append(
                    (m_word.start(), datetime(y, mo_opt, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d"))
                )
    for m_word in _MDY_WORD_DATE_RE.finditer(cleaned):
        mo_opt = _MONTH_NAME_MAP.get(m_word.group(1).lower().rstrip("."))
        d = int(m_word.group(2))
        y = int(m_word.group(3))
        if mo_opt is not None:
            with contextlib.suppress(ValueError):
                found.append(
                    (m_word.start(), datetime(y, mo_opt, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d"))
                )
    for m_num in _DMY_NUM_DATE_RE.finditer(cleaned):
        d, mo, y = int(m_num.group(1)), int(m_num.group(2)), int(m_num.group(3))
        with contextlib.suppress(ValueError):
            found.append((m_num.start(), datetime(y, mo, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")))

    found.sort(key=lambda pair: pair[0])
    out: list[str] = []
    for _, ds in found:
        if ds not in out:
            out.append(ds)
    return out


def extract_event_dates(soup: BeautifulSoup, *, source_url: str) -> list[str]:
    """Extract authoritative event date(s) (expanding bounded <=7-day From..To ranges)."""
    # 1. Authoritative JEvents calendar summary container (.jev_evdt_summary) first
    for el in soup.select(".jev_evdt_summary"):
        txt = el.get_text(" ", strip=True)
        dates = parse_all_dates_in_text(txt)
        if len(dates) >= 2 and re.search(r"\bfrom\b.*\bto\b", txt, flags=re.IGNORECASE):
            d_start = datetime.strptime(dates[0], "%Y-%m-%d")
            d_end = datetime.strptime(dates[1], "%Y-%m-%d")
            span = (d_end - d_start).days
            if 0 <= span <= 7:
                return [(d_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span + 1)]
        if dates:
            return [dates[0]]

    # 2. Explicit <time datetime="...">
    for time_tag in soup.find_all("time"):
        dt_attr = str(time_tag.get("datetime") or "")
        parsed = parse_malay_or_iso_date(dt_attr) or parse_malay_or_iso_date(time_tag.get_text(" "))
        if parsed:
            return [parsed]

    # 3. Other JEvents header/date containers
    selectors = (
        ".ev_detail",
        ".jev_evdt_header",
        ".jev_evdt_desc",
        ".contentheading",
        "h1",
        "h2",
        "title",
    )
    for sel in selectors:
        for el in soup.select(sel):
            parsed = parse_malay_or_iso_date(el.get_text(" ", strip=True))
            if parsed:
                return [parsed]

    # 4. URL /YYYY/MM/DD segment if present
    m_url = _URL_YMD_RE.search(urlsplit(source_url).path)
    if m_url:
        y, mo, d = int(m_url.group(1)), int(m_url.group(2)), int(m_url.group(3))
        try:
            return [datetime(y, mo, d, tzinfo=_KL_TZ).strftime("%Y-%m-%d")]
        except ValueError:
            pass

    # 5. Fallback to top 1000 chars of main text
    main = soup.find("main") or soup.find(id="jevents_body") or soup.body
    if main is not None:
        parsed = parse_malay_or_iso_date(main.get_text(" ", strip=True)[:1000])
        if parsed:
            return [parsed]
    return []


def extract_event_date(soup: BeautifulSoup, *, source_url: str) -> str | None:
    """Extract primary event_date (YYYY-MM-DD, Asia/Kuala_Lumpur) from an eventdetail page."""
    dates = extract_event_dates(soup, source_url=source_url)
    return dates[0] if dates else None


def _state_from_postcode(postcode: int) -> str | None:
    if 50000 <= postcode <= 60000:
        return "Kuala Lumpur"
    if 62000 <= postcode <= 62999:
        return "Putrajaya"
    if (40000 <= postcode <= 48999) or (63000 <= postcode <= 68100):
        return "Selangor"
    if 70000 <= postcode <= 73999:
        return "Negeri Sembilan"
    if 75000 <= postcode <= 78999:
        return "Melaka"
    if 79000 <= postcode <= 86999:
        return "Johor"
    if 30000 <= postcode <= 36810:
        return "Perak"
    if 25000 <= postcode <= 28800 or 39000 <= postcode <= 39200 or 49000 <= postcode <= 49999:
        return "Pahang"
    if 10000 <= postcode <= 14400:
        return "Pulau Pinang"
    if 5000 <= postcode <= 9810:
        return "Kedah"
    if 1000 <= postcode <= 2800:
        return "Perlis"
    if 15000 <= postcode <= 18500:
        return "Kelantan"
    if 20000 <= postcode <= 24300:
        return "Terengganu"
    if 87000 <= postcode <= 87033:
        return "Labuan"
    if 88000 <= postcode <= 91309:
        return "Sabah"
    if 93000 <= postcode <= 98859:
        return "Sarawak"
    return None


def infer_malaysia_state_city(text: str | None) -> tuple[str | None, str | None]:
    """Deterministically infer Malaysian (state, city) ONLY from unambiguous explicit cues."""
    if not text:
        return None, None
    lower = f" {text.lower()} "

    found_state: str | None = None
    found_city: str | None = None

    for key, canonical_state in _EXPLICIT_STATES:
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", lower):
            found_state = canonical_state
            break

    for key, canonical_city, mapped_state in _KNOWN_CITIES_TO_STATE:
        if key in lower:
            found_city = canonical_city
            if found_state is None:
                found_state = mapped_state
            break

    if found_state is None:
        for m_pc in _POSTCODE_RE.finditer(text):
            st = _state_from_postcode(int(m_pc.group(1)))
            if st is not None:
                found_state = st
                break

    return found_state, found_city


def normalize_identity_token(value: str | None) -> str:
    """Normalize organizer/location/session tokens for deterministic stable identities."""
    cleaned = normalize_semantic_text(value or "").lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned).strip()
    return cleaned


def make_session_identity(
    *,
    upstream_event_id: str,
    event_date: str | None,
    location_name: str | None,
    session_label: str | None,
) -> str:
    """Stable occurrence identity based on upstream ID + date + normalized location + session."""
    norm_loc = normalize_identity_token(location_name)
    norm_ses = normalize_identity_token(session_label or "default")
    seed = f"{upstream_event_id}|{event_date or 'unknown'}|{norm_loc}|{norm_ses}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _classify_column_header(header_text: str) -> str | None:
    h = normalize_semantic_text(header_text).lower()
    if any(k in h for k in ("sasaran", "target", "kuota")) or re.search(r"\b(?:penderma|beg)\b", h):
        return "target"
    if any(k in h for k in ("penganjur", "organizer", "organiser", "anjuran")):
        return "organizer"
    if any(k in h for k in ("lokasi", "location", "tempat", "venue")):
        return "location"
    if any(k in h for k in ("alamat", "address")):
        return "address"
    if any(k in h for k in ("negeri",)) or re.search(r"\bstate\b", h):
        return "state"
    if any(k in h for k in ("bandar", "daerah")) or re.search(r"\bcity\b", h):
        return "city"
    if any(k in h for k in ("tarikh",)) or re.search(r"\bdate\b", h):
        return "date"
    if any(k in h for k in ("mula", "sesi 1")) or re.search(r"\bstart\b", h):
        return "start"
    if any(k in h for k in ("tamat", "akhir", "sesi 2")) or re.search(r"\bend\b", h):
        return "end"
    if any(k in h for k in ("masa", "waktu")) or re.search(r"\btime\b", h):
        return "time"
    return None


def _is_schedule_table(headers: list[str]) -> bool:
    roles = {_classify_column_header(h) for h in headers}
    return "location" in roles and bool(roles & {"start", "end", "time", "organizer"})


def _extract_table_rows(table: Tag) -> tuple[list[str], list[list[str]]]:
    all_tr = table.find_all("tr")
    if not all_tr:
        return [], []
    headers: list[str] = []
    data_rows: list[list[str]] = []

    for idx, tr in enumerate(all_tr):
        cells = [
            normalize_semantic_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])
        ]
        if not any(cells):
            continue
        if idx == 0 and (tr.find("th") is not None or _is_schedule_table(cells)):
            headers = cells
            continue
        if not headers and _is_schedule_table(cells):
            headers = cells
            continue
        data_rows.append(cells)

    return headers, data_rows


def extract_jevents_eventdetail(
    *,
    raw_html: bytes | str,
    source_url: str,
    resource_id: str,
    resource_version_id: str,
    content_hash: str,
    captured_at_utc: datetime,
    policy_rule_id: str | None = None,
    month_seed_url: str | None = None,
    content_type: str | None = "text/html",
) -> ExtractionResult:
    """Parse a JEvents eventdetail HTML page into deterministic structured donation sessions."""
    soup = BeautifulSoup(raw_html, "lxml")
    for junk in soup.find_all(["script", "style", "noscript"]):
        junk.decompose()

    upstream_event_id = extract_upstream_event_id(source_url)

    title_el = (
        soup.select_one(".jev_evdt_title")
        or soup.select_one(".jev_evdt_header h2")
        or soup.select_one(".ev_detail h2")
        or soup.select_one(".contentheading")
        or soup.find("h1")
        or soup.find("h2")
        or soup.find("title")
    )
    title = normalize_semantic_text(title_el.get_text(" ", strip=True)) if title_el else None
    page_event_dates = extract_event_dates(soup, source_url=source_url)
    page_event_date = page_event_dates[0] if page_event_dates else None
    page_canceled = bool(_CANCEL_RE.search(title or ""))

    root_cand = soup.find(id="jevents_body") or soup.find("main") or soup.body
    root: Tag | BeautifulSoup = root_cand if isinstance(root_cand, Tag) else soup
    tables: list[ExtractedTable] = []
    events: list[SemanticStructuredEvent] = []
    warnings: list[str] = []
    canceled_identities: list[str] = []

    if page_event_date is None:
        warnings.append("unparseable_event_date")

    table_idx = 0
    for tbl in root.find_all("table"):
        headers, rows = _extract_table_rows(tbl)
        if not headers or not rows or not _is_schedule_table(headers):
            continue

        tables.append(
            ExtractedTable(
                table_index=table_idx,
                caption=title,
                headers=headers,
                rows=rows,
                units=[None] * len(headers),
                source_selector="jevents_table",
            )
        )
        table_idx += 1

        col_roles = [_classify_column_header(h) for h in headers]

        for row in rows:
            padded = list(row) + [""] * max(0, len(headers) - len(row))
            raw_cells = {headers[i]: padded[i] for i in range(len(headers)) if headers[i].strip()}
            role_vals: dict[str, str] = {}
            for i, role in enumerate(col_roles):
                if role and padded[i].strip():
                    role_vals[role] = padded[i].strip()

            organizer = role_vals.get("organizer") or None
            location_name = role_vals.get("location") or None
            address = role_vals.get("address") or None
            explicit_row_date = parse_malay_or_iso_date(role_vals.get("date", ""))
            row_dates: list[str | None] = (
                [explicit_row_date]
                if explicit_row_date
                else (list(page_event_dates) if page_event_dates else [None])
            )
            target_donors = normalize_target_donors(role_vals.get("target"))

            if not location_name:
                if "missing_location_name" not in warnings:
                    warnings.append("missing_location_name")
                continue

            # Explicit state/city columns take precedence over conservative inference
            explicit_state = role_vals.get("state") or None
            explicit_city = role_vals.get("city") or None
            inferred_state, inferred_city = infer_malaysia_state_city(
                " ".join(filter(None, [location_name, address, title]))
            )
            state = explicit_state or inferred_state
            city = explicit_city or inferred_city

            mula_raw = role_vals.get("start") or role_vals.get("time") or ""
            tamat_raw = role_vals.get("end") or ""
            sessions = parse_session_cells(mula_raw, tamat_raw)

            row_text_all = " ".join(padded)
            is_canceled = page_canceled or bool(_CANCEL_RE.search(row_text_all))

            for row_date in row_dates:
                for session_label, t_start, t_end in sessions:
                    if (
                        (mula_raw or tamat_raw)
                        and t_start is None
                        and t_end is None
                        and "unparseable_time_range" not in warnings
                    ):
                        warnings.append("unparseable_time_range")

                    ev = SemanticStructuredEvent(
                        organizer=organizer,
                        location_name=location_name,
                        address=address,
                        city=city,
                        state=state,
                        event_date=row_date,
                        time_start=t_start,
                        time_end=t_end,
                        session_label=session_label,
                        target_donors=target_donors,
                        raw_cells=raw_cells,
                    )
                    events.append(ev)
                    if is_canceled:
                        canceled_identities.append(
                            make_session_identity(
                                upstream_event_id=upstream_event_id,
                                event_date=row_date,
                                location_name=location_name,
                                session_label=session_label,
                            )
                        )

    if not events:
        warnings.append("empty_schedule_events")

    text_blocks: list[SemanticTextBlock] = []
    if title:
        text_blocks.append(SemanticTextBlock(order=0, heading_level=1, text=title))
    if page_event_date:
        text_blocks.append(
            SemanticTextBlock(
                order=len(text_blocks),
                heading_level=None,
                text=f"Tarikh Kempen: {page_event_date} | Jumlah Sesi: {len(events)}",
            )
        )

    now = datetime.now(UTC)
    draft = ExtractionResult(
        artifact_id=f"art_{uuid.uuid4().hex[:16]}",
        resource_id=resource_id,
        resource_version_id=resource_version_id,
        source_url=source_url,
        resource_type=ResourceType.PAGE,
        category=DiscoveryCategory.DONATION_SCHEDULE,
        policy_rule_id=policy_rule_id,
        content_hash=content_hash,
        semantic_hash="",
        extractor_name=JEVENTS_EXTRACTOR_NAME,
        extractor_version=JEVENTS_EXTRACTOR_VERSION,
        captured_at_utc=captured_at_utc,
        extracted_at_utc=now,
        source_effective_date=page_event_date,
        source_effective_date_provenance="jevents_page_date" if page_event_date else None,
        content_type=content_type,
        title=title,
        language="ms",
        text_blocks=text_blocks,
        tables=tables,
        structured_events=events,
        warnings=warnings,
        provenance={
            "upstream_event_id": upstream_event_id,
            "month_seed_url": month_seed_url,
            "explicit_cancellation_identities": canceled_identities,
        },
    )
    return draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})
