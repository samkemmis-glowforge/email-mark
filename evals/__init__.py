"""Mark eval harness — Phase 2 of the instrumentation work.

A set of known-good question/answer pairs that get re-run against Mark
on every change. Catches regressions BEFORE they reach Slack.

Cases live in `cases.py`. The runner in `runner.py` patches TOOL_HANDLERS
to return canned outputs, calls Mark's `chat()` with each test question,
captures the tool call sequence, and asserts on tool usage + response
patterns.

Run: `python -m evals.runner` (from the repo root). Set `ANTHROPIC_API_KEY`.
Non-zero exit on any failure — wire into CI to block deploys on regression.
"""
