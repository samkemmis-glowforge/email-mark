# Scripts

Entrypoint scripts that the scheduler runs. One script per lifecycle program.

Each script:

1. Pulls the audience from BigQuery (using a query file from `queries/`).
2. Generates personalized content via Claude (using a prompt template from `prompts/`).
3. Pushes the audience and content to HubSpot for sending.

Run locally with `python scripts/<name>.py`. In production these are invoked by a scheduler (cron, GitHub Actions, or Cloud Scheduler).

## Scheduled jobs

### `sync_materials_to_proofgrade.py`

Daily sync that sets `proofgrade_marketing_opt_in=true` on HubSpot contacts who recently bought Shopify materials and opted in to marketing. Picks up enrollment into the Proofgrade dynamic list (10273) via that property.

- **Schedule:** daily, `0 9 * * *` (9 AM UTC).
- **Run command:** `python scripts/sync_materials_to_proofgrade.py`
- **Env vars required:** `HUBSPOT_API_KEY`, `GCP_SERVICE_ACCOUNT_JSON` (same as the Mark worker).
- **HubSpot scopes required:** `crm.objects.contacts.read` (already granted) and `crm.objects.contacts.write` (must be added to the Service Key).
- **HubSpot list config:** list 10273's filter must include `proofgrade_marketing_opt_in = true`. Without that, the script sets the property but the list doesn't pick up the contacts.
- **Idempotent:** safe to run any number of times — setting an already-true property is a no-op on HubSpot's side.
- **Flags:** `--dry-run` queries without writing; `--lookback-days N` overrides the default 30-day window.
