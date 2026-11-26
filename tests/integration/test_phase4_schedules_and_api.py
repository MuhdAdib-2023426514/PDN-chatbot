"""Integration tests for Phase 4 JEvents sync, Gate B, SQLite/FTS5, and /v1/events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from pdn_sync.api.app import create_app
from pdn_sync.config import AppSettings
from pdn_sync.indexing.embedder import DeterministicHashEmbedder
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.models import DonationSessionRow
from pdn_sync.persistence.staging_repo import StageQueueFilter
from pdn_sync.staging.calendar_sync import CalendarSyncOptions, run_calendar_sync_pipeline
from pdn_sync.staging.gate_b import GateBService

_PHASE4_SCOPE_YAML = """\
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns:
    - id: exclude-jevents-eventsbyweek
      pattern: /v2/index.php/component/jevents/eventsbyweek/**
      reason: non_target_jevents_view
    - id: exclude-jevents-query
      query_param: option=com_jevents
      reason: non_target_jevents_view
approvals:
  pages:
    urls: []
    patterns:
      - id: pat-jevents-monthcalendar
        pattern: /v2/index.php/component/jevents/monthcalendar/*/*/*
        resource_type: page
        reason: bounded_calendar_seed
        max_matches: 12
        check_interval: 6h
        supersession: none
        approved_by: owner
        approved_at: "2026-11-26T00:00:00Z"
      - id: pat-jevents-eventdetail
        pattern: /v2/index.php/component/jevents/eventdetail/*/*/*
        resource_type: page
        reason: bounded_eventdetail
        max_matches: 250
        check_interval: 6h
        supersession: none
        approved_by: owner
        approved_at: "2026-11-26T00:00:00Z"
  image_posters:
    urls: []
    patterns: []
  documents:
    urls: []
    patterns: []
"""

_PHASE4_POLICY_YAML = """\
schema_version: 1
approval_mode: manual
retirement_consecutive_runs: 2
domain_timezone: Asia/Kuala_Lumpur
indexing:
  small_table_max_rows_for_qdrant: 30
  embed_donation_schedule_rows: false
  embedding_model: deterministic-hash-384
calendar:
  months_ahead: 1
  max_eventdetail_pages_per_run: 50
  calendar_interval_hours: 6
  allow_past_days_grace: 1
"""

_MONTH_NOV_HTML = """\
<html><body>
  <a href="/v2/index.php/component/jevents/eventdetail/901/-/kempen-nov">28 Nov</a>
  <a href="/v2/index.php/component/jevents/eventsbyweek/2026/11/24/-">Week Excluded</a>
</body></html>
"""

_MONTH_DEC_HTML = """\
<html><body>
  <a href="/v2/index.php/component/jevents/eventdetail/902/-/kempen-dec">05 Dis</a>
</body></html>
"""

_EVENT_901_V1_HTML = """\
<html><body><main id="jevents_body">
  <div class="ev_detail"><h2>Kempen Derma Darah - 28 November 2026</h2></div>
  <table>
    <tr>
      <th>PENGANJUR</th><th>LOKASI DERMA DARAH</th>
      <th>MULA</th><th>TAMAT</th><th>SASARAN PENDERMA</th>
    </tr>
    <tr>
      <td>Pusat Darah Negara</td>
      <td>Mid Valley Megamall, Kuala Lumpur</td>
      <td>10.00 PAG I</td>
      <td>5.00 PTG</td>
      <td>1 5 0</td>
    </tr>
    <tr>
      <td>St John Ambulans</td>
      <td>1 Utama Shopping Centre, Petaling Jaya, Selangor</td>
      <td>SESI 1: 10.00 PAGI - 1.45 T/ HARI</td>
      <td>SESI 2: 2.30 PTG - 6.00 PTG</td>
      <td>7 0</td>
    </tr>
  </table>
</main></body></html>
"""

_EVENT_901_V2_AMENDED_HTML = """\
<html><body><main id="jevents_body">
  <div class="ev_detail"><h2>Kempen Derma Darah - 28 November 2026</h2></div>
  <table>
    <tr>
      <th>PENGANJUR</th><th>LOKASI DERMA DARAH</th>
      <th>MULA</th><th>TAMAT</th><th>SASARAN PENDERMA</th>
    </tr>
    <tr>
      <td>Pusat Darah Negara</td>
      <td>Mid Valley Megamall, Kuala Lumpur</td>
      <td>10.00 PAGI</td>
      <td>8.00 MALAM</td>
      <td>200</td>
    </tr>
    <tr>
      <td>St John Ambulans</td>
      <td>1 Utama Shopping Centre, Petaling Jaya, Selangor</td>
      <td>SESI 1: 10.00 PAGI - 1.45 T/ HARI</td>
      <td>SESI 2: 2.30 PTG - 6.00 PTG</td>
      <td>7 0</td>
    </tr>
  </table>
</main></body></html>
"""

_EVENT_902_HTML = """\
<html><body><main id="jevents_body">
  <div class="ev_detail"><h2>Kempen Derma Darah - 05 Disember 2026</h2></div>
  <table>
    <tr>
      <th>PENGANJUR</th><th>LOKASI DERMA DARAH</th>
      <th>MULA</th><th>TAMAT</th><th>SASARAN PENDERMA</th>
    </tr>
    <tr>
      <td>Hospital Putrajaya</td>
      <td>IOI City Mall, Putrajaya</td>
      <td>11.00 PAGI</td>
      <td>6.00 PETANG</td>
      <td>100</td>
    </tr>
  </table>
</main></body></html>
"""


def test_phase4_end_to_end_calendar_gate_b_reconcile_fts5_and_api(tmp_path: Path) -> None:
    db_path = tmp_path / "p4.db"
    scope_path = tmp_path / "crawl_scope.yaml"
    policy_path = tmp_path / "ingestion_policy.yaml"
    data_dir = tmp_path / "data"
    scope_path.write_text(_PHASE4_SCOPE_YAML, encoding="utf-8")
    policy_path.write_text(_PHASE4_POLICY_YAML, encoding="utf-8")
    upgrade_database(db_path)

    settings = AppSettings(
        contact_email="owner@pdn-sync.test",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
        sqlite_path=db_path,
        scope_file=scope_path,
        ingestion_policy_file=policy_path,
        data_dir=data_dir,
    )

    pages: dict[str, tuple[int, str]] = {
        "/v2/robots.txt": (200, "User-agent: *\nDisallow: /administrator/\n"),
        "/v2/index.php/component/jevents/monthcalendar/2026/11/-": (200, _MONTH_NOV_HTML),
        "/v2/index.php/component/jevents/monthcalendar/2026/12/-": (200, _MONTH_DEC_HTML),
        "/v2/index.php/component/jevents/eventdetail/901/-/kempen-nov": (200, _EVENT_901_V1_HTML),
        "/v2/index.php/component/jevents/eventdetail/902/-/kempen-dec": (200, _EVENT_902_HTML),
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        status, body = pages.get(request.url.path, (404, "Not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    now_kl = datetime(2026, 11, 26, 10, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))

    # 1. Run bounded calendar sync -> stages 2 eventdetail artifacts in pending_review
    cal1 = run_calendar_sync_pipeline(
        options=CalendarSyncOptions(
            live=True,
            months_ahead=1,
            now_kl=now_kl,
            scope_file=scope_path,
            policy_file=policy_path,
            sqlite_path=db_path,
            data_dir=data_dir,
            resolve_dns=False,
        ),
        settings=settings,
        transport=httpx.MockTransport(_handler),
        sleep_fn=lambda _: None,
    )
    assert cal1.months_succeeded == 2
    assert cal1.eventdetail_discovered == 2
    assert cal1.staged_pending == 2
    assert cal1.sessions_extracted_total == 4
    # Confirm exported under data/extracted/donation_schedules/
    staged_sched_files = list((data_dir / "extracted/donation_schedules").glob("*.md"))
    assert len(staged_sched_files) == 2

    # 2. Before Gate B approval -> /v1/events returns 0 records and Qdrant has 0 points!
    embedder = DeterministicHashEmbedder()
    with QdrantVectorStore(in_memory=True) as qdrant:
        app = create_app(settings=settings, embedder=embedder, qdrant_store=qdrant)
        with TestClient(app) as client:
            assert client.get("/v1/events").json()["total"] == 0

            # 3. Approve clean donation_schedule batch at Gate B and publish
            gate_b = GateBService(
                sqlite_path=db_path,
                policy_path=policy_path,
                scope_path=scope_path,
                data_dir=data_dir,
            )
            batch = gate_b.batch_approve(
                flt=StageQueueFilter(category="donation_schedule", limit=0),
                reviewer="owner",
                dry_run=False,
            )
            assert len(batch.approved_ids) == 2

            pub_svc = PublicationService(
                sqlite_path=db_path,
                scope_path=scope_path,
                policy_path=policy_path,
                qdrant_store=qdrant,
                embedder=embedder,
                settings=settings,
            )
            pub_sum = pub_svc.publish_approved(dry_run=False)
            assert pub_sum.published == 2
            assert pub_sum.schedule_sessions_published == 4
            # CRITICAL INVARIANT: zero Qdrant vectors created for schedule rows by default!
            assert pub_sum.chunks_upserted == 0
            _, qd_points = qdrant.health_check()
            assert qd_points == 0

            # 4. Query /v1/events: exact date, date range, state, FTS5, and typo tolerance!
            r_all = client.get("/v1/events").json()
            assert r_all["total"] == 4

            r_nov = client.get("/v1/events", params={"date": "2026-11-28"}).json()
            assert r_nov["total"] == 3

            r_selangor = client.get(
                "/v1/events", params={"state": "Selangor", "date": "2026-11-28"}
            ).json()
            assert r_selangor["total"] == 2
            assert {s["session_label"] for s in r_selangor["results"]} == {"SESI 1", "SESI 2"}

            r_typo = client.get(
                "/v1/events", params={"q": "Mid Valey", "date": "2026-11-28"}
            ).json()
            assert r_typo["total"] == 1
            assert r_typo["results"][0]["time_end"] == "17:00"
            assert r_typo["results"][0]["raw_cells"]["SASARAN PENDERMA"] == "1 5 0"

            # Fuzzy match must NEVER leak events outside requested date!
            r_wrong_date = client.get(
                "/v1/events", params={"q": "Mid Valley", "date": "2026-12-05"}
            ).json()
            assert r_wrong_date["total"] == 0

            # Status endpoint reports published_donation_events=2, published_donation_sessions=4
            st = client.get("/v1/status").json()
            assert st["published_donation_events"] == 2
            assert st["published_donation_sessions"] == 4

            # 5. Simulate live amendment to event 901 (pending Gate B -> pending_change=True!)
            pages["/v2/index.php/component/jevents/eventdetail/901/-/kempen-nov"] = (
                200,
                _EVENT_901_V2_AMENDED_HTML,
            )
            cal2 = run_calendar_sync_pipeline(
                options=CalendarSyncOptions(
                    live=True,
                    months_ahead=1,
                    now_kl=now_kl,
                    scope_file=scope_path,
                    policy_file=policy_path,
                    sqlite_path=db_path,
                    data_dir=data_dir,
                    resolve_dns=False,
                ),
                settings=settings,
                transport=httpx.MockTransport(_handler),
                sleep_fn=lambda _: None,
            )
            assert cal2.staged_pending == 1
            assert cal2.unchanged_blob == 1

            # Old version still served with pending_change=True until approved!
            r_pending = client.get(
                "/v1/events", params={"q": "Mid Valley", "date": "2026-11-28"}
            ).json()
            assert r_pending["results"][0]["time_end"] == "17:00"
            assert r_pending["results"][0]["pending_change"] is True

            # Approve amended event 901 and publish -> status='amended', old row kept is_current=0
            gate_b.batch_approve(
                flt=StageQueueFilter(category="donation_schedule", limit=0),
                reviewer="owner",
                dry_run=False,
            )
            pub_svc.publish_approved(dry_run=False)

            r_amended = client.get(
                "/v1/events", params={"q": "Mid Valley", "date": "2026-11-28"}
            ).json()
            assert r_amended["total"] == 1
            assert r_amended["results"][0]["time_end"] == "20:00"
            assert r_amended["results"][0]["target_donors"] == 200
            assert r_amended["results"][0]["status"] == "amended"
            assert r_amended["results"][0]["pending_change"] is False

            # 6. Failed December month fetch MUST NEVER delete or cancel valid Dec event 902!
            pages["/v2/index.php/component/jevents/monthcalendar/2026/12/-"] = (503, "Down")
            cal_fail = run_calendar_sync_pipeline(
                options=CalendarSyncOptions(
                    live=True,
                    months_ahead=1,
                    now_kl=now_kl,
                    scope_file=scope_path,
                    policy_file=policy_path,
                    sqlite_path=db_path,
                    data_dir=data_dir,
                    resolve_dns=False,
                ),
                settings=settings,
                transport=httpx.MockTransport(_handler),
                sleep_fn=lambda _: None,
            )
            assert cal_fail.months_failed == 1
            assert cal_fail.marked_removed_events == 0
            assert client.get("/v1/events", params={"date": "2026-12-05"}).json()["total"] == 1

            # 7. Clean December fetch missing event 902 -> removed_from_source (not canceled)
            pages["/v2/index.php/component/jevents/monthcalendar/2026/12/-"] = (
                200,
                "<html><body>No events</body></html>",
            )
            cal_clean_missing = run_calendar_sync_pipeline(
                options=CalendarSyncOptions(
                    live=True,
                    months_ahead=1,
                    now_kl=now_kl,
                    scope_file=scope_path,
                    policy_file=policy_path,
                    sqlite_path=db_path,
                    data_dir=data_dir,
                    resolve_dns=False,
                ),
                settings=settings,
                transport=httpx.MockTransport(_handler),
                sleep_fn=lambda _: None,
            )
            assert cal_clean_missing.marked_removed_events == 1
            assert client.get("/v1/events", params={"date": "2026-12-05"}).json()["total"] == 0

            # Verify audit history & EXPLAIN QUERY PLAN B-tree indexes in SQLite
            engine = create_sqlite_engine(db_path)
            try:
                with engine.connect() as conn:
                    old_rows = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM donation_sessions WHERE is_current = 0 "
                            "AND superseded_by_session_id IS NOT NULL"
                        )
                    ).scalar_one()
                    assert old_rows == 1

                    removed_status = conn.execute(
                        text("SELECT status FROM donation_events WHERE upstream_event_id = '902'")
                    ).scalar_one()
                    assert removed_status == "removed_from_source"

                    plan_date = conn.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT session_id FROM donation_sessions "
                            "WHERE event_date = '2026-11-28' AND is_current = 1"
                        )
                    ).fetchall()
                    plan_state_date = conn.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT session_id FROM donation_sessions "
                            "WHERE state = 'Selangor' AND event_date = '2026-11-28' "
                            "AND is_current = 1"
                        )
                    ).fetchall()
                    assert any("ix_donation_sessions_event_date" in str(r) for r in plan_date)
                    assert any("ix_donation_sessions_state_date" in str(r) for r in plan_state_date)
                    # Confirm DonationSessionRow model loads cleanly
                    assert DonationSessionRow.__tablename__ == "donation_sessions"
            finally:
                engine.dispose()
