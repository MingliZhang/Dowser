"""Filename construction: the tab title becomes the file name."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
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


def compile_one(line: str) -> re.Pattern[str]:
    """Turn one user-written line into a pattern.

    Wrapped in slashes means a regular expression — ``/\\s*\\[\\d+p\\]/i``.
    Anything else is matched literally and case-insensitively, so a title
    containing ``[HD]`` can be cleaned up without anyone learning that square
    brackets mean something to a regex engine.
    """
    if len(line) > 2 and line.startswith("/") and line.rfind("/") > 0:
        end = line.rfind("/")
        body, flags = line[1:end], line[end + 1:]
        return re.compile(body, re.IGNORECASE if "i" in flags.lower() else 0)
    return re.compile(re.escape(line), re.IGNORECASE)


@lru_cache(maxsize=8)
def compile_filters(raw: str) -> tuple[re.Pattern[str], ...]:
    """Compile the whole filter list. Invalid lines are skipped.

    Bad patterns are rejected when the setting is saved, so anything reaching
    here is either valid or was written straight into settings.json by hand.
    """
    patterns = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            patterns.append(compile_one(text))
        except re.error:
            continue
    return tuple(patterns)


def apply_filters(title: str, raw_filters: str) -> str:
    """Strip every configured pattern out of a title."""
    result = title or ""
    for pattern in compile_filters(raw_filters):
        result = pattern.sub(" ", result)
    return result


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
