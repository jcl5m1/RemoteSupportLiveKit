"""GCS client helpers: signed URLs, object listing, purge."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
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


_MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".ogg": "audio/ogg"}
# <UTC stamp>_<identity>[_<source>_<track_sid>].<ext>; the suffix is the older
# per-track format, still listed so earlier recordings stay viewable.
_CALL_FILE_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}Z)_(?P<identity>guest-[a-z0-9-]+?)"
    r"(?:_(?P<source>[a-z_]+)_(?P<track>TR_\w+))?\.(?P<ext>mp4|webm|ogg)$"
)


def list_call_recordings(client: gcs.Client, call_id: str) -> list[dict]:
    """Media files under calls/<call_id>/ with signed URLs, oldest first."""
    prefix = f"calls/{call_id}/"
    items = []
    for blob in _bucket(client).list_blobs(prefix=prefix):
        match = _CALL_FILE_RE.match(blob.name[len(prefix) :])
        if match is None or not blob.size:
            continue
        identity = match["identity"]
        # guest-<name-slug>-<8 hex> -> "name slug"; bare guest-<hex> -> "Guest".
        slug = identity.removeprefix("guest-").rpartition("-")[0]
        started = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        items.append(
            {
                "name": blob.name[len(prefix) :],
                "participant": slug.replace("-", " ").title() or "Guest",
                "identity": identity,
                "source": match["source"] or "camera+microphone",
                "started_at": started,
                "size_bytes": blob.size,
                "content_type": _MEDIA_TYPES["." + match["ext"]],
                "url": blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=settings.signed_url_ttl_seconds),
                    method="GET",
                ),
            }
        )
    items.sort(key=lambda i: (i["started_at"], i["name"]))
    return items


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
