# Premium Trial NPS — reporting

Data model (contact properties, sync to warehouse):
- `nps_score`   number 0–10 (captured on email tap via the landing-page script)
- `nps_reason`  open-ended text (optional, from the form)
- `nps_category` enum: promoter / passive / detractor (derived; set by the workflow below)

## Surfaces
1. **Raw feed:** Marketing → Forms → "Premium Trial NPS Survey" → Submissions (real-time).
2. **List:** "Premium Trial NPS — Responses" (dynamic list id 10433) — nps_score >= 0.
3. **Dashboard:** HubSpot custom report grouped by `nps_category`.
4. **Durable:** BigQuery + Metabase (SQL below) once the properties sync to the warehouse.

## Workflow to populate nps_category (standalone, always-on — NOT the drip)
Trigger: `nps_score` is known.
Then, if/then branches on nps_score:
- 9 or 10  → set nps_category = Promoter
- 7 or 8   → set nps_category = Passive
- 0–6      → set nps_category = Detractor
Re-enroll when nps_score changes (so a re-survey updates the category).

## NPS in BigQuery / Metabase
NPS = %promoters − %detractors. Replace <hubspot_contacts> with the synced
contacts table (e.g. glowforge-data-production.hubspot.contact). New custom
properties only appear after the HubSpot→BigQuery sync ingests the new columns
— confirm with whoever owns the Stitch/sync config; there is typically a lag.

    SELECT
      COUNT(*)                                                   AS responses,
      ROUND(100 * COUNTIF(nps_score >= 9) / COUNT(*), 1)         AS pct_promoters,
      ROUND(100 * COUNTIF(nps_score BETWEEN 7 AND 8) / COUNT(*), 1) AS pct_passives,
      ROUND(100 * COUNTIF(nps_score <= 6) / COUNT(*), 1)         AS pct_detractors,
      ROUND(100 * (COUNTIF(nps_score >= 9) - COUNTIF(nps_score <= 6)) / COUNT(*), 1) AS nps
    FROM <hubspot_contacts>
    WHERE nps_score IS NOT NULL;

For a trend line, add: GROUP BY DATE_TRUNC(<response_date>, WEEK).
