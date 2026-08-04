# 💧 Dowser

> **dowsing** *(n.)* — finding hidden water with a forked stick.

A MeTube-style web UI on top of Stream-Detector-style stream sniffing.

Paste page URLs, and for each one Dowser opens the page in a real (headless)
browser, watches every network request the page makes, works out which of them
are video streams, and downloads the ones you pick with `ffmpeg` — saving them
under the page's tab title.

It is deliberately **not** a YouTube downloader. There is no per-site logic: the
detector only cares about what the page actually requested over the network, so
it works the same way on a lecture portal, a news site, or a self-hosted player.

<!-- Screenshot: run it and see for yourself. -->

---

## How it works

```
   URLs (one per line)
          │
          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 1. CAPTURE   Chromium loads the page, dismisses cookie      │
   │              walls, clicks play, scrolls. Every response is  │
   │              recorded with its request headers.              │
   └─────────────────────────────────────────────────────────────┘
          │  every URL the page fetched
          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 2. CLASSIFY  content-type + extension → hls / dash / smooth  │
   │              / file / subtitle / segment / noise.            │
   │              Segments and ad-tracker hosts are dropped.      │
   └─────────────────────────────────────────────────────────────┘
          │  manifests + standalone media files
          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 3. EXPAND    Fetch each manifest and read it: an HLS master  │
   │              becomes 1080p / 720p / 480p…, a DASH MPD        │
   │              becomes its representations.                    │
   └─────────────────────────────────────────────────────────────┘
          │  a labelled list of qualities
          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 4. FALLBACK  Only if nothing was captured: hand the page to  │
   │              yt-dlp's extractors and see if they know it.    │
   └─────────────────────────────────────────────────────────────┘
          │  you tick what you want
          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 5. DOWNLOAD  ffmpeg replays the captured headers and remuxes │
   │              the stream to MP4 without re-encoding.          │
   │              Plain files stream over HTTP byte-for-byte.     │
   └─────────────────────────────────────────────────────────────┘
```

### Why capture instead of extractors

An extractor library needs to know a site in advance; when the site changes, it
breaks until someone patches it. Network capture asks a different question —
*what did this page just download?* — and the player has to answer honestly,
because it needs the answer to play the video. That is the trick The Stream
Detector uses, and it is why the same code path covers sites nobody wrote an
extractor for.

The two pieces that make a captured URL actually downloadable:

- **Headers are replayed.** Many CDNs reject a bare `curl` of a manifest URL and
  require the `Referer`, `Origin`, or `Cookie` the browser sent. Those are
  captured per request and passed to `ffmpeg` (`app/downloader.py`).
- **Segments are filtered.** A playing HLS stream fires hundreds of `.ts`/`.m4s`
  requests. Anything segment-shaped — or sitting under a directory belonging to a
  manifest we already captured — is counted but never offered as its own video.

### Download engines

| Stream | Engine | Why |
|---|---|---|
| HLS, DASH, Smooth | `ffmpeg -c copy` | Joins the segments into one MP4, no re-encoding, no quality loss |
| Plain `.mp4`/`.webm`/audio | streaming HTTP GET | Byte-for-byte identical to what the browser would have saved |
| yt-dlp fallback results | `yt-dlp` | It already knows how to fetch and merge what its extractor found |

HLS variant playlists are frequently video-only, with audio in a separate
rendition. When that is the case the audio playlist is passed to `ffmpeg` as a
second input and muxed in, so you do not end up with a silent file.

---

## Install and run

Requires **Python 3.10+** and **ffmpeg** on your PATH.

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
```

Then:

```bash
./run.sh
```

On first run this creates a virtualenv, installs the dependencies, and downloads
Chromium for Playwright (~150 MB). Every later run starts immediately.

Open **http://127.0.0.1:8081**.

<details>
<summary>Manual setup, if you prefer</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m app.main
```
</details>

### Docker

```bash
docker compose up -d      # → http://localhost:8081, videos in ./downloads
```

The image is based on Playwright's, so Chromium and its system libraries are
already present; `ffmpeg` is added on top.

---

## Using it

1. **Paste URLs** into the box — one per line. You can queue a whole session's
   worth of tabs at once.
2. **Detect streams.** Each page is scanned in the background (3 at a time) and
   results appear as they land, so you are not waiting on the slowest page.
3. **Review each card:**
   - **VIDEO FOUND** — pick a quality. The best one is pre-selected.
   - **NO VIDEO DETECTED** — the page is kept on screen and marked, with notes
     explaining what happened, so nothing silently disappears from a batch.
   - The title field is pre-filled with the tab title and is **editable** — what
     you type is what the file is called.
4. **Download selected.** Progress, speed, and ETA stream live into the queue.
   Jobs can be cancelled, retried, and revealed in your file manager.

You can also paste a `.m3u8`, `.mpd`, or `.mp4` URL directly — it skips the
browser entirely and goes straight to expanding the manifest.

**Quick mode** skips the browser and only asks yt-dlp's extractors. It is much
faster, but only works on sites that have an extractor.

### File naming

Files are named after the page title (the tab name), sanitized for your
filesystem:

| Situation | Result |
|---|---|
| One video selected | `Lecture 4 — Fourier Transforms.mp4` |
| Several selected from one page | `Lecture 4 1.mp4`, `Lecture 4 2.mp4`, `Lecture 4 3.mp4` |
| Name already taken on disk | `Lecture 4 (2).mp4` — nothing is overwritten |
| Illegal characters (`/ \ : * ? " < > \|`) | replaced with spaces |
| Trailing site name (` - YouTube`) | stripped |

Set **Subfolder** to group a batch into its own directory under the download
folder.

---

## Configuration

Copy `.env.example` to `.env` and edit, or export the variables directly.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8081` | HTTP port |
| `HOST` | `127.0.0.1` | Bind address (Docker sets `0.0.0.0`) |
| `DOWNLOAD_DIR` | `./downloads` | Where finished videos land |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel downloads |
| `SNIFF_TIMEOUT` | `25` | Seconds to watch each page — raise for slow players |
| `SNIFF_HEADLESS` | `true` | Set `false` to watch the browser work |
| `SNIFF_AUTOPLAY` | `true` | Click play/consent buttons |
| `ENABLE_SNIFFER` | `true` | Turn off network capture entirely |
| `COOKIES_FROM_BROWSER` | — | `chrome`, `firefox`, `safari`… to reuse a logged-in session |
| `COOKIES_FILE` | — | Path to a `cookies.txt` instead |
| `FFMPEG_PATH` | `ffmpeg` | Override binary locations |

---

## Limitations

Worth knowing before you file a bug:

- **DRM (Widevine/FairPlay/PlayReady) cannot be downloaded.** The stream is
  encrypted with keys the browser never exposes. Netflix, Disney+, and similar
  will detect but never download. This is by design and not something to work
  around.
- **Bot walls.** Cloudflare-style interstitials serve a challenge page instead of
  the real one. The card is marked with a note; try `SNIFF_HEADLESS=false`, or a
  longer `SNIFF_TIMEOUT`.
- **Login-only pages** need `COOKIES_FROM_BROWSER` or `COOKIES_FILE`.
- **Players that need a specific interaction** (a "Load" button, a custom
  overlay) may not start on their own. The generic play/consent clicking covers
  the common cases, not all of them. "Detect again" often helps, since the
  second load is warm.
- **Live streams** download until you cancel them; there is no known duration, so
  progress shows elapsed time instead of a percentage.

Please only download material you have the rights to.

---

## Project layout

```
app/
  main.py              FastAPI routes, WebSocket, static hosting
  config.py            Environment-driven settings
  models.py            Stream / Job / request shapes
  naming.py            Title sanitizing, 1-2-3 numbering, collision handling
  jobs.py              Queue, concurrency, live broadcast, JSON persistence
  downloader.py        ffmpeg / HTTP / yt-dlp execution and progress parsing
  detector/
    __init__.py        The pipeline: capture → classify → expand → fallback
    sniffer.py         Playwright network capture
    classify.py        What is a manifest / file / segment / noise
    manifest.py        HLS + DASH parsing into quality variants
    ytdlp_probe.py     Extractor fallback
static/                Vanilla-JS UI, no build step
```

State (queue + detection history) is persisted to `state.json` and reloaded on
start; downloads interrupted by a restart come back as retryable.

### HTTP API

The UI is a client of this, and it is fine to script against.

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/api/detect` | `{"urls": [...], "quick": false}` → 202, results stream over `/ws` |
| `POST` | `/api/download` | `{"page_url", "title", "items": [{"stream": {...}}], "subfolder"}` |
| `GET` | `/api/state` | Full queue + detection snapshot |
| `WS` | `/ws` | Same snapshot, pushed on every change |
| `POST` | `/api/jobs/{id}/cancel` · `/retry` | |
| `DELETE` | `/api/jobs/{id}` | |
| `POST` | `/api/queue/clear` | Drops finished jobs |
| `DELETE` | `/api/detections?url=` | Forget one page, or all |
| `GET` | `/files/<name>` | Serves finished downloads |

---

## Troubleshooting

**"No video detected" on a page that clearly has one.**
Raise `SNIFF_TIMEOUT`, then hit "Detect again". If the notes mention segments
without a manifest, the player fetched its manifest before capture began — a
second detect usually catches it.

**Download fails with a 403.**
The stream needs a session cookie. Set `COOKIES_FROM_BROWSER=chrome` and detect
again so the capture carries the authenticated request.

**Video downloads but has no audio.**
The variant was video-only and the audio rendition was not advertised in the
master playlist. Try a different quality, or the unlabelled "HLS stream" entry.
