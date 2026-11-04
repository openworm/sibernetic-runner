"""FastAPI wrapper for Sibernetic.

Submits a sim, polls status, fetches results. Long-running work runs in a
background thread because Cloud Run's HTTP layer can't hold connections open
for minutes at a time.

Endpoints:
    GET  /                        HTML form (browser test path)
    GET  /health                  Liveness check
    POST /api/run                 Submit a job
    GET  /api/status/{job_id}     Poll job status
    GET  /api/result/{job_id}     Fetch result (metrics + GCS buffer URLs)
    GET  /api/render/{job_id}     Stream the rendered MP4

Authentication is Google Cloud IAM: the service is deployed with
--no-allow-unauthenticated, so requests only reach this app if the caller
holds roles/run.invoker. There is deliberately no application-level key.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import runner

# Set by deploy.sh. Empty means "don't upload", for local development only:
# on Cloud Run the temp output dir is gone once the job ends.
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")

# Persistent render MP4s outlive the temp output_dir so /api/render/{id}
# can stream them after the simulation cleanup completes. Cloud Run
# instances are ephemeral, so this is best-effort within an instance
# lifetime — if the instance scales to 0 between sim and download, the
# caller falls back to fetching the GCS URL from the result payload.
RENDERS_DIR = Path("/tmp/sibrunner_renders")
RENDERS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Sibernetic Runner", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

try:
    VERSION = (Path(__file__).parent / "VERSION").read_text().strip()
except OSError:
    VERSION = "unknown"

# Self-shutdown after IDLE_TIMEOUT_SECONDS of no active jobs. Cloud Run's
# default idle scale-to-zero is ~15 min; that's too generous for a
# GPU-attached service when --no-cpu-throttling means warm == billed.
# Tighten to 5 min — the watchdog thread checks every 30s and
# os._exit()s the container when idle. Cloud Run replaces it on the
# next legitimate request (cold start is ~10s).
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "300"))
_last_activity_time = [time.time()]   # mutable cell for thread access
_activity_lock = threading.Lock()


def _bump_activity() -> None:
    with _activity_lock:
        _last_activity_time[0] = time.time()


def _idle_watchdog() -> None:
    while True:
        time.sleep(30)
        with _activity_lock:
            idle = time.time() - _last_activity_time[0]
        any_active = any(j["status"] in ("queued", "running") for j in jobs.values())
        if idle > IDLE_TIMEOUT_SECONDS and not any_active:
            print(f"[watchdog] idle {idle:.0f}s with no active jobs — exiting", flush=True)
            os._exit(0)


# Start the idle watchdog when the FastAPI app starts. daemon=True so it
# doesn't block container shutdown if anything else exits first.
@app.on_event("startup")
def _start_watchdog() -> None:
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    print(f"[watchdog] started — idle timeout {IDLE_TIMEOUT_SECONDS}s", flush=True)

# In-memory job store. Single-instance Cloud Run service with concurrency=1
# means at most one active job at a time, but we keep history for status
# polling within an instance lifetime.
jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


class RunRequest(BaseModel):
    config: str = Field(default="demo1", description="Sibernetic config name (e.g. 'demo1' or 'configuration/test/test_energy')")
    c302_ref: Literal["no_c302", "FW", "TargetMuscle"] = Field(
        default="no_c302",
        description="c302 neuronal model reference passed to sibernetic_c302.py as -reference. "
                    "'no_c302' runs Sibernetic's built-in sine-wave muscle generator instead (-noc302).",
    )
    timelimit: float = Field(default=0.05, gt=0, le=60.0, description="Sim seconds")
    logstep: int = Field(default=10, ge=1, description="Log every Nth step")
    sibernetic_ref: str | None = Field(
        default=None,
        description="Optional git ref (branch, tag, or SHA) to clone + build before running. "
                    "Omit to use the binary baked into the image. Per-ref builds are cached "
                    "by SHA in /tmp; first call on a new ref pays a 2-3 min build cost.",
    )

    # Optional MP4 rendering of the position buffer. Adds ~10-60s wall
    # clock depending on frame count + scenario size. Result is uploaded
    # to GCS and streamable from /api/render/{job_id}.
    render: bool = Field(default=False, description="Render an MP4 from the position buffer after sim completes")
    render_camera: str = Field(default="iso", description="'iso' (3/4 view, floor visible) or 'side' (original)")
    render_boundary: bool = Field(default=True, description="Include boundary particles (floor/walls) in render")
    render_frame_skip: int = Field(default=1, ge=1, description="Render every Nth logged frame")
    render_max_frames: int = Field(default=500, ge=1, description="Cap total frames in MP4")

    # Inline config staging. When config_inline is supplied, the runner
    # writes it to a temp directory and uses that as the -f arg, ignoring
    # the `config` field's image-baked lookup. config_filename controls
    # the basename (used by Sibernetic for isWormConfig() etc.). This is
    # how callers run worm-formatted configs without triggering the
    # binary's c302/MuscleSimulation auto-hook (which wants a Python
    # module that's not in this image).
    config_inline: str | None = Field(default=None, description="Inline config text to stage; overrides image-baked config when set")
    config_filename: str | None = Field(default=None, description="Basename for the staged inline config (drives isWormConfig() in the binary)")


def _new_job(req: RunRequest) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request": req.model_dump(),
            "result": None,
            "error": None,
            "buffer_urls": None,
        }
    return job_id


def _run_job(job_id: str, req: RunRequest) -> None:
    with _jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
    out_dir = Path(tempfile.mkdtemp(prefix=f"sibrun_{job_id}_"))
    inline_dir: Path | None = None
    config_arg = req.config
    if req.config_inline:
        inline_dir = Path(tempfile.mkdtemp(prefix=f"sibinline_{job_id}_"))
        basename = req.config_filename or "inline_config"
        if "/" in basename or "\\" in basename:
            raise HTTPException(status_code=400, detail="config_filename must be a bare basename")
        staged = inline_dir / basename
        staged.write_text(req.config_inline)
        config_arg = str(staged)
    try:
        result = runner.run_sim(
            config=config_arg,
            c302_ref=req.c302_ref,
            timelimit=req.timelimit,
            logstep=req.logstep,
            output_dir=out_dir,
            sibernetic_ref=req.sibernetic_ref,
            render=req.render,
            render_camera=req.render_camera,
            render_boundary=req.render_boundary,
            render_frame_skip=req.render_frame_skip,
            render_max_frames=req.render_max_frames,
        )
        # Move the rendered MP4 (if any) to the persistent renders dir
        # before the temp output_dir gets cleaned up — so /api/render/{id}
        # can still stream it after this thread returns.
        persistent_render: Path | None = None
        if result.render_local_path:
            try:
                persistent_render = RENDERS_DIR / f"{job_id}.mp4"
                shutil.move(result.render_local_path, str(persistent_render))
                result.render_local_path = str(persistent_render)
            except Exception as mv_err:
                result.render_error = (result.render_error or "") + f" | move_failed: {mv_err!r}"
                persistent_render = None

        # Upload buffers (and render.mp4 if present) if the run produced any.
        buffer_urls: dict[str, str] = {}
        upload_error: str | None = None
        if result.sibernetic_exit_code == 0 and GCS_BUCKET:
            try:
                buffer_urls = runner.upload_buffers_to_gcs(
                    out_dir, GCS_BUCKET, f"runs/{job_id}"
                )
                # render.mp4 was moved out of out_dir; upload it separately
                # if present, using the same GCS prefix.
                if persistent_render and persistent_render.exists():
                    render_url = runner.upload_single_to_gcs(
                        persistent_render, GCS_BUCKET, f"runs/{job_id}/render.mp4"
                    )
                    buffer_urls["render"] = render_url
            except Exception as upload_err:
                # The buffers are deleted with the temp dir below, so a failed
                # upload means the results are lost: report it as a failure.
                upload_error = f"upload to gs://{GCS_BUCKET} failed: {upload_err!r}"

        with _jobs_lock:
            jobs[job_id]["status"] = (
                "succeeded"
                if result.sibernetic_exit_code == 0 and upload_error is None
                else "failed"
            )
            jobs[job_id]["error"] = upload_error
            jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            jobs[job_id]["result"] = runner.to_dict(result)
            jobs[job_id]["buffer_urls"] = buffer_urls
            jobs[job_id]["render_local_path"] = (
                str(persistent_render) if persistent_render and persistent_render.exists() else None
            )
    except Exception as e:
        with _jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = repr(e)
            jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        # Clean up the temp dir; metrics + GCS URLs already captured.
        shutil.rmtree(out_dir, ignore_errors=True)
        if inline_dir is not None:
            shutil.rmtree(inline_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    _bump_activity()
    return {
        "status": "ok",
        "binary": str(runner.SIBERNETIC_BIN),
        "binary_present": runner.SIBERNETIC_BIN.exists(),
        "active_jobs": sum(1 for j in jobs.values() if j["status"] in ("queued", "running")),
        "total_jobs": len(jobs),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    _bump_activity()
    return templates.TemplateResponse("home.html", {"request": request, "version": VERSION})


@app.post("/api/run")
def api_run(body: RunRequest) -> dict:
    _bump_activity()
    job_id = _new_job(body)
    threading.Thread(target=_run_job, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/status/{job_id}")
def api_status(job_id: str) -> dict:
    _bump_activity()
    with _jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="unknown job_id")
        # Return a shallow snapshot — caller polls; result is fetched separately.
        j = jobs[job_id]
        return {
            "id": j["id"],
            "status": j["status"],
            "created_at": j["created_at"],
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "request": j["request"],
            "error": j["error"],
        }


@app.get("/api/result/{job_id}")
def api_result(job_id: str) -> JSONResponse:
    _bump_activity()
    with _jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="unknown job_id")
        j = dict(jobs[job_id])
    if j["status"] not in ("succeeded", "failed"):
        raise HTTPException(status_code=409, detail=f"job is still {j['status']}")
    return JSONResponse(j)


@app.get("/api/render/{job_id}")
def api_render(job_id: str):
    """Stream the rendered MP4 back to the caller.

    Tries the persisted-on-disk copy first (fast), falls back to fetching
    from GCS and streaming through if the local file is gone (e.g. after
    instance restart).
    """
    _bump_activity()
    with _jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="unknown job_id")
        j = jobs[job_id]
        local = j.get("render_local_path")
        gcs_url = (j.get("buffer_urls") or {}).get("render")

    if local and Path(local).exists():
        return FileResponse(
            local,
            media_type="video/mp4",
            filename=f"sibernetic-{job_id}.mp4",
        )

    if gcs_url and gcs_url.startswith("gs://"):
        # gs://bucket/path → fetch and stream
        from google.cloud import storage
        from fastapi.responses import StreamingResponse

        bucket_name, _, blob_path = gcs_url[5:].partition("/")
        blob = storage.Client().bucket(bucket_name).blob(blob_path)
        if not blob.exists():
            raise HTTPException(status_code=404, detail="render not found in GCS either")
        return StreamingResponse(
            iter([blob.download_as_bytes()]),
            media_type="video/mp4",
            headers={"Content-Disposition": f'inline; filename="sibernetic-{job_id}.mp4"'},
        )

    raise HTTPException(status_code=404, detail="no render available for this job")
