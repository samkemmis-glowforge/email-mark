# Prompts

Claude API prompt templates for content generation. One file per program × output type.

**Naming:** `<program>__<output>.md` — e.g., `activation__subject.md`, `activation__body.md`.

Templates use `{{placeholder}}` syntax for variables that get filled from BigQuery row data before the prompt is sent to Claude. Keep the templates here as Markdown so they're easy to read and review.
