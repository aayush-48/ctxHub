"""Storage for uploaded export files: Cloud Storage in production, a bytea column otherwise."""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from ..config import Settings


class Blob(Protocol):
    async def put(self, repo_id: uuid.UUID, upload_id: uuid.UUID, data: bytes) -> str | None:
        """Store data. Returns a gs:// URI, or None if the caller must keep the bytes itself."""

    async def get(self, uri: str) -> bytes: ...

    async def delete_repo(self, repo_id: uuid.UUID) -> None: ...


class DbBlob:
    async def put(self, repo_id: uuid.UUID, upload_id: uuid.UUID, data: bytes) -> str | None:
        return None

    async def get(self, uri: str) -> bytes:
        raise RuntimeError("db storage keeps bytes on the upload row")

    async def delete_repo(self, repo_id: uuid.UUID) -> None:
        return None


class GcsBlob:
    def __init__(self, bucket: str):
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket)
        self._name = bucket

    async def put(self, repo_id: uuid.UUID, upload_id: uuid.UUID, data: bytes) -> str | None:
        path = f"repos/{repo_id}/uploads/{upload_id}"
        blob = self._bucket.blob(path)
        await asyncio.to_thread(blob.upload_from_string, data, "application/octet-stream")
        return f"gs://{self._name}/{path}"

    async def get(self, uri: str) -> bytes:
        path = uri.removeprefix(f"gs://{self._name}/")
        return await asyncio.to_thread(self._bucket.blob(path).download_as_bytes)

    async def delete_repo(self, repo_id: uuid.UUID) -> None:
        def _delete() -> None:
            for b in self._bucket.list_blobs(prefix=f"repos/{repo_id}/"):
                b.delete()

        await asyncio.to_thread(_delete)


def make_blob(settings: Settings) -> Blob:
    if settings.storage_provider == "gcs":
        if not settings.gcs_bucket:
            raise RuntimeError("CTX_GCS_BUCKET is required for storage_provider=gcs")
        return GcsBlob(settings.gcs_bucket)
    return DbBlob()
