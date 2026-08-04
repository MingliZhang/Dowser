"""Headless-browser network capture.

We load the page like a real browser, let its player do whatever it does, and
record every media request it makes — including the exact headers, so the
download can be replayed outside the browser. This is the site-agnostic path:
it does not care what CMS, player or CDN the site uses.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ..config import settings
from .classify import Captured, classify, dedupe_key, is_noise, looks_like_segment

#: Clicked (best effort) to get a player to start loading its stream.
PLAY_SELECTORS = [
    "button[aria-label*='play' i]",
    "button[title*='play' i]",
    "[class*='play-button' i]",
    "[class*='playButton' i]",
    "[class*='vjs-big-play' i]",
    "[class*='jw-icon-playback' i]",
    "[class*='plyr__control--overlaid' i]",
    "[id*='play' i][role='button']",
    "div[class*='poster' i]",
    "video",
]

#: Cookie banners that sit on top of the player and swallow the click.
CONSENT_SELECTORS = [
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Got it')",
    "button:has-text('Allow all')",
    "[id*='onetrust-accept' i]",
    "[class*='accept' i][class*='cookie' i]",
]

#: Headers worth replaying to ffmpeg. Anything else is browser bookkeeping.
REPLAY_HEADERS = {"referer", "origin", "cookie", "user-agent", "authorization", "x-forwarded-for"}


@dataclass
class SniffResult:
    title: str = ""
    final_url: str = ""
    captures: list[Captured] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Segment requests seen without a matching manifest — useful diagnostics.
    segment_count: int = 0


async def sniff(
    url: str,
    timeout: int | None = None,
    headless: bool | None = None,
    autoplay: bool | None = None,
) -> SniffResult:
    """Load ``url`` and return every media request the page made."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return SniffResult(notes=["Playwright is not installed — network capture skipped."])

    timeout = timeout or settings.sniff_timeout
    headless = settings.sniff_headless if headless is None else headless
    autoplay = settings.sniff_autoplay if autoplay is None else autoplay

    result = SniffResult(final_url=url)
    seen: dict[str, Captured] = {}
    lock = asyncio.Lock()

    async def record(response) -> None:
        try:
            request = response.request
            req_url = request.url
            if req_url.startswith(("data:", "blob:")) or is_noise(req_url):
                return

            content_type = (response.headers or {}).get("content-type", "")
            kind = classify(req_url, content_type)
            if kind == "unknown":
                return

            segment = looks_like_segment(req_url, content_type)
            key = dedupe_key(req_url)

            async with lock:
                if segment:
                    result.segment_count += 1
                if key in seen:
                    # Byte-range follow-ups: keep the largest known total size.
                    existing = seen[key]
                    size = _total_size(response.headers or {})
                    if size and (existing.size or 0) < size:
                        existing.size = size
                    return

                try:
                    raw_headers = await request.all_headers()
                except Exception:  # noqa: BLE001
                    raw_headers = dict(request.headers or {})

                headers = {
                    k.title(): v
                    for k, v in raw_headers.items()
                    if k.lower() in REPLAY_HEADERS and v
                }
                headers.setdefault("Referer", response.frame.url if response.frame else url)

                seen[key] = Captured(
                    url=req_url,
                    kind=kind,
                    content_type=content_type,
                    status=response.status,
                    size=_total_size(response.headers or {}),
                    headers=headers,
                    is_segment=segment,
                    frame_url=response.frame.url if response.frame else "",
                    method=request.method,
                )
        except Exception:  # noqa: BLE001 - a bad response must never kill the sniff
            return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        context = await browser.new_context(
            user_agent=settings.user_agent,
            ignore_https_errors=True,
            viewport={"width": 1366, "height": 850},
        )
        page = await context.new_page()
        context.on("response", record)

        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"Page load did not finish cleanly: {_short(exc)}")

            result.title = await _read_title(page)
            result.final_url = page.url or url

            if autoplay:
                await _dismiss_consent(page)
                await _nudge_players(page)

            # Let the player fetch its manifest and first segments.
            await _settle(page, timeout)

            # Title again — SPAs often set it after the player boots.
            if late_title := await _read_title(page):
                result.title = late_title
        finally:
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()

    result.captures = list(seen.values())
    return result


async def _read_title(page) -> str:
    for attempt in (
        lambda: page.title(),
        lambda: page.evaluate(
            "() => document.querySelector('meta[property=\"og:title\"]')?.content"
            " || document.querySelector('h1')?.innerText || ''"
        ),
    ):
        with contextlib.suppress(Exception):
            value = (await attempt() or "").strip()
            if value:
                return value
    return ""


async def _dismiss_consent(page) -> None:
    for selector in CONSENT_SELECTORS:
        with contextlib.suppress(Exception):
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=400):
                await locator.click(timeout=1200, force=True)
                await page.wait_for_timeout(300)
                return


async def _nudge_players(page) -> None:
    """Try hard to make a player start, in the main frame and every iframe."""
    for frame in [page.main_frame, *page.frames]:
        with contextlib.suppress(Exception):
            await frame.evaluate(
                """() => {
                    for (const v of document.querySelectorAll('video, audio')) {
                        try { v.muted = true; v.play?.(); } catch (e) {}
                    }
                }"""
            )

    for selector in PLAY_SELECTORS:
        with contextlib.suppress(Exception):
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=350):
                await locator.click(timeout=1500, force=True)
                await page.wait_for_timeout(800)
                break

    # Lazy-loaded players below the fold.
    with contextlib.suppress(Exception):
        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(400)


async def _settle(page, timeout: int) -> None:
    """Wait for the network to go quiet, but never longer than the budget."""
    deadline = timeout * 1000
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=min(deadline, 8000))
    with contextlib.suppress(Exception):
        await page.wait_for_timeout(min(max(deadline - 8000, 2500), 12000))


def _total_size(headers: dict[str, str]) -> int | None:
    """Full media size, preferring Content-Range's total over Content-Length."""
    content_range = headers.get("content-range") or headers.get("Content-Range")
    if content_range and "/" in content_range:
        with contextlib.suppress(ValueError):
            return int(content_range.rsplit("/", 1)[-1].strip())
    length = headers.get("content-length") or headers.get("Content-Length")
    if length:
        with contextlib.suppress(ValueError):
            return int(length)
    return None


def _short(exc: Exception, limit: int = 120) -> str:
    text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return text[:limit]
