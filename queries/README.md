# Queries

SQL files for warehouse audience definitions. One file per audience.

**Naming:** `<program>__<audience>.sql` — e.g., `activation__first_print_complete.sql`.

These run against BigQuery via the helpers in `src/email_mark/warehouse.py`. The expected output is a table with at least `user_id`, `email`, and any signal columns the prompt template wants to reference (e.g., `last_print_at`, `first_design_name`).
