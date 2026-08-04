"""Filename construction: the tab title becomes the file name."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

#: Illegal on Windows, awkward everywhere.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
#: Site-name tails that browsers put in the tab but nobody wants in a filename.
_TAB_SUFFIX = re.compile(
    r"\s*[|\-–—·•]\s*(YouTube|Vimeo|Dailymotion|Twitch|Facebook|X|Twitter|TikTok|Bilibili)\s*$",
    re.I,
)
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_STEM = 150


def sanitize(title: str, *, strip_site_suffix: bool = False) -> str:
    """Make ``title`` safe to use as a filename stem."""
    text = unicodedata.normalize("NFC", (title or "").strip())
    if strip_site_suffix:
        text = _TAB_SUFFIX.sub("", text)
    text = _ILLEGAL.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip(" .")
    if len(text) > MAX_STEM:
        text = text[:MAX_STEM].rstrip(" .")
    if text.upper() in _RESERVED:
        text = f"{text}_"
    return text or "video"


def numbered(stem: str, index: int, total: int) -> str:
    """Append 1, 2, 3... when a page yields more than one video."""
    if total <= 1:
        return stem
    return f"{stem} {index + 1}"


def unique_path(directory: Path, stem: str, extension: str) -> Path:
    """First free path for ``stem``, adding " (2)", " (3)"... on collision."""
    ext = extension if extension.startswith(".") else f".{extension}"
    candidate = directory / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    for counter in range(2, 1000):
        candidate = directory / f"{stem} ({counter}){ext}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a free filename for {stem}{ext}")


def safe_subfolder(name: str | None) -> str:
    """A single sanitized path segment — never escapes the download root."""
    if not name:
        return ""
    parts = [sanitize(p) for p in Path(name).parts if p not in {"", ".", ".."}]
    return "/".join(p for p in parts if p)
