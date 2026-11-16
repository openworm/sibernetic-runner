"""Batch entry point for running one Sibernetic simulation as a Cloud Run Job.

Unlike main.py (the HTTP service), this runs a single simulation from
environment variables, uploads the results and exits, so a long run can be
started with `gcloud run jobs execute` and no web form or polling.

Environment variables (all optional except GCS_BUCKET):
    GCS_BUCKET        results bucket (set by createjob.sh)
    CONFIG            Sibernetic config name           (default demo1)
    TIMELIMIT         simulated seconds                (default 0.05)
    LOGSTEP           log every Nth step               (default 10)
    LEMS              LEMS file, relative to the Sibernetic root; when set,
                      C302_REF and C302PARAMS are not used
    C302_REF          no_c302, FW or TargetMuscle      (default no_c302)
    C302PARAMS        c302 parameter set, e.g. C2      (default C2)
    SIBERNETIC_REF    branch/tag/SHA to build first    (default: baked binary)
    RENDER            true/1 to also make an MP4       (default false)
    TIMEOUT_SECONDS   limit for the simulation         (default 11 hours)

Results go to gs://$GCS_BUCKET/runs/<execution name>/. Output files are
uploaded even if the simulation fails, since a long run's partial output is
worth keeping. The exit code is non-zero if the simulation or the upload fails.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import runner


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def main() -> int:
    bucket = os.environ.get("GCS_BUCKET", "")
    if not bucket:
        print("GCS_BUCKET is not set: results would be lost", file=sys.stderr)
        return 2

    # Cloud Run sets CLOUD_RUN_EXECUTION to the execution's name.
    run_id = os.environ.get("CLOUD_RUN_EXECUTION") or uuid.uuid4().hex[:8]
    out_dir = Path(tempfile.mkdtemp(prefix=f"sibrun_{run_id}_"))
    params = {
        "config": os.environ.get("CONFIG", "demo1"),
        "timelimit": float(os.environ.get("TIMELIMIT", "0.05")),
        "logstep": int(os.environ.get("LOGSTEP", "10")),
        "lems": os.environ.get("LEMS") or None,
        "c302_ref": os.environ.get("C302_REF", "no_c302"),
        "c302params": os.environ.get("C302PARAMS", "C2"),
        "sibernetic_ref": os.environ.get("SIBERNETIC_REF") or None,
        "render": _flag("RENDER"),
        "timeout": int(os.environ.get("TIMEOUT_SECONDS", str(11 * 3600))),
    }
    runner.print_(f"Job {run_id}: {params}")

    exit_code = 1
    try:
        result = runner.run_sim(output_dir=out_dir, **params)
        exit_code = result.sibernetic_exit_code

        prefix = f"runs/{run_id}"
        buffer_urls: dict[str, str] = {}
        upload_error: str | None = None
        try:
            buffer_urls = runner.upload_buffers_to_gcs(out_dir, bucket, prefix)
            summary = out_dir / "job_result.json"
            summary.write_text(
                json.dumps(
                    {"params": params, "result": runner.to_dict(result), "buffer_urls": buffer_urls},
                    indent=2,
                )
            )
            runner.upload_single_to_gcs(summary, bucket, f"{prefix}/job_result.json")
        except Exception as e:
            upload_error = f"upload to gs://{bucket} failed: {e!r}"
            print(upload_error, file=sys.stderr)

        print(json.dumps({"exit_code": exit_code, "upload_error": upload_error, "buffer_urls": buffer_urls}, indent=2))
        if exit_code == 0 and upload_error is not None:
            exit_code = 3
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
