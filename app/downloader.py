"""Download execution.

Two engines:

* plain files and subtitles -> streamed HTTP GET, byte-for-byte identical to
  what the browser would have saved;
* HLS/DASH/Smooth -> ffmpeg, replaying the headers we captured, remuxing the
  segments into a single MP4 without re-encoding;
* yt-dlp -> only for streams the fallback extractor produced.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import httpx

from .config import settings
from .models import Job, Stream

ProgressFn = Callable[[dict], None]

#: HTTP options that make ffmpeg survive flaky CDNs.
FFMPEG_NET_OPTS = [
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_delay_max", "10",
    "-rw_timeout", "30000000",
]


class DownloadError(RuntimeError):
    pass


async def run(job: Job, on_progress: ProgressFn) -> Path:
    """Download ``job`` into a temp file and return that path."""
    stream = job.stream
    temp_path = settings.temp_dir / f"{job.id}.{stream.container}"

    try:
        if stream.engine == "ytdlp":
            return await _run_ytdlp(job, temp_path, on_progress)
        if stream.kind in {"file", "subtitle"}:
            return await _run_http(job, temp_path, on_progress)
        return await _run_ffmpeg(job, temp_path, on_progress)
    except (asyncio.CancelledError, Exception):
        _cleanup(temp_path)
        raise


# --- plain HTTP --------------------------------------------------------------


async def _run_http(job: Job, temp_path: Path, on_progress: ProgressFn) -> Path:
    stream = job.stream
    headers = {"User-Agent": settings.user_agent, **_clean_headers(stream.headers)}

    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0, verify=False) as client:
        async with client.stream("GET", stream.url, headers=headers) as response:
            if response.status_code >= 400:
                raise DownloadError(f"HTTP {response.status_code} fetching the media file")

            total = _content_length(response.headers) or stream.filesize
            downloaded = 0
            last_report = 0.0
            loop = asyncio.get_running_loop()
            started = loop.time()

            with temp_path.open("wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = loop.time()
                    if now - last_report < 0.4:
                        continue
                    last_report = now
                    elapsed = max(now - started, 0.001)
                    rate = downloaded / elapsed
                    on_progress(
                        {
                            "percent": round(downloaded / total * 100, 1) if total else None,
                            "downloaded_bytes": downloaded,
                            "total_bytes": total,
                            "speed": f"{_human(rate)}/s",
                            "eta": _eta((total - downloaded) / rate) if total and rate else None,
                        }
                    )

    if not temp_path.exists() or temp_path.stat().st_size == 0:
        raise DownloadError("download produced an empty file")

    on_progress({"percent": 100.0, "downloaded_bytes": temp_path.stat().st_size})
    return temp_path


# --- ffmpeg ------------------------------------------------------------------


def _input_args(url: str, headers: dict[str, str]) -> list[str]:
    clean = _clean_headers(headers)
    user_agent = clean.pop("User-Agent", settings.user_agent)
    args = ["-user_agent", user_agent]
    if header_blob := "".join(f"{k}: {v}\r\n" for k, v in clean.items()):
        args += ["-headers", header_blob]
    args += FFMPEG_NET_OPTS
    args += ["-protocol_whitelist", "file,http,https,tcp,tls,crypto,data"]
    if url.lower().split("?")[0].endswith(".m3u8"):
        args += ["-allowed_extensions", "ALL"]
    return args + ["-i", url]


def _build_ffmpeg_command(stream: Stream, output: Path) -> list[str]:
    cmd = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    cmd += _input_args(stream.url, stream.headers)

    if stream.audio_url:
        cmd += _input_args(stream.audio_url, stream.headers)
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    elif stream.video_index is not None:
        # DASH: pick one video representation, plus the best audio available.
        cmd += ["-map", f"0:v:{stream.video_index}", "-map", "0:a:0?"]
    else:
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]

    cmd += ["-c", "copy"]
    if stream.container == "mp4":
        # MPEG-TS audio needs its bitstream rewritten to live inside MP4.
        cmd += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    return cmd


async def _run_ffmpeg(job: Job, temp_path: Path, on_progress: ProgressFn) -> Path:
    stream = job.stream
    duration = stream.duration
    if not duration and not stream.is_live:
        duration = await _probe_duration(stream)

    cmd = _build_ffmpeg_command(stream, temp_path)
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    job_process[job.id] = process

    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(_drain(process.stderr, stderr_tail))

    try:
        await _read_ffmpeg_progress(process, duration, temp_path, on_progress)
        await process.wait()
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    finally:
        job_process.pop(job.id, None)
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task

    if process.returncode != 0:
        detail = " ".join(stderr_tail[-4:]).strip() or f"ffmpeg exited with {process.returncode}"
        raise DownloadError(detail[:400])

    if not temp_path.exists() or temp_path.stat().st_size == 0:
        raise DownloadError("ffmpeg produced an empty file")

    on_progress({"percent": 100.0, "downloaded_bytes": temp_path.stat().st_size})
    return temp_path


async def _read_ffmpeg_progress(process, duration, temp_path, on_progress) -> None:
    """Parse ffmpeg's -progress key=value stream into UI updates."""
    fields: dict[str, str] = {}
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
        if key.strip() != "progress":
            continue

        out_time = _to_float(fields.get("out_time_us")) or _to_float(fields.get("out_time_ms"))
        seconds = (out_time / 1_000_000) if out_time else None
        size = _to_int(fields.get("total_size"))
        speed = fields.get("speed", "").strip()

        update: dict = {"downloaded_bytes": size}
        if duration and seconds is not None:
            percent = min(seconds / duration * 100, 99.9)
            update["percent"] = round(percent, 1)
            if speed not in {"", "N/A"}:
                with contextlib.suppress(ValueError):
                    rate = float(speed.rstrip("x"))
                    if rate > 0:
                        update["eta"] = _eta((duration - seconds) / rate)
        elif seconds is not None:
            update["message"] = f"{_clock(seconds)} downloaded"
        if speed and speed != "N/A":
            update["speed"] = speed
        on_progress(update)
        fields.clear()


async def _probe_duration(stream: Stream) -> float | None:
    """Ask ffprobe how long the stream is, so progress can be a percentage."""
    if not shutil.which(settings.ffprobe):
        return None
    clean = _clean_headers(stream.headers)
    user_agent = clean.pop("User-Agent", settings.user_agent)
    cmd = [
        settings.ffprobe, "-v", "quiet", "-print_format", "json", "-show_format",
        "-user_agent", user_agent,
    ]
    if header_blob := "".join(f"{k}: {v}\r\n" for k, v in clean.items()):
        cmd += ["-headers", header_blob]
    cmd += ["-i", stream.url]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        payload = json.loads(stdout or b"{}")
        return float(payload.get("format", {}).get("duration") or 0) or None
    except (TimeoutError, ValueError, json.JSONDecodeError, OSError):
        return None


# --- yt-dlp ------------------------------------------------------------------

_YTDLP_PROGRESS = re.compile(r"^PROG\|(?P<done>[^|]*)\|(?P<total>[^|]*)\|(?P<speed>[^|]*)\|(?P<eta>[^|]*)")


async def _run_ytdlp(job: Job, temp_path: Path, on_progress: ProgressFn) -> Path:
    stream = job.stream
    out_template = str(settings.temp_dir / f"{job.id}.%(ext)s")
    binary = shutil.which(settings.ytdlp) or shutil.which("yt-dlp")
    cmd = (
        [binary] if binary else ["python3", "-m", "yt_dlp"]
    ) + [
        "--no-playlist", "--no-warnings", "--newline", "--no-color",
        "--user-agent", settings.user_agent,
        "-f", stream.format_id or "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", out_template,
        "--progress-template",
        "PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes,progress.total_bytes_estimate)s"
        "|%(progress.speed)s|%(progress.eta)s",
    ]
    if settings.cookies_file:
        cmd += ["--cookies", settings.cookies_file]
    elif settings.cookies_from_browser:
        cmd += ["--cookies-from-browser", settings.cookies_from_browser]
    cmd.append(stream.page_url or stream.url)

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    job_process[job.id] = process
    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(_drain(process.stderr, stderr_tail))

    try:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            match = _YTDLP_PROGRESS.match(line)
            if not match:
                continue
            done = _to_int(match["done"])
            total = _to_int(match["total"])
            speed = _to_float(match["speed"])
            eta = _to_float(match["eta"])
            on_progress(
                {
                    "percent": round(done / total * 100, 1) if done and total else None,
                    "downloaded_bytes": done,
                    "total_bytes": total,
                    "speed": f"{_human(speed)}/s" if speed else None,
                    "eta": _eta(eta) if eta else None,
                }
            )
        await process.wait()
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    finally:
        job_process.pop(job.id, None)
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task

    if process.returncode != 0:
        detail = " ".join(stderr_tail[-4:]).strip() or f"yt-dlp exited with {process.returncode}"
        raise DownloadError(detail[:400])

    produced = sorted(settings.temp_dir.glob(f"{job.id}.*"), key=lambda p: p.stat().st_size)
    if not produced:
        raise DownloadError("yt-dlp did not produce a file")
    return produced[-1]


# --- shared helpers ----------------------------------------------------------

#: job id -> live subprocess, so cancellation can reach it.
job_process: dict[str, asyncio.subprocess.Process] = {}


async def _terminate(process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _drain(stream, sink: list[str], keep: int = 12) -> None:
    if stream is None:
        return
    async for raw in stream:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            sink.append(text)
            del sink[:-keep]


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip CR/LF so header values cannot inject extra headers into ffmpeg."""
    return {
        k.strip(): re.sub(r"[\r\n]", "", v).strip()
        for k, v in (headers or {}).items()
        if k and v
    }


def _cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.exists():
            os.unlink(path)
    for leftover in path.parent.glob(f"{path.stem}.*"):
        with contextlib.suppress(OSError):
            os.unlink(leftover)


def _content_length(headers) -> int | None:
    return _to_int(headers.get("content-length"))


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _human(num: float | None) -> str:
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _eta(seconds: float | None) -> str | None:
    if not seconds or seconds < 0 or seconds > 86_400 * 7:
        return None
    return _clock(seconds)


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
