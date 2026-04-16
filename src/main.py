"""FastAPI entry point: receives Lark webhook events and orchestrates agent responses."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src import config, agent_client, lark_client, ingest_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Per-thread locks for serial message processing
_thread_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting")
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
    asyncio.create_task(_handle_message(chat_id, chat_type, thread_id, message_id_lark, user_text))
    return JSONResponse({"code": 0})


async def _handle_message(chat_id: str, chat_type: str, thread_id: str, user_message_id: str, user_text: str) -> None:
    """Handle a single message: call Claude, reply with card."""
    # Per-thread lock for serial processing
    if thread_id not in _thread_locks:
        _thread_locks[thread_id] = asyncio.Lock()

    async with _thread_locks[thread_id]:
        try:
            await _process_message(chat_id, chat_type, thread_id, user_message_id, user_text)
        except Exception:
            logger.exception("Error processing message in thread %s", thread_id)


async def _process_message(chat_id: str, chat_type: str, thread_id: str, user_message_id: str, user_text: str) -> None:
    """Core message processing: call Claude API, send/reply with card."""
    # p2p: use create message with chat_id
    # group: use reply to message_id
    if chat_type == "p2p":
        card_id = lark_client.send_card_p2p(chat_id)
    else:
        card_id = lark_client.reply_card(user_message_id)

    if not card_id:
        logger.warning("Card send/reply failed for thread %s", thread_id)
        return

    # Get agent response
    full_text = ""
    async for chunk in agent_client.send_and_stream(thread_id, user_text):
        full_text += chunk

    # Update card with final response
    if full_text:
        lark_client.update_card(card_id, full_text)
    else:
        lark_client.update_card(card_id, "Sorry, no response generated.")
