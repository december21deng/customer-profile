"""Lark/Feishu API wrapper: send and update interactive JSON cards."""

import json
import logging
import requests as http_requests

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


def add_reaction(message_id: str, emoji_type: str = "EYES") -> bool:
    """Add an emoji reaction to a message. Common types: EYES, THUMBSUP, OK."""
    token = _get_tenant_token()
    if not token:
        logger.error("Failed to get tenant token for reaction")
        return False

    resp = http_requests.post(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"reaction_type": {"emoji_type": emoji_type}},
    )
    data = resp.json()
    if data.get("code", -1) != 0:
        logger.error("Failed to add reaction: %s", data.get("msg"))
        return False
    logger.info("Added %s reaction to %s", emoji_type, message_id)
    return True


def _get_tenant_token() -> str | None:
    """Get tenant_access_token for API calls not covered by lark SDK."""
    resp = http_requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET},
    )
    data = resp.json()
    if data.get("code", -1) != 0:
        return None
    return data.get("tenant_access_token")


def send_card_p2p(receive_id: str, content: str = "thinking...", receive_id_type: str = "open_id") -> str | None:
    """Send a card in p2p (private) chat. Returns message_id."""
    body = CreateMessageRequestBody.builder() \
        .receive_id(receive_id) \
        .msg_type("interactive") \
        .content(_build_card(content)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send p2p card: %s %s", response.code, response.msg)
        return None

    message_id = response.data.message_id
    logger.info("Sent p2p card %s", message_id)
    return message_id


def reply_card(reply_to_message_id: str, content: str = "thinking...") -> str | None:
    """Reply to a message with a card (for group chats). Returns message_id."""
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
