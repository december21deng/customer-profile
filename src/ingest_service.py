"""Ingest service: periodically pull new Feishu minutes and send to agent for wiki compilation."""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone

from src.config import ANTHROPIC_API_KEY, AGENT_ID, AGENT_VERSION
from src import session_store, agent_client

logger = logging.getLogger(__name__)

# Dedicated environment and session for ingest
INGEST_THREAD_ID = "__ingest__"
POLL_INTERVAL_SECONDS = 3600  # Check for new minutes every hour


def _run_lark_cli(args: list[str]) -> dict | None:
    """Run a lark-cli command and return parsed JSON output."""
    cmd = ["lark-cli"] + args
    env_extra = {"LARK_CLI_NO_PROXY": "1"}
    import os
    env = {**os.environ, **env_extra}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        if result.returncode != 0:
            logger.error("lark-cli failed: %s", result.stderr)
            return None
        return json.loads(result.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        logger.error("lark-cli error: %s", e)
        return None


def fetch_recent_minutes(since_date: str = "2026-01-01") -> list[dict]:
    """Fetch list of minutes from Feishu. Returns list of {token, title, date}."""
    data = _run_lark_cli([
        "minutes", "+search",
        "--owner-ids", "me",
        "--start", since_date,
        "--page-size", "30",
        "--format", "json",
    ])
    if not data or not data.get("ok"):
        return []

    items = data.get("data", {}).get("items", [])
    results = []
    for item in items:
        token = item.get("token", "")
        display = item.get("display_info", "")
        title = display.split("\n")[0] if display else token
        results.append({"token": token, "title": title})
    return results


def fetch_transcript(token: str) -> str | None:
    """Fetch transcript text for a specific minute token."""
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, dir=".") as tmp:
        tmp_name = tmp.name

    try:
        result = subprocess.run(
            ["lark-cli", "api", "GET",
             f"/open-apis/minutes/v1/minutes/{token}/transcript",
             "--as", "user", "-o", os.path.basename(tmp_name)],
            capture_output=True, text=True,
            env={**os.environ, "LARK_CLI_NO_PROXY": "1"},
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("Failed to fetch transcript for %s: %s", token, result.stderr)
            return None

        with open(tmp_name, "r") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


async def ingest_new_minutes() -> int:
    """Pull new minutes and send to agent for wiki compilation. Returns count of ingested."""
    minutes = fetch_recent_minutes()
    if not minutes:
        logger.info("No minutes found")
        return 0

    # Get or create ingest session
    session_id = await _ensure_ingest_session()

    ingested = 0
    for minute in minutes:
        token = minute["token"]
        title = minute["title"]

        # Check if already ingested (simple: check if raw file exists in local dir)
        import os
        raw_marker = os.path.join("raw", "meetings", f"{token}.ingested")
        if os.path.exists(raw_marker):
            continue

        logger.info("Ingesting: %s (%s)", title, token)
        transcript = fetch_transcript(token)
        if not transcript:
            continue

        # Send to agent with [INGEST] prefix
        message = f"[INGEST] 新的飞书妙记转写，标题：{title}\n\n{transcript}"

        try:
            full_response = ""
            async for chunk in agent_client.send_and_stream(session_id, message):
                full_response += chunk
            logger.info("Ingested %s, agent response: %d chars", title, len(full_response))

            # Mark as ingested
            os.makedirs(os.path.dirname(raw_marker), exist_ok=True)
            with open(raw_marker, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
            ingested += 1

        except agent_client.SessionUnavailableError:
            logger.warning("Ingest session unavailable, recreating")
            await session_store.delete_session(INGEST_THREAD_ID)
            session_id = await _ensure_ingest_session()

    return ingested


async def _ensure_ingest_session() -> str:
    """Get or create the dedicated ingest session."""
    existing = await session_store.get_session(INGEST_THREAD_ID)
    if existing:
        return existing[0]

    env_id = agent_client.create_environment("wiki-ingest")
    session_id = agent_client.create_session(env_id)

    # Initialize wiki files in the environment
    agent_client.init_wiki_in_environment(session_id)

    await session_store.save_session(INGEST_THREAD_ID, session_id, env_id)
    return session_id


async def run_ingest_loop():
    """Run ingest periodically."""
    await session_store.init_db()
    logger.info("Ingest service started, polling every %ds", POLL_INTERVAL_SECONDS)

    while True:
        try:
            count = await ingest_new_minutes()
            if count > 0:
                logger.info("Ingested %d new minutes", count)
        except Exception:
            logger.exception("Ingest cycle failed")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_ingest_loop())
