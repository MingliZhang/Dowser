"""Detection pipeline: page URL in, list of downloadable streams out."""
from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlparse

from ..config import settings
from ..models import DetectResult, Stream
from . import manifest as manifest_mod
from . import ytdlp_probe
from .classify import Captured, classify, container_for, guess_container
from .sniffer import sniff

#: Media files below this are almost always tracking pixels or ad bumpers.
MIN_FILE_BYTES = 100_000
MAX_STREAMS = 40


async def detect(url: str, quick: bool = False, allow_fallback: bool = True) -> DetectResult:
    """Find everything downloadable on ``url``."""
    url = url.strip()
    notes: list[str] = []

    # A media URL pasted straight in — no browser needed.
    direct_kind = classify(url)
    if direct_kind != "unknown":
        streams = await _streams_from_capture(
            Captured(url=url, kind=direct_kind, headers={"Referer": url}), notes
        )
        for stream in streams:
            stream.source = "direct"
            stream.page_url = url
        return DetectResult(
            url=url,
            title=_title_from_url(url),
            title_source="url",
            streams=_finalize(streams),
            notes=["Direct media URL — used as-is.", *notes],
        )

    title = ""
    title_source = "tab"
    streams: list[Stream] = []

    if quick:
        notes.append("Quick mode — network capture skipped.")
    elif not settings.playwright_available:
        notes.append(
            "Playwright unavailable — network capture skipped. "
            "Install it with: python -m playwright install chromium"
        )
    else:
        result = await sniff(url)
        title = result.title
        notes.extend(result.notes)

        media = [c for c in result.captures if not c.is_segment]
        notes.append(
            f"Captured {len(media)} media request(s)"
            + (f" and {result.segment_count} stream segment(s)." if result.segment_count else ".")
        )

        if _looks_like_bot_wall(result.title):
            notes.append(
                "The site served a bot check instead of the page. Try again, or run "
                "with SNIFF_HEADLESS=false / a longer SNIFF_TIMEOUT."
            )

        for capture in _rank_captures(_drop_stream_members(media)):
            streams.extend(await _streams_from_capture(capture, notes))

        streams = _drop_covered_variants(streams)

        if not streams and result.segment_count:
            notes.append(
                "Stream segments were requested but no manifest was seen — "
                "the player may fetch it before load; try detecting again."
            )

    # Fallback: let a site-specific extractor try where capture came up empty.
    if allow_fallback and not any(s.kind != "subtitle" for s in streams):
        yt_title, yt_streams, error = await ytdlp_probe.probe(url)
        if yt_streams:
            notes.append("No streams captured — fell back to the yt-dlp extractor.")
            streams.extend(yt_streams)
            if not title and yt_title:
                title, title_source = yt_title, "extractor"
        elif error:
            notes.append(f"yt-dlp fallback found nothing ({error}).")

    for stream in streams:
        stream.page_url = stream.page_url or url

    if not title:
        title = _title_from_url(url)
        title_source = "url"

    return DetectResult(
        url=url,
        title=title,
        title_source=title_source,
        streams=_finalize(streams),
        notes=notes,
    )


async def _streams_from_capture(capture: Captured, notes: list[str]) -> list[Stream]:
    """Turn one captured request into the stream(s) a user can pick."""
    if capture.kind in {"hls", "dash"}:
        return await _expand_manifest(capture, notes)

    if capture.kind == "smooth":
        return [
            Stream(
                url=capture.url,
                kind="smooth",
                engine="ffmpeg",
                label="Smooth Streaming manifest",
                container="mp4",
                headers=capture.headers,
            )
        ]

    if capture.kind == "live":
        return [
            Stream(
                url=capture.url,
                kind="live",
                engine="ffmpeg",
                label=f"Live stream ({urlparse(capture.url).scheme.upper()})",
                container="mkv",
                headers=capture.headers,
                is_live=True,
            )
        ]

    if capture.kind == "hds":
        # Detected rather than offered: ffmpeg has no F4M demuxer, so a job
        # would only fail. Saying so beats a silent "no video found".
        notes.append(
            "Found an Adobe HDS manifest (.f4m). ffmpeg cannot download HDS, so "
            "it is not offered — look for an HLS or DASH version of the same stream."
        )
        return []

    if capture.kind == "subtitle":
        return [
            Stream(
                url=capture.url,
                kind="subtitle",
                engine="ffmpeg",
                label=f"Subtitles ({posixpath.basename(urlparse(capture.url).path) or 'track'})",
                container=guess_container("subtitle", capture.url),
                headers=capture.headers,
            )
        ]

    if capture.kind == "file":
        if capture.size is not None and capture.size < MIN_FILE_BYTES:
            return []
        name = posixpath.basename(urlparse(capture.url).path) or "media"
        label = name if len(name) <= 48 else name[:45] + "..."
        if capture.size:
            label += f" ({_human_size(capture.size)})"
        return [
            Stream(
                url=capture.url,
                kind="file",
                engine="ffmpeg",
                label=label,
                container=guess_container("file", capture.url),
                filesize=capture.size,
                headers=capture.headers,
            )
        ]

    return []


async def _expand_manifest(capture: Captured, notes: list[str]) -> list[Stream]:
    info = await manifest_mod.inspect(capture.url, capture.kind, capture.headers)
    label_base = "HLS" if capture.kind == "hls" else "DASH"

    if info.error or not info.variants:
        if info.error:
            notes.append(f"Could not read {label_base} manifest: {info.error}")
        return [
            Stream(
                url=capture.url,
                kind=capture.kind,
                engine="ffmpeg",
                label=f"{label_base} stream",
                container=container_for(None, info.is_live),
                headers=capture.headers,
                duration=info.duration,
                is_live=info.is_live,
            )
        ]

    if info.is_media_playlist:
        return [
            Stream(
                url=capture.url,
                kind=capture.kind,
                engine="ffmpeg",
                label=f"{label_base} stream" + (" (live)" if info.is_live else ""),
                container=container_for(None, info.is_live),
                headers=capture.headers,
                duration=info.duration,
                is_live=info.is_live,
            )
        ]

    videos = [v for v in info.variants if v.kind == "video"]
    audios = [v for v in info.variants if v.kind == "audio"]
    subs = [v for v in info.variants if v.kind == "subtitle"]

    # HLS variant playlists are frequently video-only; carry an audio rendition.
    default_audio = audios[0].url if audios and capture.kind == "hls" else None

    streams: list[Stream] = []
    for variant in sorted(videos, key=lambda v: (v.height or 0, v.bitrate or 0), reverse=True):
        streams.append(
            Stream(
                url=capture.url if capture.kind == "dash" else variant.url,
                kind=capture.kind,
                engine="ffmpeg",
                label=_variant_label(variant, label_base, info.is_live),
                container=container_for(variant.codecs, info.is_live),
                width=variant.width,
                height=variant.height,
                fps=variant.fps,
                bitrate=variant.bitrate,
                codecs=variant.codecs,
                duration=info.duration,
                headers=capture.headers,
                audio_url=default_audio,
                video_index=variant.stream_index,
                is_live=info.is_live,
            )
        )

    if not streams:
        # Audio-only or unusual manifest — offer the manifest itself.
        streams.append(
            Stream(
                url=capture.url,
                kind=capture.kind,
                engine="ffmpeg",
                label=f"{label_base} stream",
                container=container_for(None, info.is_live),
                headers=capture.headers,
                duration=info.duration,
                is_live=info.is_live,
            )
        )

    for sub in subs[:4]:
        streams.append(
            Stream(
                url=sub.url,
                kind="subtitle",
                engine="ffmpeg",
                label=f"Subtitles ({sub.language or 'track'})",
                container="vtt",
                language=sub.language,
                headers=capture.headers,
            )
        )

    return streams


def _variant_label(variant, base: str, is_live: bool) -> str:
    parts = []
    if variant.height:
        parts.append(f"{variant.height}p")
    elif variant.bitrate:
        parts.append(f"{round(variant.bitrate / 1000)}kbps")
    else:
        parts.append(base)
    if variant.fps and variant.fps >= 45:
        parts.append(f"{round(variant.fps)}fps")
    if variant.bitrate and variant.height:
        parts.append(f"{round(variant.bitrate / 1_000_000, 1)} Mbps")
    if is_live:
        parts.append("live")
    return " · ".join(parts)


def _drop_stream_members(captures: list[Captured]) -> list[Captured]:
    """Discard media files that live under a manifest we already captured.

    Modern HLS/DASH ships fragments as .mp4/.m4a, which look exactly like a
    standalone file. If one sits inside a captured manifest's directory tree it
    is a piece of that stream, not another video on the page.
    """
    manifest_roots = {
        f"{p.scheme}://{p.netloc}{posixpath.dirname(p.path)}/"
        for p in (
            urlparse(c.url) for c in captures
            if c.kind in {"hls", "dash", "smooth"}
        )
    }
    if not manifest_roots:
        return captures

    kept: list[Captured] = []
    for capture in captures:
        if capture.kind == "file":
            parsed = urlparse(capture.url)
            location = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if any(location.startswith(root) for root in manifest_roots):
                continue
        kept.append(capture)
    return kept


def _looks_like_bot_wall(title: str) -> bool:
    lowered = (title or "").lower()
    return any(
        marker in lowered
        for marker in (
            "just a moment", "attention required", "checking your browser",
            "access denied", "are you a robot", "verify you are human",
        )
    )


def _rank_captures(captures: list[Captured]) -> list[Captured]:
    """Manifests first — they describe a whole stream, files are one-offs."""
    order = {"hls": 0, "dash": 1, "smooth": 2, "file": 3, "subtitle": 4}
    return sorted(captures, key=lambda c: (order.get(c.kind, 5), -(c.size or 0)))


def _drop_covered_variants(streams: list[Stream]) -> list[Stream]:
    """Remove variant playlists the player fetched that a master already covers.

    Players request the master *and* the rendition they settled on, so both show
    up in the capture. Note that DASH representations all share the manifest URL
    and are told apart by resolution, so the URL alone cannot identify a stream.
    """
    labelled = {s.url for s in streams if s.height is not None}
    kept: list[Stream] = []
    seen: set[tuple] = set()
    for stream in streams:
        key = (stream.url, stream.kind, stream.height, stream.bitrate, stream.video_index)
        if key in seen:
            continue
        # An unlabelled manifest the master already exposes as named qualities.
        if stream.height is None and stream.kind != "subtitle" and stream.url in labelled:
            continue
        seen.add(key)
        kept.append(stream)
    return kept


def _finalize(streams: list[Stream]) -> list[Stream]:
    videos = [s for s in streams if s.kind != "subtitle"]
    subs = [s for s in streams if s.kind == "subtitle"]
    videos.sort(key=lambda s: s.sort_key, reverse=True)
    if videos:
        videos[0].recommended = True
        for other in videos[1:]:
            other.recommended = False
    return (videos + subs)[:MAX_STREAMS]


def _title_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = posixpath.basename(parsed.path.rstrip("/"))
    if name:
        stem = posixpath.splitext(unquote(name))[0]
        if stem:
            return stem.replace("-", " ").replace("_", " ").strip()
    return parsed.netloc or "video"


def _human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
