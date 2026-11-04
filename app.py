"""Local web UI for the scraper.  Run:  python app.py   (then use the page that opens in your browser)"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import socket
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from scraper.engines import ENGINES
from scraper.pipeline import ScrapeConfig, scrape

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
INDEX_HTML = ROOT / "web" / "index.html"

log = logging.getLogger("scraper")


class ScrapeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    engines: list[str] = Field(min_length=1)
    top: int = Field(5, ge=1, le=50)
    depth: int = Field(1, ge=0, le=3)
    links: int = Field(3, ge=0, le=20)
    any_domain: bool = False
    max_pages: int = Field(100, ge=1, le=500)
    max_chars: int = Field(0, ge=0, le=1_000_000)
    concurrency: int = Field(6, ge=1, le=20)
    timeout: float = Field(20, ge=5, le=120)
    browser: bool = False
    show_browser: bool = False
    js_fallback: bool = True
    respect_robots: bool = True


@dataclass
class Job:
    id: str
    query: str
    status: str = "running"  # running | done | error | cancelled
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    run_id: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None
    cancel_requested: bool = False


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.job.logs.append(self.format(record))


_jobs: dict[str, Job] = {}
_current: Job | None = None
_jobs_lock = threading.Lock()


def _run_job(job: Job, cfg: ScrapeConfig) -> None:
    # Own event loop per job; Proactor is required on Windows for Playwright's browser subprocess.
    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    handler = _JobLogHandler(job)
    log.addHandler(handler)
    try:
        job.task = loop.create_task(scrape(cfg))
        job.loop = loop
        if job.cancel_requested:
            job.task.cancel()
        run = loop.run_until_complete(job.task)
        job.run_id = run.paths["dir"].name
        job.logs.append(
            f"Done in {run.elapsed:.0f}s: read {run.pages_ok}/{len(run.pages)} pages "
            f"({run.duplicates} duplicates skipped), {run.total_chars:,} characters."
        )
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.logs.append("Cancelled.")
    except Exception as e:
        job.status, job.error = "error", str(e)
        job.logs.append(f"Error: {e}")
    finally:
        log.removeHandler(handler)
        job.finished = time.time()
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def _job_view(job: Job, since: int = 0) -> dict:
    return {
        "id": job.id,
        "query": job.query,
        "status": job.status,
        "logs": job.logs[since:],
        "log_count": len(job.logs),
        "error": job.error,
        "run_id": job.run_id,
        "elapsed": (job.finished or time.time()) - job.started,
    }


def _run_dir(run_id: str) -> Path:
    path = (OUTPUT_DIR / run_id).resolve()
    if path.parent != OUTPUT_DIR.resolve() or not (path / "data.json").is_file():
        raise HTTPException(404, "Run not found")
    return path


app = FastAPI(title="Web Scraper")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/api/engines")
def engines():
    return {"engines": list(ENGINES), "default": ["google", "bing", "duckduckgo"]}


@app.post("/api/scrape")
def start_scrape(req: ScrapeRequest):
    global _current
    unknown = [e for e in req.engines if e not in ENGINES]
    if unknown:
        raise HTTPException(400, f"Unknown engine(s): {', '.join(unknown)}")
    with _jobs_lock:
        if _current and _current.status == "running":
            raise HTTPException(409, "A scrape is already running. Wait for it or stop it first.")
        job = Job(id=uuid.uuid4().hex[:12], query=req.query.strip())
        _jobs[job.id] = job
        _current = job
    cfg = ScrapeConfig(
        query=req.query, engines=",".join(req.engines), top=req.top, depth=req.depth, links=req.links,
        any_domain=req.any_domain, max_pages=req.max_pages, max_chars=req.max_chars,
        concurrency=req.concurrency, timeout=req.timeout, browser=req.browser,
        show_browser=req.show_browser, js_fallback=req.js_fallback,
        respect_robots=req.respect_robots, out_dir=str(OUTPUT_DIR),
    )
    threading.Thread(target=_run_job, args=(job, cfg), daemon=True, name=f"scrape-{job.id}").start()
    return {"job_id": job.id}


@app.get("/api/jobs/current")
def current_job():
    return {"job": _job_view(_current) if _current else None}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, since: int = 0):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_view(job, max(0, since))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status == "running":
        job.cancel_requested = True
        job.logs.append("Stopping...")
        if job.loop and job.task:
            job.loop.call_soon_threadsafe(job.task.cancel)
    return {"status": job.status}


@app.get("/api/runs")
def list_runs():
    runs = []
    if OUTPUT_DIR.is_dir():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            meta = d / "data.json"
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            runs.append({
                "id": d.name,
                "query": data.get("query", d.name),
                "generated_at": data.get("generated_at"),
                "engines": data.get("settings", {}).get("engines", []),
                "stats": data.get("stats", {}),
            })
    return {"runs": runs[:100]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return FileResponse(_run_dir(run_id) / "data.json", media_type="application/json")


@app.get("/api/runs/{run_id}/files/{name}")
def get_run_file(run_id: str, name: str, download: bool = False):
    if name not in ("raw_data.md", "data.json"):
        raise HTTPException(404, "File not found")
    path = _run_dir(run_id) / name
    media = "text/markdown; charset=utf-8" if name.endswith(".md") else "application/json"
    filename = f"{run_id}_{name}" if download else None
    return FileResponse(path, media_type=media, filename=filename,
                        content_disposition_type="attachment" if download else "inline")


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    shutil.rmtree(_run_dir(run_id))
    return {"deleted": run_id}


def _free_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:
                return port
    raise SystemExit(f"No free port found in {preferred}-{preferred + 19}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Web UI for the multi-engine scraper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpcore", "trafilatura", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    port = _free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    print(f"\n  Web Scraper UI running at {url}  (Ctrl+C to stop)\n")
    if not args.no_open:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
