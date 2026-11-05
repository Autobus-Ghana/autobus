"""Orchestrate storage upload, text extraction, and RAG indexing with progress."""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import UploadFile
from starlette.datastructures import Headers

from core.cloudstorage.dto.filedto import FileUploadRagResponse
from core.cloudstorage.service.file_content_extractor import FileContentExtractor
from core.cloudstorage.service.storageservice import StorageFolder, StorageService
from core.rag.document_indexer import index_extracted_text_for_user
from core.rag.rag_index_job_store import RagIndexJobStatus, RagIndexJobStore
from core.rag.website_content_extractor import (
    WebsiteContentExtractor,
    filename_from_url,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[[RagIndexJobStatus, int, str], None]
_CREDIT_SHORTAGE = (
    "You don't have enough credits for this. Buy more credits to continue."
)


def _charge_storage_credits(user_id: str, raw: bytes, operation: str) -> None:
    """Deduct storage credits for bytes that will be saved. Raises ValueError."""
    from fastapi import HTTPException

    from core.credits.model.credit_types import CreditType
    from core.credits.service.credit_service import CreditService
    from utilities.dbconfig import SessionLocal

    size_mb = max(0.01, len(raw) / (1024 * 1024))
    db = SessionLocal()
    try:
        CreditService(db).require_credits(
            str(user_id),
            CreditType.STORAGE_MB.value,
            size_mb,
            operation,
        )
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or _CREDIT_SHORTAGE)
        else:
            message = str(detail or _CREDIT_SHORTAGE)
        if len(message) > 180 or "credit" not in message.lower():
            message = _CREDIT_SHORTAGE
        raise ValueError(message) from exc
    finally:
        db.close()


def _safe_user_prefix(subject: str) -> str:
    safe = (subject or "unknown").strip()
    safe = safe.replace("\\", "_").replace("/", "_")
    return f"{safe}/"


class RagIndexService:
    def __init__(
        self,
        *,
        storage_service: Optional[StorageService] = None,
        job_store: Optional[RagIndexJobStore] = None,
    ) -> None:
        self.storage_service = storage_service or StorageService()
        self.job_store = job_store or RagIndexJobStore()

    def run_file_job(
        self,
        *,
        job_id: str,
        subject: str,
        user_data: Dict[str, Any],
        raw: bytes,
        safe_name: str,
        content_type: Optional[str],
        folder: StorageFolder,
    ) -> None:
        def report(status: RagIndexJobStatus, progress: int, message: str) -> None:
            self.job_store.update_job(
                job_id, status=status, progress=progress, message=message
            )

        try:
            result = self._index_file_bytes(
                subject=subject,
                user_data=user_data,
                raw=raw,
                safe_name=safe_name,
                content_type=content_type,
                folder=folder,
                on_progress=report,
            )
            self.job_store.update_job(
                job_id,
                status=RagIndexJobStatus.completed,
                progress=100,
                message="Indexing complete",
                result=result.model_dump(mode="json"),
            )
        except Exception as e:
            logger.error("[RAG] file index job %s failed: %s", job_id, e, exc_info=True)
            self.job_store.update_job(
                job_id,
                status=RagIndexJobStatus.failed,
                progress=100,
                message="Indexing failed",
                error=str(e),
            )

    async def run_url_job(
        self,
        *,
        job_id: str,
        subject: str,
        user_data: Dict[str, Any],
        url: str,
        folder: StorageFolder,
    ) -> None:
        def report(status: RagIndexJobStatus, progress: int, message: str) -> None:
            self.job_store.update_job(
                job_id, status=status, progress=progress, message=message
            )

        try:
            report(RagIndexJobStatus.scraping, 15, "Fetching page content")
            extracted = await WebsiteContentExtractor.fetch_text(url)
            safe_name = filename_from_url(url)
            raw = extracted.encode("utf-8")

            report(RagIndexJobStatus.validating, 25, "Checking storage credits")
            user_id = user_data.get("db_user_id")
            if not user_id:
                raise ValueError("Could not resolve the account for this upload")
            _charge_storage_credits(str(user_id), raw, "rag_url")

            report(RagIndexJobStatus.uploading, 30, "Saving scraped content")
            result = self._index_file_bytes(
                subject=subject,
                user_data=user_data,
                raw=raw,
                safe_name=safe_name,
                content_type="text/plain",
                folder=folder,
                extracted_override=extracted,
                source_url=url,
                on_progress=report,
            )
            self.job_store.update_job(
                job_id,
                status=RagIndexJobStatus.completed,
                progress=100,
                message="Indexing complete",
                result=result.model_dump(mode="json"),
            )
        except Exception as e:
            logger.error("[RAG] url index job %s failed: %s", job_id, e, exc_info=True)
            self.job_store.update_job(
                job_id,
                status=RagIndexJobStatus.failed,
                progress=100,
                message="Indexing failed",
                error=str(e),
            )

    def index_file_sync(
        self,
        *,
        subject: str,
        user_data: Dict[str, Any],
        raw: bytes,
        safe_name: str,
        content_type: Optional[str],
        folder: StorageFolder,
        upload_file: UploadFile,
    ) -> FileUploadRagResponse:
        extracted = FileContentExtractor.extract_content(upload_file, raw)
        return self._index_file_bytes(
            subject=subject,
            user_data=user_data,
            raw=raw,
            safe_name=safe_name,
            content_type=content_type,
            folder=folder,
            extracted_override=extracted,
        )

    def _index_file_bytes(
        self,
        *,
        subject: str,
        user_data: Dict[str, Any],
        raw: bytes,
        safe_name: str,
        content_type: Optional[str],
        folder: StorageFolder,
        extracted_override: Optional[str] = None,
        source_url: Optional[str] = None,
        on_progress: Optional[ProgressFn] = None,
    ) -> FileUploadRagResponse:
        if on_progress:
            on_progress(RagIndexJobStatus.validating, 5, "Validating content")

        if not raw:
            raise ValueError("Empty file")

        user_prefix = _safe_user_prefix(subject)
        key_name = f"{user_prefix}{safe_name}"

        if on_progress:
            on_progress(RagIndexJobStatus.uploading, 20, "Uploading to storage")

        object_metadata: Optional[Dict[str, str]] = None
        if source_url:
            # Use underscore keys: some S3-compatible stores mishandle hyphenated
            # x-amz-meta-* names so GET /me/files never sees source_url / source_type.
            object_metadata = {
                "source_type": "website",
                "source_url": source_url,
            }

        url = self.storage_service.upload_file(
            io.BytesIO(raw),
            key_name,
            content_type=content_type or "text/plain",
            folder=folder,
            metadata=object_metadata,
        )
        object_key = f"{self.storage_service.resolve_subfolder(folder=folder)}{key_name}"

        if on_progress:
            on_progress(RagIndexJobStatus.extracting, 40, "Extracting text")

        extracted = extracted_override
        if extracted is None:
            pseudo_headers = (
                Headers({"content-type": content_type}) if content_type else None
            )
            pseudo = UploadFile(
                filename=safe_name,
                file=io.BytesIO(raw),
                headers=pseudo_headers,
            )
            extracted = FileContentExtractor.extract_content(pseudo, raw)

        if on_progress:
            on_progress(RagIndexJobStatus.chunking, 55, "Preparing chunks for indexing")

        def indexing_progress(done: int, total: int) -> None:
            if not on_progress or total <= 0:
                return
            # Map batch progress into 55–95%
            frac = done / total
            pct = 55 + int(40 * frac)
            on_progress(
                RagIndexJobStatus.indexing,
                pct,
                f"Indexing chunks ({done}/{total})",
            )

        n_chunks, rag_detail = index_extracted_text_for_user(
            user_data=user_data,
            object_key=object_key,
            file_name=safe_name,
            extracted_text=extracted or "",
            on_index_progress=indexing_progress,
            source_url=source_url,
        )

        if on_progress:
            on_progress(RagIndexJobStatus.indexing, 98, "Finalizing")

        return FileUploadRagResponse(
            file_name=safe_name,
            file_url=url,
            folder=folder.value,
            object_key=object_key,
            uploaded_at=datetime.now(timezone.utc),
            source_type="website" if source_url else "document",
            source_url=source_url,
            rag_indexed_chunks=n_chunks,
            rag_detail=rag_detail,
        )
