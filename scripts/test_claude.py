"""Sanity-check the Claude connection.

Run from the project root:
    .venv/bin/python scripts/test_claude.py
"""

import sys
from pathlib import Path

# Make `src/` importable without needing to install the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.content import generate  # noqa: E402

PROMPT = (
    "Generate three friendly subject-line options for an email celebrating "
    "someone's first 3D print on a Glowforge laser cutter. "
    "Keep each under 50 characters. "
    "Return them as a numbered list, no preamble."
)


def main() -> None:
    print("Asking Claude for subject-line ideas...\n")
    print(generate(PROMPT))


if __name__ == "__main__":
    main()
