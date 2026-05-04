"""Test the tool-using agent from a CLI, no Slack required.

Run from the project root:
    .venv/bin/python scripts/test_agent.py "how did our universal premium A/B test do?"

Or interactively (no argument): each line you type is sent to the agent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.agent import chat  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(f"You: {question}\n")
        print(f"Agent: {chat(question)}")
        return

    print("Interactive agent. Type a question, hit return. Ctrl+C to exit.\n")
    while True:
        try:
            line = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        print(f"\nAgent: {chat(line)}\n")


if __name__ == "__main__":
    main()
