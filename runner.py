"""Run a Sibernetic simulation in a subprocess and extract metrics.

Built to be called from the FastAPI app's background thread. Stays
deliberately small — every Sibernetic-specific decision (where the
binary lives, how to read position_buffer.txt)
lives here so main.py can stay HTTP-shaped.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SIBERNETIC_ROOT = Path(os.environ.get("SIBERNETIC_ROOT", "/app/sibernetic"))
SIBERNETIC_BIN = SIBERNETIC_ROOT / "Release" / "Sibernetic"

# Pattern A iteration support: when a request supplies sibernetic_ref, we
# clone + build that ref into BUILDS_CACHE/<sha>/ and run the resulting
# binary. The baked-in binary at SIBERNETIC_ROOT is the fast-path default.
SIBERNETIC_REPO_URL = os.environ.get(
    "SIBERNETIC_REPO_URL", "https://github.com/openworm/sibernetic.git"
)
BUILDS_CACHE = Path(os.environ.get("SIBERNETIC_BUILDS_CACHE", "/tmp/sibernetic-builds"))
BUILDS_CACHE_KEEP = int(os.environ.get("SIBERNETIC_BUILDS_CACHE_KEEP", "5"))
GIT_REF_TIMEOUT_SECONDS = 60
SIBERNETIC_BUILD_TIMEOUT_SECONDS = 900  # `make all` on Linux ≈ 2-3 min on 8 vCPU

# Hard subprocess timeout. Longer than the longest reasonable single sim
# but short enough to surface a hung Sibernetic before Cloud Run kills
# the whole instance at 3600s.
DEFAULT_SIM_TIMEOUT_SECONDS = 3300


@dataclass
class RunResult:
    config: str
    timelimit: float
    logstep: int
    wall_clock_seconds: float
    sibernetic_exit_code: int
    sibernetic_stdout_tail: str
    sibernetic_stderr_tail: str
    output_dir: str
    sibernetic_commit: str
    stability_metrics: dict | None  # extracted from position_buffer if available
    sibernetic_ref_requested: str | None = None
    build_seconds: float = 0.0     # 0 when using baked binary; >0 when ref supplied
    build_was_cached: bool = False
    render_seconds: float = 0.0    # 0 when render not requested
    render_local_path: str | None = None  # set when render produced; consumed by /api/render
    render_error: str | None = None


def _read_commit() -> str:
    p = Path("/app/sibernetic-commit.txt")
    return p.read_text().strip() if p.exists() else "unknown"


def _measure_cube_stability(position_file: Path, sim_root: Path) -> dict | None:
    """Use Sibernetic's own measure_cube_stability.py if present."""
    script = sim_root / "scripts" / "measure_cube_stability.py"
    if not script.exists() or not position_file.exists():
        return None
    try:
        # Import as a module so we don't pay subprocess startup cost.
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcs", str(script))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod.measure(position_file)
    except Exception as e:  # pragma: no cover — diagnostic only
        return {"error": f"measure failed: {e!r}"}


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _resolve_ref_to_sha(ref: str) -> str:
    """Turn a branch/tag/short-SHA into a full 40-char SHA via ls-remote.

    SHAs are returned as-is (lowercased). Pure-alphanumeric refs that don't
    look like SHAs are treated as branch/tag names and resolved against
    the upstream repo. Cheap (~100 ms) and lets us cache builds by SHA so
    re-running the same branch reuses the build even after upstream moves.
    """
    ref = ref.strip()
    if not ref:
        raise ValueError("sibernetic_ref must not be empty")
    if _SHA_RE.fullmatch(ref):
        return ref.lower()
    proc = subprocess.run(
        ["git", "ls-remote", SIBERNETIC_REPO_URL, ref, f"refs/heads/{ref}", f"refs/tags/{ref}"],
        capture_output=True, text=True, timeout=GIT_REF_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-remote failed for ref={ref!r}: {proc.stderr.strip()}"
        )
    if not proc.stdout.strip():
        raise ValueError(f"sibernetic_ref={ref!r} not found in {SIBERNETIC_REPO_URL}")
    # First column of first matching line is the full SHA
    return proc.stdout.split()[0]


def _evict_old_builds(keep: int) -> None:
    """LRU eviction by mtime. Cloud Run's tmpfs is sized but not infinite."""
    if not BUILDS_CACHE.exists():
        return
    builds = sorted(
        (p for p in BUILDS_CACHE.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    while len(builds) > keep:
        oldest = builds.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)


def _ensure_build(ref: str) -> tuple[Path, Path, str, float, bool]:
    """Resolve ref, ensure a built binary exists, return paths + timing.

    Returns (binary_path, sim_root_path, resolved_sha, build_seconds, was_cached).
    Caches by SHA so re-runs against the same commit are instant (~ls-remote
    overhead only, ~100 ms).
    """
    sha = _resolve_ref_to_sha(ref)
    build_dir = BUILDS_CACHE / sha
    binary = build_dir / "Release" / "Sibernetic"

    if binary.exists():
        # Touch to update LRU position, return immediately.
        os.utime(build_dir, None)
        return binary, build_dir, sha, 0.0, True

    BUILDS_CACHE.mkdir(parents=True, exist_ok=True)
    _evict_old_builds(keep=BUILDS_CACHE_KEEP - 1)  # leave room for the new one

    # Clean up any partial build dir from a previous failure.
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)

    start = time.monotonic()
    # Full clone (Sibernetic is small enough); checkout the resolved SHA so
    # branch refs lock to a specific commit at request time.
    subprocess.run(
        ["git", "clone", "--quiet", SIBERNETIC_REPO_URL, str(build_dir)],
        check=True, timeout=180,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", sha],
        cwd=str(build_dir), check=True, timeout=30,
    )
    # The Linux makefile reads NUMPYHEADERDIR via shellout; the image already
    # has python3-numpy on the system path so the default works.
    proc = subprocess.run(
        ["make", "all"],
        cwd=str(build_dir),
        capture_output=True, text=True,
        timeout=SIBERNETIC_BUILD_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"sibernetic build failed for ref={ref!r} (sha={sha[:8]}):\n"
            f"--- stdout (tail) ---\n{proc.stdout[-2000:]}\n"
            f"--- stderr (tail) ---\n{proc.stderr[-2000:]}"
        )
    if not binary.exists():
        raise RuntimeError(
            f"sibernetic build completed but binary missing at {binary}"
        )

    return binary, build_dir, sha, time.monotonic() - start, False


def _render_movie(
    position_file: Path,
    output_path: Path,
    *,
    camera: str = "iso",
    include_boundary: bool = True,
    frame_skip: int = 1,
    max_frames: int = 500,
    fps: int = 30,
) -> tuple[float, str | None]:
    """Render an MP4 from position_buffer.txt using the bundled render_movie.py.

    The script is bundled because Sibernetic's development branch ships only
    an interactive viewer. Imported as a module so we don't pay subprocess
    startup cost for every request, and so failures surface as exceptions
    with usable tracebacks.

    Returns (seconds_taken, error_or_None).
    """
    script = Path(__file__).parent / "render_movie.py"
    if not position_file.exists():
        return 0.0, f"position_buffer.txt missing at {position_file}"

    # pyvista offscreen rendering on Linux Cloud Run needs no display
    # server — VTK ships with an OSMesa-capable build. Set the env var
    # before importing pyvista so the right backend is selected.
    os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

    start = time.monotonic()
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("rm", str(script))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        mod.render_to_movie(
            str(position_file),
            str(output_path),
            fps=fps,
            frame_skip=frame_skip,
            max_frames=max_frames,
            include_boundary=include_boundary,
            camera=camera,
        )
    except Exception as e:
        return time.monotonic() - start, f"render failed: {e!r}"
    return time.monotonic() - start, None


def run_sim(
    *,
    config: str,
    timelimit: float,
    logstep: int,
    output_dir: Path,
    timeout: int = DEFAULT_SIM_TIMEOUT_SECONDS,
    sibernetic_ref: str | None = None,
    render: bool = False,
    render_camera: str = "iso",
    render_boundary: bool = True,
    render_frame_skip: int = 1,
    render_max_frames: int = 500,
) -> RunResult:
    # Pick the binary: per-request build if a ref was supplied, else the
    # baked image default for fastest cold-start.
    build_seconds = 0.0
    build_was_cached = False
    resolved_sha: str
    if sibernetic_ref:
        binary, sim_root, resolved_sha, build_seconds, build_was_cached = (
            _ensure_build(sibernetic_ref)
        )
    else:
        binary = SIBERNETIC_BIN
        sim_root = SIBERNETIC_ROOT
        resolved_sha = _read_commit()
        if not binary.exists():
            raise FileNotFoundError(
                f"Sibernetic binary not at {binary}. Did the image build fail?"
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(binary),
        "-no_g",
        "-f", config,           # e.g. "demo1" or "configuration/test/test_energy"
        f"timelimit={timelimit}",
        f"logstep={logstep}",
        "-l_to",
        f"lpath={output_dir}",
    ]

    start = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(sim_root),  # cwd matters: Sibernetic resolves -f config/* relative to it
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    wall_clock = time.monotonic() - start

    pos_file = output_dir / "position_buffer.txt"
    metrics = _measure_cube_stability(pos_file, sim_root) if proc.returncode == 0 else None

    # Optional: render an MP4 from the position buffer. Only attempted if
    # the sim succeeded and the caller asked for it. Render time is
    # reported separately so callers can see the breakdown.
    render_seconds = 0.0
    render_local_path: str | None = None
    render_error: str | None = None
    if render and proc.returncode == 0:
        mp4_path = output_dir / "render.mp4"
        render_seconds, render_error = _render_movie(
            pos_file, mp4_path,
            camera=render_camera,
            include_boundary=render_boundary,
            frame_skip=render_frame_skip,
            max_frames=render_max_frames,
        )
        if render_error is None and mp4_path.exists():
            render_local_path = str(mp4_path)

    return RunResult(
        config=config,
        timelimit=timelimit,
        logstep=logstep,
        wall_clock_seconds=wall_clock,
        sibernetic_exit_code=proc.returncode,
        sibernetic_stdout_tail=proc.stdout[-4000:],
        sibernetic_stderr_tail=proc.stderr[-4000:],
        output_dir=str(output_dir),
        sibernetic_commit=resolved_sha,
        stability_metrics=metrics,
        sibernetic_ref_requested=sibernetic_ref,
        build_seconds=build_seconds,
        build_was_cached=build_was_cached,
        render_seconds=render_seconds,
        render_local_path=render_local_path,
        render_error=render_error,
    )


def upload_buffers_to_gcs(output_dir: Path, gcs_bucket: str, gcs_prefix: str) -> dict[str, str]:
    """Upload position/velocity buffers to GCS, return public-URL-shaped map.

    Returns {"position_buffer": "gs://...", "velocity_buffer": "gs://..."}.
    Buffers are large (often 100s of MB); Cloud Run's per-request memory
    is limited and ephemeral, so we stream them to durable storage and
    return references rather than the bytes themselves.
    """
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(gcs_bucket)
    out: dict[str, str] = {}
    for name in (
        "position_buffer.txt",
        "velocity_buffer.txt",
        "connection_buffer.txt",
        "membranes_buffer.txt",
        "render.mp4",
    ):
        local = output_dir / name
        if not local.exists():
            continue
        blob_path = f"{gcs_prefix}/{name}"
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(str(local))
        key = name.replace(".txt", "").replace(".mp4", "")
        out[key] = f"gs://{gcs_bucket}/{blob_path}"
    return out


def upload_single_to_gcs(local: Path, gcs_bucket: str, gcs_path: str) -> str:
    """Upload one file (e.g. a rendered MP4) to GCS, return gs:// URL.

    Used for files that live outside the temp output_dir — the rendered
    MP4 is moved to a persistent location before the sim's temp dir is
    cleaned up, so it needs its own upload entry point.
    """
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(gcs_bucket)
    blob = bucket.blob(gcs_path)
    blob.upload_from_filename(str(local))
    return f"gs://{gcs_bucket}/{gcs_path}"


def to_dict(r: RunResult) -> dict:
    return asdict(r)
