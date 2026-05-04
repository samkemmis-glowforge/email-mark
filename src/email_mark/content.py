"""Claude API client for content generation."""

from __future__ import annotations

import os
from typing import Optional

from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to the .env file in the project root."
            )
        _client = Anthropic(api_key=api_key)
    return _client


def generate(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 1024,
) -> str:
    """Send a prompt to Claude and return the text response."""
    client = _get_client()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
