"""Runtime configuration, driven by environment variables."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


def _env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


@dataclass
class Settings:
    #: Every interface by default — this is meant to run on a home server and be
    #: reached from other machines. Set HOST=127.0.0.1 to keep it local-only.
    #: Note there is no authentication, so do not expose this to the internet.
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8477))

    #: Where finished videos land.
    download_dir: Path = field(default_factory=lambda: _env_path("DOWNLOAD_DIR", "./downloads"))
    #: Partial downloads live here until they complete, then get moved. Defaults
    #: to a folder inside download_dir so the final move stays on one filesystem.
    temp_dir: Path | None = field(
        default_factory=lambda: _env_path("TEMP_DIR", "") if os.getenv("TEMP_DIR") else None
    )
    #: Queue + history persistence.
    state_file: Path = field(default_factory=lambda: _env_path("STATE_FILE", "./state.json"))
    #: UI-tunable settings; these values are only the starting defaults.
    settings_file: Path = field(default_factory=lambda: _env_path("SETTINGS_FILE", "./settings.json"))

    max_concurrent: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_DOWNLOADS", 2))

    #: Seconds a running download may make no progress before it counts as stuck.
    stall_timeout: int = field(default_factory=lambda: _env_int("STALL_TIMEOUT", 90))
    auto_retry: bool = field(default_factory=lambda: _env_bool("AUTO_RETRY", True))
    retry_delay: int = field(default_factory=lambda: _env_int("RETRY_DELAY", 30))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 3))
    #: When retries run out, send the page back to the detection stage.
    refetch_on_failure: bool = field(default_factory=lambda: _env_bool("REFETCH_ON_FAILURE", True))

    #: Probe finished files for truncation and corruption before accepting them.
    verify_downloads: bool = field(default_factory=lambda: _env_bool("VERIFY_DOWNLOADS", True))
    #: Percent shorter than advertised a video may be before it counts as short.
    verify_tolerance: int = field(default_factory=lambda: _env_int("VERIFY_TOLERANCE", 2))
    #: Also walk every packet. On by default: metadata alone cannot detect a
    #: truncated file whose header still claims the full duration, and a
    #: demux-only pass costs milliseconds next to the download itself.
    verify_deep: bool = field(default_factory=lambda: _env_bool("VERIFY_DEEP", True))

    ffmpeg: str = field(default_factory=lambda: os.getenv("FFMPEG_PATH", "ffmpeg"))
    ffprobe: str = field(default_factory=lambda: os.getenv("FFPROBE_PATH", "ffprobe"))
    ytdlp: str = field(default_factory=lambda: os.getenv("YTDLP_PATH", "yt-dlp"))

    #: Load the page in a headless browser and watch its network traffic.
    sniffer_enabled: bool = field(default_factory=lambda: _env_bool("ENABLE_SNIFFER", True))
    #: Seconds to let the page run before we stop collecting requests.
    sniff_timeout: int = field(default_factory=lambda: _env_int("SNIFF_TIMEOUT", 25))
    sniff_headless: bool = field(default_factory=lambda: _env_bool("SNIFF_HEADLESS", True))
    #: Try clicking play/consent buttons to coax a player into loading its stream.
    sniff_autoplay: bool = field(default_factory=lambda: _env_bool("SNIFF_AUTOPLAY", True))
    #: Pages scanned at once. Browser pages dominate this app's memory use, so
    #: this is the first thing to turn down on a small server.
    detect_concurrency: int = field(default_factory=lambda: _env_int("DETECT_CONCURRENCY", 2))
    #: Drop images and fonts during a scan; they cannot be streams and they are
    #: the bulk of a renderer's memory.
    block_heavy_assets: bool = field(default_factory=lambda: _env_bool("BLOCK_HEAVY_ASSETS", True))
    #: Close the shared browser after this many idle seconds to release its RAM.
    browser_idle_timeout: int = field(default_factory=lambda: _env_int("BROWSER_IDLE_TIMEOUT", 180))

    #: e.g. "chrome", "firefox", "safari" — lets yt-dlp reuse your logged-in session.
    cookies_from_browser: str = field(default_factory=lambda: os.getenv("COOKIES_FROM_BROWSER", ""))
    cookies_file: str = field(default_factory=lambda: os.getenv("COOKIES_FILE", ""))

    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
    )

    #: True when TEMP_DIR was set explicitly. If it was not, partials follow the
    #: download folder around when that is changed from the UI.
    temp_dir_pinned: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.temp_dir_pinned = self.temp_dir is not None
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if self.temp_dir is None:
            self.temp_dir = self.download_dir / ".incomplete"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.max_concurrent = max(1, self.max_concurrent)

    @property
    def ffmpeg_available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None

    @property
    def playwright_available(self) -> bool:
        if not self.sniffer_enabled:
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return True


settings = Settings()
