"""Deterministic retrieval evaluation and multilingual embedding benchmark runner."""

from __future__ import annotations

import shutil
import statistics
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from pdn_sync.config import AppSettings
from pdn_sync.indexing.embedder import EmbeddingBackend, create_embedder
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from pdn_sync.persistence.db import create_sqlite_engine
from pdn_sync.search.hybrid import ChunkBM25SearchService, fuse_rrf
from pdn_sync.search.tables import StructuredTableSearchService


class EvalQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    lang: Literal["ms", "en", "any"] = "any"
    category: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_urls: list[str] = Field(min_length=1)
    expected_facts: list[str] = Field(default_factory=list)
    search_target: Literal["knowledge", "tables", "both"] = "knowledge"


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    description: str = ""
    questions: list[EvalQuestion] = Field(min_length=1)


@dataclass(slots=True)
class QuestionEvalResult:
    question_id: str
    lang: str
    category: str
    query: str
    hit_at_1: bool
    hit_at_5: bool
    reciprocal_rank: float
    fact_recall_at_5: float
    citation_valid: bool
    latency_ms: float
    top_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EvalReport:
    model_name: str
    questions_total: int
    hit_at_1: float
    hit_at_5: float
    mrr_at_5: float
    fact_recall_at_5: float
    citation_accuracy: float
    latency_p50_ms: float
    latency_p95_ms: float
    index_seconds: float | None = None
    peak_rss_mb: float | None = None
    per_question: list[QuestionEvalResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "questions_total": self.questions_total,
            "hit_at_1": round(self.hit_at_1, 4),
            "hit_at_5": round(self.hit_at_5, 4),
            "mrr_at_5": round(self.mrr_at_5, 4),
            "fact_recall_at_5": round(self.fact_recall_at_5, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
            "latency_p50_ms": round(self.latency_p50_ms, 2),
            "latency_p95_ms": round(self.latency_p95_ms, 2),
            "index_seconds": round(self.index_seconds, 2)
            if self.index_seconds is not None
            else None,
            "peak_rss_mb": round(self.peak_rss_mb, 2) if self.peak_rss_mb is not None else None,
        }


def load_eval_suite(path: Path) -> EvalSuite:
    yaml = YAML(typ="safe")
    raw = yaml.load(path.read_text(encoding="utf-8"))
    return EvalSuite.model_validate(raw)


def _normalize_url_for_match(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _current_rss_mb() -> float | None:
    try:
        import psutil

        return round(float(psutil.Process().memory_info().rss) / (1024.0 * 1024.0), 2)
    except Exception:
        return None


def evaluate_retrieval(
    *,
    suite: EvalSuite,
    embedder: EmbeddingBackend,
    qdrant_store: QdrantVectorStore,
    table_search: StructuredTableSearchService | None = None,
    bm25_search: ChunkBM25SearchService | None = None,
    retrieval_mode: Literal["dense", "hybrid"] = "dense",
    rrf_k: int = 60,
    hybrid_candidate_multiplier: int = 4,
    top_k: int = 5,
    index_seconds: float | None = None,
) -> EvalReport:
    """Run all EvalQuestions against Qdrant (+ optional SQLite FTS5 BM25 RRF) and tables."""
    results: list[QuestionEvalResult] = []
    latencies: list[float] = []

    for q in suite.questions:
        t0 = time.monotonic()
        retrieved_urls: list[str] = []
        retrieved_texts: list[str] = []
        citations_ok = True

        if q.search_target in {"knowledge", "both"}:
            q_vec = embedder.embed_query(q.query)
            if retrieval_mode == "hybrid" and bm25_search is not None:
                cand_k = max(top_k, top_k * hybrid_candidate_multiplier)
                dense_hits = qdrant_store.search(q_vec, limit=cand_k, lang="any")
                bm25_hits = bm25_search.search_chunks(q=q.query, lang="any", limit=cand_k)
                fused = fuse_rrf(
                    dense_hits=dense_hits,
                    bm25_hits=bm25_hits,
                    k=rrf_k,
                    limit=top_k,
                )
                for fh in fused:
                    url = str(fh.payload.get("source_url") or "")
                    lv = str(fh.payload.get("last_verified_at") or "")
                    if not url or not lv:
                        citations_ok = False
                    retrieved_urls.append(url)
                    retrieved_texts.append(str(fh.payload.get("text") or ""))
            else:
                hits = qdrant_store.search(q_vec, limit=top_k, lang="any")
                for h in hits:
                    url = str(h.payload.get("source_url") or "")
                    lv = str(h.payload.get("last_verified_at") or "")
                    if not url or not lv:
                        citations_ok = False
                    retrieved_urls.append(url)
                    retrieved_texts.append(str(h.payload.get("text") or ""))

        if q.search_target in {"tables", "both"} and table_search is not None:
            thits = table_search.search_tables(q=q.query, limit=top_k)
            for th in thits:
                if not th.source_url or not th.last_verified_at:
                    citations_ok = False
                if th.source_url not in retrieved_urls:
                    retrieved_urls.append(th.source_url)
                flat_rows = " ".join(" ".join(r) for r in th.rows)
                retrieved_texts.append(f"{th.caption or ''} {' '.join(th.headers)} {flat_rows}")

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        latencies.append(elapsed_ms)

        # Deduplicate ranked URLs while preserving rank order
        dedup_urls: list[str] = []
        seen_norm: set[str] = set()
        for u in retrieved_urls:
            nu = _normalize_url_for_match(u)
            if nu and nu not in seen_norm:
                seen_norm.add(nu)
                dedup_urls.append(u)
        top_urls = dedup_urls[:top_k]

        expected_norm = {_normalize_url_for_match(u) for u in q.expected_urls}
        rr = 0.0
        for rank, u in enumerate(top_urls, start=1):
            if _normalize_url_for_match(u) in expected_norm:
                rr = 1.0 / rank
                break

        hit_1 = bool(top_urls and _normalize_url_for_match(top_urls[0]) in expected_norm)
        hit_5 = rr > 0.0

        combined_text = "\n".join(retrieved_texts[: top_k * 2]).lower()
        if q.expected_facts:
            matched_facts = sum(
                1 for fact in q.expected_facts if fact.strip().lower() in combined_text
            )
            fact_recall = matched_facts / len(q.expected_facts)
        else:
            fact_recall = 1.0 if hit_5 else 0.0

        results.append(
            QuestionEvalResult(
                question_id=q.id,
                lang=q.lang,
                category=q.category,
                query=q.query,
                hit_at_1=hit_1,
                hit_at_5=hit_5,
                reciprocal_rank=rr,
                fact_recall_at_5=fact_recall,
                citation_valid=bool(top_urls) and citations_ok,
                latency_ms=elapsed_ms,
                top_urls=top_urls,
            )
        )

    n = len(results)
    sorted_lat = sorted(latencies)
    p50 = statistics.median(sorted_lat) if sorted_lat else 0.0
    p95_idx = min(len(sorted_lat) - 1, max(0, int(len(sorted_lat) * 0.95)))
    p95 = sorted_lat[p95_idx] if sorted_lat else 0.0

    return EvalReport(
        model_name=embedder.model_name,
        questions_total=n,
        hit_at_1=sum(1 for r in results if r.hit_at_1) / n,
        hit_at_5=sum(1 for r in results if r.hit_at_5) / n,
        mrr_at_5=sum(r.reciprocal_rank for r in results) / n,
        fact_recall_at_5=sum(r.fact_recall_at_5 for r in results) / n,
        citation_accuracy=sum(1 for r in results if r.citation_valid) / n,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        index_seconds=index_seconds,
        peak_rss_mb=_current_rss_mb(),
        per_question=results,
    )


def _make_benchmark_cb(model_name: str) -> Callable[[str], None]:
    def _cb(msg: str) -> None:
        print(f"[benchmark:{model_name}] {msg}", flush=True)

    return _cb


def benchmark_embedding_models(
    *,
    models: Sequence[str],
    questions_path: Path,
    source_sqlite_path: Path,
    scope_path: Path,
    policy_path: Path,
    settings: AppSettings | None = None,
) -> list[EvalReport]:
    """Benchmark candidate embedding models cleanly in isolated temp SQLite + Qdrant dirs."""
    suite = load_eval_suite(questions_path)
    cfg = settings or AppSettings()
    reports: list[EvalReport] = []

    for model_name in models:
        with tempfile.TemporaryDirectory(prefix="pdn_eval_") as tmp:
            tmp_dir = Path(tmp)
            tmp_db = tmp_dir / "pdn_eval.db"
            shutil.copy2(source_sqlite_path, tmp_db)
            tmp_qdrant_dir = tmp_dir / "qdrant"
            collection = f"eval_{abs(hash(model_name)) % 100000}"

            embedder = create_embedder(model_name)
            with QdrantVectorStore(
                collection_name=collection,
                path=tmp_qdrant_dir,
            ) as qdrant:
                pub_svc = PublicationService(
                    sqlite_path=tmp_db,
                    scope_path=scope_path,
                    policy_path=policy_path,
                    qdrant_store=qdrant,
                    embedder=embedder,
                    settings=cfg,
                )
                t0 = time.monotonic()
                pub_svc.publish_approved(
                    dry_run=False,
                    embedding_model=model_name,
                    qdrant_collection=collection,
                    progress_cb=_make_benchmark_cb(model_name),
                )
                idx_secs = time.monotonic() - t0

                engine = create_sqlite_engine(tmp_db)
                try:
                    table_search = StructuredTableSearchService(engine)
                    report = evaluate_retrieval(
                        suite=suite,
                        embedder=embedder,
                        qdrant_store=qdrant,
                        table_search=table_search,
                        index_seconds=idx_secs,
                    )
                    reports.append(report)
                finally:
                    engine.dispose()
            if hasattr(embedder, "release"):
                embedder.release()

    return reports
