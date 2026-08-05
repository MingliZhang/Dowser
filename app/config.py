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
    #: Local-only by default; the Docker image overrides this to 0.0.0.0.
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
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

    def __post_init__(self) -> None:
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
