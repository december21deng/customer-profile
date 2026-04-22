"""Long-connection (WebSocket) entry point: connects to Lark and handles messages."""

import asyncio
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from src import config, agent_client, lark_client, session_store, docx_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Per-thread locks for serial message processing
_thread_locks: dict[str, asyncio.Lock] = {}

# Dedicated asyncio loop running in a background thread (lark ws client is sync)
_loop: asyncio.AbstractEventLoop | None = None


def _start_background_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return loop


def on_message(data: P2ImMessageReceiveV1) -> None:
    """Callback invoked by lark ws client in its own thread."""
    logger.info("on_message CALLED with data=%s", data)
    event = data.event
    message = event.message
    sender = event.sender

    open_id = sender.sender_id.open_id if sender and sender.sender_id else ""

    logger.info(
        "Event: chat_type=%s chat_id=%s message_id=%s sender_type=%s open_id=%s",
        message.chat_type, message.chat_id, message.message_id,
        sender.sender_type if sender else None, open_id,
    )

    if not sender or sender.sender_type != "user":
        return

    chat_type = message.chat_type or ""
    message_id = message.message_id or ""
    chat_id = message.chat_id or ""

    # v0.1: only handle p2p (private chat)
    if chat_type != "p2p":
        return

    if message.message_type != "text":
        return

    try:
        content_json = json.loads(message.content or "{}")
        user_text = content_json.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return

    if not user_text:
        return

    thread_id = chat_id  # p2p: one conversation per chat
    asyncio.run_coroutine_threadsafe(
        _handle_message(chat_id, message_id, thread_id, user_text),
        _loop,
    )


async def _handle_message(chat_id: str, message_id: str, thread_id: str, user_text: str) -> None:
    if thread_id not in _thread_locks:
        _thread_locks[thread_id] = asyncio.Lock()

    async with _thread_locks[thread_id]:
        try:
            await _process_message(chat_id, message_id, thread_id, user_text)
        except Exception:
            logger.exception("Error processing message in thread %s", thread_id)


async def _process_message(chat_id: str, message_id: str, thread_id: str, user_text: str) -> None:
    # Acknowledge with emoji reaction
    lark_client.add_reaction(message_id, "Typing")

    docx_hit = docx_client.extract_docx_url(user_text)

    if docx_hit:
        doc_id, url = docx_hit

        if await session_store.document_exists(doc_id):
            lark_client.reply_card(message_id, f"这份文档已经整理过了（doc_id={doc_id}）。")
            return

        card_id = lark_client.reply_card(message_id, "正在读取飞书文档...")
        if not card_id:
            logger.warning("Failed to send placeholder card (reply to %s)", message_id)
            return

        # Fetch raw content via Feishu Docx API (blocking HTTP in a thread)
        doc, err = await asyncio.to_thread(docx_client.fetch_raw_content, doc_id, url)
        if not doc:
            lark_client.update_card(card_id, f"读取文档失败：{err}")
            return

        lark_client.update_card(card_id, f"已读取「{doc.title or doc_id}」，正在整理到 wiki...")

        prompt_text = (
            f"下面是一份飞书会议纪要文档的内容，请整理到 wiki。\n\n"
            f"标题：{doc.title or '(无标题)'}\n"
            f"文档 ID：{doc.doc_id}\n"
            f"URL：{doc.url}\n\n"
            f"正文：\n{doc.raw_content}"
        )
        mode = "ingest"
        ingest_doc = doc
    else:
        card_id = lark_client.reply_card(message_id, "思考中...")
        if not card_id:
            logger.warning("Failed to send placeholder card (reply to %s)", message_id)
            return
        prompt_text = user_text
        mode = "query"
        ingest_doc = None

    full_text = ""
    error_msg: str | None = None
    try:
        async for chunk in agent_client.send_and_stream(thread_id, prompt_text, mode=mode):
            full_text += chunk
    except Exception as e:
        logger.exception("Agent stream failed thread=%s", thread_id)
        error_msg = f"处理失败：{type(e).__name__}: {str(e)[:300]}"

    # Only mark a doc as ingested after Claude finishes successfully.
    if ingest_doc and full_text and not error_msg:
        await session_store.save_document(
            ingest_doc.doc_id, ingest_doc.url, ingest_doc.title, thread_id
        )

    if error_msg:
        display = (full_text + "\n\n---\n" + error_msg) if full_text else error_msg
    else:
        display = full_text or "抱歉，没有生成回复。"
    lark_client.update_card(card_id, display)


def main() -> None:
    global _loop
    _loop = _start_background_loop()

    # Initialize DB on the background loop
    future = asyncio.run_coroutine_threadsafe(session_store.init_db(), _loop)
    future.result(timeout=10)
    logger.info("DB ready; starting long-connection client")

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    cli = lark.ws.Client(
        app_id=config.LARK_APP_ID,
        app_secret=config.LARK_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )
    cli.start()


if __name__ == "__main__":
    main()
