"""Dowser's HTTP layer: detection endpoints, queue control, live state socket."""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .jobs import queue
from .models import DownloadRequest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await queue.start()
    try:
        yield
    finally:
        await queue.stop()


app = FastAPI(title="Dowser", lifespan=lifespan)


class DetectBatch(BaseModel):
    urls: list[str]
    #: Skip the headless browser — extractor only, much faster.
    quick: bool = False


class RevealRequest(BaseModel):
    path: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def state() -> dict:
    return queue.snapshot()


@app.post("/api/detect", status_code=202)
async def detect(payload: DetectBatch) -> dict:
    urls = [u for u in payload.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs given")
    accepted = queue.start_detection(urls, quick=payload.quick)
    return {"accepted": accepted}


@app.post("/api/download", status_code=201)
async def download(payload: DownloadRequest) -> dict:
    if not payload.items:
        raise HTTPException(status_code=400, detail="Nothing selected to download")
    jobs = queue.add(
        page_url=payload.page_url,
        title=payload.title,
        items=payload.items,
        subfolder=payload.subfolder,
    )
    return {"queued": [job.id for job in jobs]}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel(job_id: str) -> dict:
    if not queue.cancel(job_id):
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry(job_id: str) -> dict:
    if not queue.retry(job_id):
        raise HTTPException(status_code=409, detail="Job cannot be retried right now")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def remove(job_id: str) -> dict:
    if not queue.remove(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


@app.post("/api/queue/clear")
async def clear_queue() -> dict:
    return {"removed": queue.clear_finished()}


@app.delete("/api/detections")
async def clear_detections(url: str | None = None) -> dict:
    if url:
        if not queue.forget_detection(url):
            raise HTTPException(status_code=404, detail="Unknown URL")
        queue._notify()  # noqa: SLF001 - same module family, keeps the UI in sync
        return {"removed": 1}
    return {"removed": queue.clear_detections()}


@app.post("/api/reveal")
async def reveal(payload: RevealRequest) -> JSONResponse:
    """Show a finished file in the OS file manager (local convenience only)."""
    target = Path(payload.path).resolve()
    try:
        target.relative_to(settings.download_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path is outside the download folder")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File no longer exists")

    command = {
        "darwin": ["open", "-R", str(target)],
        "win32": ["explorer", "/select,", str(target)],
    }.get(sys.platform, ["xdg-open", str(target.parent)])
    with contextlib.suppress(OSError):
        subprocess.Popen(command)  # noqa: S603
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue.subscribe(websocket)
    try:
        await websocket.send_json(queue.snapshot())
        while True:
            # We only need the socket open; incoming messages act as a keepalive.
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        queue.unsubscribe(websocket)


# Finished videos, browsable straight from the queue.
app.mount("/files", StaticFiles(directory=settings.download_dir), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
