#!/usr/bin/env bash
# Zet "Sirene in de buurt" op Google Cloud Run.
#
# Draai dit in Google Cloud Shell (shell.cloud.google.com): clone de repo en
# start ./deploy.sh. Geen secrets of tokens nodig, de app gebruikt alleen
# gratis openbare databronnen.
set -euo pipefail

REGION="${REGION:-europe-west1}"
SERVICE_NAME="${SERVICE_NAME:-sirene-in-de-buurt}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud niet gevonden. Draai dit script in Google Cloud Shell (shell.cloud.google.com)."
  exit 1
fi

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  read -rp "GCP project-id: " PROJECT_ID
  gcloud config set project "$PROJECT_ID"
fi

echo "Project: $PROJECT_ID | Regio: $REGION | Service: $SERVICE_NAME"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT_ID"

cd "$(dirname "$0")"
gcloud run deploy "$SERVICE_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --source . \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=1

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
echo
echo "== Klaar =="
echo "Open deze URL op je telefoon in Chrome en kies menu > 'Toevoegen aan startscherm':"
echo
echo "  $SERVICE_URL"
