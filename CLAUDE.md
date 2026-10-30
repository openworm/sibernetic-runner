# sibernetic-runner

See `README.md` for what this is and how to use it. Notes for working on it:

- **Auth is Cloud Run IAM only.** Keep `--no-allow-unauthenticated` in `deploy.sh`. A public URL with a GPU behind it can be kept warm by anyone, at about $1.31 per hour.
- **`--max-instances 1` is a cost guard.** Raise it only on purpose.
- **A failed upload must stay a failed job.** Buffers are deleted with the temp dir, so "succeeded" with no upload means lost results.
- **This repo is public.** No keys or service URLs in commits. Project and bucket names are fine.
- **`render_movie.py` is bundled** because Sibernetic's `development` branch ships only the interactive `SiberneticReplay.py`.
- **Stability metrics are optional.** `_measure_cube_stability` looks for `scripts/measure_cube_stability.py` in the Sibernetic checkout and returns `None` on `development`, which doesn't have it.
- **Verify on the real service.** The GPU's OpenCL driver is mounted at runtime, so a local build can't prove a GPU run works. `demo1` with `timelimit` 0.05 is the cheap check.
