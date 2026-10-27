"""/show link validation: classification, SSRF guard, redirects, endpoint."""

from __future__ import annotations

import httpx
import pytest

from app.services import media_check as mc

H = httpx.Headers


@pytest.mark.parametrize(
    "url, vid",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/channel/UCxyz", None),
        ("https://notyoutube.com/watch?v=dQw4w9WgXcQ", None),
    ],
)
def test_youtube_id(url, vid):
    assert mc.youtube_id(url) == vid


@pytest.mark.parametrize(
    "url, q",
    [
        ("https://www.google.com/maps/place/Eiffel+Tower/@48.85,2.29,17z", "Eiffel+Tower"),
        ("https://www.google.com/maps/@37.4219999,-122.0840575,15z", "37.4219999%2C-122.0840575"),
        ("https://maps.google.com/maps?q=Golden+Gate+Bridge", "Golden+Gate+Bridge"),
        ("https://www.google.co.uk/maps/search/coffee", "coffee"),
    ],
)
def test_google_maps_embed(url, q):
    assert mc.google_maps_embed(url) == f"https://maps.google.com/maps?q={q}&output=embed"


def test_google_maps_embed_rejects_non_maps():
    assert mc.google_maps_embed("https://www.google.com/search?q=maps") is None
    assert mc.google_maps_embed("https://example.com/maps/place/x") is None


@pytest.mark.parametrize(
    "ctype, url, kind, embedded",
    [
        ("image/png", "https://x.test/a", "image", True),
        ("audio/mpeg", "https://x.test/a", "audio", True),
        ("video/mp4; codecs=avc1", "https://x.test/a", "video", True),
        (None, "https://x.test/clip.webm", "video", True),
        ("application/octet-stream", "https://x.test/song.mp3", "audio", True),
        ("text/html; charset=utf-8", "https://x.test/", "webpage", True),
        ("application/pdf", "https://x.test/doc.pdf", "link", False),
    ],
)
def test_classify_by_content(ctype, url, kind, embedded):
    info = mc.classify(url, url, ctype, H({}), 200)
    assert info.kind == kind
    assert (info.embed_url is not None) is embedded


@pytest.mark.parametrize(
    "headers, frameable",
    [
        ({}, True),
        ({"X-Frame-Options": "DENY"}, False),
        ({"X-Frame-Options": "SAMEORIGIN"}, False),
        ({"Content-Security-Policy": "frame-ancestors 'self'"}, False),
        ({"Content-Security-Policy": "frame-ancestors 'none'"}, False),
        ({"Content-Security-Policy": "frame-ancestors *"}, True),
        ({"Content-Security-Policy": "default-src 'self'"}, True),
    ],
)
def test_webpage_framing_respects_headers(headers, frameable):
    info = mc.classify("https://x.test/", "https://x.test/", "text/html", H(headers), 200)
    assert info.kind == "webpage"
    assert (info.embed_url == "https://x.test/") is frameable


def test_classify_uses_final_url_for_short_links():
    final = "https://www.google.com/maps/place/Space+Needle/@47.62,-122.35,17z"
    info = mc.classify("https://maps.app.goo.gl/abc", final, "text/html", H({}), 200)
    assert info.kind == "map" and "Space+Needle" in info.embed_url


@pytest.mark.parametrize(
    "addr, public",
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("192.168.0.5", False),
        ("172.16.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("0.0.0.0", False),
    ],
)
def test_is_public_ip(addr, public):
    assert mc._is_public_ip(addr) is public


def _fake_dns(monkeypatch, table):
    def getaddrinfo(host, *args, **kwargs):
        return [(None, None, None, "", (table[host], 0))]

    monkeypatch.setattr(mc.socket, "getaddrinfo", getaddrinfo)


@pytest.mark.asyncio
async def test_check_url_follows_redirects_and_guards_each_hop(monkeypatch):
    _fake_dns(monkeypatch, {"short.test": "93.184.216.34", "img.test": "93.184.216.35",
                             "evil.test": "93.184.216.36", "internal.test": "10.0.0.7"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short.test":
            return httpx.Response(302, headers={"Location": "https://img.test/cat.jpg"})
        if request.url.host == "img.test":
            return httpx.Response(200, headers={"Content-Type": "image/jpeg"})
        if request.url.host == "evil.test":  # redirect into a private network
            return httpx.Response(302, headers={"Location": "http://internal.test/admin"})
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    info = await mc.check_url("https://short.test/x", client=client)
    assert info.kind == "image" and info.final_url == "https://img.test/cat.jpg"

    with pytest.raises(mc.MediaCheckError, match="isn't reachable"):
        await mc.check_url("https://evil.test/", client=client)


@pytest.mark.asyncio
async def test_check_url_errors_are_user_facing(monkeypatch):
    _fake_dns(monkeypatch, {"x.test": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/loop":
            return httpx.Response(302, headers={"Location": "/loop"})
        if request.url.path == "/throttled.jpg":
            return httpx.Response(429)
        if request.url.path == "/head-blocked":
            # HEAD rejected; the GET fallback succeeds.
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(200, headers={"Content-Type": "video/mp4"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(mc.MediaCheckError, match="HTTP 404"):
        await mc.check_url("https://x.test/missing", client=client)
    with pytest.raises(mc.MediaCheckError, match="too many"):
        await mc.check_url("https://x.test/loop", client=client)
    with pytest.raises(mc.MediaCheckError, match="http"):
        await mc.check_url("file:///etc/passwd", client=client)
    info = await mc.check_url("https://x.test/head-blocked", client=client)
    assert info.kind == "video"
    # 429 = exists but throttled: accepted, classified from the URL.
    info = await mc.check_url("https://x.test/throttled.jpg", client=client)
    assert info.kind == "image" and info.status == 429


# --- endpoint ---------------------------------------------------------------

import app.routers.calls as calls_module  # noqa: E402


@pytest.fixture
def open_calls(monkeypatch):
    monkeypatch.setattr(calls_module.settings, "open_calls_enabled", True)


@pytest.mark.asyncio
async def test_media_check_endpoint(client, open_calls, monkeypatch):
    token = (
        await client.post("/v1/calls/team-sync/join", json={"display_name": "A"})
    ).json()["livekit"]["token"]
    auth = {"Authorization": f"Bearer {token}"}

    async def fake_check(url):
        if "bad" in url:
            raise mc.MediaCheckError("Link didn't load (HTTP 404)")
        return mc.MediaInfo(url, url, "image", url, "image/png", 200)

    monkeypatch.setattr(calls_module.media_check, "check_url", fake_check)

    resp = await client.get(
        "/v1/calls/team-sync/media-check", params={"url": "https://x.test/a.png"}, headers=auth
    )
    assert resp.status_code == 200 and resp.json()["kind"] == "image"

    resp = await client.get(
        "/v1/calls/team-sync/media-check", params={"url": "https://x.test/bad"}, headers=auth
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == {
        "code": "media_unavailable", "message": "Link didn't load (HTTP 404)", "details": {}
    }

    # Requires a token for this call.
    resp = await client.get("/v1/calls/team-sync/media-check", params={"url": "https://x.test"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "url, expected",
    [
        (  # named place with only the camera centre: approximate, refined later
            "https://www.google.com/maps/place/Space+Needle/@47.6205,-122.3593,17z/data=x",
            {"lat": 47.6205, "lng": -122.3593, "zoom": 17.0, "label": "Space Needle",
             "approx": True},
        ),
        (  # !3d/!4d (the place itself) beats the offset @camera centre
            "https://www.google.com/maps/place/Space+Needle/@47.6205,-122.3593,15z/"
            "data=!3m1!4b1!4m6!3m5!1s0x5490:0xdb2b!8m2!3d47.6205063!4d-122.3492774",
            {"lat": 47.6205063, "lng": -122.3492774, "zoom": 15.0, "label": "Space Needle"},
        ),
        (
            "https://www.google.com/maps/@37.42,-122.08,21z",
            {"lat": 37.42, "lng": -122.08, "zoom": 19.0, "label": None},  # zoom clamped
        ),
        (
            "https://maps.google.com/maps?q=48.8584,%202.2945",
            {"lat": 48.8584, "lng": 2.2945, "zoom": 16.0, "label": None},
        ),
        ("https://maps.google.com/maps?q=Golden+Gate+Bridge", None),  # needs geocoding
        ("https://example.com/maps/@1,2,3z", None),
    ],
)
def test_google_maps_geo(url, expected):
    assert mc.google_maps_geo(url) == expected


@pytest.mark.asyncio
async def test_map_geocodes_place_names_and_tolerates_failure(monkeypatch):
    _fake_dns(monkeypatch, {"maps.google.com": "142.250.1.1"})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            calls.append(request)
            q = request.url.params["q"]
            if q == "Nowhere Special":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200, json=[{"lat": "37.8199", "lon": "-122.4783", "name": "Golden Gate Bridge"}]
            )
        return httpx.Response(200, headers={"Content-Type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    info = await mc.check_url("https://maps.google.com/maps?q=Golden+Gate+Bridge", client=client)
    assert info.kind == "map"
    assert info.geo == {
        "lat": 37.8199, "lng": -122.4783, "zoom": 16.0, "label": "Golden Gate Bridge"
    }
    assert calls[0].headers["User-Agent"].startswith("RemoteSupportLiveKit/")

    # Not found: still a map (Google embed fallback), just no geo.
    info = await mc.check_url("https://maps.google.com/maps?q=Nowhere+Special", client=client)
    assert info.kind == "map" and info.geo is None and info.embed_url


@pytest.mark.asyncio
async def test_named_place_without_exact_coords_is_geocoded_near_the_link(monkeypatch):
    """The @lat,lng camera centre is offset from the place (e.g. ~1 mile west
    with Google's side panel open); geocode the name near it instead."""
    _fake_dns(monkeypatch, {"www.google.com": "142.250.1.1"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            seen["params"] = dict(request.url.params)
            if request.url.params["q"] == "Main Street":
                hit = {"lat": "40.0", "lon": "-75.0", "name": "Main St"}  # far away
                return httpx.Response(200, json=[hit])
            return httpx.Response(
                200, json=[{"lat": "47.6205063", "lon": "-122.3492774", "name": "Space Needle"}]
            )
        return httpx.Response(200, headers={"Content-Type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    info = await mc.check_url(
        "https://www.google.com/maps/place/Space+Needle/@47.6205,-122.3793,15z", client=client
    )
    assert info.geo == {
        "lat": 47.6205063, "lng": -122.3492774, "zoom": 15.0, "label": "Space Needle"
    }
    # Search biased to a box around the link's camera centre.
    assert seen["params"]["viewbox"] == "-122.5793,47.8205,-122.1793,47.4205"

    # A same-named hit far away is rejected: keep the link's own area.
    info = await mc.check_url(
        "https://www.google.com/maps/place/Main+Street/@47.6,-122.3,16z", client=client
    )
    assert info.geo == {"lat": 47.6, "lng": -122.3, "zoom": 16.0, "label": "Main Street"}
