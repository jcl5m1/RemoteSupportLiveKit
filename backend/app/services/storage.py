"""GCS client helpers: signed URLs, object listing, purge."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from google.cloud import storage as gcs

from ..config import get_settings

if TYPE_CHECKING:
    from ..models import Recording

settings = get_settings()


def _bucket(client: gcs.Client) -> gcs.Bucket:
    return client.bucket(settings.gcs_bucket)


def gcs_uri(filename: str) -> str:
    return f"gs://{settings.gcs_bucket}/{filename}"


async def delete_session_objects(client: gcs.Client, session_id: uuid.UUID) -> int:
    """Delete all objects under sessions/{session_id}/. Returns count deleted."""
    prefix = f"sessions/{session_id}/"
    blobs = list(_bucket(client).list_blobs(prefix=prefix))
    for blob in blobs:
        blob.delete()
    return len(blobs)


def make_signed_url(client: gcs.Client, gcs_uri_str: str, ttl_seconds: int | None = None) -> str:
    """V4 signed URL for a gs:// URI."""
    ttl = ttl_seconds or settings.signed_url_ttl_seconds
    if not gcs_uri_str.startswith(f"gs://{settings.gcs_bucket}/"):
        raise ValueError("URI belongs to a different bucket")
    blob_name = gcs_uri_str[len(f"gs://{settings.gcs_bucket}/") :]
    blob = _bucket(client).blob(blob_name)
    url: str = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=ttl),
        method="GET",
    )
    return url


def add_download_urls(
    client: gcs.Client,
    recordings: list[Recording],
) -> list[dict]:
    """Attach V4 signed download URLs to recording info dicts."""
    result = []
    for rec in recordings:
        info = {
            "kind": rec.kind.value,
            "role": rec.role.value if rec.role else None,
            "state": rec.state.value,
            "mime_type": rec.mime_type,
            "duration_ms": rec.duration_ms,
            "size_bytes": rec.size_bytes,
            "gcs_uri": rec.gcs_uri,
        }
        if rec.gcs_uri:
            info["download_url"] = make_signed_url(client, rec.gcs_uri)
        result.append(info)
    return result
