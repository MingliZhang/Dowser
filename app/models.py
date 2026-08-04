"""Shared data shapes for detection results and download jobs."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

StreamKind = Literal["hls", "dash", "smooth", "file", "subtitle", "unknown"]
Engine = Literal["ffmpeg", "ytdlp"]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class Stream(BaseModel):
    """One downloadable thing found on a page."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    url: str
    kind: StreamKind = "unknown"
    #: How we found it: "ytdlp" (site extractor) or "sniffer" (network capture).
    source: str = "sniffer"
    #: Preferred download engine for this stream.
    engine: Engine = "ffmpeg"

    label: str = ""
    container: str = "mp4"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    codecs: str | None = None
    duration: float | None = None
    filesize: int | None = None
    language: str | None = None

    #: yt-dlp format selector, when engine == "ytdlp".
    format_id: str | None = None
    #: Separate HLS audio rendition to mux in (variant playlists are often video-only).
    audio_url: str | None = None
    #: Which video representation to pick out of a DASH manifest.
    video_index: int | None = None
    #: True for live streams — the download runs until stopped.
    is_live: bool = False
    #: Request headers needed to fetch the stream (Referer, Cookie, ...).
    headers: dict[str, str] = Field(default_factory=dict)
    #: Page this stream was found on — used as the yt-dlp input URL.
    page_url: str = ""
    #: True for the option we pre-select in the UI.
    recommended: bool = False

    @property
    def sort_key(self) -> tuple:
        return (self.height or 0, self.bitrate or 0, self.width or 0)


class DetectResult(BaseModel):
    url: str
    title: str
    #: Where the title came from: "tab" (page <title>) or "extractor".
    title_source: str = "tab"
    streams: list[Stream] = Field(default_factory=list)
    #: Human-readable notes: what ran, what failed, what was skipped.
    notes: list[str] = Field(default_factory=list)
    detected_at: float = Field(default_factory=time.time)

    @property
    def has_video(self) -> bool:
        return any(s.kind != "subtitle" for s in self.streams)


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    page_url: str
    page_title: str
    filename: str
    stream: Stream

    status: JobStatus = JobStatus.QUEUED
    percent: float | None = None
    speed: str | None = None
    eta: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    message: str = ""
    output_path: str | None = None

    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["stream"] = {
            "url": self.stream.url,
            "kind": self.stream.kind,
            "label": self.stream.label,
            "source": self.stream.source,
            "engine": self.stream.engine,
        }
        return data


class DetectRequest(BaseModel):
    url: str
    #: Skip the headless-browser pass (faster, extractor-only).
    quick: bool = False


class DownloadItem(BaseModel):
    stream: Stream
    #: Optional explicit filename stem; overrides automatic naming.
    filename: str | None = None


class DownloadRequest(BaseModel):
    page_url: str
    title: str
    items: list[DownloadItem]
    subfolder: str | None = None
