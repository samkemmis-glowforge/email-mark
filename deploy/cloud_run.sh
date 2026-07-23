#!/usr/bin/env bash
# One-command deploy of the email-mark ("Mark") Slack bot to Cloud Run.
#
# Runs AS the marketing-slack-bot service account, so BigQuery / Drive / Sheets
# authenticate via Application Default Credentials — NO downloaded key needed.
#
# Why these flags: Socket Mode holds an outbound WebSocket, so the service must
# never scale to zero (--min-instances 1) and must keep CPU allocated
# (--no-cpu-throttling). One instance only (--max-instances 1, --concurrency 1)
# — a second would open a duplicate Slack connection. No public ingress
# (--no-allow-unauthenticated); nothing calls it over HTTP except the probe.
#
# One-time prereqs: see deploy/DEPLOY.md (create secrets, grant the SA).
set -euo pipefail

PROJECT="${PROJECT:-glowforge-internal}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-email-mark-agent}"
RUNTIME_SA="${RUNTIME_SA:-marketing-slack-bot@${PROJECT}.iam.gserviceaccount.com}"

# --- Secrets (sensitive) : ENV_VAR=secret-manager-name:version ---
SECRETS="SLACK_BOT_TOKEN=email-mark-slack-bot-token:latest"
SECRETS+=",SLACK_APP_TOKEN=email-mark-slack-app-token:latest"
SECRETS+=",SLACK_SIGNING_SECRET=email-mark-slack-signing-secret:latest"
SECRETS+=",ANTHROPIC_API_KEY=email-mark-anthropic-key:latest"

# Optional: HubSpot. Enabled only if the secret exists.
if gcloud secrets describe email-mark-hubspot-key --project "${PROJECT}" >/dev/null 2>&1; then
  SECRETS+=",HUBSPOT_API_KEY=email-mark-hubspot-key:latest"
  echo "▸ HubSpot key found — wiring HUBSPOT_API_KEY."
else
  echo "▸ No HubSpot key secret — skipping (add email-mark-hubspot-key to enable)."
fi

# Optional: Meta / Graph. Publishing stays OFF (SOCIAL_MARK_ALLOW_PUBLISH=false).
if gcloud secrets describe email-mark-meta-access-token --project "${PROJECT}" >/dev/null 2>&1; then
  SECRETS+=",META_ACCESS_TOKEN=email-mark-meta-access-token:latest"
  echo "▸ Meta token found — wiring META_ACCESS_TOKEN (publish still gated off)."
else
  echo "▸ No Meta token secret — skipping."
fi

# --- Non-secret config (safe to keep in the clear) ---
ENVVARS="GCP_PROJECT_ID=${PROJECT}"
ENVVARS+=",SOCIAL_MARK_ALLOW_PUBLISH=false"
ENVVARS+=",META_GRAPH_VERSION=v21.0"
# Pass through any of these from your shell if set (account-specific IDs/channels).
for v in SLACK_REVIEW_CHANNEL SLACK_ALLOWED_USERS SLACK_ALLOWED_CHANNELS \
         META_PAGE_ID META_IG_USER_ID META_AD_ACCOUNT_ID \
         CONTENT_CALENDAR_SHEET_ID CONTENT_CALENDAR_GID; do
  val="${!v:-}"
  [ -n "${val}" ] && ENVVARS+=",${v}=${val}"
done

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
  --set-env-vars "${ENVVARS}"

echo "✓ Deployed. Tail logs:  gcloud run services logs read ${SERVICE} --region ${REGION} --project ${PROJECT} --follow"
