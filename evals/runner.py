"""Eval runner — calls Mark with each test case, asserts on tool usage
and response patterns, reports pass/fail.

Run from the repo root:
    python -m evals.runner

Exit code 0 if all cases pass; 1 if any fail. Wire into CI to block
deploys on regressions.

The runner patches TOOL_HANDLERS to return canned responses (no real
HubSpot/BQ calls) but still hits the real Anthropic API — that's the
whole point: we want to know whether Claude under our prompt + tool
schemas actually picks the right tools. Cost ~$0.50/run at current
case count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

# Make `evals.cases` importable when running as a script from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

# Suppress card writes during evals — use a temp DB.
import tempfile
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("ANSWER_CARDS_DB_PATH", _tmp_db.name)

from evals.cases import CASES  # noqa: E402

# email_mark.agent is imported lazily inside run_case() — it pulls in
# anthropic + slack_bolt which aren't needed for inspecting the runner
# or running unit tests on the assertion engine.


# --- Tool call interception -------------------------------------------

@dataclass
class CallRecord:
    name: str
    args: Dict[str, Any]
    output: Any


@contextmanager
def patched_tool_handlers(
    mocked_tools: Dict[str, Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]],
    call_log: List[CallRecord],
) -> Iterator[None]:
    """Replace TOOL_HANDLERS with mocks that record what was called.

    Any tool not present in `mocked_tools` returns a generic error stub
    so Mark can see "tool unavailable" rather than crashing — and the
    runner can assert on whether forbidden tools were attempted.
    """
    from email_mark import agent  # lazy: needs anthropic at import time
    original = dict(agent.TOOL_HANDLERS)
    try:
        patched: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        # First, for every real tool, install a recording wrapper that
        # returns either the mock value or a synthetic "unavailable"
        # response. We do this for ALL keys, not just mocked ones, so we
        # capture forbidden-tool attempts.
        for tool_name in original.keys():
            patched[tool_name] = _build_recording_handler(
                tool_name, mocked_tools, call_log
            )
        agent.TOOL_HANDLERS.clear()
        agent.TOOL_HANDLERS.update(patched)
        yield
    finally:
        agent.TOOL_HANDLERS.clear()
        agent.TOOL_HANDLERS.update(original)


def _build_recording_handler(
    tool_name: str,
    mocked_tools: Dict[str, Any],
    call_log: List[CallRecord],
) -> Callable[[Dict[str, Any]], Any]:
    def handler(args: Dict[str, Any]) -> Any:
        if tool_name in mocked_tools:
            mock_value = mocked_tools[tool_name]
            output = mock_value(args) if callable(mock_value) else mock_value
        else:
            output = {
                "error": (
                    f"Tool {tool_name!r} is not mocked in this eval case. "
                    "Calling it counts as a forbidden bypass."
                ),
                "_eval_unavailable": True,
            }
        call_log.append(CallRecord(name=tool_name, args=dict(args), output=output))
        return output
    return handler


# --- Assertion engine -------------------------------------------------

@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: List[str] = field(default_factory=list)
    tool_calls: List[CallRecord] = field(default_factory=list)
    final_response: str = ""
    elapsed_s: float = 0.0


def run_case(case: Dict[str, Any]) -> CaseResult:
    from email_mark import agent  # lazy: needs anthropic + slack_bolt
    case_id = case["id"]
    turns = case["turns"]
    mocked_tools = case.get("mocked_tools", {})
    call_log: List[CallRecord] = []

    result = CaseResult(case_id=case_id, passed=True)
    start = time.perf_counter()

    final_response = ""
    conversation_id = f"eval:{case_id}:{int(time.time())}"
    try:
        with patched_tool_handlers(mocked_tools, call_log):
            for turn_idx, user_message in enumerate(turns):
                final_response = agent.chat(
                    user_message,
                    conversation_id=conversation_id,
                )
    except Exception as exc:
        result.passed = False
        result.failures.append(f"chat() raised: {type(exc).__name__}: {exc}")

    result.tool_calls = call_log
    result.final_response = final_response
    result.elapsed_s = time.perf_counter() - start

    if result.passed:
        _run_assertions(case, result)

    return result


def _run_assertions(case: Dict[str, Any], result: CaseResult) -> None:
    called_names = [c.name for c in result.tool_calls]

    # expected_tool_calls — every entry must appear
    for expected in case.get("expected_tool_calls", []):
        if expected not in called_names:
            result.failures.append(
                f"expected tool call missing: {expected!r} "
                f"(called: {called_names})"
            )

    # forbidden_tool_calls — none may appear
    for forbidden in case.get("forbidden_tool_calls", []):
        if forbidden in called_names:
            result.failures.append(
                f"forbidden tool call attempted: {forbidden!r}"
            )

    # tool_call_args — for the FIRST call of the named tool, args must
    # contain the expected key/value pairs (other args are fine).
    for tool_name, expected_args in case.get("tool_call_args", {}).items():
        match = next(
            (c for c in result.tool_calls if c.name == tool_name), None
        )
        if match is None:
            # Already caught by expected_tool_calls if it was expected;
            # skip a duplicate failure here.
            continue
        for arg_name, expected_value in expected_args.items():
            actual = match.args.get(arg_name)
            if actual != expected_value:
                result.failures.append(
                    f"tool_call_args mismatch on {tool_name!r}: "
                    f"arg {arg_name!r} expected {expected_value!r}, "
                    f"got {actual!r}"
                )

    # response_must_contain — substring check, case-insensitive
    response_lc = result.final_response.lower()
    for substr in case.get("response_must_contain", []):
        if substr.lower() not in response_lc:
            result.failures.append(
                f"response missing required substring: {substr!r}"
            )

    # response_must_contain_one_of — at least one must match
    one_of = case.get("response_must_contain_one_of")
    if one_of:
        if not any(s.lower() in response_lc for s in one_of):
            result.failures.append(
                f"response missing any of {one_of!r}"
            )

    # response_must_not_contain — must not appear
    for substr in case.get("response_must_not_contain", []):
        if substr.lower() in response_lc:
            result.failures.append(
                f"response contains forbidden substring: {substr!r}"
            )

    if result.failures:
        result.passed = False


# --- Reporting --------------------------------------------------------

def format_result(result: CaseResult, verbose: bool = False) -> str:
    icon = "✅" if result.passed else "❌"
    parts = [
        f"{icon} {result.case_id} ({result.elapsed_s:.1f}s, "
        f"{len(result.tool_calls)} tool calls)"
    ]
    if not result.passed:
        for f in result.failures:
            parts.append(f"   ↳ {f}")
        if verbose:
            parts.append(
                f"   tool sequence: {[c.name for c in result.tool_calls]}"
            )
            preview = result.final_response[:300].replace("\n", " ")
            parts.append(f"   response: {preview}…")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", default=None,
        help="Run only specific case id(s). Repeatable. Omit to run all.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="On failure, also print the tool sequence and response preview.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List case ids without running.",
    )
    args = parser.parse_args()

    if args.list:
        for c in CASES:
            print(f"{c['id']}: {c.get('description', '')[:80]}")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set. The eval runner calls "
            "the real Anthropic API.",
            file=sys.stderr,
        )
        return 2

    selected = CASES
    if args.case:
        selected = [c for c in CASES if c["id"] in set(args.case)]
        if not selected:
            print(f"ERROR: no cases matched {args.case}", file=sys.stderr)
            return 2

    print(f"Running {len(selected)} case(s)...\n")
    results = []
    for case in selected:
        r = run_case(case)
        results.append(r)
        print(format_result(r, verbose=args.verbose))

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_s = sum(r.elapsed_s for r in results)

    print()
    print(f"--- {passed}/{len(results)} passed ({failed} failed) in {total_s:.1f}s ---")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
