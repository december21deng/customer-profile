"""Lark/Feishu API wrapper: send and update interactive JSON cards."""

import hashlib
import json
import logging
import secrets
import time

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


# ------------- JSSDK 鉴权（H5 SDK config + selectChatter） -----------
_jsapi_ticket_cache: dict = {"ticket": None, "expire_at": 0.0}


def _get_jsapi_ticket() -> str | None:
    """拿 jsapi_ticket，模块级缓存（2h）。提前 60s 过期。"""
    now = time.time()
    if _jsapi_ticket_cache["ticket"] and _jsapi_ticket_cache["expire_at"] > now + 60:
        return _jsapi_ticket_cache["ticket"]

    token = _get_tenant_token()
    if not token:
        logger.error("Failed to get tenant token for jsapi ticket")
        return None

    resp = http_requests.post(
        "https://open.feishu.cn/open-apis/jssdk/ticket/get",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={},
        timeout=10,
    )
    data = resp.json()
    if data.get("code", -1) != 0:
        logger.error("Failed to get jsapi ticket: code=%s msg=%s", data.get("code"), data.get("msg"))
        return None
    payload = data.get("data") or {}
    ticket = payload.get("ticket")
    expire_in = int(payload.get("expire_in") or 7200)
    if not ticket:
        logger.error("jsapi ticket response missing ticket: %s", data)
        return None
    _jsapi_ticket_cache["ticket"] = ticket
    _jsapi_ticket_cache["expire_at"] = now + expire_in
    return ticket


def sign_jssdk(url: str) -> dict | None:
    """生成 h5sdk.config 需要的 {app_id, timestamp, nonce_str, signature}。

    签名算法：sha1("jsapi_ticket=X&noncestr=Y&timestamp=Z&url=W")
    URL 去 hash，原样传入（不 URL-encode）。
    """
    ticket = _get_jsapi_ticket()
    if not ticket:
        return None
    # 去掉 hash
    url = url.split("#", 1)[0]
    timestamp = str(int(time.time()))
    nonce_str = secrets.token_hex(8)
    string1 = f"jsapi_ticket={ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={url}"
    signature = hashlib.sha1(string1.encode("utf-8")).hexdigest()
    return {
        "app_id": LARK_APP_ID,
        "timestamp": timestamp,
        "nonce_str": nonce_str,
        "signature": signature,
    }


def upload_im_image(file_bytes: bytes, filename: str = "photo.jpg") -> str | None:
    """上传图片到飞书消息图片存储（私有）。返回 image_key。

    需要权限：im:resource 或 im:resource:upload。
    单张 ≤ 10MB，格式 JPEG/PNG/GIF。
    """
    token = _get_tenant_token()
    if not token:
        logger.error("Failed to get tenant token for image upload")
        return None

    resp = http_requests.post(
        "https://open.feishu.cn/open-apis/im/v1/images",
        headers={"Authorization": f"Bearer {token}"},
        data={"image_type": "message"},
        files={"image": (filename, file_bytes)},
        timeout=30,
    )
    data = resp.json()
    if data.get("code", -1) != 0:
        logger.error("Failed to upload im image: code=%s msg=%s", data.get("code"), data.get("msg"))
        return None
    image_key = (data.get("data") or {}).get("image_key")
    if not image_key:
        logger.error("Upload im image: no image_key in %s", data)
        return None
    logger.info("Uploaded im image: %s", image_key)
    return image_key


def stream_im_image(image_key: str):
    """从飞书拉取图片二进制流（供代理路由透传）。

    返回 (iterator, content_type) 或 (None, None)。
    需要权限：im:resource。
    """
    token = _get_tenant_token()
    if not token:
        logger.error("Failed to get tenant token for image download")
        return None, None

    resp = http_requests.get(
        f"https://open.feishu.cn/open-apis/im/v1/images/{image_key}",
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=30,
    )
    if resp.status_code != 200:
        logger.error("Failed to stream im image %s: status=%s", image_key, resp.status_code)
        return None, None
    ctype = resp.headers.get("Content-Type", "application/octet-stream")
    return resp.iter_content(chunk_size=8192), ctype


_docx_raw_cache: dict = {}  # {doc_id: (text, expire_at)}
_DOCX_RAW_TTL = 300  # 5 分钟


def fetch_docx_raw(doc_id: str) -> tuple[str | None, str | None]:
    """拉飞书 docx 原文（纯文字，跳过富内容）。进程内缓存 5 分钟。

    返回 (raw_text, error)。err 非 None 时 raw_text 为 None。
    需要权限：docx:document:readonly。
    """
    now = time.time()
    cached = _docx_raw_cache.get(doc_id)
    if cached and cached[1] > now:
        return cached[0], None

    token = _get_tenant_token()
    if not token:
        return None, "tenant_token_failed"

    resp = http_requests.get(
        f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/raw_content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    try:
        data = resp.json()
    except Exception:
        logger.error("docx raw_content: non-json response, status=%s", resp.status_code)
        return None, f"http_{resp.status_code}"
    if data.get("code", -1) != 0:
        logger.error("docx raw_content failed: code=%s msg=%s", data.get("code"), data.get("msg"))
        return None, data.get("msg") or f"code_{data.get('code')}"
    content = (data.get("data") or {}).get("content") or ""
    _docx_raw_cache[doc_id] = (content, now + _DOCX_RAW_TTL)
    return content, None


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
