#!/usr/bin/env bash
# Deploy mtg-mcp-server to Cloud Run with Google sign-in.
#
# Run from the repo root in Google Cloud Shell (or any shell where gcloud is
# logged in). Safe to re-run: every step checks before it creates.
#
#   PROJECT_ID=my-project ALLOWED_EMAILS=me@gmail.com,friend@gmail.com \
#   GOOGLE_CLIENT_ID=123.apps.googleusercontent.com \
#   bash scripts/deploy_cloud_run.sh
#
# The OAuth client secret is read from the terminal (hidden) the first time and
# stored in Secret Manager. It is never written to disk or passed on the
# command line.
#
# OAuth state (client registrations, encrypted tokens) lives in a Cloud Storage
# bucket mounted at /data, so sign-ins survive scale-to-zero and redeploys.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${ALLOWED_EMAILS:?set ALLOWED_EMAILS (comma-separated Google emails)}"
: "${GOOGLE_CLIENT_ID:?set GOOGLE_CLIENT_ID (OAuth client ID, type Web)}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-mtg-mcp}"
BUCKET="${BUCKET:-${PROJECT_ID}-mtg-mcp-oauth}"
SA_NAME="${SA_NAME:-mtg-mcp-runner}"
RULES_URL="${RULES_URL:-https://media.wizards.com/2026/downloads/MagicCompRules%2020260925.txt}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SECRET_CLIENT="mtg-mcp-google-client-secret"
SECRET_JWT="mtg-mcp-jwt-signing-key"

say() { printf '\n==> %s\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
# Public URL used for OAuth. Defaults to Cloud Run's deterministic URL
# (https://SERVICE-PROJECT_NUMBER.REGION.run.app). Set BASE_URL for a custom
# domain, e.g. BASE_URL=https://balthazar.io (no trailing slash, no /mcp).
BASE_URL="${BASE_URL:-https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app}"
BASE_URL="${BASE_URL%/}"

say "Enabling APIs (first run takes a minute)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com

say "Service account ${SA_EMAIL}"
if ! gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --display-name="MTG MCP Cloud Run"
fi

say "Secrets"
if ! gcloud secrets describe "$SECRET_CLIENT" >/dev/null 2>&1; then
  read -rsp "Paste the OAuth client secret (input hidden): " CLIENT_SECRET; echo
  [ -n "$CLIENT_SECRET" ] || { echo "empty secret" >&2; exit 1; }
  printf '%s' "$CLIENT_SECRET" | gcloud secrets create "$SECRET_CLIENT" --data-file=- \
    --replication-policy=automatic
  unset CLIENT_SECRET
fi
if ! gcloud secrets describe "$SECRET_JWT" >/dev/null 2>&1; then
  openssl rand -base64 48 | tr -d '\n' | gcloud secrets create "$SECRET_JWT" --data-file=- \
    --replication-policy=automatic
fi
for s in "$SECRET_CLIENT" "$SECRET_JWT"; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:${SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

say "Bucket gs://${BUCKET} (OAuth state)"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role=roles/storage.objectUser >/dev/null

say "Building and deploying ${SERVICE} to ${REGION}"
# ^|^ switches the env-var list separator so the comma-separated email list survives.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --execution-environment gen2 \
  --memory 1Gi --cpu 1 --cpu-boost \
  --min-instances 0 --max-instances 1 \
  --timeout 300 \
  --add-volume "name=oauth,type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount "volume=oauth,mount-path=/data" \
  --set-env-vars "^|^FASTMCP_HOME=/data|MTG_MCP_AUTH_BASE_URL=${BASE_URL}|MTG_MCP_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}|MTG_MCP_AUTH_ALLOWED_EMAILS=${ALLOWED_EMAILS}|MTG_MCP_RULES_URL=${RULES_URL}|MTG_MCP_ENABLE_MOXFIELD=false|MTG_MCP_ENABLE_MTGGOLDFISH=false|MTG_MCP_ENABLE_17LANDS=false|MTG_MCP_ENABLE_SPICERACK=false" \
  --set-secrets "MTG_MCP_GOOGLE_CLIENT_SECRET=${SECRET_CLIENT}:latest,MTG_MCP_AUTH_JWT_SIGNING_KEY=${SECRET_JWT}:latest"

say "Done"
cat <<EOF
Server URL:     ${BASE_URL}
Connector URL:  ${BASE_URL}/mcp

If you have not yet, add this Authorized redirect URI to the Google OAuth client:
  ${BASE_URL}/auth/callback
EOF
