# Scripts

Entrypoint scripts that the scheduler runs. One script per lifecycle program.

Each script:

1. Pulls the audience from BigQuery (using a query file from `queries/`).
2. Generates personalized content via Claude (using a prompt template from `prompts/`).
3. Pushes the audience and content to HubSpot for sending.

Run locally with `python scripts/<name>.py`. In production these are invoked by a scheduler (cron, GitHub Actions, or Cloud Scheduler).
