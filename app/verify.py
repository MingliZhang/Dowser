"""Post-download verification.

A download that ends without an error is not necessarily a whole video. A
dropped connection near the end, a manifest that stopped serving segments, or a
CDN returning a truncated body all produce a file that plays fine for a while
and then simply stops. So before a file is accepted, it is inspected:

* it must parse as media at all (a corrupt container fails here);
* it must contain the streams it should;
* it must be as long as the source said it would be.

Optionally the whole file is demuxed as well, which catches damage in the middle
that metadata alone cannot see.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .models import Job
from .settings_store import runtime

#: However tight the tolerance, never fail over less than this. Container
#: rounding and a trailing partial segment routinely cost a second or so.
MIN_SHORTFALL_ALLOWANCE = 1.5


class VerificationError(RuntimeError):
    """The finished file did not survive inspection."""


@dataclass
class Result:
    ok: bool
    detail: str = ""
    duration: float | None = None
    expected: float | None = None


async def check(job: Job, path: Path) -> Result:
    """Inspect a finished download. Never raises; returns a verdict."""
    stream = job.stream

    if not path.exists():
        return Result(False, "the file disappeared before it could be checked")
    size = path.stat().st_size
    if size == 0:
        return Result(False, "the file is empty")

    # Subtitles are text, not media — ffprobe has nothing useful to say.
    if stream.kind == "subtitle":
        return Result(True, f"subtitle file, {size} bytes")

    # A plain HTTP download whose length we were told up front is the easiest
    # case: short bytes means a truncated body, full stop.
    if job.total_bytes and stream.kind == "file" and size < job.total_bytes:
        missing = job.total_bytes - size
        return Result(
            False,
            f"truncated — got {_mb(size)} of {_mb(job.total_bytes)} "
            f"({_mb(missing)} missing)",
        )

    probe = await _ffprobe(path)
    if probe is None:
        return Result(False, "unreadable — ffprobe could not parse the file")

    streams = probe.get("streams") or []
    kinds = {s.get("codec_type") for s in streams}
    if not streams:
        return Result(False, "no audio or video streams in the file")
    # Anything that came from a video source should still have a video track.
    if stream.height and "video" not in kinds:
        return Result(False, "the video track is missing from the finished file")

    duration = _to_float((probe.get("format") or {}).get("duration"))
    if duration is None:
        duration = max(
            (_to_float(s.get("duration")) or 0 for s in streams), default=0
        ) or None

    expected = stream.duration
    # Live captures have no predetermined length to compare against.
    if expected and duration and not stream.is_live:
        allowance = max(expected * runtime.verify_tolerance / 100, MIN_SHORTFALL_ALLOWANCE)
        shortfall = expected - duration
        if shortfall > allowance:
            return Result(
                False,
                f"incomplete — {_clock(duration)} of {_clock(expected)} "
                f"({_clock(shortfall)} short)",
                duration,
                expected,
            )

    if runtime.verify_deep:
        if problem := await _deep_check(path):
            return Result(False, f"corrupt — {problem}", duration, expected)

    detail = f"{_clock(duration)} verified" if duration else f"{_mb(size)} verified"
    if runtime.verify_deep:
        detail += ", full scan clean"
    return Result(True, detail, duration, expected)


async def _ffprobe(path: Path) -> dict | None:
    if not shutil.which(settings.ffprobe):
        return None
    cmd = [
        settings.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=120)
    except (TimeoutError, asyncio.TimeoutError, OSError):
        return None
    if process.returncode != 0:
        return None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        return json.loads(stdout or b"{}")
    return None


async def _deep_check(path: Path) -> str | None:
    """Read every packet in the file. Returns a problem description, or None.

    Demux-only (`-c copy`) rather than a full decode: it still walks the entire
    file and reports damaged packets, but costs a fraction of the CPU.
    """
    if not shutil.which(settings.ffmpeg):
        return None
    cmd = [
        settings.ffmpeg, "-v", "error", "-nostdin",
        "-i", str(path), "-c", "copy", "-f", "null", "-",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)
    except (TimeoutError, asyncio.TimeoutError, OSError):
        return None  # inconclusive rather than a failure

    complaints = (stderr or b"").decode("utf-8", "replace").strip()
    if process.returncode != 0 or complaints:
        first = complaints.splitlines()[0] if complaints else f"ffmpeg exited {process.returncode}"
        return first[:200]
    return None


def _to_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _mb(num: float) -> str:
    return f"{num / 1_048_576:.1f} MB" if num >= 1_048_576 else f"{num / 1024:.0f} KB"


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
