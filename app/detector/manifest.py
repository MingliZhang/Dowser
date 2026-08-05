"""Expand HLS/DASH manifests into the individual qualities they offer.

Stream Detector hands you the master playlist URL; we go one step further and
read it so the UI can offer "1080p / 720p / 480p" instead of one opaque link.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from ..config import settings

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_DURATION_RE = re.compile(
    r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?"
)


@dataclass
class Variant:
    url: str
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None
    codecs: str | None = None
    fps: float | None = None
    language: str | None = None
    #: For DASH, the index of this representation among video streams.
    stream_index: int | None = None
    kind: str = "video"


@dataclass
class ManifestInfo:
    variants: list[Variant]
    duration: float | None = None
    #: True when the URL was a media playlist (already a single quality).
    is_media_playlist: bool = False
    is_live: bool = False
    error: str | None = None


#: One client for every manifest fetch. A page's master playlist and its variant
#: playlists all live on the same CDN host, so reusing the pool turns a TLS
#: handshake per manifest into one handshake per scan.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client

    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                follow_redirects=True,
                # A manifest that has not connected in 8s is not going to.
                timeout=httpx.Timeout(20.0, connect=8.0),
                verify=False,
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            )
        return _client


async def aclose() -> None:
    """Drop the shared client. Called when the app shuts down."""
    global _client

    async with _client_lock:
        if _client is not None and not _client.is_closed:
            with contextlib.suppress(Exception):
                await _client.aclose()
        _client = None


async def fetch_text(url: str, headers: dict[str, str], limit: int = 4_000_000) -> str:
    request_headers = {"User-Agent": settings.user_agent, **headers}
    client = await _get_client()
    resp = await client.get(url, headers=request_headers)
    resp.raise_for_status()
    return resp.text[:limit]


def _parse_attrs(line: str) -> dict[str, str]:
    return {
        k: v.strip('"')
        for k, v in _ATTR_RE.findall(line.split(":", 1)[-1])
    }


def parse_hls(text: str, base_url: str) -> ManifestInfo:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        return ManifestInfo(variants=[], error="not an HLS playlist")

    variants: list[Variant] = []
    is_live = "#EXT-X-ENDLIST" not in text and "#EXTINF" in text

    # Media playlist: sum the segment durations and report it as a single quality.
    if "#EXTINF" in text and "#EXT-X-STREAM-INF" not in text:
        total = sum(
            float(m.group(1))
            for m in re.finditer(r"#EXTINF:\s*([\d.]+)", text)
        )
        return ManifestInfo(
            variants=[Variant(url=base_url)],
            duration=total or None,
            is_media_playlist=True,
            is_live=is_live,
        )

    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attrs(line)
            target = next(
                (lines[j] for j in range(i + 1, len(lines)) if not lines[j].startswith("#")),
                None,
            )
            if not target:
                continue
            width = height = None
            if res := attrs.get("RESOLUTION"):
                try:
                    width, height = (int(x) for x in res.lower().split("x", 1))
                except ValueError:
                    pass
            variants.append(
                Variant(
                    url=urljoin(base_url, target),
                    width=width,
                    height=height,
                    bitrate=_int(attrs.get("BANDWIDTH") or attrs.get("AVERAGE-BANDWIDTH")),
                    codecs=attrs.get("CODECS"),
                    fps=_float(attrs.get("FRAME-RATE")),
                )
            )
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attrs(line)
            uri = attrs.get("URI")
            media_type = (attrs.get("TYPE") or "").upper()
            if not uri or media_type not in {"AUDIO", "SUBTITLES"}:
                continue
            variants.append(
                Variant(
                    url=urljoin(base_url, uri),
                    language=attrs.get("LANGUAGE") or attrs.get("NAME"),
                    kind="audio" if media_type == "AUDIO" else "subtitle",
                )
            )

    return ManifestInfo(variants=variants, is_live=is_live)


def _iso_duration(value: str | None) -> float | None:
    if not value:
        return None
    m = _DURATION_RE.fullmatch(value.strip())
    if not m:
        return None
    years, months, days, hours, minutes, seconds = (
        float(g) if g else 0.0 for g in m.groups()
    )
    return (
        years * 31_536_000
        + months * 2_592_000
        + days * 86_400
        + hours * 3600
        + minutes * 60
        + seconds
    ) or None


def parse_dash(text: str, base_url: str) -> ManifestInfo:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return ManifestInfo(variants=[], error=f"invalid MPD: {exc}")

    ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def find_all(node, tag):
        return node.findall(f"m:{tag}", ns) if ns else node.findall(tag)

    duration = _iso_duration(root.get("mediaPresentationDuration"))
    is_live = (root.get("type") or "static").lower() == "dynamic"

    variants: list[Variant] = []
    video_index = 0
    for period in find_all(root, "Period"):
        for adaptation in find_all(period, "AdaptationSet"):
            mime = (adaptation.get("mimeType") or adaptation.get("contentType") or "").lower()
            lang = adaptation.get("lang")
            for rep in find_all(adaptation, "Representation"):
                rep_mime = (rep.get("mimeType") or mime).lower()
                is_video = rep_mime.startswith("video") or rep.get("height")
                kind = "video" if is_video else ("audio" if "audio" in rep_mime else "other")
                variant = Variant(
                    url=base_url,
                    width=_int(rep.get("width")),
                    height=_int(rep.get("height")),
                    bitrate=_int(rep.get("bandwidth")),
                    codecs=rep.get("codecs"),
                    fps=_float(rep.get("frameRate")),
                    language=lang,
                    kind=kind,
                )
                if is_video:
                    variant.stream_index = video_index
                    video_index += 1
                variants.append(variant)

    return ManifestInfo(variants=variants, duration=duration, is_live=is_live)


async def inspect(url: str, kind: str, headers: dict[str, str]) -> ManifestInfo:
    """Download and parse a manifest; never raises."""
    try:
        text = await fetch_text(url, headers)
    except Exception as exc:  # noqa: BLE001 - detection must survive any failure
        return ManifestInfo(variants=[], error=str(exc))

    if kind == "hls":
        return parse_hls(text, url)
    if kind == "dash":
        return parse_dash(text, url)
    return ManifestInfo(variants=[], error=f"no parser for {kind}")


def _int(value: str | None) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float(value: str | None) -> float | None:
    if not value:
        return None
    if "/" in value:  # DASH frameRate can be "30000/1001"
        try:
            num, den = value.split("/", 1)
            return round(float(num) / float(den), 3)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(value)
    except ValueError:
        return None
