"""FastAPI entry point: receives Lark webhook events and orchestrates agent responses."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src import config, session_store, agent_client, lark_client, ingest_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Per-thread locks for serial message processing
_thread_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_store.init_db()
    logger.info("Database initialized")
    # Start ingest service in background
    ingest_task = asyncio.create_task(ingest_service.run_ingest_loop())
    logger.info("Ingest service started in background")
    yield
    ingest_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook/event")
async def webhook_event(request: Request):
    body = await request.json()

    # Challenge verification (first-time callback URL setup)
    if "challenge" in body:
        return JSONResponse({"challenge": body["challenge"]})

    # Verify token (skip if not configured yet)
    if config.LARK_VERIFICATION_TOKEN:
        token = body.get("token")
        if token != config.LARK_VERIFICATION_TOKEN:
            logger.warning("Invalid verification token")
            return JSONResponse({"code": 403}, status_code=403)

    # Extract event
    header = body.get("header", {})
    event_type = header.get("event_type", "")
    if event_type != "im.message.receive_v1":
        return JSONResponse({"code": 0})

    event = body.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})

    # Ignore bot's own messages
    sender_type = sender.get("sender_type", "")
    if sender_type != "user":
        return JSONResponse({"code": 0})

    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")
    message_id_lark = message.get("message_id", "")
    thread_id = message.get("root_id") or message.get("parent_id") or ""
    msg_type = message.get("message_type", "")

    # For p2p (private chat), use chat_id as thread_id
    if chat_type == "p2p":
        thread_id = chat_id
    elif chat_type == "group" and not thread_id:
        # Group chat without thread — ignore (only respond in threads)
        return JSONResponse({"code": 0})

    # Extract text content
    if msg_type != "text":
        return JSONResponse({"code": 0})

    try:
        content_json = json.loads(message.get("content", "{}"))
        user_text = content_json.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return JSONResponse({"code": 0})

    if not user_text:
        return JSONResponse({"code": 0})

    # Process asynchronously
    asyncio.create_task(_handle_message(chat_id, thread_id, user_text))
    return JSONResponse({"code": 0})


async def _handle_message(chat_id: str, thread_id: str, user_text: str) -> None:
    """Handle a single message: route to session, stream response, update card."""
    # Per-thread lock for serial processing
    if thread_id not in _thread_locks:
        _thread_locks[thread_id] = asyncio.Lock()

    async with _thread_locks[thread_id]:
        try:
            await _process_message(chat_id, thread_id, user_text)
        except Exception:
            logger.exception("Error processing message in thread %s", thread_id)
            lark_client.send_card(chat_id, thread_id, "抱歉，处理消息时出错了，请稍后重试。")


async def _process_message(chat_id: str, thread_id: str, user_text: str) -> None:
    """Core message processing: session lookup, agent call, card updates."""
    # Get or create session
    session_id = await _ensure_session(thread_id)

    # Send "thinking" card
    message_id = lark_client.send_card(chat_id, thread_id)
    if not message_id:
        return

    # Stream agent response and update card periodically
    full_text = ""
    last_update = time.monotonic()
    update_interval = 1.5  # seconds

    try:
        async for chunk in agent_client.send_and_stream(session_id, user_text):
            full_text += chunk
            now = time.monotonic()
            if now - last_update >= update_interval:
                lark_client.update_card(message_id, full_text + " ▌")
                last_update = now
    except agent_client.SessionUnavailableError:
        logger.warning("Session %s unavailable, recreating for thread %s", session_id, thread_id)
        await session_store.delete_session(thread_id)
        session_id = await _ensure_session(thread_id)
        # Retry once with new session
        full_text = ""
        async for chunk in agent_client.send_and_stream(session_id, user_text):
            full_text += chunk
            now = time.monotonic()
            if now - last_update >= update_interval:
                lark_client.update_card(message_id, full_text + " ▌")
                last_update = now

    # Final card update
    if full_text:
        lark_client.update_card(message_id, full_text)
    else:
        lark_client.update_card(message_id, "（Agent 未返回内容）")


async def _ensure_session(thread_id: str) -> str:
    """Get existing session or create new one for the thread."""
    existing = await session_store.get_session(thread_id)
    if existing:
        return existing[0]  # session_id

    # Create new environment + session, init wiki files
    env_id = agent_client.create_environment(f"thread-{thread_id}")
    session_id = agent_client.create_session(env_id)
    agent_client.init_wiki_in_environment(session_id)
    await session_store.save_session(thread_id, session_id, env_id)
    return session_id
