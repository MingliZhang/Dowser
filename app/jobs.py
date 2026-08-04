"""Download queue: bounded concurrency, live progress, disk persistence."""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import downloader, naming
from .config import settings
from .models import DownloadItem, Job, JobStatus


class QueueManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        #: url -> detection entry, including the ones that found nothing.
        self.detections: dict[str, dict[str, Any]] = {}
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: dict[str, asyncio.Task] = {}
        self._workers: list[asyncio.Task] = []
        self._subscribers: set[Any] = set()
        self._dirty = asyncio.Event()
        self._flusher: asyncio.Task | None = None
        self._detect_tasks: set[asyncio.Task] = set()
        #: Headless browsers are heavy — only a few pages at a time.
        self._detect_sem = asyncio.Semaphore(3)

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._load()
        for _ in range(settings.max_concurrent):
            self._workers.append(asyncio.create_task(self._worker()))
        self._flusher = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        for task in [*self._workers, *self._tasks.values()]:
            task.cancel()
        if self._flusher:
            self._flusher.cancel()
        await asyncio.gather(
            *self._workers, *self._tasks.values(), return_exceptions=True
        )
        self._save()

    # --- public API ----------------------------------------------------------

    def add(
        self,
        page_url: str,
        title: str,
        items: list[DownloadItem],
        subfolder: str | None = None,
    ) -> list[Job]:
        """Queue every selected stream from one page, numbered if there are several."""
        stem = naming.sanitize(title, strip_site_suffix=True)
        folder = naming.safe_subfolder(subfolder)
        created: list[Job] = []

        for index, item in enumerate(items):
            base = naming.sanitize(item.filename) if item.filename else stem
            filename = naming.numbered(base, index, len(items)) if not item.filename else base
            if folder:
                filename = f"{folder}/{filename}"
            job = Job(
                page_url=page_url,
                page_title=title,
                filename=filename,
                stream=item.stream,
            )
            job.stream.page_url = job.stream.page_url or page_url
            self.jobs[job.id] = job
            self._pending.put_nowait(job.id)
            created.append(job)

        self._notify()
        return created

    # --- detection -----------------------------------------------------------

    def start_detection(self, urls: list[str], quick: bool = False) -> list[str]:
        """Queue a detection pass for each URL; results stream over the socket."""
        accepted: list[str] = []
        for raw in urls:
            url = raw.strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            self.detections[url] = {
                "url": url,
                "status": "queued",
                "title": "",
                "title_source": "",
                "streams": [],
                "notes": [],
                "detected_at": time.time(),
            }
            accepted.append(url)
            task = asyncio.create_task(self._detect(url, quick))
            self._detect_tasks.add(task)
            task.add_done_callback(self._detect_tasks.discard)
        self._notify()
        return accepted

    async def _detect(self, url: str, quick: bool) -> None:
        from . import detector  # imported lazily so startup stays fast

        entry = self.detections[url]
        async with self._detect_sem:
            entry["status"] = "running"
            self._notify()
            try:
                result = await detector.detect(url, quick=quick)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop a batch
                entry.update(
                    status="error",
                    notes=[f"Detection failed: {exc}"[:400]],
                    detected_at=time.time(),
                )
                self._notify()
                return

            entry.update(
                # "none" is the explicit mark for a page with no video on it.
                status="ok" if result.has_video else "none",
                title=result.title,
                title_source=result.title_source,
                streams=[s.model_dump(mode="json") for s in result.streams],
                notes=result.notes,
                detected_at=time.time(),
            )
            self._notify()

    def forget_detection(self, url: str) -> bool:
        return self.detections.pop(url, None) is not None

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status == JobStatus.RUNNING:
            if task := self._tasks.get(job_id):
                task.cancel()
            return True
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
            job.message = "Cancelled before it started"
            job.finished_at = time.time()
            self._notify()
            return True
        return False

    def remove(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status in {JobStatus.RUNNING, JobStatus.QUEUED}:
            self.cancel(job_id)
        self.jobs.pop(job_id, None)
        self._notify()
        return True

    def retry(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status in {JobStatus.RUNNING, JobStatus.QUEUED}:
            return False
        job.status = JobStatus.QUEUED
        job.percent = job.downloaded_bytes = job.total_bytes = None
        job.speed = job.eta = None
        job.message = ""
        job.started_at = job.finished_at = None
        self._pending.put_nowait(job.id)
        self._notify()
        return True

    def clear_finished(self) -> int:
        done = [
            jid for jid, job in self.jobs.items()
            if job.status in {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}
        ]
        for jid in done:
            self.jobs.pop(jid, None)
        self._notify()
        return len(done)

    def clear_detections(self) -> int:
        count = len(self.detections)
        self.detections.clear()
        self._notify()
        return count

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "state",
            "jobs": [job.public() for job in self.jobs.values()],
            "detections": list(self.detections.values()),
            "settings": {
                "download_dir": str(settings.download_dir),
                "max_concurrent": settings.max_concurrent,
                "sniffer": settings.playwright_available,
                "ffmpeg": settings.ffmpeg_available,
            },
        }

    # --- subscribers ---------------------------------------------------------

    def subscribe(self, websocket) -> None:
        self._subscribers.add(websocket)

    def unsubscribe(self, websocket) -> None:
        self._subscribers.discard(websocket)

    def _notify(self) -> None:
        self._dirty.set()

    async def _flush_loop(self) -> None:
        """Coalesce rapid progress updates into ~4 broadcasts a second."""
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            payload = self.snapshot()
            dead = []
            for websocket in list(self._subscribers):
                try:
                    await websocket.send_json(payload)
                except Exception:  # noqa: BLE001
                    dead.append(websocket)
            for websocket in dead:
                self._subscribers.discard(websocket)
            self._save()
            await asyncio.sleep(0.25)

    # --- worker --------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job_id = await self._pending.get()
            job = self.jobs.get(job_id)
            if not job or job.status != JobStatus.QUEUED:
                continue
            task = asyncio.create_task(self._process(job))
            self._tasks[job.id] = task
            try:
                await task
            except asyncio.CancelledError:
                if job.status == JobStatus.RUNNING:
                    job.status = JobStatus.CANCELLED
                    job.message = "Cancelled"
                    job.finished_at = time.time()
                    self._notify()
            finally:
                self._tasks.pop(job.id, None)

    async def _process(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.message = "Starting..."
        self._notify()

        def on_progress(update: dict) -> None:
            for key, value in update.items():
                if value is not None:
                    setattr(job, key, value)
            self._notify()

        try:
            temp_path = await downloader.run(job, on_progress)
            final_path = self._finalize(job, temp_path)
            job.status = JobStatus.DONE
            job.percent = 100.0
            job.output_path = str(final_path)
            job.message = f"Saved to {final_path.name}"
            job.total_bytes = job.total_bytes or final_path.stat().st_size
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.message = "Cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            job.status = JobStatus.ERROR
            job.message = str(exc)[:400] or exc.__class__.__name__
        finally:
            job.finished_at = time.time()
            job.speed = job.eta = None
            self._notify()

    def _finalize(self, job: Job, temp_path: Path) -> Path:
        """Move the finished temp file to its finished name in the download dir."""
        relative = Path(job.filename)
        target_dir = settings.download_dir / relative.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        extension = temp_path.suffix or f".{job.stream.container}"
        final_path = naming.unique_path(target_dir, relative.name, extension)
        shutil.move(str(temp_path), str(final_path))
        return final_path

    # --- persistence ---------------------------------------------------------

    def _save(self) -> None:
        payload = {
            "jobs": [job.model_dump(mode="json") for job in self.jobs.values()],
            # Only settled detections are worth keeping across a restart.
            "detections": [
                d for d in self.detections.values()
                if d.get("status") in {"ok", "none", "error"}
            ],
        }
        tmp = settings.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(settings.state_file)
        except OSError:
            pass

    def _load(self) -> None:
        if not settings.state_file.exists():
            return
        try:
            payload = json.loads(settings.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        for raw in payload.get("jobs", []):
            with contextlib.suppress(Exception):
                job = Job.model_validate(raw)
                # Anything mid-flight when we shut down never finished.
                if job.status in {JobStatus.RUNNING, JobStatus.QUEUED}:
                    job.status = JobStatus.ERROR
                    job.message = "Interrupted by a restart — retry to resume"
                self.jobs[job.id] = job
        for entry in payload.get("detections", []):
            if url := entry.get("url"):
                self.detections[url] = entry


queue = QueueManager()
