"""Lark/Feishu API wrapper: send and update interactive JSON cards."""

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
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


def reply_card(reply_to_message_id: str, content: str = "thinking...") -> str | None:
    """Reply to a message with a JSON interactive card. Returns message_id or None."""
    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(_build_card(content)) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(reply_to_message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.reply(request)
    if not response.success():
        logger.error("Failed to reply card: %s %s", response.code, response.msg)
        return None

    message_id = response.data.message_id
    logger.info("Replied card %s to message %s", message_id, reply_to_message_id)
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
