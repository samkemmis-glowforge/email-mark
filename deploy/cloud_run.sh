#!/usr/bin/env bash
# One-command deploy of the email-mark ("Mark") Slack bot to Cloud Run.
#
# Runs AS the marketing-slack-bot service account, so BigQuery / Drive / Sheets
# authenticate via Application Default Credentials — NO downloaded key needed.
# (GCP_SERVICE_ACCOUNT_JSON from the old Render env is deliberately dropped.)
#
# Config values are read from an env file (ENV_FILE=~/mark-secrets.env) or from
# your shell environment; shell wins when both are set. Secrets are NOT read
# from the file — they come from Secret Manager (see deploy/DEPLOY.md).
#
# Persistent state: Render kept /var/data on a persistent disk (lessons file +
# answer-cards SQLite). Here a GCS bucket is mounted at /var/data instead, so
# the same paths keep working. Light single-writer SQLite over the GCS mount is
# fine at Mark's volume (one instance, handfuls of writes/day).
#
# Why these flags: Socket Mode holds an outbound WebSocket, so the service must
# never scale to zero (--min-instances 1) and must keep CPU allocated
# (--no-cpu-throttling). One instance only (--max-instances 1, --concurrency 1)
# — a second would open a duplicate Slack connection. No public ingress
# (--no-allow-unauthenticated); nothing calls it over HTTP except the probe.
set -euo pipefail

PROJECT="${PROJECT:-glowforge-internal}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-email-mark-agent}"
RUNTIME_SA="${RUNTIME_SA:-marketing-slack-bot@${PROJECT}.iam.gserviceaccount.com}"
DATA_BUCKET="${DATA_BUCKET:-glowforge-email-mark-data}"
ENV_FILE="${ENV_FILE:-}"

# --- Config lookup: shell env first, then the env file ---
_from_file() {
  # Never returns non-zero: a var absent from the file is just "empty",
  # not an error (set -e would otherwise silently kill the script here).
  [ -n "${ENV_FILE}" ] && [ -f "${ENV_FILE}" ] || return 0
  { grep -m1 "^$1=" "${ENV_FILE}" || true; } | cut -d= -f2-
}
cfg() {
  local v="${!1:-}"
  if [ -n "${v}" ]; then printf '%s' "${v}"; else _from_file "$1"; fi
}

# --- Secrets (sensitive) : ENV_VAR=secret-manager-name:version ---
SECRETS="SLACK_BOT_TOKEN=email-mark-slack-bot-token:latest"
SECRETS+=",SLACK_APP_TOKEN=email-mark-slack-app-token:latest"
SECRETS+=",SLACK_SIGNING_SECRET=email-mark-slack-signing-secret:latest"
SECRETS+=",ANTHROPIC_API_KEY=email-mark-anthropic-key:latest"

# Optional secrets — wired only if they exist in Secret Manager.
if gcloud secrets describe email-mark-hubspot-key --project "${PROJECT}" >/dev/null 2>&1; then
  SECRETS+=",HUBSPOT_API_KEY=email-mark-hubspot-key:latest"
  echo "▸ HubSpot key found — wiring HUBSPOT_API_KEY."
else
  echo "▸ No HubSpot key secret — skipping (add email-mark-hubspot-key to enable)."
fi
if gcloud secrets describe email-mark-meta-access-token --project "${PROJECT}" >/dev/null 2>&1; then
  SECRETS+=",META_ACCESS_TOKEN=email-mark-meta-access-token:latest"
  echo "▸ Meta token found — wiring META_ACCESS_TOKEN."
else
  echo "▸ No Meta token secret — skipping."
fi

# --- Non-secret config (IDs, channels, paths, gates) ---
# Everything the Render env carried except secrets and GCP_SERVICE_ACCOUNT_JSON.
CONFIG_VARS=(
  ADS_MARK_ALLOW_WRITE
  ANSWER_CARDS_DB_PATH
  CONTENT_CALENDAR_GID
  CONTENT_CALENDAR_SHEET_ID
  HUBSPOT_BLANK_CANVAS_TEMPLATE_ID
  HUBSPOT_SOCIAL_FB_CHANNEL_GUID
  HUBSPOT_SOCIAL_IG_CHANNEL_GUID
  LESSONS_FILE_PATH
  META_AD_ACCOUNT_ID
  META_GRAPH_VERSION
  META_IG_USER_ID
  META_PAGE_ID
  SLACK_ALLOWED_CHANNELS
  SLACK_ALLOWED_USERS
  SLACK_REVIEW_CHANNEL
  SOCIAL_ASSETS_DRIVE_FOLDER_ID
  SOCIAL_MARK_ALLOW_PUBLISH
)
ENVVARS="GCP_PROJECT_ID=${PROJECT}"
for v in "${CONFIG_VARS[@]}"; do
  val="$(cfg "${v}")"
  [ -n "${val}" ] && ENVVARS+=",${v}=${val}"
done
# Safety defaults if absent from env/file: publishing gate OFF, Graph pinned.
case ",${ENVVARS}," in
  *",SOCIAL_MARK_ALLOW_PUBLISH="*) : ;;
  *) ENVVARS+=",SOCIAL_MARK_ALLOW_PUBLISH=false" ;;
esac
case ",${ENVVARS}," in
  *",META_GRAPH_VERSION="*) : ;;
  *) ENVVARS+=",META_GRAPH_VERSION=v21.0" ;;
esac

# --- Persistent /var/data via GCS volume mount ---
VOLUME_FLAGS=()
if gcloud storage buckets describe "gs://${DATA_BUCKET}" >/dev/null 2>&1; then
  VOLUME_FLAGS=(--add-volume "name=data,type=cloud-storage,bucket=${DATA_BUCKET}"
                --add-volume-mount "volume=data,mount-path=/var/data")
  echo "▸ Data bucket gs://${DATA_BUCKET} found — mounting at /var/data."
else
  echo "!! Bucket gs://${DATA_BUCKET} not found — /var/data will be EPHEMERAL."
  echo "   Create it first (see deploy/DEPLOY.md) so lessons/answer-cards persist."
fi

echo "▸ Building & deploying ${SERVICE} to Cloud Run (${PROJECT}/${REGION}) as ${RUNTIME_SA}…"
gcloud run deploy "${SERVICE}" \
  --source . \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-account "${RUNTIME_SA}" \
  --no-allow-unauthenticated \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --concurrency 1 \
  --memory 1Gi \
  --cpu 1 \
  --port 8080 \
  --set-secrets "${SECRETS}" \
  --set-env-vars "${ENVVARS}" \
  ${VOLUME_FLAGS[@]+"${VOLUME_FLAGS[@]}"}

echo "✓ Deployed. Tail logs:  gcloud run services logs read ${SERVICE} --region ${REGION} --project ${PROJECT} --follow"
