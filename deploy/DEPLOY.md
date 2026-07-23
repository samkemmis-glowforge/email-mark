# Deploying email-mark ("Mark") to Cloud Run

Moves the bot off personal Render onto Cloud Run in `glowforge-internal`, so it's
org-owned and any project admin can manage it. The service runs **as the
`marketing-slack-bot` service account**, so BigQuery / Drive / Sheets use
Application Default Credentials — **no downloaded key is deployed**
(`GCP_SERVICE_ACCOUNT_JSON` from the Render env is deliberately dropped).

It's a Socket Mode worker: outbound WebSocket to Slack, no public URL. Cloud Run
still needs a `$PORT` listener, which `scripts/run_bot.py` now binds.

## Inputs

1. **An env file** (e.g. `~/mark-secrets.env`) containing the full old Render
   environment as `NAME=value` lines — master copy lives in the 1Password
   "Marketing" vault → `email-mark — Render env (all secrets)`.
2. From that file, the six **sensitive** values go into Secret Manager;
   everything else (IDs, channels, paths, gates) is passed through as plain
   env vars automatically by `deploy/cloud_run.sh` via `ENV_FILE=`.

| Secret Manager name | Env var |
|---|---|
| `email-mark-slack-bot-token` | `SLACK_BOT_TOKEN` (`xoxb-…`) |
| `email-mark-slack-app-token` | `SLACK_APP_TOKEN` (`xapp-…`) |
| `email-mark-slack-signing-secret` | `SLACK_SIGNING_SECRET` |
| `email-mark-anthropic-key` | `ANTHROPIC_API_KEY` |
| `email-mark-hubspot-key` (optional) | `HUBSPOT_API_KEY` |
| `email-mark-meta-access-token` (optional) | `META_ACCESS_TOKEN` |

## Persistent state (/var/data)

On Render, `/var/data/` was a persistent disk holding the **lessons file**
(Mark's accumulated memory, `LESSONS_FILE_PATH`) and the **answer-cards SQLite
DB** (`ANSWER_CARDS_DB_PATH`). On Cloud Run, the GCS bucket
`gs://glowforge-email-mark-data` is mounted at `/var/data` so the same paths
keep working. **Copy both files from Render into the bucket before deleting the
Render service** — otherwise Mark loses his memory.

SQLite over the GCS mount is fine at Mark's volume (single instance,
single-writer, handfuls of writes/day). If it ever misbehaves, move cards to
BigQuery and keep only the lessons file on the mount.

## One-time setup

```bash
gcloud config set project glowforge-internal

# 1. Load the six secrets from the env file (auto-skips missing optionals).
get() { grep -m1 "^$1=" ~/mark-secrets.env | cut -d= -f2-; }
for pair in \
  "email-mark-slack-bot-token:SLACK_BOT_TOKEN" \
  "email-mark-slack-app-token:SLACK_APP_TOKEN" \
  "email-mark-slack-signing-secret:SLACK_SIGNING_SECRET" \
  "email-mark-anthropic-key:ANTHROPIC_API_KEY" \
  "email-mark-hubspot-key:HUBSPOT_API_KEY" \
  "email-mark-meta-access-token:META_ACCESS_TOKEN"; do
  secret="${pair%%:*}"; var="${pair##*:}"
  val=$(get "$var"); [ -z "$val" ] && { echo "!! MISSING $var — skipped"; continue; }
  gcloud secrets create "$secret" --replication-policy=automatic 2>/dev/null || true
  printf '%s' "$val" | gcloud secrets versions add "$secret" --data-file=-
  echo "✓ $secret"
done

# 2. Grants + data bucket.
SA=marketing-slack-bot@glowforge-internal.iam.gserviceaccount.com
for s in email-mark-slack-bot-token email-mark-slack-app-token \
         email-mark-slack-signing-secret email-mark-anthropic-key \
         email-mark-hubspot-key email-mark-meta-access-token; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor 2>/dev/null || true
done
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --member="user:$(gcloud config get-value account)" \
  --role=roles/iam.serviceAccountUser
gcloud storage buckets create gs://glowforge-email-mark-data \
  --location=us-central1 --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://glowforge-email-mark-data \
  --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin
```

## Deploy

From the repo root (branch with this `deploy/` directory):

```bash
ENV_FILE=~/mark-secrets.env ./deploy/cloud_run.sh
```

## Verify

```bash
gcloud run services logs read email-mark-agent --region us-central1 \
  --project glowforge-internal --follow
# expect: "health listener on :8080" then "Bot starting..."
```
Then DM the bot or @-mention it in Slack.

## After it's confirmed working

1. **Migrate `/var/data`**: copy the lessons file and answer-cards DB from the
   Render service into `gs://glowforge-email-mark-data` (Render Shell → copy
   contents out), then restart the Cloud Run service.
2. Delete the Render service (nothing should run on personal Render).
3. `rm ~/mark-secrets.env` — the master copy lives in 1Password.
4. The deployed bot no longer uses the downloaded `marketing-slack-bot` key; its
   only remaining consumers are local dev and design_mark's indexer, so it can
   eventually be rotated/deleted. (Tracked in the handoff doc.)

## Notes

- One instance only (`--min/--max-instances 1`, `--concurrency 1`): a second
  would open a duplicate Slack socket.
- In-memory conversation state resets on redeploy; the bot rehydrates thread
  history from Slack on the next message, so this is invisible to users.
