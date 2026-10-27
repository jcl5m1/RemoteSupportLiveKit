"""Validate and classify a URL shared with ``/show`` in open-call chat.

The sender's browser cannot check arbitrary URLs itself (CORS), so the backend
resolves the URL -- following redirects -- and reports what it is and how the
other participants' browsers should present it full screen:

  youtube  -> YouTube embed player
  map      -> Google Maps embed (also for maps.app.goo.gl short links)
  image / audio / video -> played directly from the source URL
  webpage  -> iframe when the site allows framing, else an "open" link
  link     -> anything else that resolved (shown as an "open" link)

SSRF guard: only http(s), and every hop's host must resolve exclusively to
public addresses (no loopback, private, link-local, reserved, multicast...).
Only headers are read; bodies are never downloaded.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx

MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 6.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 RemoteSupport-LinkCheck"
)

_EXT_KIND = {
    **dict.fromkeys(("jpg", "jpeg", "png", "gif", "webp", "svg", "avif", "bmp"), "image"),
    **dict.fromkeys(("mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus"), "audio"),
    **dict.fromkeys(("mp4", "webm", "mov", "m4v", "ogv"), "video"),
}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


class MediaCheckError(Exception):
    """The URL is not acceptable or did not resolve; message is user-facing."""


@dataclass
class MediaInfo:
    url: str  # what the sender typed
    final_url: str  # after redirects
    kind: str  # youtube | map | image | audio | video | webpage | link
    embed_url: str | None  # what to load full screen (None -> show an open link)
    content_type: str | None
    status: int
    # Maps: where to centre an interactive (one-finger-draggable) map.
    geo: dict | None = None  # {"lat", "lng", "zoom", "label"}

    def as_dict(self) -> dict:
        return asdict(self)


# --- classification (pure) ------------------------------------------------


def youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    vid: str | None = None
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if p.path == "/watch":
            vid = next(iter(parse_qs(p.query).get("v", [])), None)
        else:
            parts = p.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                vid = parts[1]
    return vid if vid and _YT_ID.match(vid) else None


def _is_google_maps(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return (
        host.startswith("maps.google.")
        or re.match(r"^(www\.)?google\.[a-z.]+$", host) is not None
        and p.path.startswith("/maps")
    )


_COORD = r"(-?\d{1,2}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)"


def google_maps_geo(url: str) -> dict | None:
    """Location from a Google Maps link without any lookup.

    Precedence (most to least exact):
      1. ``!3d<lat>!4d<lng>`` in the ``data=`` segment -- the place itself.
      2. Coordinates given as the ``q`` value.
      3. ``@lat,lng,<zoom>z`` -- only the *camera* centre when the link was
         copied. Google offsets it from the place (e.g. to clear the side
         panel), so for a named place it is marked ``approx`` and refined by
         geocoding the name near it (see ``check_url``).
    Label from ``/place/<name>``.
    """
    if not _is_google_maps(url):
        return None
    p = urlparse(url)
    query = parse_qs(p.query)
    place = re.search(r"/maps/(?:place|search)/([^/@]+)", p.path)
    label = unquote(place.group(1)).replace("+", " ") if place else None
    at = re.search(r"@" + _COORD + r"(?:,(\d+(?:\.\d+)?)z)?", p.path)
    zoom = float(at.group(3)) if at and at.group(3) else 16.0
    zoom = min(max(zoom, 2.0), 19.0)

    exact = re.findall(r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)", unquote(url))
    q = next(iter(query.get("q") or query.get("query") or []), "")
    qcoords = re.fullmatch(_COORD, q.strip()) if q else None
    approx = False
    if exact:
        lat, lng = float(exact[-1][0]), float(exact[-1][1])  # last pair = the place
    elif qcoords:
        lat, lng = float(qcoords.group(1)), float(qcoords.group(2))
    elif at:
        lat, lng = float(at.group(1)), float(at.group(2))
        approx = label is not None  # a named place: centre is only near it
    else:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    geo = {"lat": lat, "lng": lng, "zoom": zoom, "label": label}
    if approx:
        geo["approx"] = True
    return geo


def google_maps_query(url: str) -> str | None:
    """Free-text place for geocoding (``q=`` or ``/place/<name>``)."""
    if not _is_google_maps(url):
        return None
    p = urlparse(url)
    query = parse_qs(p.query)
    q = next(iter(query.get("q") or query.get("query") or []), None)
    if q:
        return q
    place = re.search(r"/maps/(?:place|search)/([^/@]+)", p.path)
    return unquote(place.group(1)).replace("+", " ") if place else None


def google_maps_embed(url: str) -> str | None:
    """Embed URL for a Google Maps link, or None if it is not one."""
    if not _is_google_maps(url):
        return None
    p = urlparse(url)
    query = parse_qs(p.query)
    q: str | None = next(iter(query.get("q") or query.get("query") or []), None)
    if not q:
        place = re.search(r"/maps/(?:place|search)/([^/]+)", p.path)
        coords = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", p.path)
        if place:
            q = unquote(place.group(1)).replace("+", " ")
        elif coords:
            q = f"{coords.group(1)},{coords.group(2)}"
    if not q:
        return None
    return f"https://maps.google.com/maps?q={quote_plus(q)}&output=embed"


def _frameable(headers: httpx.Headers) -> bool:
    xfo = headers.get("x-frame-options", "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return False
    csp = headers.get("content-security-policy", "").lower()
    m = re.search(r"frame-ancestors([^;]*)", csp)
    if m:
        sources = m.group(1).split()
        if "*" not in sources and "https:" not in sources:
            return False
    return True


def classify(
    url: str, final_url: str, content_type: str | None, headers: httpx.Headers, status: int
) -> MediaInfo:
    for candidate in (final_url, url):
        vid = youtube_id(candidate)
        if vid:
            embed = f"https://www.youtube.com/embed/{vid}?autoplay=1&playsinline=1&rel=0"
            return MediaInfo(url, final_url, "youtube", embed, content_type, status)
    for candidate in (final_url, url):
        maps = google_maps_embed(candidate)
        if maps:
            geo = google_maps_geo(candidate) or google_maps_geo(url)
            return MediaInfo(url, final_url, "map", maps, content_type, status, geo)

    ctype = (content_type or "").split(";")[0].strip().lower()
    major = ctype.split("/")[0]
    if major in ("image", "audio", "video"):
        return MediaInfo(url, final_url, major, final_url, ctype, status)
    ext = urlparse(final_url).path.rsplit(".", 1)[-1].lower()
    if not ctype or ctype in ("application/octet-stream", "binary/octet-stream"):
        ext_kind = _EXT_KIND.get(ext)
        if ext_kind:
            return MediaInfo(url, final_url, ext_kind, final_url, ctype or None, status)
    if ctype in ("text/html", "application/xhtml+xml"):
        frame_url = final_url if _frameable(headers) else None
        return MediaInfo(url, final_url, "webpage", frame_url, ctype, status)
    return MediaInfo(url, final_url, "link", None, ctype or None, status)


# --- resolution (network) --------------------------------------------------


def _is_public_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and ip.ipv4_mapped is not None
            and not _is_public_ip(str(ip.ipv4_mapped))
        )
    )


async def _assert_public_host(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise MediaCheckError("Only http(s) links can be shown")
    host = p.hostname
    if not host:
        raise MediaCheckError("That doesn't look like a link")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, p.port or 443, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise MediaCheckError(f"Couldn't find {host}") from exc
    addrs = {str(info[4][0]) for info in infos}
    if not addrs or not all(_is_public_ip(a) for a in addrs):
        raise MediaCheckError("That address isn't reachable from the internet")


async def check_url(url: str, client: httpx.AsyncClient | None = None) -> MediaInfo:
    """Resolve ``url`` (manual redirects, SSRF-checked per hop) and classify it."""
    url = url.strip()
    if len(url) > 2048:
        raise MediaCheckError("Link is too long")
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await _assert_public_host(current)
            resp = await _probe(client, current)
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise MediaCheckError(f"Link redirected nowhere (HTTP {resp.status_code})")
                current = urljoin(current, location)
                continue
            if resp.status_code == 429:
                # Exists, but the server is throttling us (not the viewers):
                # accept and classify from the URL alone.
                return classify(url, current, None, httpx.Headers(), resp.status_code)
            if resp.status_code >= 400:
                raise MediaCheckError(f"Link didn't load (HTTP {resp.status_code})")
            info = classify(
                url, current, resp.headers.get("content-type"), resp.headers, resp.status_code
            )
            if info.kind == "map":
                info.geo = await _refine_map_geo(info.geo, current, url, client)
            return info
        raise MediaCheckError("Link redirected too many times")
    except httpx.TimeoutException as exc:
        raise MediaCheckError("Link timed out") from exc
    except httpx.HTTPError as exc:
        raise MediaCheckError(f"Link didn't load ({type(exc).__name__})") from exc
    finally:
        if own_client:
            await client.aclose()


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy asks for an identifying User-Agent.
GEOCODE_USER_AGENT = "RemoteSupportLiveKit/1.0 (+https://remotesupport.lgitech.net)"


NEAR_KM = 25.0  # a "near" geocode hit must lie within this of the camera centre


def _km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    r = math.radians
    h = (
        math.sin(r(b_lat - a_lat) / 2) ** 2
        + math.cos(r(a_lat)) * math.cos(r(b_lat)) * math.sin(r(b_lng - a_lng) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


async def _refine_map_geo(
    geo: dict | None, final_url: str, url: str, client: httpx.AsyncClient
) -> dict | None:
    """Exact coordinates as-is; otherwise geocode the place name (biased to the
    link's camera centre when there is one), falling back to that centre."""
    if geo and not geo.get("approx"):
        return geo
    query = google_maps_query(final_url) or google_maps_query(url)
    near = (geo["lat"], geo["lng"]) if geo else None
    hit = await geocode(query, client, near=near)
    if hit and near and _km(near[0], near[1], hit["lat"], hit["lng"]) > NEAR_KM:
        hit = None  # a same-named place elsewhere: keep the link's own area
    if hit:
        hit["zoom"] = geo["zoom"] if geo else hit["zoom"]
        hit["label"] = (geo or {}).get("label") or hit["label"]
        return hit
    if geo:
        geo = dict(geo)
        geo.pop("approx", None)
        return geo
    return None


async def geocode(
    query: str | None, client: httpx.AsyncClient, near: tuple[float, float] | None = None
) -> dict | None:
    """Best-effort place lookup (OpenStreetMap Nominatim); None on any failure,
    in which case receivers fall back to the Google embed. ``near`` biases the
    search to a ~20 km box around that point."""
    if not query:
        return None
    params: dict[str, str | int] = {"q": query, "format": "jsonv2", "limit": 1}
    if near:
        d = 0.2
        params["viewbox"] = f"{near[1] - d},{near[0] + d},{near[1] + d},{near[0] - d}"
    try:
        resp = await client.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": GEOCODE_USER_AGENT},
            timeout=4.0,
        )
        hits = resp.json() if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    if not hits:
        return None
    hit = hits[0]
    try:
        lat, lng = float(hit["lat"]), float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    label = str(hit.get("name") or hit.get("display_name") or query)[:200]
    return {"lat": lat, "lng": lng, "zoom": 16.0, "label": label}


async def _probe(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """HEAD, falling back to a GET whose body is never read (some servers
    reject HEAD)."""
    resp = await client.head(url)
    if resp.status_code in (403, 405, 501) or (resp.status_code >= 400 and resp.status_code != 404):
        async with client.stream("GET", url) as streamed:
            return streamed
    return resp
