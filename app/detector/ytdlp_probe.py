"""yt-dlp fallback.

Only used when network capture comes up empty — some sites hide the real media
URL behind signing/obfuscation that a site-specific extractor already knows how
to undo. The sniffer stays the primary path.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings
from ..models import Stream


class _SilentLogger:
    """Swallows yt-dlp's output; failures are reported through the return value."""

    def debug(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def _ydl_options() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "extract_flat": False,
        "user_agent": settings.user_agent,
        # This runs as a fallback on pages no extractor knows, so its "ERROR:
        # Unsupported URL" lines are expected and would only spam the journal.
        "logger": _SilentLogger(),
    }
    if settings.cookies_file:
        opts["cookiefile"] = settings.cookies_file
    elif settings.cookies_from_browser:
        opts["cookiesfrombrowser"] = (settings.cookies_from_browser,)
    return opts


def _probe_sync(url: str) -> dict[str, Any] | None:
    try:
        import yt_dlp
    except ImportError:
        return None
    try:
        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 - fallback failing is normal, not an error
        return None


async def probe(url: str) -> tuple[str, list[Stream], str | None]:
    """Return (title, streams, error) from yt-dlp's extractors."""
    info = await asyncio.to_thread(_probe_sync, url)
    if not info:
        return "", [], "no extractor matched this page"

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return info.get("title") or "", [], "playlist had no entries"
        info = entries[0]

    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    formats = info.get("formats") or []

    streams: list[Stream] = [
        Stream(
            url=info.get("webpage_url") or url,
            kind="file",
            source="ytdlp",
            engine="ytdlp",
            label="Best available (yt-dlp picks video + audio)",
            container=info.get("ext") or "mp4",
            duration=duration,
            format_id="bv*+ba/b",
            page_url=url,
            recommended=True,
        )
    ]

    # One entry per distinct height, best bitrate wins.
    by_height: dict[int, dict[str, Any]] = {}
    for fmt in formats:
        height = fmt.get("height")
        if not height or fmt.get("vcodec") == "none":
            continue
        current = by_height.get(height)
        if not current or (fmt.get("tbr") or 0) > (current.get("tbr") or 0):
            by_height[height] = fmt

    for height in sorted(by_height, reverse=True)[:6]:
        fmt = by_height[height]
        streams.append(
            Stream(
                url=info.get("webpage_url") or url,
                kind="file",
                source="ytdlp",
                engine="ytdlp",
                label=f"{height}p",
                container=fmt.get("ext") or "mp4",
                width=fmt.get("width"),
                height=height,
                fps=fmt.get("fps"),
                bitrate=int(fmt["tbr"] * 1000) if fmt.get("tbr") else None,
                codecs=_codecs(fmt),
                duration=duration,
                filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
                format_id=f"bv*[height={height}]+ba/b[height={height}]",
                page_url=url,
            )
        )

    return title, streams, None


def _codecs(fmt: dict[str, Any]) -> str | None:
    parts = [
        c for c in (fmt.get("vcodec"), fmt.get("acodec"))
        if c and c != "none"
    ]
    return ", ".join(parts) or None
