"""Unit tests for Phase 4 JEvents parser, Malay time/typo normalization, and month bounds."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pdn_sync.domain.policy import IngestionPolicyConfig
from pdn_sync.extractors.contracts import ReviewStatus, ValidationStatus
from pdn_sync.extractors.jevents import (
    build_monthcalendar_urls,
    extract_eventdetail_links_from_monthcalendar,
    extract_jevents_eventdetail,
    infer_malaysia_state_city,
    make_session_identity,
    normalize_malay_time,
    normalize_target_donors,
    parse_session_cells,
)
from pdn_sync.staging.validator import validate_and_tag

_SAMPLE_MULTI_LOCATION_AND_MULTI_SESSION_HTML = """\
<html>
<head><title>Kempen Derma Darah 28 November 2026</title></head>
<body>
<main id="jevents_body">
  <div class="ev_detail">
    <h2>Jadual Kempen Derma Darah Bergerak - 28 November 2026</h2>
  </div>
  <table>
    <tr>
      <th>PENGANJUR</th>
      <th>LOKASI DERMA DARAH</th>
      <th>MULA</th>
      <th>TAMAT</th>
      <th>SASARAN PENDERMA</th>
    </tr>
    <tr>
      <td>Pusat Darah Negara</td>
      <td>Mid Valley Megamall, Kuala Lumpur</td>
      <td>10.00 PAG I</td>
      <td>4.30 PTG</td>
      <td>1 5 0</td>
    </tr>
    <tr>
      <td>St John Ambulans Malaysia</td>
      <td>Dewan Serbaguna, 1 Utama Shopping Centre, Petaling Jaya</td>
      <td>SESI 1: 10.00 PAG I - 1.45 T/ HARI</td>
      <td>SESI 2: 2.30 PETANG - 5.00 PETANG</td>
      <td>7 0</td>
    </tr>
    <tr>
      <td>Persatuan Belia</td>
      <td>Dewan Komuniti ABC</td>
      <td>9.00 PAGI</td>
      <td>3.00 PETANG</td>
      <td>80 orang</td>
    </tr>
  </table>
</main>
</body>
</html>
"""


def test_build_monthcalendar_urls_handles_year_rollover() -> None:
    now_kl = datetime(2026, 11, 26, 15, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    urls = build_monthcalendar_urls(now_kl, months_ahead=3)
    assert [(y, m) for y, m, _ in urls] == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]
    assert urls[0][2] == "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-"
    assert urls[2][2] == "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2027/01/-"


def test_extract_eventdetail_links_filters_non_target_views() -> None:
    html = """
    <a href="/v2/index.php/component/jevents/eventdetail/501/-/kempen-a">Event A</a>
    <a href="/v2/index.php/component/jevents/eventdetail/501/-/kempen-a?Itemid=99">Event A dup</a>
    <a href="/v2/index.php/component/jevents/eventdetail/502/-/kempen-b">Event B</a>
    <a href="/v2/index.php/component/jevents/eventsbyweek/2026/11/24/-">Week View</a>
    <a href="/v2/index.php/component/jevents/monthcalendar/2028/01/-">Far Future</a>
    <a href="https://evil.example.org/v2/index.php/component/jevents/eventdetail/999/-">Off</a>
    """
    links = extract_eventdetail_links_from_monthcalendar(
        html,
        page_url="https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-",
    )
    assert links == [
        "https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/501/-/kempen-a",
        "https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/502/-/kempen-b",
    ]


def test_malay_time_and_target_typo_normalization() -> None:
    assert normalize_malay_time("10.00 PAG I") == "10:00"
    assert normalize_malay_time("1 1 .00 PAGI") == "11:00"
    assert normalize_malay_time("1 0 .00 PAGI") == "10:00"
    assert normalize_malay_time("1.45 T/ HARI") == "13:45"
    assert normalize_malay_time("12.00 TGH") == "12:00"
    assert normalize_malay_time("4.30 PTG") == "16:30"
    assert normalize_malay_time("9.00 MLM") == "21:00"
    assert normalize_malay_time("12.15 AM") == "00:15"
    assert normalize_malay_time("invalid time") is None

    assert normalize_target_donors("7 0") == 70
    assert normalize_target_donors("1 5 0") == 150
    assert normalize_target_donors("100 orang") == 100
    assert normalize_target_donors("TBD") is None

    sessions = parse_session_cells(
        "SESI 1: 10.00 PAG I - 1.45 T/ HARI",
        "SESI 2: 2.30 - 5.00 PETANG",
    )
    assert sessions == [
        ("SESI 1", "10:00", "13:45"),
        ("SESI 2", "14:30", "17:00"),
    ]


def test_conservative_malaysia_state_city_inference() -> None:
    assert infer_malaysia_state_city("Mid Valley Megamall, Kuala Lumpur") == (
        "Kuala Lumpur",
        "Kuala Lumpur",
    )
    assert infer_malaysia_state_city("1 Utama Shopping Centre, Petaling Jaya") == (
        "Selangor",
        "Petaling Jaya",
    )
    assert infer_malaysia_state_city("IOI City Mall, 62502") == ("Putrajaya", "Putrajaya")
    # Never hallucinate when location gives no unambiguous Malaysian geographic cue
    assert infer_malaysia_state_city("Dewan Komuniti ABC") == (None, None)


def test_extract_jevents_multi_location_multi_session_preserves_raw_cells() -> None:
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    res = extract_jevents_eventdetail(
        raw_html=_SAMPLE_MULTI_LOCATION_AND_MULTI_SESSION_HTML,
        source_url="https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/777/-/kempen",
        resource_id="res_jev_777",
        resource_version_id="ver_jev_777_001",
        content_hash="a" * 64,
        captured_at_utc=now,
    )
    validated = validate_and_tag(res, IngestionPolicyConfig())
    assert validated.validation_status == ValidationStatus.VALID
    assert validated.review_status == ReviewStatus.PENDING_REVIEW
    assert validated.source_effective_date == "2026-11-28"
    # 1 session for Mid Valley + 2 sessions for 1 Utama + 1 session for Dewan Komuniti ABC = 4
    assert len(validated.structured_events) == 4

    mv = validated.structured_events[0]
    assert mv.event_date == "2026-11-28"
    assert mv.time_start == "10:00"
    assert mv.time_end == "16:30"
    assert mv.target_donors == 150
    assert mv.state == "Kuala Lumpur"
    assert mv.raw_cells["MULA"] == "10.00 PAG I"
    assert mv.raw_cells["SASARAN PENDERMA"] == "1 5 0"

    s1, s2 = validated.structured_events[1], validated.structured_events[2]
    assert (s1.session_label, s1.time_start, s1.time_end, s1.target_donors, s1.state) == (
        "SESI 1",
        "10:00",
        "13:45",
        70,
        "Selangor",
    )
    assert (s2.session_label, s2.time_start, s2.time_end, s2.target_donors) == (
        "SESI 2",
        "14:30",
        "17:00",
        70,
    )
    assert s1.raw_cells["MULA"] == "SESI 1: 10.00 PAG I - 1.45 T/ HARI"
    assert s2.raw_cells["TAMAT"] == "SESI 2: 2.30 PETANG - 5.00 PETANG"

    ambig = validated.structured_events[3]
    assert ambig.state is None
    assert ambig.city is None

    # Stable identity is independent of row order
    id_a = make_session_identity(
        upstream_event_id="777",
        event_date="2026-11-28",
        location_name="Mid Valley Megamall, Kuala Lumpur",
        session_label=None,
    )
    id_b = make_session_identity(
        upstream_event_id="777",
        event_date="2026-11-28",
        location_name="  mid valley megamall, kuala lumpur ",
        session_label=None,
    )
    assert id_a == id_b


def test_malformed_schedule_without_date_auto_quarantines() -> None:
    bad_html = """
    <html><body><main>
      <table>
        <tr><th>PENGANJUR</th><th>LOKASI</th><th>MULA</th><th>TAMAT</th></tr>
        <tr><td>PDN</td><td>Dewan X</td><td>TBA</td><td>TBA</td></tr>
      </table>
    </main></body></html>
    """
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    res = extract_jevents_eventdetail(
        raw_html=bad_html,
        source_url="https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/888/-/bad",
        resource_id="res_bad",
        resource_version_id="ver_bad_001",
        content_hash="b" * 64,
        captured_at_utc=now,
    )
    validated = validate_and_tag(res, IngestionPolicyConfig())
    assert "unparseable_event_date" in validated.warnings
    assert "unparseable_time_range" in validated.warnings
    assert validated.validation_status == ValidationStatus.NEEDS_REVIEW
    assert validated.review_status == ReviewStatus.QUARANTINED


def test_jevents_summary_date_overrides_body_typo_and_expands_from_to_range() -> None:
    html_range = """
    <html><body>
      <div class="jev_evdt_title">PUSAT PENDERMAAN STATIK</div>
      <div class="jev_evdt_summary">
        From Saturday 31 October 2026<br/>To Sunday 01 November 2026
      </div>
      <h2>( AHAD - 28 OCTOBER 2026 )</h2>
      <table>
        <tr><th>PENGANJUR</th><th>LOKASI DERMA DARAH</th><th>MULA</th><th>TAMAT</th></tr>
        <tr>
          <td>PDN</td><td>Jalan Tun Razak, Kuala Lumpur</td>
          <td>8 .00 PAGI</td><td>4 .00 PETANG</td>
        </tr>
      </table>
    </body></html>
    """
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    res = extract_jevents_eventdetail(
        raw_html=html_range,
        source_url="https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/2780/-/statik",
        resource_id="res_2780",
        resource_version_id="ver_2780_001",
        content_hash="c" * 64,
        captured_at_utc=now,
    )
    assert res.title == "PUSAT PENDERMAAN STATIK"
    assert [ev.event_date for ev in res.structured_events] == ["2026-10-31", "2026-11-01"]
    assert all(ev.time_start == "08:00" and ev.time_end == "16:00" for ev in res.structured_events)
