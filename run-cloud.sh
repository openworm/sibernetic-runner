#!/bin/bash
set -ex

source ./user_settings.sh

gcloud run services proxy sibernetic-runner --region=${USER_REGION} --project=${USER_PROJECT} --port=8080
