"""Managed Agents API wrapper: environment, session, streaming, wiki init."""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator

from anthropic import Anthropic, APIError, RateLimitError

from src.config import ANTHROPIC_API_KEY, AGENT_ID, AGENT_VERSION

logger = logging.getLogger(__name__)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Local wiki directory to sync to Environment on first session creation
WIKI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wiki")
RAW_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "raw")


class SessionUnavailableError(Exception):
    """Raised when a session is no longer usable (archived, not found, etc.)."""


def create_environment(name: str) -> str:
    """Create a cloud environment. Returns environment_id."""
    env = client.beta.environments.create(
        name=name,
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    logger.info("Created environment %s", env.id)
    return env.id


def create_session(environment_id: str) -> str:
    """Create a session bound to an agent and environment. Returns session_id."""
    session = client.beta.sessions.create(
        agent={"type": "agent", "id": AGENT_ID, "version": AGENT_VERSION},
        environment_id=environment_id,
    )
    logger.info("Created session %s", session.id)
    return session.id


def init_wiki_in_environment(session_id: str) -> None:
    """Upload local wiki and raw files into the Environment via agent commands.

    Sends a message asking the agent to create the wiki structure,
    then feeds it the content of each file.
    """
    files_content = []

    # Collect wiki files
    for root, _, filenames in os.walk(WIKI_DIR):
        for fname in filenames:
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, os.path.dirname(WIKI_DIR))
            with open(fpath, "r") as f:
                content = f.read()
            if content.strip():
                files_content.append((relpath, content))

    # Collect raw files
    for root, _, filenames in os.walk(RAW_DIR):
        for fname in filenames:
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, os.path.dirname(RAW_DIR))
            with open(fpath, "r") as f:
                content = f.read()
            if content.strip():
                files_content.append((relpath, content))

    if not files_content:
        logger.info("No wiki/raw files to upload")
        return

    # Build a single message with all files for the agent to write
    parts = ["请在 Environment 中创建以下文件结构。用工具把每个文件写到对应路径：\n"]
    for relpath, content in files_content:
        parts.append(f"--- FILE: {relpath} ---\n{content}\n--- END FILE ---\n")

    message = "\n".join(parts)

    logger.info("Uploading %d files to Environment via agent", len(files_content))
    with client.beta.sessions.stream(session_id=session_id) as stream:
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": message}],
            }],
        )
        for event in stream:
            if event.type == "session.status_idle":
                break

    logger.info("Wiki files uploaded to Environment")


async def send_and_stream(session_id: str, message: str) -> AsyncGenerator[str, None]:
    """Send a user message and yield text chunks from the agent response.

    Runs the synchronous streaming SDK call in a thread to avoid blocking the event loop.
    Raises SessionUnavailableError if the session is gone.
    """
    def _stream():
        chunks: list[str] = []
        try:
            with client.beta.sessions.stream(session_id=session_id) as stream:
                client.beta.sessions.events.send(
                    session_id=session_id,
                    events=[{
                        "type": "user.message",
                        "content": [{"type": "text", "text": message}],
                    }],
                )
                for event in stream:
                    if event.type == "agent.message":
                        for block in event.content:
                            if block.type == "text":
                                chunks.append(block.text)
                    elif event.type == "session.status_idle":
                        break
        except APIError as e:
            if e.status_code in (404, 409):
                raise SessionUnavailableError(f"Session {session_id} unavailable: {e}")
            raise
        return chunks

    retries = 0
    while True:
        try:
            loop = asyncio.get_event_loop()
            chunks = await loop.run_in_executor(None, _stream)
            for chunk in chunks:
                yield chunk
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
