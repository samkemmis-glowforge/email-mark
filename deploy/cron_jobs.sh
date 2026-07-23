#!/usr/bin/env bash
# Port of the Render cron job: daily Shopify-materials → HubSpot Proofgrade sync.
#
# Creates a Cloud Run JOB (same container as the bot, different entrypoint) and
# a Cloud Scheduler trigger at 09:00 UTC daily — identical to the Render cron
# ("0 9 * * *"; Render crons run in UTC).
#
# Runs AS marketing-slack-bot, so BigQuery is keyless (ADC); HubSpot key comes
# from Secret Manager. Stateless & idempotent per the script's own docstring.
set -euo pipefail

PROJECT="${PROJECT:-glowforge-internal}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-email-mark-materials-sync}"
RUNTIME_SA="${RUNTIME_SA:-marketing-slack-bot@${PROJECT}.iam.gserviceaccount.com}"
SCHEDULE="${SCHEDULE:-0 9 * * *}"   # UTC, matches the old Render cron

gcloud services enable cloudscheduler.googleapis.com --project "${PROJECT}"

echo "▸ Deploying Cloud Run job ${JOB}…"
gcloud run jobs deploy "${JOB}" \
  --source . \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-account "${RUNTIME_SA}" \
  --set-secrets "HUBSPOT_API_KEY=email-mark-hubspot-key:latest" \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT}" \
  --command "python" \
  --args "scripts/sync_materials_to_proofgrade.py" \
  --memory 512Mi \
  --task-timeout 15m \
  --max-retries 1

echo "▸ Allowing ${RUNTIME_SA} to trigger the job…"
gcloud run jobs add-iam-policy-binding "${JOB}" \
  --project "${PROJECT}" --region "${REGION}" \
  --member "serviceAccount:${RUNTIME_SA}" \
  --role roles/run.invoker

echo "▸ Creating daily schedule (${SCHEDULE} UTC)…"
gcloud scheduler jobs create http "${JOB}-daily" \
  --project "${PROJECT}" \
  --location "${REGION}" \
  --schedule "${SCHEDULE}" \
  --time-zone "Etc/UTC" \
  --uri "https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method POST \
  --oauth-service-account-email "${RUNTIME_SA}" \
  || echo "  (schedule may already exist — update with: gcloud scheduler jobs update http ${JOB}-daily …)"

echo "✓ Done. Test now with:"
echo "  gcloud run jobs execute ${JOB} --region ${REGION} --project ${PROJECT} --wait"
