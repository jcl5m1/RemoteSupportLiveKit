"""GCS client helpers: signed URLs, object listing, purge."""

from __future__ import annotations

import re
import secrets
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


_EVENTS_FILE_RE = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)_(?P<sid>[\w-]+)_events\.jsonl$")


def list_call_event_logs(client: gcs.Client, call_id: str) -> list[dict]:
    """Interaction logs (``*_events.jsonl``) under calls/<call_id>/, oldest first."""
    prefix = f"calls/{call_id}/"
    logs = []
    for blob in _bucket(client).list_blobs(prefix=prefix):
        match = _EVENTS_FILE_RE.match(blob.name[len(prefix) :])
        if match is None or not blob.size:
            continue
        logs.append(
            {
                "name": blob.name[len(prefix) :],
                "started_at": datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC
                ),
                "size_bytes": blob.size,
                "url": blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=settings.signed_url_ttl_seconds),
                    method="GET",
                ),
            }
        )
    logs.sort(key=lambda i: i["started_at"])
    return logs


# photos/<UTC stamp with ms>_<identity>_<6 hex>.<ext>
_PHOTO_FILE_RE = re.compile(
    r"^photos/(?P<stamp>\d{8}T\d{6})(?P<ms>\d{3})Z_(?P<identity>guest-[a-z0-9-]+?)"
    r"_(?P<rand>[0-9a-f]{6})\.(?P<ext>jpg|png|webp)$"
)
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_PHOTO_CTYPES = {ext: ctype for ctype, ext in PHOTO_TYPES.items()}


def _signed(blob: gcs.Blob) -> str:
    return str(
        blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=settings.signed_url_ttl_seconds),
            method="GET",
        )
    )


def _photo_info(call_id: str, name: str, size: int, blob: gcs.Blob) -> dict | None:
    match = _PHOTO_FILE_RE.match(name)
    if match is None:
        return None
    identity = match["identity"]
    slug = identity.removeprefix("guest-").rpartition("-")[0]
    taken = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%S").replace(
        tzinfo=UTC, microsecond=int(match["ms"]) * 1000
    )
    return {
        "name": name,
        "participant": slug.replace("-", " ").title() or "Guest",
        "identity": identity,
        "taken_at": taken,
        "size_bytes": size,
        "content_type": _PHOTO_CTYPES[match["ext"]],
        "path": f"/v1/calls/{call_id}/{name}",
        "url": _signed(blob),
    }


def save_call_photo(
    client: gcs.Client, call_id: str, identity: str, data: bytes, content_type: str
) -> dict:
    """Store a phone photo at calls/<call_id>/photos/... and describe it."""
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    name = f"photos/{stamp}_{identity}_{secrets.token_hex(3)}.{PHOTO_TYPES[content_type]}"
    blob = _bucket(client).blob(f"calls/{call_id}/{name}")
    blob.upload_from_string(data, content_type=content_type)
    info = _photo_info(call_id, name, len(data), blob)
    if info is None:  # identity outside the guest pattern
        raise ValueError(f"unexpected photo name {name!r}")
    return info


def call_photo_url(client: gcs.Client, call_id: str, name: str) -> str | None:
    """Fresh signed URL for one stored photo, or None if the name is not a photo."""
    if _PHOTO_FILE_RE.match(name) is None:
        return None
    return _signed(_bucket(client).blob(f"calls/{call_id}/{name}"))


def list_call_photos(client: gcs.Client, call_id: str) -> list[dict]:
    """Phone photos under calls/<call_id>/photos/, oldest first."""
    prefix = f"calls/{call_id}/"
    photos = []
    for blob in _bucket(client).list_blobs(prefix=f"{prefix}photos/"):
        if not blob.size:
            continue
        info = _photo_info(call_id, blob.name[len(prefix) :], blob.size, blob)
        if info:
            photos.append(info)
    photos.sort(key=lambda i: i["taken_at"])
    return photos


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
