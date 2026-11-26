"""Integration tests for Phase 5 SQLite online backup, WAL recovery, restore, and concurrency."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdn_sync.persistence.backup import (
    BackupVerificationError,
    create_online_backup,
    restore_online_backup,
    verify_backup,
)
from pdn_sync.persistence.db import create_session_factory, create_sqlite_engine, upgrade_database
from pdn_sync.persistence.models import CrawlResourceRow, SyncRunRow
from pdn_sync.persistence.ops_repo import OpsRepository


def _seed_sample_rows(db_path: Path, count: int = 5) -> None:
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        sf = create_session_factory(engine)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                SyncRunRow(
                    run_id="run_seed_01",
                    profile="site",
                    status="completed",
                    scope_file_hash="abc123",
                    started_at_utc=now,
                    finished_at_utc=now,
                    bounds_json="{}",
                    summary_counts_json="{}",
                )
            )
            for i in range(count):
                session.add(
                    CrawlResourceRow(
                        resource_id=f"res_{i}",
                        canonical_key=f"ckey_{i}",
                        url=f"https://pdn.gov.my/v2/item-{i}",
                        resource_type="page",
                        category="general_info",
                        first_seen_at_utc=now,
                        last_seen_at_utc=now,
                    )
                )
            session.commit()
    finally:
        engine.dispose()


def test_online_backup_during_active_wal_writes_and_clean_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "pdn_sync.db"
    backup_root = tmp_path / "backups"
    qdrant_dir = tmp_path / "qdrant"
    qdrant_dir.mkdir()
    (qdrant_dir / "meta.json").write_text('{"ok": true}', encoding="utf-8")

    _seed_sample_rows(db_path, count=4)

    # Open a WAL writer connection that holds committed WAL frames (without truncating)
    wal_conn = sqlite3.connect(db_path.as_posix())
    try:
        wal_conn.execute("PRAGMA journal_mode=WAL")
        wal_conn.execute("PRAGMA wal_autocheckpoint=0")
        now_iso = datetime.now(UTC).isoformat()
        wal_conn.execute(
            """
            INSERT INTO crawl_resources
                (resource_id, canonical_key, url, resource_type, category,
                 first_seen_at_utc, last_seen_at_utc, lifecycle_state, missing_consecutive_runs)
            VALUES ('res_wal_live', 'ckey_wal_live', 'https://pdn.gov.my/v2/wal-live',
                    'page', 'general_info', ?, ?, 'linked', 0)
            """,
            (now_iso, now_iso),
        )
        wal_conn.commit()
        # Keep an open reader transaction on WAL while online backup executes
        wal_conn.execute("BEGIN")
        wal_conn.execute("SELECT count(*) FROM crawl_resources").fetchone()

        b_res = create_online_backup(
            sqlite_path=db_path,
            backup_root=backup_root,
            include_qdrant=True,
            qdrant_path=qdrant_dir,
        )
        assert b_res.integrity_ok is True
        assert b_res.foreign_key_errors == 0
        assert b_res.table_counts["crawl_resources"] == 5
        assert b_res.qdrant_included is True
        assert b_res.qdrant_file_count == 1
        assert b_res.alembic_revision == "0005_phase5_reliability_and_ops"

        v_res = verify_backup(b_res.backup_dir)
        assert v_res.valid is True
        assert v_res.sqlite_sha256_ok is True
        assert v_res.integrity_ok is True
        assert v_res.qdrant_files_checked == 1
    finally:
        wal_conn.rollback()
        wal_conn.close()

    # Simulate DB corruption + stale WAL/SHM sidecars + Qdrant loss
    db_path.write_bytes(b"CORRUPTED_SQLITE_BYTES_NOT_A_DATABASE")
    Path(f"{db_path.as_posix()}-wal").write_bytes(b"STALE_WAL_GARBAGE")
    Path(f"{db_path.as_posix()}-shm").write_bytes(b"STALE_SHM_GARBAGE")
    (qdrant_dir / "meta.json").write_text('{"corrupt": true}', encoding="utf-8")

    # Restore refuses without confirm=True
    with pytest.raises(ValueError, match="confirm=True"):
        restore_online_backup(
            backup_dir=b_res.backup_dir,
            target_sqlite_path=db_path,
            confirm=False,
        )

    r_res = restore_online_backup(
        backup_dir=b_res.backup_dir,
        target_sqlite_path=db_path,
        confirm=True,
        restore_qdrant=True,
        target_qdrant_path=qdrant_dir,
    )
    assert r_res.integrity_ok is True
    assert r_res.qdrant_restored is True
    assert not Path(f"{db_path.as_posix()}-wal").exists()
    assert not Path(f"{db_path.as_posix()}-shm").exists()
    assert (qdrant_dir / "meta.json").read_text(encoding="utf-8") == '{"ok": true}'

    # Confirm all 5 rows including the WAL-committed row were recovered cleanly
    check_conn = sqlite3.connect(db_path.as_posix())
    try:
        cnt = check_conn.execute("SELECT count(*) FROM crawl_resources").fetchone()[0]
        assert cnt == 5
        has_wal_row = check_conn.execute(
            "SELECT count(*) FROM crawl_resources WHERE resource_id='res_wal_live'"
        ).fetchone()[0]
        assert has_wal_row == 1
    finally:
        check_conn.close()


def test_corrupted_backup_is_rejected_before_touching_live_db(tmp_path: Path) -> None:
    db_path = tmp_path / "live.db"
    backup_root = tmp_path / "backups"
    _seed_sample_rows(db_path, count=2)

    b_res = create_online_backup(sqlite_path=db_path, backup_root=backup_root)
    # Tamper with backup file bytes
    with b_res.sqlite_backup_path.open("ab") as fh:
        fh.write(b"tampered")

    v_res = verify_backup(b_res.backup_dir)
    assert v_res.valid is False
    assert v_res.sqlite_sha256_ok is False

    live_bytes_before = db_path.read_bytes()
    with pytest.raises(BackupVerificationError):
        restore_online_backup(
            backup_dir=b_res.backup_dir,
            target_sqlite_path=db_path,
            confirm=True,
        )
    assert db_path.read_bytes() == live_bytes_before


def test_sqlite_wal_concurrent_writes_and_reads_do_not_corrupt_or_lock_fail(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "concurrent.db"
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    errors: list[Exception] = []

    def _writer(worker_idx: int) -> None:
        try:
            ops = OpsRepository(engine)
            for j in range(15):
                ops.record_task_attempt(
                    task_type="concurrent_write",
                    status="succeeded",
                    correlation_id=f"w{worker_idx}_j{j}",
                    attempt_number=1,
                    latency_ms=j,
                )
        except Exception as exc:
            errors.append(exc)

    def _reader() -> None:
        try:
            ops = OpsRepository(engine)
            for _ in range(25):
                _ = ops.list_task_attempts(limit=10)
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=_writer, args=(0,)),
            threading.Thread(target=_writer, args=(1,)),
            threading.Thread(target=_reader),
            threading.Thread(target=_reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        ops = OpsRepository(engine)
        attempts = ops.list_task_attempts(task_type="concurrent_write", limit=100)
        assert len(attempts) == 30
    finally:
        engine.dispose()
