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

## Supported formats

Detection leads with **content-type**, falling back to the file extension, so a
stream served from an extensionless URL is still recognised. Anything reported
as `video/*` or `audio/*` is picked up even if its container is not in the list
below — the extensions matter only when the server sends a useless content-type.

**Streaming formats**

| | |
|---|---|
| HLS | `.m3u8`, `.m3u` — master playlists expanded into qualities |
| DASH | `.mpd` — representations expanded into qualities |
| Smooth Streaming | `.ism`, `.isml`, `/Manifest` |
| Adobe HDS | `.f4m` — *detected and reported, but not downloadable* (ffmpeg has no F4M demuxer) |

**Streaming protocols** — paste these directly; a browser never requests them,
so they cannot be discovered by scanning a page:

`rtsp://` · `rtsps://` · `rtmp://` · `rtmps://` · `rtmpe://` · `rtmpt://` ·
`mms://` · `mmsh://` · `mmst://` · `srt://` · `rtp://` · `udp://`

**Video containers**

`mp4` `m4v` `mov` `qt` `webm` `mkv` `avi` `flv` `f4v` `wmv` `asf` `3gp` `3g2`
`ogv` `ogm` `mpg` `mpeg` `m1v` `m2v` `mpv` `m2p` `vob` `ts` `mts` `m2ts` `mxf`
`gxf` `lxf` `dv` `divx` `amv` `nsv` `rm` `rmvb` — plus raw elementary streams
`h264` `h265` `hevc` `av1` `ivf` `y4m` `mjpeg` `vc1`

**Audio**

`m4a` `m4b` `mp3` `mp2` `aac` `ogg` `oga` `opus` `spx` `wav` `flac` `weba`
`mka` `wma` `ac3` `eac3` `dts` `amr` `ape` `wv` `tta` `caf` `aiff` `au` `ra`
`mpc` `3ga` `f4a`

**Subtitles**

`vtt` `srt` `ass` `ssa` `ttml` `dfxp` `sub` `sbv` `smi` `sami` `stl` `scc` `lrc`

### What you get out

Streams are remuxed, never re-encoded, so quality is untouched and a download
runs at network speed rather than CPU speed. The output container is chosen to
fit the codecs rather than being forced to MP4:

| Source codecs | Output |
|---|---|
| H.264/H.265 + AAC (the common case) | `.mp4` |
| VP8/VP9/AV1 + Opus/Vorbis | `.webm` |
| A mix WebM cannot hold, e.g. H.264 + Opus | `.mkv` |
| Anything live | `.mkv` — survives being cut off mid-recording, unlike an unfinalised MP4 |

Plain files are downloaded byte-for-byte over HTTP and keep their original
extension — no remuxing at all.

**Deliberately excluded:** `.gif` and other image formats. Technically ffmpeg
handles animated GIFs, but every page has decorative ones and they would bury
the real results.

### Verifying finished files

A download that ends without an error is not necessarily a whole video. So
before a file is accepted into the download folder it is inspected, and one that
fails is **deleted and retried** rather than quietly kept.

| Check | Catches |
|---|---|
| Byte count vs `Content-Length` | A body that stopped early |
| Does ffprobe parse it | A container damaged badly enough to be unreadable |
| Are the streams present | A file that lost its video track |
| Measured length vs the source's | A stream that stopped serving partway |
| Full packet scan | Damage the metadata does not admit to |

That last one earns its place. An MP4 keeps its duration in a header, so a file
missing 40% of its data can still *report* the full length:

```
truncated to 60%  → length check:      PASS  "0:30 verified"      ← wrong
truncated to 60%  → full packet scan:  FAIL  "partial file"       ← correct
```

The scan is demux-only — it reads the file without decoding it, costing about
60 ms for a 16 MB file — so it is on by default. **Allowed shortfall** (2%)
absorbs the second or two that manifests are routinely out by; live captures and
files of unknown length skip the length comparison and are checked structurally.

### Recovery: stalls and retries

Downloads do not only fail loudly. A CDN can accept the connection, send a few
hundred kilobytes, and then go silent forever — `ffmpeg` will happily sit on
that dead socket until someone notices. So a watchdog tracks the last moment
each job actually *moved*:

- Progress that repeats itself does not count. `ffmpeg` keeps emitting progress
  lines when a transfer is wedged, so only a genuine increase in bytes or
  percentage resets the clock.
- A job that has not moved for **Stall timeout** seconds is killed and marked
  stalled.
- Stalled and failed jobs are re-run after **Wait before retrying**, up to
  **Retry attempts** times, then left alone with what went wrong.
- Cancelling by hand never retries — an explicit stop means stop. Pressing
  **Retry now** resets the automatic budget.
- If the process dies mid-download, jobs come back as failed on restart with a
  retry armed, so an unattended server picks up where it left off.

Retries start the file over rather than resuming; a partly written file is
deleted rather than left to confuse you.

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

Open **http://localhost:8477**, or `http://<server-ip>:8477` from another
machine — startup prints both addresses.

> **No authentication.** Dowser listens on all interfaces so a home server is
> reachable from your LAN. Keep it there: do not port-forward it or expose it to
> the internet. If you need it remotely, put it behind a reverse proxy that
> handles auth, and set `HOST=127.0.0.1` so only the proxy can reach it.

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
docker compose up -d      # → http://localhost:8477, videos in ./downloads
```

The image is based on Playwright's, so Chromium and its system libraries are
already present; `ffmpeg` is added on top.

---

## Using it

Work moves along a pipeline, and a page sits in exactly one stage at a time:

```
   ┌───────────┐  Detect streams   ┌───────────┐  Download selected  ┌───────────┐
   │ Page URLs │ ────────────────▶ │ Detected  │ ──────────────────▶ │ Downloads │
   └───────────┘   input clears    │   pages   │    card disappears  └───────────┘
                                   └───────────┘                           │
                                         ▲                                 │
                                         └─────────────────────────────────┘
                                          fails → back for a fresh scan
```

Each step forward needs you to confirm it, and nothing lingers in the stage it
came from. A download that fails past its retries returns to the detected-pages
stage automatically, because stream links are often signed and expire — a fresh
scan is usually what fixes it. It stops there and waits for you rather than
looping on its own. **Back to detect** on a failed job does the same by hand.

1. **Paste URLs** into the box — one per line. You can queue a whole session's
   worth of tabs at once. Set **Subfolder** to group the batch; it travels with
   those URLs, so the box can be cleared and reused for the next batch.
2. **Detect streams.** The input empties immediately — those URLs are now cards.
   Each page is scanned in the background (3 at a time) and results appear as
   they land, so you are not waiting on the slowest page.
3. **Review each card** — it leaves this stage once you start downloading:
   - **VIDEO FOUND** — pick a quality. The best one is pre-selected.
   - **NO VIDEO DETECTED** — the page is kept on screen and marked, with notes
     explaining what happened, so nothing silently disappears from a batch.
   - The title field is pre-filled with the tab title and is **editable** — what
     you type is what the file is called.
4. **Download selected** on one card, or **Download all** to queue every card
   that has something ticked. Either way the cards leave this stage, and you can
   keep queueing while other downloads are still running — the queue holds them
   until a slot frees up. Progress, speed, and ETA stream live into the queue.
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

## Settings

The **Settings** panel in the UI holds the knobs worth changing while the thing
is running. Edits save as you type and persist to `settings.json`.

| Setting | Default | What it does |
|---|---|---|
| Download folder | `./downloads` | Where finished videos are written. Created if missing, and rejected with a reason if it is not writable |
| Parallel downloads | 2 | Applies immediately, even to jobs already queued |
| Stall timeout | 90s | No progress for this long → killed as stuck. `0` disables |
| Retry automatically | on | Re-run jobs that fail or stall |
| Wait before retrying | 30s | Pause before starting a failed download over |
| Retry attempts | 3 | Give up after this many automatic tries |
| Re-scan the page when retries run out | on | Move the page back to the detection stage for fresh links |
| Check finished files | on | Probe each download; failures are deleted and retried |
| Allowed shortfall | 2% | How much shorter than advertised a video may be |
| Full-file scan | on | Read every packet — the only way to catch a truncated file that lies about its length |
| Page scan time | 25s | How long to watch each page's traffic — raise for slow players |
| Headless browser | on | Turn off to watch the browser work |
| Click play & consent buttons | on | Dismiss cookie walls and start players |

Settings marked *· next run* apply to the next detection or download rather
than to work already in flight.

Changing the **download folder** takes effect for the next job queued. Anything
already downloading finishes into the folder it started with, since its partial
file is already sitting there — partials are kept in a `.incomplete` directory
inside the download folder so the final move is a rename rather than a copy
across filesystems.

> **Adding a knob later.** The panel builds itself from a schema the server
> sends, so a new setting is one entry in `SCHEMA` in `app/settings_store.py` —
> no frontend change. Tell me what you want tunable and it is a few lines.

### Environment variables

`.env` values are the *starting defaults*. Once a setting is changed in the UI
the stored value wins, so editing `.env` afterwards has no effect — reset from
the panel (or delete `settings.json`) to go back to it.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8477` | HTTP port |
| `HOST` | `0.0.0.0` | Bind address. All interfaces, so your network can reach it — use `127.0.0.1` for local-only |
| `DOWNLOAD_DIR` | `./downloads` | Where finished videos land |
| `STATE_FILE` / `SETTINGS_FILE` | `./state.json`, `./settings.json` | Persistence |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Default for Parallel downloads |
| `STALL_TIMEOUT` | `90` | Default for Stall timeout |
| `AUTO_RETRY` / `RETRY_DELAY` / `MAX_RETRIES` | `true` / `30` / `3` | Retry defaults |
| `SNIFF_TIMEOUT` / `SNIFF_HEADLESS` / `SNIFF_AUTOPLAY` | `25` / `true` / `true` | Detection defaults |
| `ENABLE_SNIFFER` | `true` | Turn off network capture entirely (not in the UI) |
| `COOKIES_FROM_BROWSER` | — | `chrome`, `firefox`, `safari`… to reuse a logged-in session |
| `COOKIES_FILE` | — | Path to a `cookies.txt` instead |
| `FFMPEG_PATH` | `ffmpeg` | Override binary locations |

---

## Running it 24/7

Ready-made service definitions are in [`deploy/`](deploy/). All of them restart
the app if it crashes, and shut it down with `SIGINT` so in-flight `ffmpeg`
children die cleanly instead of leaving partial files behind.

**Linux home server (systemd)** — the usual choice:

```bash
sudo deploy/install-service.sh
journalctl -u dowser -f
```

The installer works out the project path, the service user, and the download
folder from your actual setup, writes the unit, and starts it. Override the user
with `sudo DOWSER_USER=someone deploy/install-service.sh`.

`deploy/dowser.service` is the same thing as a hand-editable template. Copying
it unchanged **fails** — `User=dowser` and `/opt/dowser` are placeholders, and
systemd reports `status=217/USER` when that user does not exist.

**Inside an existing container, alongside MeTube** — use
`deploy/supervisord-dowser.conf`. Drop it in the container's supervisor include
directory and `supervisorctl reread && supervisorctl update`. The container
needs `ffmpeg` plus Chromium; install both with:

```bash
pip install -r requirements.txt && python -m playwright install --with-deps chromium
```

If Chromium genuinely will not fit, `ENABLE_SNIFFER=false` still runs — but that
turns off the network capture, which is the whole point of Dowser, leaving only
the yt-dlp fallback. Prefer giving it Chromium.

**Its own container:** `docker compose up -d`. It sets `restart: unless-stopped`
and has a healthcheck, so Docker restarts it if the app wedges.

**macOS:** `deploy/com.dowser.plist` → `~/Library/LaunchAgents/`, then
`launchctl load -w ~/Library/LaunchAgents/com.dowser.plist`.

Point a monitor at `GET /api/health` — it returns the version, job counts, and
whether ffmpeg and the sniffer are usable.

### Running next to MeTube

Give each app its own port and its own folder. Two things to keep in mind:

- **Ports.** Dowser defaults to **8477**, chosen to stay clear of MeTube (8081)
  and the usual self-hosted crowd — Jellyfin 8096, Sonarr 8989, Radarr 7878,
  Home Assistant 8123. Override with `PORT` if it still clashes; check first
  with `ss -tlnp | grep 8477`.
- **Different download folders**, or at least a Subfolder. Both name files after
  a title, so a shared folder invites collisions. Dowser never overwrites — it
  adds ` (2)` — but separate folders are cleaner.

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
  progress shows elapsed time instead of a percentage. They are written to `.mkv`
  so the file stays playable however you stop it.
- **Adobe HDS (`.f4m`)** is detected and named in the notes, but ffmpeg cannot
  demux it, so it is not offered for download. Sites still serving HDS almost
  always serve HLS alongside it.

Please only download material you have the rights to.

---

## Project layout

```
app/
  main.py              FastAPI routes, WebSocket, static hosting
  config.py            Environment-driven startup settings
  settings_store.py    UI-tunable settings + the schema the panel renders from
  models.py            Stream / Job / request shapes
  naming.py            Title sanitizing, 1-2-3 numbering, collision handling
  jobs.py              Queue, concurrency, stall watchdog, retries, persistence
  downloader.py        ffmpeg / HTTP / yt-dlp execution and progress parsing
  detector/
    __init__.py        The pipeline: capture → classify → expand → fallback
    sniffer.py         Playwright network capture
    classify.py        What is a manifest / file / segment / noise
    manifest.py        HLS + DASH parsing into quality variants
    ytdlp_probe.py     Extractor fallback
static/                Vanilla-JS UI, no build step
deploy/                systemd, supervisord and launchd service definitions
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
| `GET` | `/api/health` | Liveness probe for supervisors and uptime monitors |
| `GET` · `PATCH` | `/api/settings` | Read settings + schema; PATCH a partial update |
| `POST` | `/api/settings/reset` | Back to the `.env` defaults |
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

**A download keeps stalling and retrying.**
Some CDNs throttle hard enough to look dead. Raise **Stall timeout** — the
default of 90s is aggressive for a slow server. If it stalls at the same byte
count every time, the stream likely needs a session cookie that has since
expired; detect the page again to capture fresh headers.

**Video downloads but has no audio.**
The variant was video-only and the audio rendition was not advertised in the
master playlist. Try a different quality, or the unlabelled "HLS stream" entry.
