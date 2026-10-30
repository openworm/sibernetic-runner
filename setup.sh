#!/bin/bash
# One-time project setup for sibernetic-runner. Safe to re-run.
# Creates the results bucket, the image repository and the runtime service
# account, which can write to that bucket and nothing else.
set -euo pipefail

PROJECT="${PROJECT:-openworm-agents-prod}"
REGION="${REGION:-us-east4}"
BUCKET="${BUCKET:-${PROJECT}-sibernetic-results}"
REPOSITORY="${REPOSITORY:-sibernetic-runner}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-sibernetic-runner}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

if ! gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="$PROJECT" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi

if ! gcloud artifacts repositories describe "$REPOSITORY" \
    --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPOSITORY" \
    --project="$PROJECT" --location="$REGION" --repository-format=docker
fi

if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --project="$PROJECT" --display-name="Sibernetic runner (Cloud Run runtime)"
fi

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --project="$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/storage.objectAdmin >/dev/null

echo "Bucket:     gs://${BUCKET}"
echo "Repository: ${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}"
echo "Runtime SA: ${RUNTIME_SA}"
