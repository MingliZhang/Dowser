"""URL/content-type classification.

This is the heart of the Stream-Detector-style approach: we don't know anything
about the site, we only look at what the page actually requested over the
network and decide what each request was.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

from ..models import StreamKind

#: Media files below this are almost always tracking pixels or ad bumpers.
MIN_FILE_BYTES = 100_000

# --- content types -----------------------------------------------------------

HLS_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "vnd.apple.mpegurl",
}
DASH_TYPES = {"application/dash+xml", "video/vnd.mpeg.dash.mpd"}
SMOOTH_TYPES = {"application/vnd.ms-sstr+xml", "application/vnd.ms-sstr+xml;charset=utf-8"}
HDS_TYPES = {"application/f4m+xml", "video/f4m", "application/f4m"}
SUBTITLE_TYPES = {
    "text/vtt",
    "text/srt",
    "text/x-srt",
    "text/x-ssa",
    "text/sami",
    "application/x-subrip",
    "application/x-ass",
    "application/ttml+xml",
    "application/ttaf+xml",
    "application/x-sami",
}
#: A response of one of these types is a stream fragment whatever it is named.
#: application/octet-stream is deliberately absent — plenty of servers use it
#: for whole video files.
SEGMENT_TYPES = {"video/mp2t", "video/iso.segment", "video/x-m4s"}

#: Schemes ffmpeg can pull directly. A browser never requests these, so they
#: only arrive when someone pastes one in.
STREAM_PROTOCOLS = {
    "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpte",
    "rtsp", "rtsps", "rtspu",
    "mms", "mmsh", "mmst",
    "srt", "rtp", "udp",
}

# --- extensions --------------------------------------------------------------

HLS_EXT = {".m3u8", ".m3u"}
DASH_EXT = {".mpd"}
SMOOTH_EXT = {".ism", ".isml"}
#: Adobe HDS. Detected so we can say so — ffmpeg has no F4M demuxer.
HDS_EXT = {".f4m"}
SUBTITLE_EXT = {
    ".vtt", ".srt", ".ass", ".ssa", ".ttml", ".dfxp", ".sub", ".sbv",
    ".smi", ".sami", ".stl", ".scc", ".lrc", ".ttml2",
}
SEGMENT_EXT = {".ts", ".m4s", ".cmfv", ".cmfa", ".cmft", ".fmp4", ".dash", ".m4f", ".seg"}

VIDEO_EXT = {
    # mainstream
    ".mp4", ".m4v", ".mov", ".qt", ".webm", ".mkv", ".avi", ".flv", ".f4v",
    ".wmv", ".asf", ".3gp", ".3g2", ".ogv", ".ogm", ".ogx",
    # broadcast / disc / pro
    ".mpg", ".mpeg", ".m1v", ".m2v", ".mpv", ".m2p", ".vob", ".mts", ".m2ts",
    ".ts", ".mxf", ".gxf", ".lxf", ".dv", ".divx", ".amv", ".nsv",
    # legacy real / misc
    ".rm", ".rmvb", ".rmhd",
    # raw elementary streams ffmpeg can still remux
    ".h264", ".264", ".h265", ".265", ".hevc", ".av1", ".ivf", ".y4m",
    ".mjpeg", ".mjpg", ".vc1",
}
AUDIO_EXT = {
    ".m4a", ".m4b", ".mp3", ".mp2", ".aac", ".ogg", ".oga", ".opus", ".spx",
    ".wav", ".flac", ".weba", ".mka", ".wma", ".ac3", ".eac3", ".dts",
    ".amr", ".ape", ".wv", ".tta", ".caf", ".aiff", ".aif", ".au", ".ra",
    ".mpc", ".3ga", ".f4a",
}
FILE_EXT = VIDEO_EXT | AUDIO_EXT

#: Query params that vary between byte-range requests for the same media file.
VOLATILE_PARAMS = {
    "range", "bytes", "offset", "start", "end", "startrange", "endrange",
    "rn", "rbuf", "sq", "seg", "segment", "ei", "_", "t", "ts", "time",
    "keepalive", "ump", "srfvp", "cpn",
}

#: Filename shapes that mean "this is one chunk of a stream", not the stream.
SEGMENT_PATTERNS = [
    re.compile(r"(^|[-_/])(seg|segment|chunk|frag|fragment|part)[-_]?\d+", re.I),
    re.compile(r"(^|[-_/])init(ialization)?([-_.]|$)", re.I),
    re.compile(r"^\d+\.(ts|m4s|mp4|aac|m4a)$", re.I),
    re.compile(r"\$?number\$?", re.I),
    re.compile(r"(^|[-_/])(video|audio)[-_]?\d+[-_]?\d*\.(m4s|mp4|ts)$", re.I),
]

#: Third-party noise we never want to offer as a "video on this page".
AD_TRACKER_HOSTS = re.compile(
    r"(doubleclick|googlesyndication|google-analytics|googletagmanager|scorecardresearch"
    r"|adservice|adnxs|adsystem|amazon-adsystem|criteo|taboola|outbrain|moatads"
    r"|imasdk\.googleapis|segment\.io|hotjar|newrelic|sentry\.io)",
    re.I,
)


@dataclass
class Captured:
    """One media-ish network request we saw the page make."""

    url: str
    kind: StreamKind
    content_type: str = ""
    status: int = 0
    size: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: Set for requests we classified as stream segments rather than streams.
    is_segment: bool = False
    frame_url: str = ""
    method: str = "GET"

    @property
    def ext(self) -> str:
        return _ext(self.url)


def _ext(url: str) -> str:
    path = urlparse(url).path
    return posixpath.splitext(path)[1].lower()


def _basename(url: str) -> str:
    return posixpath.basename(urlparse(url).path)


def looks_like_segment(url: str, content_type: str = "") -> bool:
    ext = _ext(url)
    name = _basename(url)
    # Catches fragments served from extensionless URLs.
    if content_type.lower().split(";")[0].strip() in SEGMENT_TYPES:
        return True
    # .ts on the web is an HLS chunk far more often than a standalone file, and
    # a whole stream's worth of them is pure noise in the UI.
    if ext in SEGMENT_EXT:
        return True
    if any(p.search(name) for p in SEGMENT_PATTERNS):
        return True
    if "smooth" in url.lower() and re.search(r"/(quality|fragments)\(", url, re.I):
        return True
    return False


def classify(url: str, content_type: str = "") -> StreamKind:
    """Decide what a single request is, preferring content-type over extension."""
    ct = content_type.lower().split(";")[0].strip()
    ext = _ext(url)
    low = url.lower()

    # Nothing but media uses these schemes, so the URL alone settles it.
    if urlparse(url).scheme.lower() in STREAM_PROTOCOLS:
        return "live"

    if ct in HLS_TYPES or ext in HLS_EXT or ".m3u8?" in low:
        return "hls"
    if ct in DASH_TYPES or ext in DASH_EXT or ".mpd?" in low:
        return "dash"
    if ct in SMOOTH_TYPES or ext in SMOOTH_EXT or low.rstrip("/").endswith("/manifest"):
        return "smooth"
    if ct in HDS_TYPES or ext in HDS_EXT:
        return "hds"
    if ct in SUBTITLE_TYPES or ext in SUBTITLE_EXT:
        return "subtitle"
    # Typed as a fragment: kept as media here, then filtered by looks_like_segment.
    if ct in SEGMENT_TYPES:
        return "file"
    if ct.startswith("video/") or ct.startswith("audio/"):
        return "file"
    if ext in FILE_EXT:
        return "file"
    return "unknown"


def dedupe_key(url: str) -> str:
    """Collapse byte-range / cache-buster variants of the same media URL."""
    parsed = urlparse(url)
    stable = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in VOLATILE_PARAMS
    ]
    stable.sort()
    query = "&".join(f"{k}={v}" for k, v in stable)
    return f"{parsed.netloc.lower()}{parsed.path}?{query}"


def is_noise(url: str) -> bool:
    return bool(AD_TRACKER_HOSTS.search(urlparse(url).netloc))


#: Codecs MP4 was never meant to carry.
_WEBM_VIDEO = ("vp8", "vp9", "vp09", "av01", "av1")
_WEBM_AUDIO = ("opus", "vorbis")


def container_for(codecs: str | None = None, is_live: bool = False) -> str:
    """Pick an output container that can actually hold these codecs."""
    # A live capture can be cut off at any moment. Matroska stays playable when
    # that happens; an MP4 that never got finalised often does not.
    if is_live:
        return "mkv"
    text = (codecs or "").lower()
    webm_video = any(codec in text for codec in _WEBM_VIDEO)
    webm_audio = any(codec in text for codec in _WEBM_AUDIO)
    if webm_video and webm_audio:
        return "webm"
    if webm_video or webm_audio:
        return "mkv"  # a mix such as H.264 + Opus fits neither cleanly
    return "mp4"


def guess_container(kind: StreamKind, url: str) -> str:
    """Output container for a given stream."""
    ext = _ext(url)
    if kind == "live":
        return "mkv"
    if kind in {"hls", "dash", "smooth"}:
        return "mp4"
    if kind == "subtitle":
        return (ext or ".vtt").lstrip(".")
    if ext in AUDIO_EXT or ext in {".webm", ".mkv"}:
        return ext.lstrip(".")
    return "mp4"
