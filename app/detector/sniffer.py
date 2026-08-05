"""Headless-browser network capture.

We load the page like a real browser, let its player do whatever it does, and
record every media request it makes — including the exact headers, so the
download can be replayed outside the browser. This is the site-agnostic path:
it does not care what CMS, player or CDN the site uses.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass, field

from ..config import settings
from ..settings_store import runtime
from .classify import Captured, classify, dedupe_key, is_noise, looks_like_segment


def _launch_args() -> list[str]:
    """Chromium flags that keep it alive and small on a headless server."""
    args = [
        "--autoplay-policy=no-user-gesture-required",
        "--mute-audio",
        # /dev/shm is 64MB in most containers; Chromium crashes when it fills.
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--metrics-recording-only",
    ]
    # Chromium's sandbox cannot be used as root, and refuses to start without
    # this. Home servers routinely run services as root.
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    return args


#: One browser shared by every scan, with a fresh context per page. Launching a
#: browser per page meant three full Chromium instances at a time — roughly a
#: gigabyte of RAM, which is what runs a small server out of memory.
_playwright = None
_browser = None
_browser_headless: bool | None = None
_browser_lock = asyncio.Lock()
#: Pages currently open, and when the last one closed.
_active_pages = 0
_idle_since: float | None = None


async def _get_browser(headless: bool):
    global _playwright, _browser, _browser_headless, _active_pages, _idle_since

    async with _browser_lock:
        _active_pages += 1
        _idle_since = None
        if _browser is not None and _browser.is_connected() and _browser_headless == headless:
            return _browser

        # A dead browser, or one running in the wrong mode, gets replaced.
        if _browser is not None:
            with contextlib.suppress(Exception):
                await _browser.close()
            _browser = None

        if _playwright is None:
            from playwright.async_api import async_playwright

            _playwright = await async_playwright().start()

        _browser = await _playwright.chromium.launch(headless=headless, args=_launch_args())
        _browser_headless = headless
        return _browser


async def _release_browser() -> None:
    """Mark one page finished; the browser closes once nothing is using it."""
    global _active_pages, _idle_since

    async with _browser_lock:
        _active_pages = max(0, _active_pages - 1)
        if _active_pages == 0:
            _idle_since = asyncio.get_running_loop().time()


async def close_if_idle(timeout: int | None = None) -> bool:
    """Drop the shared browser once it has been unused for a while.

    A parked Chromium holds on to a few hundred megabytes indefinitely, which
    is worth reclaiming on a small always-on server between batches.
    """
    global _playwright, _browser, _browser_headless, _idle_since

    timeout = settings.browser_idle_timeout if timeout is None else timeout
    if timeout <= 0:
        return False

    async with _browser_lock:
        if _browser is None or _active_pages > 0 or _idle_since is None:
            return False
        if asyncio.get_running_loop().time() - _idle_since < timeout:
            return False
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = _browser_headless = _idle_since = None
        return True


async def shutdown() -> None:
    """Close the shared browser. Called when the app shuts down."""
    global _playwright, _browser, _browser_headless

    async with _browser_lock:
        if _browser is not None:
            with contextlib.suppress(Exception):
                await _browser.close()
        if _playwright is not None:
            with contextlib.suppress(Exception):
                await _playwright.stop()
        _browser = _playwright = _browser_headless = None

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

    # Defaults come from the live settings, so the UI can retune detection
    # without a restart.
    timeout = timeout or runtime.sniff_timeout
    headless = runtime.sniff_headless if headless is None else headless
    autoplay = runtime.sniff_autoplay if autoplay is None else autoplay

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

    try:
        browser = await _get_browser(headless)
        context = await browser.new_context(
            user_agent=settings.user_agent,
            ignore_https_errors=True,
            viewport={"width": 1366, "height": 850},
        )
    except Exception as exc:  # noqa: BLE001
        await _release_browser()
        result.notes.append(f"Could not start the browser: {_short(exc, 200)}")
        return result

    if runtime.block_heavy_assets:
        # Images and fonts are never a stream, and decoded bitmaps are the
        # single largest thing in a renderer's memory. Blocking them cuts RAM
        # and page-load time without touching what we are here to observe.
        async def _skip(route) -> None:
            with contextlib.suppress(Exception):
                await route.abort()

        with contextlib.suppress(Exception):
            await context.route(
                lambda _url: True,
                lambda route: (
                    _skip(route)
                    if route.request.resource_type in {"image", "font"}
                    else route.continue_()
                ),
            )

    page = await context.new_page()
    context.on("response", record)

    try:
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
            # Only the context goes; the browser stays for the next page.
            with contextlib.suppress(Exception):
                await context.close()
    except Exception as exc:  # noqa: BLE001 - a browser crash is not fatal
        result.notes.append(f"Browser stopped early: {_short(exc, 200)}")
    finally:
        await _release_browser()

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
