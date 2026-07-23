# Deploying email-mark ("Mark") to Cloud Run

Moves the bot off personal Render onto Cloud Run in `glowforge-internal`, so it's
org-owned and any project admin can manage it. The service runs **as the
`marketing-slack-bot` service account**, so BigQuery / Drive / Sheets use
Application Default Credentials — **no downloaded key is deployed**.

It's a Socket Mode worker: outbound WebSocket to Slack, no public URL. Cloud Run
still needs a `$PORT` listener, which `scripts/run_bot.py` now binds.

## What it needs (secrets)
Values come from the 1Password "Marketing" vault → `email-mark — Render env (all secrets)`.

| Secret Manager name | From Render env var | Required |
|---|---|---|
| `email-mark-slack-bot-token` | `SLACK_BOT_TOKEN` (`xoxb-…`) | yes |
| `email-mark-slack-app-token` | `SLACK_APP_TOKEN` (`xapp-…`) | yes |
| `email-mark-slack-signing-secret` | `SLACK_SIGNING_SECRET` | yes |
| `email-mark-anthropic-key` | `ANTHROPIC_API_KEY` | yes |
| `email-mark-hubspot-key` | `HUBSPOT_API_KEY` | optional |
| `email-mark-meta-access-token` | `META_ACCESS_TOKEN` | optional |

No `GCP_SERVICE_ACCOUNT_JSON` / `GOOGLE_APPLICATION_CREDENTIALS` — that's the point.

## One-time setup
```bash
gcloud config set project glowforge-internal

# 1. Create the required secrets (paste each value from 1Password, then Ctrl-D).
for s in email-mark-slack-bot-token email-mark-slack-app-token \
         email-mark-slack-signing-secret email-mark-anthropic-key; do
  gcloud secrets create "$s" --replication-policy=automatic 2>/dev/null || true
  gcloud secrets versions add "$s" --data-file=-
done
# Optional extras — only if you want HubSpot / Meta wired now:
#   gcloud secrets create email-mark-hubspot-key --replication-policy=automatic && \
#     gcloud secrets versions add email-mark-hubspot-key --data-file=-
#   gcloud secrets create email-mark-meta-access-token --replication-policy=automatic && \
#     gcloud secrets versions add email-mark-meta-access-token --data-file=-

# 2. Let the runtime SA read the secrets.
SA=marketing-slack-bot@glowforge-internal.iam.gserviceaccount.com
for s in email-mark-slack-bot-token email-mark-slack-app-token \
         email-mark-slack-signing-secret email-mark-anthropic-key \
         email-mark-hubspot-key email-mark-meta-access-token; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor 2>/dev/null || true
done

# 3. Let yourself deploy a service that RUNS AS that SA.
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --member="user:$(gcloud config get-value account)" \
  --role=roles/iam.serviceAccountUser
```

## Deploy
```bash
./deploy/cloud_run.sh
```
Account-specific IDs (review channel, Meta page/IG IDs, allowlists) are read from
your shell env if set — e.g. `SLACK_REVIEW_CHANNEL=C0123ABCD ./deploy/cloud_run.sh`.

## Verify
```bash
gcloud run services logs read email-mark-agent --region us-central1 \
  --project glowforge-internal --follow
# expect: "health listener on :8080" then "Bot starting..."
```
Then DM the bot or @-mention it in Slack.

## After it's confirmed working
1. Delete the Render service (nothing should run on personal Render).
2. Since the deployed bot no longer uses a downloaded key, the
   `marketing-slack-bot` key can later be rotated/deleted — its only remaining
   consumers are local dev and design_mark's indexer. (Track in the handoff doc.)

## Notes
- One instance only (`--min/--max-instances 1`, `--concurrency 1`): a second
  would open a duplicate Slack socket.
- In-memory conversation state resets on redeploy; the bot rehydrates thread
  history from Slack on the next message, so this is invisible to users.
