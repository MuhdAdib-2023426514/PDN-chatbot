"""Phase 2 extraction orchestrator and Gate B human review / override / reextract service."""

from __future__ import annotations

import difflib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pdn_sync.config import AppSettings, load_crawl_scope, load_ingestion_policy
from pdn_sync.crawling.clearance import SourceAccessEvidence, require_live_clearance
from pdn_sync.crawling.fetcher import (
    ApprovedContentFetcher,
    FetchBlockedError,
    FetchCaptureResult,
)
from pdn_sync.crawling.robots import RobotsFetchOutcome, SubfolderRobotsPolicy
from pdn_sync.discovery.classifier import classify_url
from pdn_sync.discovery.exporter import atomic_write_text
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.discovery.scope_manager import GateABlockedError
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.domain.scope import CrawlScopeConfig, evaluate_scope
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    ExtractionResult,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.extractors.html_extractor import HTML_EXTRACTOR_NAME, extract_html
from pdn_sync.extractors.jevents import JEVENTS_EXTRACTOR_NAME, extract_jevents_eventdetail
from pdn_sync.extractors.pdf_docling import PDF_EXTRACTOR_NAME, PdfBackend, extract_pdf
from pdn_sync.extractors.poster_ocr import OCR_EXTRACTOR_NAME, OcrBackend, extract_poster_ocr
from pdn_sync.persistence.blob_store import LocalBlobStore
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.models import StagedArtifactRow
from pdn_sync.persistence.repository import ControlPlaneRepository, InventoryFilter
from pdn_sync.persistence.staging_repo import (
    GateBPublishBlockedError,
    StageQueueFilter,
    StagingRepository,
)
from pdn_sync.staging.exporter import (
    export_review_snapshot,
    export_staged_artifact,
    render_extraction_markdown,
)
from pdn_sync.staging.semantic_hash import SemanticTextBlock
from pdn_sync.staging.validator import validate_and_tag


def _current_rss_mb() -> float | None:
    try:
        import psutil

        rss_bytes = float(psutil.Process().memory_info().rss)
        return round(rss_bytes / (1024.0 * 1024.0), 2)
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class ExtractionOptions:
    live: bool = False
    pilot: bool = False
    resume: bool = False
    urls: list[str] = field(default_factory=list)
    resource_type: ResourceType | None = None
    category: DiscoveryCategory | None = None
    limit: int | None = None
    scope_file: Path | None = None
    policy_file: Path | None = None
    sqlite_path: Path | None = None
    data_dir: Path | None = None
    benchmark_out: Path | None = None
    resolve_dns: bool = True


@dataclass(slots=True)
class ExtractionRunSummary:
    run_id: str
    targets_total: int = 0
    skipped_resumed: int = 0
    fetched: int = 0
    not_modified: int = 0
    unchanged_blob: int = 0
    failed: int = 0
    staged_pending: int = 0
    staged_quarantined: int = 0
    carried_forward: int = 0
    artifact_ids: list[str] = field(default_factory=list)
    benchmark: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchApproveSummary:
    dry_run: bool
    batch_id: str | None = None
    approved_ids: list[str] = field(default_factory=list)
    excluded_critical_or_invalid: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReextractSummary:
    extractor: str
    dry_run: bool
    total_considered: int = 0
    unchanged_carry_forward: int = 0
    changed_pending_review: int = 0
    quarantined: int = 0
    new_artifact_ids: list[str] = field(default_factory=list)


def _select_pilot_targets(
    approved_items: list[tuple[str, ResourceType, DiscoveryCategory, str | None]],
) -> list[tuple[str, ResourceType, DiscoveryCategory, str | None]]:
    """Select a balanced representative live pilot (~20-25 pages, ~6 PDFs, ~14 posters)."""
    pages = [t for t in approved_items if t[1] == ResourceType.PAGE]
    pdfs = [t for t in approved_items if t[1] == ResourceType.DOCUMENT]
    posters = [t for t in approved_items if t[1] == ResourceType.IMAGE_POSTER]

    selected: list[tuple[str, ResourceType, DiscoveryCategory, str | None]] = []
    seen: set[str] = set()

    def _add(item: tuple[str, ResourceType, DiscoveryCategory, str | None]) -> None:
        if item[0] not in seen:
            seen.add(item[0])
            selected.append(item)

    # Pages: homepage + corporate + static centres + FAQ + services + 2 staff directory profiles
    page_quotas: list[tuple[DiscoveryCategory, int]] = [
        (DiscoveryCategory.HOMEPAGE, 1),
        (DiscoveryCategory.CONTACT_STATIC_CENTERS, 3),
        (DiscoveryCategory.CORPORATE, 3),
        (DiscoveryCategory.FAQ, 6),
        (DiscoveryCategory.SERVICES, 6),
        (DiscoveryCategory.PUBLICATIONS, 2),
        (DiscoveryCategory.STANDALONE_TOOL, 2),
        (DiscoveryCategory.STAFF_DIRECTORY, 3),
    ]
    for cat, quota in page_quotas:
        count = 0
        for p in pages:
            if p[2] == cat and count < quota:
                _add(p)
                count += 1

    # PDFs: 6 diverse PDFs (favor smaller/typical operational guides & forms)
    for doc in pdfs[:6]:
        _add(doc)

    # Posters: blood stock, SmartSlider operational notices, educational posters, 1 facility photo
    priority_poster_substrings = (
        "1stockseptember2026",
        "WaktuOperasiApril2024",
        "Mpoxbanner",
        "Needle_Related_Procedures",
        "Variant_Creutzfeldt",
        "PosterKriteriaPendermaanDarah",
        "PosterAnemia",
        "Kemudahan_2025",
    )
    for sub in priority_poster_substrings:
        for img in posters:
            if sub.lower() in img[0].lower():
                _add(img)
                break

    for img in posters:
        if len([x for x in selected if x[1] == ResourceType.IMAGE_POSTER]) >= 14:
            break
        _add(img)

    return selected


def _resolve_approved_targets(
    *,
    opts: ExtractionOptions,
    scope: CrawlScopeConfig,
    ctrl_repo: ControlPlaneRepository,
) -> list[tuple[str, ResourceType, DiscoveryCategory, str | None]]:
    """Return list of (url, resource_type, category, policy_rule_id) strictly approved at Gate A."""
    candidates: list[tuple[str, ResourceType, DiscoveryCategory, str | None]] = []

    if opts.urls:
        for raw_u in opts.urls:
            cls = classify_url(
                raw_u,
                allowed_host=scope.allowed_host,
                allowed_path_prefix=scope.allowed_path_prefix,
            )
            ev = evaluate_scope(raw_u, scope, resource_type=cls.resource_type)
            if ev.disposition == Disposition.APPROVED and ev.normalized_url is not None:
                candidates.append((ev.normalized_url, cls.resource_type, cls.category, ev.rule_id))
    else:
        _run, inv_items = ctrl_repo.list_inventory_items(flt=InventoryFilter(limit=0))
        for item in inv_items:
            ev = evaluate_scope(item.url, scope, resource_type=item.resource_type)
            if ev.disposition != Disposition.APPROVED or ev.normalized_url is None:
                continue
            if opts.resource_type is not None and item.resource_type != opts.resource_type:
                continue
            if opts.category is not None and item.category != opts.category:
                continue
            candidates.append((ev.normalized_url, item.resource_type, item.category, ev.rule_id))

    if opts.pilot:
        candidates = _select_pilot_targets(candidates)
    if opts.limit is not None and opts.limit > 0:
        candidates = candidates[: opts.limit]
    return candidates


def run_extraction_pipeline(
    *,
    options: ExtractionOptions | None = None,
    settings: AppSettings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    pdf_backend: PdfBackend | None = None,
    ocr_backend: OcrBackend | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> ExtractionRunSummary:
    """Fetch Gate-A-approved resources into BlobStore, run 2a/2b/2c extractors, and stage."""
    opts = options or ExtractionOptions()
    cfg = settings or AppSettings()

    scope_path = opts.scope_file or cfg.scope_file
    policy_path = opts.policy_file or cfg.ingestion_policy_file
    sqlite_path = opts.sqlite_path or cfg.sqlite_path
    data_dir = opts.data_dir or cfg.data_dir

    scope, scope_hash = load_crawl_scope(scope_path)
    policy = load_ingestion_policy(policy_path)
    upgrade_database(sqlite_path)

    engine = create_sqlite_engine(sqlite_path)
    ctrl_repo = ControlPlaneRepository(engine)
    stage_repo = StagingRepository(engine)
    blob_store = LocalBlobStore(data_dir / "blobs")

    run_id = f"ext_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(UTC)
    summary = ExtractionRunSummary(run_id=run_id)

    timings_by_type: dict[str, list[float]] = {"page": [], "document": [], "image_poster": []}
    fetch_ms_by_type: dict[str, list[int]] = {"page": [], "document": [], "image_poster": []}
    rss_start = _current_rss_mb()
    peak_rss = rss_start or 0.0

    try:
        ctrl_repo.sync_compiled_scope_cache(scope=scope, scope_file_hash=scope_hash)

        # Preflight: /v2/robots.txt + 5-point source clearance
        headers = {"User-Agent": cfg.user_agent()}
        client_kwargs: dict[str, object] = {
            "headers": headers,
            "timeout": policy.politeness.request_timeout_seconds,
            "follow_redirects": False,
        }
        if transport is not None:
            client_kwargs["transport"] = transport

        with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
            v2_robots = client.get(
                f"https://{scope.allowed_host}{scope.allowed_path_prefix}/robots.txt"
            )
            robots_policy = SubfolderRobotsPolicy.from_fetch_outcome(
                RobotsFetchOutcome(
                    root_robots_status=403,
                    v2_robots_status=v2_robots.status_code,
                    v2_robots_text=v2_robots.text if v2_robots.status_code == 200 else None,
                ),
                scope_prefix=scope.allowed_path_prefix,
            )

        require_live_clearance(
            settings=cfg,
            politeness=policy.politeness,
            evidence=SourceAccessEvidence(
                v2_robots_fetched=True,
                v2_robots_policy=robots_policy,
                publicly_reachable_without_controls=True,
                no_prohibiting_terms_or_directives=True,
                verified_date=started_at.strftime("%Y-%m-%d"),
            ),
            require_live_flag=opts.live,
        )

        targets = _resolve_approved_targets(opts=opts, scope=scope, ctrl_repo=ctrl_repo)
        summary.targets_total = len(targets)

        ctrl_repo.create_sync_run(
            run_id=run_id,
            profile="extract_stage",
            scope_file_hash=scope_hash,
            started_at_utc=started_at,
            bounds={"pilot": opts.pilot, "limit": opts.limit, "targets_total": len(targets)},
        )

        fetcher = ApprovedContentFetcher(
            settings=cfg,
            scope=scope,
            scope_path=scope_path,
            politeness=policy.politeness,
            robots=robots_policy,
            blob_store=blob_store,
            transport=transport,
            sleep_fn=sleep_fn,
            resolve_dns=opts.resolve_dns,
        )

        # Share backend instances across loop so ML weights load only once per run
        shared_pdf_backend = pdf_backend
        shared_ocr_backend = ocr_backend

        for idx, (url, res_type, category, policy_rule_id) in enumerate(targets, start=1):
            res_row = stage_repo.ensure_crawl_resource(
                canonical_url=url,
                resource_type=res_type.value,
                category=category.value,
            )
            latest_staged = stage_repo.get_latest_artifact_for_resource(res_row.resource_id)
            if opts.resume and latest_staged is not None:
                summary.skipped_resumed += 1
                continue

            last_fetch = stage_repo.get_latest_successful_fetch(res_row.resource_id)

            try:
                cap = fetcher.fetch(
                    url,
                    resource_type=res_type,
                    etag=last_fetch.etag if last_fetch else None,
                    last_modified=last_fetch.last_modified if last_fetch else None,
                )
            except (FetchBlockedError, GateABlockedError) as exc:
                cap = FetchCaptureResult(
                    outcome="blocked_gate_a",
                    url=url,
                    error_message=str(exc),
                )
            fetch_row = stage_repo.record_fetch(
                run_id=run_id,
                resource_id=res_row.resource_id,
                url=url,
                final_url=cap.final_url,
                redirect_chain=cap.redirect_chain,
                fetched_at_utc=cap.fetched_at_utc,
                http_status=cap.http_status,
                etag=cap.etag,
                last_modified=cap.last_modified,
                content_type=cap.content_type,
                byte_size=cap.byte_size,
                blob_sha256=cap.blob_sha256,
                latency_ms=cap.latency_ms,
                fetch_outcome=cap.outcome,
                error_message=cap.error_message,
            )

            if cap.outcome == "not_modified":
                summary.not_modified += 1
                stage_repo.touch_last_verified(res_row.resource_id, cap.fetched_at_utc)
                continue

            if cap.outcome != "fetched" or cap.blob_sha256 is None:
                summary.failed += 1
                if progress_cb:
                    progress_cb(f"[{idx}/{len(targets)}] FAILED {url}: {cap.error_message}")
                continue

            summary.fetched += 1
            fetch_ms_by_type[res_type.value].append(cap.latency_ms)

            # Incremental by default (Rule 6): skip re-extracting/re-OCR'ing identical bytes
            if latest_staged is not None and latest_staged.content_hash == cap.blob_sha256:
                summary.unchanged_blob += 1
                stage_repo.touch_last_verified(res_row.resource_id, cap.fetched_at_utc)
                if progress_cb:
                    progress_cb(
                        f"[{idx}/{len(targets)}] {res_type.value:12s} [unchanged_blob] {url}"
                    )
                continue

            raw_bytes = blob_store.get_bytes(cap.blob_sha256)
            version_seq = stage_repo.next_version_seq(res_row.resource_id)
            version_id = f"ver_{res_row.resource_id}_{version_seq:03d}_{uuid.uuid4().hex[:6]}"

            ext_t0 = time.monotonic()
            media_meta: dict[str, Any] | None = None
            try:
                if res_type == ResourceType.PAGE:
                    extracted = extract_html(
                        raw_html=raw_bytes,
                        source_url=url,
                        resource_id=res_row.resource_id,
                        resource_version_id=version_id,
                        content_hash=cap.blob_sha256,
                        category=category,
                        scope=scope,
                        captured_at_utc=cap.fetched_at_utc,
                        policy_rule_id=policy_rule_id,
                        content_type=cap.content_type,
                    )
                elif res_type == ResourceType.DOCUMENT:
                    if shared_pdf_backend is None:
                        from pdn_sync.extractors.pdf_docling import DefaultDoclingBackend

                        shared_pdf_backend = DefaultDoclingBackend()
                    extracted = extract_pdf(
                        raw_bytes=raw_bytes,
                        source_url=url,
                        resource_id=res_row.resource_id,
                        resource_version_id=version_id,
                        content_hash=cap.blob_sha256,
                        category=category,
                        captured_at_utc=cap.fetched_at_utc,
                        policy_rule_id=policy_rule_id,
                        content_type=cap.content_type,
                        backend=shared_pdf_backend,
                    )
                else:
                    if shared_ocr_backend is None:
                        from pdn_sync.extractors.poster_ocr import DefaultPaddleOcrBackend

                        shared_ocr_backend = DefaultPaddleOcrBackend()
                    extracted, media_meta = extract_poster_ocr(
                        raw_bytes=raw_bytes,
                        source_url=url,
                        resource_id=res_row.resource_id,
                        resource_version_id=version_id,
                        content_hash=cap.blob_sha256,
                        category=category,
                        captured_at_utc=cap.fetched_at_utc,
                        policy_rule_id=policy_rule_id,
                        content_type=cap.content_type,
                        backend=shared_ocr_backend,
                    )
            except Exception as exc:
                summary.failed += 1
                if progress_cb:
                    progress_cb(
                        f"[{idx}/{len(targets)}] EXTRACT_ERROR {url}: {type(exc).__name__}: {exc}"
                    )
                continue

            ext_sec = time.monotonic() - ext_t0
            timings_by_type[res_type.value].append(ext_sec)
            rss_now = _current_rss_mb()
            if rss_now is not None and rss_now > peak_rss:
                peak_rss = rss_now

            validated = validate_and_tag(extracted, policy)

            # Diff-based approval carry-forward check (AGENTS.md Section 7)
            prior_approved = stage_repo.get_latest_approved_artifact(res_row.resource_id)
            should_carry_forward = (
                prior_approved is not None
                and prior_approved[0].semantic_hash == validated.semantic_hash
                and validated.validation_status == ValidationStatus.VALID
                and validated.review_status != ReviewStatus.QUARANTINED
            )
            if should_carry_forward:
                validated = validated.model_copy(update={"review_status": ReviewStatus.APPROVED})

            json_path, md_path = export_staged_artifact(
                result=validated,
                version_seq=version_seq,
                data_dir=data_dir,
            )
            stage_repo.save_staged_version(
                result=validated,
                version_seq=version_seq,
                fetch_id=fetch_row.fetch_id,
                json_path=json_path,
                markdown_path=md_path,
                media_meta=media_meta,
            )

            if should_carry_forward and prior_approved is not None:
                stage_repo.record_review_decision(
                    artifact_id=validated.artifact_id,
                    reviewer="system:carry_forward",
                    decision="carried_forward",
                    new_review_status=ReviewStatus.APPROVED,
                    policy_mode=policy.approval_mode.value,
                    carried_from_decision_id=prior_approved[1].decision_id,
                    notes="semantic_hash unchanged from approved version",
                )
                export_review_snapshot(result=validated, approved=True, data_dir=data_dir)
                summary.carried_forward += 1
            elif validated.review_status == ReviewStatus.QUARANTINED:
                summary.staged_quarantined += 1
            else:
                summary.staged_pending += 1

            summary.artifact_ids.append(validated.artifact_id)
            if progress_cb:
                progress_cb(
                    f"[{idx}/{len(targets)}] {res_type.value:12s} "
                    f"[{validated.review_status.value:14s}] "
                    f"ext={ext_sec:.2f}s {url}"
                )

        finished_at = datetime.now(UTC)

        def _stats(vals: list[float]) -> dict[str, float | int]:
            if not vals:
                return {"count": 0, "mean_s": 0.0, "max_s": 0.0, "total_s": 0.0}
            return {
                "count": len(vals),
                "mean_s": round(sum(vals) / len(vals), 3),
                "max_s": round(max(vals), 3),
                "total_s": round(sum(vals), 3),
            }

        summary.benchmark = {
            "run_id": run_id,
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": finished_at.isoformat(),
            "wall_clock_seconds": round((finished_at - started_at).total_seconds(), 2),
            "targets_total": summary.targets_total,
            "skipped_resumed": summary.skipped_resumed,
            "fetched": summary.fetched,
            "not_modified": summary.not_modified,
            "unchanged_blob": summary.unchanged_blob,
            "failed": summary.failed,
            "staged_pending": summary.staged_pending,
            "staged_quarantined": summary.staged_quarantined,
            "carried_forward": summary.carried_forward,
            "rss_start_mb": rss_start,
            "peak_rss_mb": round(peak_rss, 2),
            "extract_latency_seconds": {k: _stats(v) for k, v in timings_by_type.items()},
            "fetch_latency_mean_ms": {
                k: round(sum(v) / len(v), 1) if v else 0.0 for k, v in fetch_ms_by_type.items()
            },
        }

        bench_path = opts.benchmark_out or (data_dir / "extracted/phase2_benchmark.json")
        atomic_write_text(
            bench_path, json.dumps(summary.benchmark, indent=2, sort_keys=True) + "\n"
        )

        # Update sync_runs status cleanly
        with stage_repo._session_factory() as session:
            from pdn_sync.persistence.models import SyncRunRow

            run_row = session.get(SyncRunRow, run_id)
            if run_row is not None:
                run_row.status = "completed"
                run_row.finished_at_utc = finished_at
                run_row.summary_counts_json = json.dumps(summary.benchmark, sort_keys=True)
                session.commit()

        return summary
    finally:
        engine.dispose()


class GateBService:
    """Human review, batch approval, diff, override, reextract, and publish-guard service."""

    def __init__(
        self,
        *,
        sqlite_path: Path,
        policy_path: Path,
        scope_path: Path,
        data_dir: Path,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.policy_path = policy_path
        self.scope_path = scope_path
        self.data_dir = data_dir
        upgrade_database(sqlite_path)

    def _with_repo(self) -> tuple[Any, StagingRepository]:
        engine = create_sqlite_engine(self.sqlite_path)
        return engine, StagingRepository(engine)

    def list_queue(self, flt: StageQueueFilter | None = None) -> list[StagedArtifactRow]:
        engine, repo = self._with_repo()
        try:
            return repo.list_staged_artifacts(flt=flt)
        finally:
            engine.dispose()

    def show_artifact(self, artifact_id: str) -> tuple[StagedArtifactRow, ExtractionResult, str]:
        engine, repo = self._with_repo()
        try:
            row = repo.get_artifact(artifact_id)
            if row is None:
                raise ValueError(f"Unknown staged artifact: {artifact_id}")
            result = ExtractionResult.model_validate_json(
                Path(row.json_path).read_text(encoding="utf-8")
            )
            md = Path(row.markdown_path).read_text(encoding="utf-8")
            return row, result, md
        finally:
            engine.dispose()

    def diff_artifact(self, artifact_id: str) -> str:
        """Compute unified Markdown diff between an artifact and its prior approved version."""
        engine, repo = self._with_repo()
        try:
            row = repo.get_artifact(artifact_id)
            if row is None:
                raise ValueError(f"Unknown staged artifact: {artifact_id}")
            new_lines = (
                Path(row.markdown_path).read_text(encoding="utf-8").splitlines(keepends=True)
            )
            prior = repo.get_latest_approved_artifact(row.resource_id)
            if prior is None or prior[0].artifact_id == row.artifact_id:
                return "(No prior approved version — new resource)\n" + "".join(new_lines[:80])
            old_lines = (
                Path(prior[0].markdown_path).read_text(encoding="utf-8").splitlines(keepends=True)
            )
            diff_iter = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"approved:{prior[0].artifact_id}",
                tofile=f"staged:{row.artifact_id}",
            )
            rendered = "".join(diff_iter)
            return rendered or "(No textual diff in Markdown)\n"
        finally:
            engine.dispose()

    def approve_single(
        self,
        *,
        artifact_id: str,
        reviewer: str,
        notes: str | None = None,
    ) -> StagedArtifactRow:
        policy = load_ingestion_policy(self.policy_path)
        engine, repo = self._with_repo()
        try:
            row = repo.get_artifact(artifact_id)
            if row is None:
                raise ValueError(f"Unknown staged artifact: {artifact_id}")
            if row.validation_status != ValidationStatus.VALID.value:
                raise ValueError(
                    f"Cannot approve {artifact_id}: validation_status={row.validation_status!r}. "
                    "Use `pdn-sync stage override` to supply verified human transcription first."
                )
            if row.review_status == ReviewStatus.QUARANTINED.value:
                raise ValueError(
                    f"Cannot approve quarantined artifact {artifact_id} directly; "
                    "use `pdn-sync stage override` to create a verified version first."
                )

            repo.record_review_decision(
                artifact_id=artifact_id,
                reviewer=reviewer,
                decision="approved",
                new_review_status=ReviewStatus.APPROVED,
                policy_mode=policy.approval_mode.value,
                notes=notes,
            )
            updated_row = repo.get_artifact(artifact_id)
            assert updated_row is not None
            staged = ExtractionResult.model_validate_json(
                Path(updated_row.json_path).read_text(encoding="utf-8")
            ).model_copy(update={"review_status": ReviewStatus.APPROVED})
            export_review_snapshot(result=staged, approved=True, data_dir=self.data_dir)
            return updated_row
        finally:
            engine.dispose()

    def batch_approve(
        self,
        *,
        flt: StageQueueFilter,
        reviewer: str,
        dry_run: bool = False,
        notes: str | None = None,
    ) -> BatchApproveSummary:
        """Batch-approve valid, non-quarantined, non-critical-fact artifacts only."""
        policy = load_ingestion_policy(self.policy_path)
        engine, repo = self._with_repo()
        summary = BatchApproveSummary(dry_run=dry_run)
        try:
            candidates = repo.list_staged_artifacts(flt=flt)
            eligible: list[StagedArtifactRow] = []
            for row in candidates:
                if row.review_status == ReviewStatus.APPROVED.value:
                    continue
                flags = json.loads(row.critical_fact_flags_json)
                if (
                    row.validation_status != ValidationStatus.VALID.value
                    or row.review_status == ReviewStatus.QUARANTINED.value
                    or bool(flags)
                ):
                    summary.excluded_critical_or_invalid.append(row.artifact_id)
                    continue
                if row.review_status == ReviewStatus.PENDING_REVIEW.value:
                    eligible.append(row)

            summary.approved_ids = [r.artifact_id for r in eligible]
            if dry_run or not eligible:
                return summary

            batch_id = f"batch_{uuid.uuid4().hex[:12]}"
            summary.batch_id = batch_id
            for row in eligible:
                repo.record_review_decision(
                    artifact_id=row.artifact_id,
                    reviewer=reviewer,
                    decision="approved",
                    new_review_status=ReviewStatus.APPROVED,
                    policy_mode=policy.approval_mode.value,
                    batch_id=batch_id,
                    notes=notes or "gate_b_batch_approval",
                )
                staged = ExtractionResult.model_validate_json(
                    Path(row.json_path).read_text(encoding="utf-8")
                ).model_copy(update={"review_status": ReviewStatus.APPROVED})
                export_review_snapshot(result=staged, approved=True, data_dir=self.data_dir)
            return summary
        finally:
            engine.dispose()

    def reject(
        self,
        *,
        artifact_id: str,
        reviewer: str,
        notes: str | None = None,
    ) -> StagedArtifactRow:
        policy = load_ingestion_policy(self.policy_path)
        engine, repo = self._with_repo()
        try:
            row = repo.get_artifact(artifact_id)
            if row is None:
                raise ValueError(f"Unknown staged artifact: {artifact_id}")
            repo.record_review_decision(
                artifact_id=artifact_id,
                reviewer=reviewer,
                decision="rejected",
                new_review_status=ReviewStatus.REJECTED,
                policy_mode=policy.approval_mode.value,
                notes=notes,
            )
            updated = repo.get_artifact(artifact_id)
            assert updated is not None
            staged = ExtractionResult.model_validate_json(
                Path(updated.json_path).read_text(encoding="utf-8")
            ).model_copy(update={"review_status": ReviewStatus.REJECTED})
            export_review_snapshot(result=staged, approved=False, data_dir=self.data_dir)
            return updated
        finally:
            engine.dispose()

    def override(
        self,
        *,
        artifact_id: str,
        override_file: Path,
        reviewer: str,
        notes: str | None = None,
    ) -> StagedArtifactRow:
        """Create a new immutable human_transcription staged version linked to the same raw blob."""
        if not override_file.is_file():
            raise ValueError(f"Override file not found: {override_file}")

        policy = load_ingestion_policy(self.policy_path)
        engine, repo = self._with_repo()
        try:
            orig_row = repo.get_artifact(artifact_id)
            if orig_row is None:
                raise ValueError(f"Unknown staged artifact: {artifact_id}")

            orig_result = ExtractionResult.model_validate_json(
                Path(orig_row.json_path).read_text(encoding="utf-8")
            )
            raw_text = override_file.read_text(encoding="utf-8")

            text_blocks: list[SemanticTextBlock] = []
            tables: list[ExtractedTable] = []
            title = orig_result.title

            if override_file.suffix.lower() == ".json":
                payload = json.loads(raw_text)
                title = payload.get("title", title)
                for idx, blk in enumerate(payload.get("text_blocks", [])):
                    text_blocks.append(
                        SemanticTextBlock(
                            order=int(blk.get("order", idx)),
                            heading_level=blk.get("heading_level"),
                            text=str(blk["text"]),
                        )
                    )
                for idx, tbl in enumerate(payload.get("tables", [])):
                    tables.append(
                        ExtractedTable(
                            table_index=idx,
                            caption=tbl.get("caption"),
                            headers=[str(h) for h in tbl.get("headers", [])],
                            rows=[[str(c) for c in r] for r in tbl.get("rows", [])],
                            units=list(tbl.get("units", [None] * len(tbl.get("headers", [])))),
                            source_selector="human_transcription",
                        )
                    )
            else:
                order = 0
                for para in raw_text.split("\n\n"):
                    cleaned = para.strip()
                    if not cleaned:
                        continue
                    heading_level: int | None = None
                    val = cleaned
                    if cleaned.startswith("#"):
                        stripped = cleaned.lstrip("#")
                        lvl = len(cleaned) - len(stripped)
                        if 1 <= lvl <= 6 and stripped.startswith(" "):
                            heading_level = lvl
                            val = stripped.strip()
                            if order == 0:
                                title = val
                    text_blocks.append(
                        SemanticTextBlock(order=order, heading_level=heading_level, text=val)
                    )
                    order += 1

            version_seq = stage_repo_seq = repo.next_version_seq(orig_row.resource_id)
            new_ver_id = f"ver_{orig_row.resource_id}_{stage_repo_seq:03d}_{uuid.uuid4().hex[:6]}"
            now = datetime.now(UTC)

            draft = ExtractionResult(
                artifact_id=f"art_{uuid.uuid4().hex[:16]}",
                resource_id=orig_result.resource_id,
                resource_version_id=new_ver_id,
                source_url=orig_result.source_url,
                resource_type=orig_result.resource_type,
                category=orig_result.category,
                policy_rule_id=orig_result.policy_rule_id,
                content_hash=orig_result.content_hash,
                semantic_hash="",
                extractor_name="human_transcription",
                extractor_version="1.0.0",
                captured_at_utc=orig_result.captured_at_utc,
                extracted_at_utc=now,
                source_effective_date=orig_result.source_effective_date,
                source_effective_date_provenance=orig_result.source_effective_date_provenance,
                content_type=orig_result.content_type,
                title=title,
                language=orig_result.language,
                text_blocks=text_blocks,
                tables=tables,
                media_refs=orig_result.media_refs,
                confidence=1.0,
                critical_fact_flags=list(orig_result.critical_fact_flags),
                warnings=[],
                provenance={
                    "override_from_artifact_id": orig_row.artifact_id,
                    "transcribed_by": reviewer,
                    "notes": notes,
                },
            )
            draft = draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})
            validated = validate_and_tag(draft, policy)

            json_path, md_path = export_staged_artifact(
                result=validated,
                version_seq=version_seq,
                data_dir=self.data_dir,
            )
            _, new_art_row = repo.save_staged_version(
                result=validated,
                version_seq=version_seq,
                fetch_id=None,
                json_path=json_path,
                markdown_path=md_path,
            )
            repo.record_review_decision(
                artifact_id=new_art_row.artifact_id,
                reviewer=reviewer,
                decision="override_created",
                new_review_status=validated.review_status,
                policy_mode=policy.approval_mode.value,
                notes=notes or f"human_transcription override from {orig_row.artifact_id}",
            )
            return new_art_row
        finally:
            engine.dispose()

    def reextract(
        self,
        *,
        extractor: str,
        reviewer: str,
        dry_run: bool = True,
        pdf_backend: PdfBackend | None = None,
        ocr_backend: OcrBackend | None = None,
    ) -> ReextractSummary:
        """Re-run an extractor over stored immutable blobs (zero network I/O) with carry-forward."""
        prefix_map = {
            "html": HTML_EXTRACTOR_NAME,
            "pdf": PDF_EXTRACTOR_NAME,
            "ocr": OCR_EXTRACTOR_NAME,
            "jevents": JEVENTS_EXTRACTOR_NAME,
        }
        if extractor not in prefix_map:
            raise ValueError(
                f"Unsupported extractor {extractor!r}; choose from html, pdf, ocr, jevents"
            )

        scope, _ = load_crawl_scope(self.scope_path)
        policy = load_ingestion_policy(self.policy_path)
        blob_store = LocalBlobStore(self.data_dir / "blobs")
        summary = ReextractSummary(extractor=extractor, dry_run=dry_run)

        engine, repo = self._with_repo()
        try:
            targets = repo.list_latest_artifacts_by_extractor(prefix_map[extractor])
            summary.total_considered = len(targets)

            for row in targets:
                raw_bytes = blob_store.get_bytes(row.content_hash)
                orig = ExtractionResult.model_validate_json(
                    Path(row.json_path).read_text(encoding="utf-8")
                )
                ver_seq = repo.next_version_seq(row.resource_id)
                ver_id = f"ver_{row.resource_id}_{ver_seq:03d}_{uuid.uuid4().hex[:6]}"

                if extractor == "html":
                    re_res = extract_html(
                        raw_html=raw_bytes,
                        source_url=orig.source_url,
                        resource_id=orig.resource_id,
                        resource_version_id=ver_id,
                        content_hash=orig.content_hash,
                        category=orig.category,
                        scope=scope,
                        captured_at_utc=orig.captured_at_utc,
                        policy_rule_id=orig.policy_rule_id,
                        content_type=orig.content_type,
                    )
                elif extractor == "pdf":
                    re_res = extract_pdf(
                        raw_bytes=raw_bytes,
                        source_url=orig.source_url,
                        resource_id=orig.resource_id,
                        resource_version_id=ver_id,
                        content_hash=orig.content_hash,
                        category=orig.category,
                        captured_at_utc=orig.captured_at_utc,
                        policy_rule_id=orig.policy_rule_id,
                        content_type=orig.content_type,
                        backend=pdf_backend,
                    )
                elif extractor == "jevents":
                    re_res = extract_jevents_eventdetail(
                        raw_html=raw_bytes,
                        source_url=orig.source_url,
                        resource_id=orig.resource_id,
                        resource_version_id=ver_id,
                        content_hash=orig.content_hash,
                        captured_at_utc=orig.captured_at_utc,
                        policy_rule_id=orig.policy_rule_id,
                        content_type=orig.content_type,
                    )
                else:
                    re_res, _ = extract_poster_ocr(
                        raw_bytes=raw_bytes,
                        source_url=orig.source_url,
                        resource_id=orig.resource_id,
                        resource_version_id=ver_id,
                        content_hash=orig.content_hash,
                        category=orig.category,
                        captured_at_utc=orig.captured_at_utc,
                        policy_rule_id=orig.policy_rule_id,
                        content_type=orig.content_type,
                        backend=ocr_backend,
                    )

                validated = validate_and_tag(re_res, policy)
                prior_approved = repo.get_latest_approved_artifact(row.resource_id)
                unchanged = (
                    prior_approved is not None
                    and prior_approved[0].semantic_hash == validated.semantic_hash
                    and validated.validation_status == ValidationStatus.VALID
                    and validated.review_status != ReviewStatus.QUARANTINED
                )

                if unchanged:
                    summary.unchanged_carry_forward += 1
                    validated = validated.model_copy(
                        update={"review_status": ReviewStatus.APPROVED}
                    )
                elif validated.review_status == ReviewStatus.QUARANTINED:
                    summary.quarantined += 1
                else:
                    summary.changed_pending_review += 1

                if dry_run:
                    continue

                json_path, md_path = export_staged_artifact(
                    result=validated,
                    version_seq=ver_seq,
                    data_dir=self.data_dir,
                )
                repo.save_staged_version(
                    result=validated,
                    version_seq=ver_seq,
                    fetch_id=None,
                    json_path=json_path,
                    markdown_path=md_path,
                )
                if unchanged and prior_approved is not None:
                    repo.record_review_decision(
                        artifact_id=validated.artifact_id,
                        reviewer=f"reextract:{reviewer}",
                        decision="carried_forward_extractor_upgrade",
                        new_review_status=ReviewStatus.APPROVED,
                        policy_mode=policy.approval_mode.value,
                        carried_from_decision_id=prior_approved[1].decision_id,
                        notes=f"semantic_hash unchanged after {extractor} reextract",
                    )
                summary.new_artifact_ids.append(validated.artifact_id)

            return summary
        finally:
            engine.dispose()

    def check_publish_eligibility(self) -> tuple[list[str], list[tuple[str, str]]]:
        """Verify publisher-layer Gate B enforcement across all staged artifacts (dry-run)."""
        engine, repo = self._with_repo()
        ready: list[str] = []
        blocked: list[tuple[str, str]] = []
        try:
            all_rows = repo.list_staged_artifacts(flt=StageQueueFilter(limit=0))
            for row in all_rows:
                try:
                    repo.assert_artifact_approved_for_publish(row.artifact_id)
                    ready.append(row.artifact_id)
                except GateBPublishBlockedError as exc:
                    blocked.append((row.artifact_id, str(exc)))
            return ready, blocked
        finally:
            engine.dispose()


__all__ = [
    "BatchApproveSummary",
    "ExtractionOptions",
    "ExtractionRunSummary",
    "GateBService",
    "ReextractSummary",
    "render_extraction_markdown",
    "run_extraction_pipeline",
]
