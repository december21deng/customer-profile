"""Claude API wrapper: simple messages API for v1, Managed Agents for later."""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator

from anthropic import Anthropic, APIError, RateLimitError

from src.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Load wiki files as system context
WIKI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wiki")

# In-memory conversation history per thread (simple v1)
_conversations: dict[str, list[dict]] = {}


class SessionUnavailableError(Exception):
    """Raised when a session is no longer usable."""


def _load_wiki_context() -> str:
    """Load all wiki files into a single context string."""
    parts = []
    if os.path.isdir(WIKI_DIR):
        for root, _, filenames in os.walk(WIKI_DIR):
            for fname in sorted(filenames):
                if fname.endswith(".md"):
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r") as f:
                        content = f.read()
                    if content.strip():
                        relpath = os.path.relpath(fpath, WIKI_DIR)
                        parts.append(f"## {relpath}\n{content}")
    return "\n\n---\n\n".join(parts) if parts else ""


_wiki_context = _load_wiki_context()

SYSTEM_PROMPT = f"""You are a helpful assistant. You have access to a knowledge base (wiki) built from meeting transcripts and documents. Use this knowledge to answer questions when relevant.

{f'''Here is the knowledge base:

{_wiki_context}''' if _wiki_context else 'No knowledge base loaded yet.'}

Respond in the same language as the user's message. Be concise and helpful."""


async def send_and_stream(thread_id: str, message: str) -> AsyncGenerator[str, None]:
    """Send a user message and yield the full response text.

    Uses simple Messages API with in-memory conversation history.
    """
    # Get or init conversation history
    if thread_id not in _conversations:
        _conversations[thread_id] = []

    history = _conversations[thread_id]
    history.append({"role": "user", "content": message})

    # Keep last 20 messages to avoid context overflow
    if len(history) > 20:
        history = history[-20:]
        _conversations[thread_id] = history

    def _call_api():
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        return response.content[0].text

    retries = 0
    while True:
        try:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, _call_api)
            # Save assistant response to history
            history.append({"role": "assistant", "content": text})
            yield text
            return
        except RateLimitError:
            retries += 1
            if retries > 3:
                raise
            wait = 2 ** retries
            logger.warning("Rate limited, retrying in %ds (attempt %d/3)", wait, retries)
            await asyncio.sleep(wait)
        except APIError as e:
            if e.status_code and e.status_code >= 500:
                retries += 1
                if retries > 3:
                    raise
                wait = 2 ** retries
                logger.warning("Server error %s, retrying in %ds", e.status_code, wait)
                await asyncio.sleep(wait)
            else:
                raise
