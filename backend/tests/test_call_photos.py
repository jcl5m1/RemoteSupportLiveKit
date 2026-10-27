"""Phone photos taken on request during an open call."""

from __future__ import annotations

import json

import pytest

from app.services import call_events
from tests.test_call_events import _join, call_env  # noqa: F401  (fixture)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class _PhotoBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @property
    def size(self):
        return len(self.store[self.name][0])

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = (data, content_type)

    def generate_signed_url(self, **kw):
        return f"https://signed.example/{self.name}"


class _PhotoBucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return _PhotoBlob(self.store, name)

    def list_blobs(self, prefix):
        return [_PhotoBlob(self.store, n) for n in sorted(self.store) if n.startswith(prefix)]


@pytest.fixture
def gcs(app, call_env):  # noqa: F811
    store = call_env.store
    app.state.gcs_client = type("C", (), {"bucket": lambda self, n: _PhotoBucket(store)})()
    return store


@pytest.mark.asyncio
async def test_photo_upload_is_stored_logged_and_listed(client, gcs):
    bob = await _join(client, "Bob")
    resp = await client.post(
        "/v1/calls/team-sync/photos?requested_by=guest-alice-1&width=4032&height=3024",
        content=JPEG,
        headers={"Authorization": f"Bearer {bob}", "Content-Type": "image/jpeg"},
    )
    assert resp.status_code == 201, resp.text
    photo = resp.json()
    assert photo["participant"] == "Bob" and photo["identity"].startswith("guest-bob")
    assert photo["content_type"] == "image/jpeg" and photo["size_bytes"] == len(JPEG)
    assert photo["name"].startswith("photos/") and photo["name"].endswith(".jpg")
    key = f"calls/team-sync/{photo['name']}"
    assert gcs[key] == (JPEG, "image/jpeg")
    assert photo["path"] == f"/v1/calls/team-sync/{photo['name']}"

    # Stable link redirects to a fresh signed URL (no auth, like recordings).
    resp = await client.get(photo["path"], follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"].endswith(key)
    assert (await client.get("/v1/calls/team-sync/photos/../x.jpg")).status_code == 404

    listed = (await client.get("/v1/calls/team-sync/recordings")).json()["photos"]
    assert [p["name"] for p in listed] == [photo["name"]]

    exporter = type("G", (), {"bucket": lambda self, n: _PhotoBucket(gcs)})()
    await call_events.export_now(exporter, "team-sync", "RM_test123")
    (log,) = [k for k in gcs if k.endswith("_events.jsonl")]
    rows = [json.loads(line) for line in gcs[log][0].splitlines()]
    (ev,) = [r for r in rows if r["type"] == "photo"]
    assert ev["data"] == {"name": photo["name"], "requested_by": "guest-alice-1",
                          "size_bytes": len(JPEG), "width": 4032, "height": 3024}


@pytest.mark.asyncio
async def test_photo_upload_validates(client, gcs):
    bob = await _join(client, "Bob")
    auth = {"Authorization": f"Bearer {bob}"}
    url = "/v1/calls/team-sync/photos"
    resp = await client.post(url, content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert resp.status_code == 401
    resp = await client.post(url, content=b"GIF89a", headers={**auth, "Content-Type": "image/gif"})
    assert resp.status_code == 415
    jpeg_auth = {**auth, "Content-Type": "image/jpeg"}
    resp = await client.post(url, content=b"not a jpeg", headers=jpeg_auth)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_photo"
    resp = await client.post(
        url, content=JPEG, headers={**auth, "Content-Type": "image/jpeg",
                                    "Content-Length": str(26 * 1024 * 1024)},
    )
    assert resp.status_code == 413
    assert not [k for k in gcs if "/photos/" in k]
