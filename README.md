# sibernetic-runner

Run [Sibernetic](https://github.com/openworm/sibernetic) simulations on a cloud GPU over HTTP. Submit a simulation, get back metrics, the position and other buffer files, and an optional MP4.

It is a small FastAPI service on Google Cloud Run with one NVIDIA L4 GPU. It scales to zero when idle.

## What it runs

The service runs the **Sibernetic binary with the OpenCL solver**. The default image is built from Sibernetic's `development` branch, and any request can build and run another branch, tag or commit.

It does not currently cover:

- **Coupled c302 simulations.** The image has no c302 or NEURON, so this is the physics engine on its own. Configs that auto-load the muscle simulation need `config_inline` and `config_filename` to avoid that hook, as described in `main.py`.
- **The native Metal and WebGPU backends.** They need Apple hardware or a browser.

## Using it

You need the [gcloud CLI](https://cloud.google.com/sdk/docs/install) and a Google account that has been granted access (see [Granting access](#granting-access)).

```bash
gcloud auth login
gcloud run services proxy sibernetic-runner \
  --region=us-east4 --project=openworm-agents-prod --port=8080
```

Leave that running, then open http://localhost:8080/ for a test form, or use the API:

```bash
# Submit
curl -X POST http://localhost:8080/api/run \
  -H "Content-Type: application/json" \
  -d '{"config":"demo1","timelimit":0.05,"logstep":10}'
# → {"job_id":"abc12345","status":"queued"}

curl http://localhost:8080/api/status/abc12345    # poll
curl http://localhost:8080/api/result/abc12345    # fetch the result
```

**Keep polling while a job runs.** Job state lives in the instance's memory, and Cloud Run can reclaim an instance that gets no requests. Polling every minute or so is enough. Buffers already uploaded to the bucket survive either way.

### Request fields

| Field | Default | Meaning |
|---|---|---|
| `config` | `demo1` | Config name, or a path such as `configuration/test/test_energy` |
| `timelimit` | `0.05` | Simulated seconds, up to 60 |
| `logstep` | `10` | Log every Nth step |
| `sibernetic_ref` | none | Branch, tag or commit to clone and build first. Takes 2-3 minutes the first time on an instance, then is cached by commit until the instance goes away. |
| `render` | `false` | Also produce an MP4, streamed from `/api/render/{job_id}` |
| `render_camera` | `iso` | `iso` or `side` |
| `render_boundary` | `true` | Include floor and wall particles |
| `render_frame_skip` | `1` | Render every Nth logged frame |
| `render_max_frames` | `500` | Limit on total frames |
| `config_inline`, `config_filename` | none | Supply a config as text instead of using one from the image |

### Limits

- One job runs at a time, on one instance.
- A simulation is stopped after 55 minutes (`DEFAULT_SIM_TIMEOUT_SECONDS` in `runner.py`). For scale, `demo1` has taken about 84 seconds of wall clock per simulated second on an L4.
- An instance shuts itself down after 5 minutes with no jobs and no requests.

## Results

A finished job's buffers go to `gs://openworm-agents-prod-sibernetic-results/runs/<job_id>/`, and `/api/result/<job_id>` lists them. A job whose upload fails is reported as `failed`, with the reason in `error`.

```bash
gcloud storage cp -r gs://openworm-agents-prod-sibernetic-results/runs/abc12345 .
```

## Cost

About **$1.31 per hour while an instance is up**, at list prices for 8 vCPU, 16 GiB and an L4 without zonal redundancy. There is no compute charge when it is idle. Google bills for the whole time an instance is up, including its idle shutdown window, so a short test costs about 5 minutes more than the simulation itself.

## Deploying

```bash
./setup.sh     # once per project: bucket, image repository, service account
./deploy.sh    # build (about 25 minutes the first time) and deploy
```

`PROJECT`, `REGION`, `SERVICE` and `BUCKET` can be overridden with environment variables. Cloud Run grants a project 3 L4 GPUs in a region automatically at its first deployment there.

## Granting access

Access is Google Cloud IAM only. There is no API key, and the service refuses anyone without `roles/run.invoker`.

```bash
USER=user:someone@example.org
gcloud run services add-iam-policy-binding sibernetic-runner \
  --region=us-east4 --project=openworm-agents-prod --member="$USER" --role=roles/run.invoker
gcloud run services add-iam-policy-binding sibernetic-runner \
  --region=us-east4 --project=openworm-agents-prod --member="$USER" --role=roles/run.viewer
gcloud storage buckets add-iam-policy-binding gs://openworm-agents-prod-sibernetic-results \
  --member="$USER" --role=roles/storage.objectViewer
gcloud projects add-iam-policy-binding openworm-agents-prod \
  --member="$USER" --role=roles/logging.viewer --condition=None
```

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app and in-memory job store |
| `runner.py` | Runs the simulation, builds other refs, renders, uploads |
| `render_movie.py` | Headless MP4 renderer, from Sibernetic's `ow-native-gpu-0.1.0` branch |
| `Dockerfile`, `requirements.txt`, `cloudbuild.yaml` | The image |
| `setup.sh`, `deploy.sh` | Project setup and deployment |
| `monitoring/long-running-instance.yaml` | Alert policy for an instance left running |

## License

MIT. See `LICENSE`.
