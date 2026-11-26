"""Safe SQLite Online Backup API (`sqlite3.Connection.backup`), verification, and atomic restore."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class BackupVerificationError(RuntimeError):
    """Raised when a backup fails checksum, integrity_check, or foreign_key_check verification."""


@dataclass(slots=True)
class BackupResult:
    backup_dir: Path
    manifest_path: Path
    sqlite_backup_path: Path
    sqlite_sha256: str
    sqlite_bytes: int
    alembic_revision: str | None
    integrity_ok: bool
    foreign_key_errors: int
    wal_checkpoint: dict[str, int]
    table_counts: dict[str, int]
    qdrant_included: bool = False
    qdrant_file_count: int = 0


@dataclass(slots=True)
class BackupVerificationResult:
    valid: bool
    backup_dir: Path
    sqlite_sha256_ok: bool
    integrity_ok: bool
    foreign_key_errors: int
    qdrant_files_checked: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RestoreResult:
    backup_dir: Path
    target_sqlite_path: Path
    restored_sha256: str
    integrity_ok: bool
    qdrant_restored: bool = False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect_sqlite_health(db_path: Path) -> tuple[bool, int, str | None, dict[str, int]]:
    """Run PRAGMA integrity_check, PRAGMA foreign_key_check, alembic version, and key row counts."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        ic_rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        integrity_ok = ic_rows == ["ok"]
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        fk_errors = len(fk_rows)

        alembic_rev: str | None = None
        try:
            rev_row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            if rev_row:
                alembic_rev = str(rev_row[0])
        except sqlite3.OperationalError:
            alembic_rev = None

        counts: dict[str, int] = {}
        for tbl in (
            "crawl_resources",
            "staged_artifacts",
            "publication_versions",
            "knowledge_documents",
            "knowledge_chunks",
            "structured_tables",
            "donation_events",
            "donation_sessions",
            "outbox_events",
            "task_attempts",
        ):
            try:
                cnt_row = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()
                counts[tbl] = int(cnt_row[0]) if cnt_row else 0
            except sqlite3.OperationalError:
                continue

        return integrity_ok, fk_errors, alembic_rev, counts
    finally:
        conn.close()


def create_online_backup(
    *,
    sqlite_path: Path,
    backup_root: Path,
    include_qdrant: bool = False,
    qdrant_path: Path | None = None,
    pages_per_step: int = 100,
    now_utc: datetime | None = None,
) -> BackupResult:
    """Create a verified SQLite backup using sqlite3.Connection.backup() (never live file copy)."""
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")

    now = now_utc or datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup_root / f"backup_{stamp}"
    suffix = 1
    while backup_dir.exists():
        backup_dir = backup_root / f"backup_{stamp}_{suffix}"
        suffix += 1
    backup_dir.mkdir(parents=True, exist_ok=False)

    dest_db = backup_dir / "pdn_sync.db"

    # Use SQLite's Online Backup API — safe with concurrent WAL readers/writers
    src_conn = sqlite3.connect(sqlite_path.as_posix(), timeout=15.0)
    dst_conn = sqlite3.connect(dest_db.as_posix())
    wal_info: dict[str, int] = {"busy": 0, "log_frames": 0, "checkpointed_frames": 0}
    try:
        src_conn.execute("PRAGMA busy_timeout=15000")
        try:
            cp = src_conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if cp is not None and len(cp) >= 3:
                wal_info = {
                    "busy": int(cp[0]),
                    "log_frames": int(cp[1]),
                    "checkpointed_frames": int(cp[2]),
                }
        except sqlite3.OperationalError:
            pass

        src_conn.backup(dst_conn, pages=pages_per_step)
        dst_conn.commit()
        # Convert the backup copy itself to standalone DELETE journal mode so it is
        # 100% self-contained with zero WAL/SHM sidecars required during verification/restore.
        dst_conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst_conn.close()
        src_conn.close()

    integrity_ok, fk_errors, alembic_rev, table_counts = _inspect_sqlite_health(dest_db)
    if not integrity_ok or fk_errors > 0:
        raise BackupVerificationError(
            f"Online backup verification failed: integrity_ok={integrity_ok}, "
            f"foreign_key_errors={fk_errors}"
        )

    db_sha = _sha256_file(dest_db)
    db_bytes = dest_db.stat().st_size

    qdrant_files: dict[str, str] = {}
    qdrant_included = False
    if include_qdrant and qdrant_path is not None and qdrant_path.exists():
        qdrant_dest = backup_dir / "qdrant"
        shutil.copytree(
            qdrant_path,
            qdrant_dest,
            ignore=shutil.ignore_patterns(".lock"),
        )
        for fpath in sorted(qdrant_dest.rglob("*")):
            if fpath.is_file():
                rel = fpath.relative_to(backup_dir).as_posix()
                qdrant_files[rel] = _sha256_file(fpath)
        qdrant_included = True

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at_utc": now.isoformat(),
        "source_sqlite_path": str(sqlite_path),
        "sqlite_file": "pdn_sync.db",
        "sqlite_sha256": db_sha,
        "sqlite_bytes": db_bytes,
        "alembic_revision": alembic_rev,
        "integrity_ok": integrity_ok,
        "foreign_key_errors": fk_errors,
        "wal_checkpoint": wal_info,
        "table_counts": table_counts,
        "qdrant_included": qdrant_included,
        "qdrant_files": qdrant_files,
    }
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    return BackupResult(
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        sqlite_backup_path=dest_db,
        sqlite_sha256=db_sha,
        sqlite_bytes=db_bytes,
        alembic_revision=alembic_rev,
        integrity_ok=integrity_ok,
        foreign_key_errors=fk_errors,
        wal_checkpoint=wal_info,
        table_counts=table_counts,
        qdrant_included=qdrant_included,
        qdrant_file_count=len(qdrant_files),
    )


def verify_backup(backup_dir: Path) -> BackupVerificationResult:
    """Verify backup manifest, SHA-256 checksums, PRAGMA integrity_check, and foreign_key_check."""
    errors: list[str] = []
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        return BackupVerificationResult(
            valid=False,
            backup_dir=backup_dir,
            sqlite_sha256_ok=False,
            integrity_ok=False,
            foreign_key_errors=-1,
            errors=[f"Missing manifest.json in {backup_dir}"],
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db_file = backup_dir / str(manifest.get("sqlite_file", "pdn_sync.db"))
    if not db_file.is_file():
        return BackupVerificationResult(
            valid=False,
            backup_dir=backup_dir,
            sqlite_sha256_ok=False,
            integrity_ok=False,
            foreign_key_errors=-1,
            errors=[f"Missing SQLite backup file: {db_file}"],
        )

    actual_sha = _sha256_file(db_file)
    expected_sha = str(manifest.get("sqlite_sha256", ""))
    sha_ok = actual_sha == expected_sha
    if not sha_ok:
        errors.append(f"SQLite SHA-256 mismatch: expected {expected_sha}, got {actual_sha}")

    integrity_ok = False
    fk_errors = -1
    try:
        integrity_ok, fk_errors, _rev, _counts = _inspect_sqlite_health(db_file)
        if not integrity_ok:
            errors.append("PRAGMA integrity_check failed on backup database")
        if fk_errors > 0:
            errors.append(f"PRAGMA foreign_key_check reported {fk_errors} violation(s)")
    except sqlite3.DatabaseError as exc:
        errors.append(f"Backup SQLite database is unreadable/corrupt: {exc}")

    qdrant_checked = 0
    qdrant_files = manifest.get("qdrant_files") or {}
    for rel_path, expected_qsha in qdrant_files.items():
        qfile = backup_dir / rel_path
        if not qfile.is_file():
            errors.append(f"Missing Qdrant snapshot file: {rel_path}")
            continue
        qdrant_checked += 1
        if _sha256_file(qfile) != expected_qsha:
            errors.append(f"Qdrant snapshot SHA-256 mismatch: {rel_path}")

    return BackupVerificationResult(
        valid=len(errors) == 0,
        backup_dir=backup_dir,
        sqlite_sha256_ok=sha_ok,
        integrity_ok=integrity_ok,
        foreign_key_errors=fk_errors,
        qdrant_files_checked=qdrant_checked,
        errors=errors,
    )


def restore_online_backup(
    *,
    backup_dir: Path,
    target_sqlite_path: Path,
    confirm: bool = False,
    restore_qdrant: bool = False,
    target_qdrant_path: Path | None = None,
) -> RestoreResult:
    """Verify backup and atomically restore SQLite (+ optional Qdrant) cleaning stale WAL/SHM."""
    if not confirm:
        raise ValueError("Refusing restore without confirm=True (--confirm).")

    verification = verify_backup(backup_dir)
    if not verification.valid:
        raise BackupVerificationError(
            "Refusing to restore invalid backup: " + "; ".join(verification.errors)
        )

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    source_db = backup_dir / str(manifest.get("sqlite_file", "pdn_sync.db"))

    target_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_restore = target_sqlite_path.with_name(f"{target_sqlite_path.name}.restoring.{os.getpid()}")

    # Copy via SQLite Online Backup API into the staging sibling file
    src_conn = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    tmp_conn = sqlite3.connect(tmp_restore.as_posix())
    try:
        src_conn.backup(tmp_conn, pages=100)
        tmp_conn.commit()
    finally:
        tmp_conn.close()
        src_conn.close()

    # Clean stale WAL/SHM sidecars before swapping the database file in place
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target_sqlite_path.as_posix()}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    os.replace(tmp_restore, target_sqlite_path)

    # Post-restore verification
    integrity_ok, fk_errors, _rev, _counts = _inspect_sqlite_health(target_sqlite_path)
    if not integrity_ok or fk_errors > 0:
        raise BackupVerificationError(
            f"Post-restore check failed: integrity_ok={integrity_ok}, fk_errors={fk_errors}"
        )

    qdrant_restored = False
    if restore_qdrant and manifest.get("qdrant_included") and target_qdrant_path is not None:
        qdrant_src = backup_dir / "qdrant"
        if qdrant_src.is_dir():
            if target_qdrant_path.exists():
                shutil.rmtree(target_qdrant_path)
            shutil.copytree(qdrant_src, target_qdrant_path)
            qdrant_restored = True

    return RestoreResult(
        backup_dir=backup_dir,
        target_sqlite_path=target_sqlite_path,
        restored_sha256=_sha256_file(target_sqlite_path),
        integrity_ok=integrity_ok,
        qdrant_restored=qdrant_restored,
    )
