#!/bin/bash
# Build and deploy sibernetic-runner to Cloud Run with an NVIDIA L4 GPU.
# Run ./setup.sh once first.
#
# Usage:
#   ./deploy.sh                             # build the development branch and deploy
#   SIBERNETIC_REF=my-branch ./deploy.sh    # bake a different Sibernetic ref into the image
#   SKIP_BUILD=1 ./deploy.sh                # redeploy the existing image without rebuilding
set -euo pipefail

PROJECT="${PROJECT:-openworm-agents-prod}"
REGION="${REGION:-us-east4}"
SERVICE="${SERVICE:-sibernetic-runner}"
BUCKET="${BUCKET:-${PROJECT}-sibernetic-results}"
REPOSITORY="${REPOSITORY:-sibernetic-runner}"
RUNTIME_SA="${RUNTIME_SA:-sibernetic-runner@${PROJECT}.iam.gserviceaccount.com}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/sibernetic-runner:latest"
SIBERNETIC_REPO="${SIBERNETIC_REPO:-https://github.com/openworm/sibernetic.git}"
SIBERNETIC_REF="${SIBERNETIC_REF:-development}"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  echo "==> Building image (Sibernetic ref: ${SIBERNETIC_REF})..."
  gcloud builds submit \
    --config=cloudbuild.yaml \
    --project="$PROJECT" \
    --substitutions="_IMAGE=${IMAGE},_SIBERNETIC_REPO=${SIBERNETIC_REPO},_SIBERNETIC_REF=${SIBERNETIC_REF}"
fi

# One instance at a time keeps a stuck or runaway job to the cost of one GPU.
echo "==> Deploying to Cloud Run with GPU..."
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --service-account "$RUNTIME_SA" \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --no-gpu-zonal-redundancy \
  --cpu 8 \
  --memory 16Gi \
  --timeout 3600 \
  --max-instances 1 \
  --min-instances 0 \
  --concurrency 1 \
  --no-cpu-throttling \
  --no-allow-unauthenticated \
  --execution-environment gen2 \
  --set-env-vars "GCS_BUCKET=${BUCKET}"

echo
echo "==> Deployed. To use it from your machine:"
echo "  gcloud run services proxy ${SERVICE} --region=${REGION} --project=${PROJECT} --port=8080"
echo "  open http://localhost:8080/"
