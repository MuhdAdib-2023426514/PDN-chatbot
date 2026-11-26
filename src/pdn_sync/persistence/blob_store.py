"""Content-addressed immutable BlobStore interface and local filesystem implementation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    sha256: str
    byte_size: int
    path: Path
    deduplicated: bool


class BlobStore(Protocol):
    """Abstract immutable blob storage interface (local in Phase 2; S3/MinIO drop-in later)."""

    def put_bytes(self, data: bytes) -> BlobMetadata: ...

    def get_bytes(self, sha256: str) -> bytes: ...

    def exists(self, sha256: str) -> bool: ...

    def path_for(self, sha256: str) -> Path: ...


class LocalBlobStore:
    """Content-addressed immutable local store at <root_dir>/<sha256[:2]>/<sha256>."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    @staticmethod
    def _validate_sha256(sha256: str) -> str:
        cleaned = sha256.strip().lower()
        if len(cleaned) != 64 or any(ch not in "0123456789abcdef" for ch in cleaned):
            msg = f"Invalid SHA-256 digest: {sha256!r}"
            raise ValueError(msg)
        return cleaned

    def path_for(self, sha256: str) -> Path:
        digest = self._validate_sha256(sha256)
        return self.root_dir / digest[:2] / digest

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def put_bytes(self, data: bytes) -> BlobMetadata:
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        if target.is_file():
            return BlobMetadata(
                sha256=digest,
                byte_size=len(data),
                path=target,
                deduplicated=True,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{digest[:12]}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

        return BlobMetadata(
            sha256=digest,
            byte_size=len(data),
            path=target,
            deduplicated=False,
        )

    def get_bytes(self, sha256: str) -> bytes:
        target = self.path_for(sha256)
        if not target.is_file():
            raise FileNotFoundError(f"Blob not found in LocalBlobStore: {sha256}")
        return target.read_bytes()
