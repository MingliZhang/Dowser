"""Runtime-tunable settings.

Environment variables provide the defaults; the UI can override any of them and
the result is persisted. Adding a new knob means appending one entry to SCHEMA —
the settings panel renders itself from it, so no frontend change is needed.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import settings

Kind = Literal["int", "bool", "path"]


@dataclass(frozen=True)
class Knob:
    key: str
    label: str
    kind: Kind
    default: Any
    group: str
    help: str = ""
    unit: str = ""
    minimum: int | None = None
    maximum: int | None = None
    #: Changes take effect on the next job/detection rather than immediately.
    deferred: bool = False


def _schema() -> tuple[Knob, ...]:
    return (
        Knob(
            key="download_dir",
            label="Download folder",
            kind="path",
            default=str(settings.download_dir),
            group="Storage",
            help=(
                "Where finished videos are written. Created if missing. "
                "Downloads already running finish into the old folder."
            ),
        ),
        Knob(
            key="max_concurrent",
            label="Parallel downloads",
            kind="int",
            default=settings.max_concurrent,
            group="Downloads",
            minimum=1,
            maximum=10,
            help="How many downloads run at the same time. Applies immediately.",
        ),
        Knob(
            key="stall_timeout",
            label="Stall timeout",
            kind="int",
            default=settings.stall_timeout,
            group="Recovery",
            unit="seconds",
            minimum=0,
            maximum=3600,
            help=(
                "A running download that makes no progress for this long is "
                "treated as stuck and killed. 0 disables stall detection."
            ),
        ),
        Knob(
            key="auto_retry",
            label="Retry automatically",
            kind="bool",
            default=settings.auto_retry,
            group="Recovery",
            help="Re-run downloads that fail or stall. Cancelling by hand never retries.",
        ),
        Knob(
            key="retry_delay",
            label="Wait before retrying",
            kind="int",
            default=settings.retry_delay,
            group="Recovery",
            unit="seconds",
            minimum=1,
            maximum=3600,
            help="How long to wait after a failure before starting the download over.",
        ),
        Knob(
            key="max_retries",
            label="Retry attempts",
            kind="int",
            default=settings.max_retries,
            group="Recovery",
            minimum=0,
            maximum=20,
            help="Give up after this many automatic attempts. Manual retry resets the count.",
        ),
        Knob(
            key="refetch_on_failure",
            label="Re-scan the page when retries run out",
            kind="bool",
            default=settings.refetch_on_failure,
            group="Recovery",
            help=(
                "Put the page back at the detection stage so you can pick a "
                "stream again. Stream links are often signed and expire, so a "
                "fresh scan usually fixes a download that stopped working."
            ),
        ),
        Knob(
            key="verify_downloads",
            label="Check finished files",
            kind="bool",
            default=settings.verify_downloads,
            group="Verification",
            help=(
                "Probe each finished file: does it parse, does it still have a "
                "video track, and is it as long as the source claimed. A file "
                "that fails is deleted and retried rather than kept."
            ),
        ),
        Knob(
            key="verify_tolerance",
            label="Allowed shortfall",
            kind="int",
            default=settings.verify_tolerance,
            group="Verification",
            unit="percent",
            minimum=0,
            maximum=50,
            help=(
                "How much shorter than advertised a video may be before it "
                "counts as incomplete. Manifests are often a second or two out."
            ),
        ),
        Knob(
            key="verify_deep",
            label="Full-file scan",
            kind="bool",
            default=settings.verify_deep,
            group="Verification",
            help=(
                "Also read every packet. Needed to catch a truncated file whose "
                "header still claims the full length — the length check alone "
                "cannot see that. Costs well under a second per gigabyte."
            ),
        ),
        Knob(
            key="detect_concurrency",
            label="Pages scanned at once",
            kind="int",
            default=settings.detect_concurrency,
            group="Detection",
            minimum=1,
            maximum=6,
            help=(
                "Browser pages are by far the largest thing in memory here. "
                "Lower this first if the server runs out of RAM; 1 is safe on a "
                "container with 2GB or less."
            ),
        ),
        Knob(
            key="block_heavy_assets",
            label="Skip images and fonts while scanning",
            kind="bool",
            default=settings.block_heavy_assets,
            group="Detection",
            help=(
                "Neither can ever be a video stream, and decoded images are the "
                "biggest thing in a browser page's memory. Turn off only if a "
                "site stops revealing its player without them."
            ),
        ),
        Knob(
            key="sniff_timeout",
            label="Page scan time",
            kind="int",
            default=settings.sniff_timeout,
            group="Detection",
            unit="seconds",
            minimum=5,
            maximum=180,
            deferred=True,
            help=(
                "The longest a scan may watch a page's network traffic. Most "
                "scans finish well inside this, because they stop as soon as a "
                "stream appears — raise it only for players that boot slowly."
            ),
        ),
        Knob(
            key="sniff_settle_grace",
            label="Extra time after a stream appears",
            kind="int",
            default=settings.sniff_settle_grace,
            group="Detection",
            unit="seconds",
            minimum=0,
            maximum=30,
            deferred=True,
            help=(
                "Once a stream is found the scan keeps watching this much longer, "
                "which is what catches the other qualities and subtitle tracks "
                "that follow it. Raise it if a site keeps offering only one quality."
            ),
        ),
        Knob(
            key="sniff_headless",
            label="Headless browser",
            kind="bool",
            default=settings.sniff_headless,
            group="Detection",
            deferred=True,
            help="Turn off to watch the browser work — useful for pages behind a bot check.",
        ),
        Knob(
            key="sniff_autoplay",
            label="Click play & consent buttons",
            kind="bool",
            default=settings.sniff_autoplay,
            group="Detection",
            deferred=True,
            help="Dismiss cookie walls and start players so they load their stream.",
        ),
    )


class SettingsStore:
    """Mutable settings with validation, persistence, and a UI schema."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.schema: tuple[Knob, ...] = _schema()
        self.values: dict[str, Any] = {knob.key: knob.default for knob in self.schema}
        self._by_key = {knob.key: knob for knob in self.schema}
        self._load()

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal attribute lookup fails, so self.values is safe.
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def coerce(self, knob: Knob, raw: Any) -> Any:
        if knob.kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)

        if knob.kind == "path":
            text = str(raw or "").strip()
            if not text:
                raise ValueError(f"{knob.label} cannot be empty")
            path = Path(text).expanduser()
            # Prove it is usable now rather than failing on the first download.
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"Cannot create {path}: {exc.strerror or exc}") from exc
            if not path.is_dir():
                raise ValueError(f"{path} is not a folder")
            if not os.access(path, os.W_OK):
                raise ValueError(f"{path} is not writable by this user")
            return str(path.resolve())
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{knob.label} must be a whole number") from exc
        if knob.minimum is not None:
            value = max(knob.minimum, value)
        if knob.maximum is not None:
            value = min(knob.maximum, value)
        return value

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update; unknown keys are ignored, values are clamped."""
        changed: dict[str, Any] = {}
        for key, raw in (patch or {}).items():
            knob = self._by_key.get(key)
            if knob is None:
                continue
            value = self.coerce(knob, raw)
            if self.values.get(key) != value:
                self.values[key] = value
                changed[key] = value
        if changed:
            self._save()
        return changed

    def reset(self) -> dict[str, Any]:
        self.values = {knob.key: knob.default for knob in self.schema}
        self._save()
        return self.values

    def public(self) -> dict[str, Any]:
        return {
            "values": dict(self.values),
            "schema": [asdict(knob) for knob in self.schema],
        }

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key, raw in (stored or {}).items():
            if knob := self._by_key.get(key):
                try:
                    self.values[key] = self.coerce(knob, raw)
                except ValueError:
                    continue


runtime = SettingsStore(settings.settings_file)


# --- resolved paths ----------------------------------------------------------
#
# Read these instead of settings.download_dir / settings.temp_dir anywhere a
# download is actually written, so a folder change from the UI takes effect
# without a restart.


def download_dir() -> Path:
    raw = str(runtime.values.get("download_dir") or "").strip()
    path = Path(raw).expanduser() if raw else settings.download_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_dir() -> Path:
    """Partials live beside their destination so the final move never copies."""
    path = settings.temp_dir if settings.temp_dir_pinned else download_dir() / ".incomplete"
    path.mkdir(parents=True, exist_ok=True)
    return path
