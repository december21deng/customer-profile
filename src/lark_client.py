"""Lark/Feishu API wrapper: send and update interactive JSON cards."""

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from src.config import LARK_APP_ID, LARK_APP_SECRET

logger = logging.getLogger(__name__)

_client = lark.Client.builder().app_id(LARK_APP_ID).app_secret(LARK_APP_SECRET).build()


def _build_card(content: str) -> str:
    """Build a JSON interactive card with markdown content."""
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            }
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def send_card(chat_id: str, thread_id: str, content: str = "思考中...") -> str | None:
    """Send a JSON interactive card to a thread. Returns message_id or None on failure."""
    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(_build_card(content)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send card: %s %s", response.code, response.msg)
        return None

    message_id = response.data.message_id
    logger.info("Sent card %s to thread %s", message_id, thread_id)
    return message_id


def update_card(message_id: str, content: str) -> bool:
    """Update (PATCH) an existing card's content. Returns success."""
    body = PatchMessageRequestBody.builder() \
        .content(_build_card(content)) \
        .build()

    request = PatchMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.patch(request)
    if not response.success():
        logger.error("Failed to update card %s: %s %s", message_id, response.code, response.msg)
        return False
    return True
