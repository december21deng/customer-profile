"""Lark/Feishu API wrapper: send and update interactive JSON cards."""

import hashlib
import json
import logging
import re
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
    # 去掉 hash；query string 保留（必须和 location.href.split('#')[0] 对齐）
    url = url.split("#", 1)[0]
    # ⚠️ 飞书 h5sdk.config 的 timestamp **必须是毫秒（13 位）**。秒会被服务端
    # 按 ms 解读成 1970 年 → 报 333444 signature expired。
    # 以前改毫秒时报 104 是其他参数问题，不是 timestamp 本身。
    timestamp = int(time.time() * 1000)
    nonce_str = secrets.token_hex(8)
    string1 = (
        f"jsapi_ticket={ticket}"
        f"&noncestr={nonce_str}"
        f"&timestamp={timestamp}"
        f"&url={url}"
    )
    signature = hashlib.sha1(string1.encode("utf-8")).hexdigest()
    return {
        "appId": LARK_APP_ID,
        "timestamp": timestamp,   # 13 位 int，签名串里用同一值
        "nonceStr": nonce_str,
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


def resolve_wiki_node(wiki_token: str, access_token: str | None = None) -> str | None:
    """把飞书 `/wiki/<token>` URL 里的 token 解析成真实的 docx document_id。

    调的是 wiki/v2/spaces/get_node。obj_type=docx 时返回 obj_token；否则 None。
    需要权限：wiki:node:read（或 wiki:wiki / wiki:wiki:readonly）+ 调用者对该 wiki 节点有读权限。
    """
    token = access_token or _get_tenant_token()
    if not token:
        return None
    try:
        resp = http_requests.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            params={"token": wiki_token},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
    except Exception:
        logger.exception("resolve_wiki_node: request failed token=%s", wiki_token[:12])
        return None
    if data.get("code", -1) != 0:
        logger.warning(
            "resolve_wiki_node failed: code=%s msg=%s token=%s",
            data.get("code"), data.get("msg"), wiki_token[:12],
        )
        return None
    node = (data.get("data") or {}).get("node") or {}
    obj_type = node.get("obj_type")
    obj_token = node.get("obj_token")
    # 支持 docx / doc（老版）两种底层类型；其他类型（sheet / bitable）我们处理不了
    if obj_type not in ("docx", "doc"):
        logger.warning(
            "wiki node obj_type=%s not supported (token=%s)",
            obj_type, wiki_token[:12],
        )
        return None
    return obj_token or None


def fetch_docx_raw(doc_id: str, access_token: str | None = None) -> tuple[str | None, str | None]:
    """拉飞书 docx 原文（纯文字，跳过富内容）。进程内缓存 5 分钟。

    返回 (raw_text, error)。err 非 None 时 raw_text 为 None。
    需要权限：docx:document:readonly + 调用者对该文档有读权限。

    access_token 若传入则用（推荐 user_access_token：调用者身份必有文档权限）；
    None 时 fallback 到 tenant_access_token（app 身份，多数文档会 forBidden）。
    """
    now = time.time()
    cached = _docx_raw_cache.get(doc_id)
    if cached and cached[1] > now:
        return cached[0], None

    token = access_token or _get_tenant_token()
    if not token:
        return None, "no_token"

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
    content = _clean_minutes_text(content)
    _docx_raw_cache[doc_id] = (content, now + _DOCX_RAW_TTL)
    return content, None


# 孤立一行的图片 hash：32+ 位 hex + 常见图片扩展名
_NOISE_IMAGE_LINE = re.compile(
    r"^\s*[0-9a-fA-F]{24,}\.(jpg|jpeg|png|gif|webp|bmp|svg)\s*$",
    re.IGNORECASE,
)
# 从这些标签开始到文末全部砍掉（都是妙记的 meta 尾巴）
_TAIL_CUT_MARKERS = ("客户合影", "客户合照", "会议合影", "会议合照", "相关链接")

# 头部元信息行 —— 妙记自动或用户按老规定手动加的 "标签：值" 行，
# 详情页顶部 KV 表已经覆盖，body 里重复展示是噪音。只去这些明确的 label，
# 不碰正文里可能出现的 "XXX：" 句式。
_HEADER_META_PATTERNS = [
    re.compile(r"^\s*智能纪要\s*[:：]"),
    re.compile(r"^\s*录音主题\s*[:：]"),
    re.compile(r"^\s*录音时间\s*[:：]"),
    re.compile(r"^\s*CRM\s*客户[^\n:：]*[:：]"),
    re.compile(r"^\s*客户简称\s*[:：]"),
    re.compile(r"^\s*会议主题\s*[:：]"),
    re.compile(r"^\s*会议时间\s*[:：]"),
    re.compile(r"^\s*(参会人员|客方参会人员|我方参会人员)\s*[:：]"),
    re.compile(r"^\s*时间\s*[:：]\s*20\d{2}"),    # "时间：2026..." 只匹配开头带年份的，避免误伤
    re.compile(r"^\s*地点\s*[:：]"),
]


def _clean_minutes_text(raw: str) -> str:
    """清理飞书智能纪要 raw_content 里的尾部噪音：
    - "客户合影" / "相关链接" 这类元信息 section 全砍
    - 孤立的图片 hash 行去掉
    - AI 免责声明行去掉
    - 连续空行压缩为 1 行
    """
    if not raw:
        return raw or ""

    # 1. 尾部元信息 section 整砍
    cut = len(raw)
    for marker in _TAIL_CUT_MARKERS:
        idx = raw.find(marker)
        if idx >= 0 and idx < cut:
            cut = idx
    raw = raw[:cut].rstrip()

    # 2. 按行过滤
    lines = raw.splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if _NOISE_IMAGE_LINE.match(s):
            continue
        if "智能纪要由 AI 生成" in s:
            continue
        # 标签类元信息行（老规定的手动 header）
        if any(p.match(s) for p in _HEADER_META_PATTERNS):
            continue
        out.append(line)

    # 3. 连续空行合一
    result: list[str] = []
    blank = 0
    for line in out:
        if not line.strip():
            blank += 1
            if blank <= 1:
                result.append("")
        else:
            blank = 0
            result.append(line)

    return "\n".join(result).rstrip()


# ------------- docx 块列表（图片 / 画板）-----------------------------
#
# 为什么需要：raw_content 只给纯文字，图片/画板会丢。
# 业务需求：详情页"会议总结"区域要显示妙记的画板/概览图 + 销售内嵌的图片。
#
# 方案：GET /open-apis/docx/v1/documents/{doc_id}/blocks 遍历所有 block，
#       挑出 type=27（image）和 type=43（board/画板）。
# 需要权限：docx:document:readonly （"文档查看权限"）—— 和 raw_content 同一套。
# 画板单独导出为图需要：board:whiteboard:node:read（"画板导出"权限，审批中）。

_DOCX_BLOCKS_CACHE: dict = {}  # {doc_id: (media_list, expire_at)}
_DOCX_BLOCKS_TTL = 300  # 5 分钟

# 飞书 block type 枚举（只列我们关心的）
BLOCK_TYPE_IMAGE = 27
BLOCK_TYPE_BOARD = 43


def fetch_docx_media(doc_id: str, access_token: str | None = None) -> tuple[list[dict], str | None]:
    """遍历 docx 所有 block，按文档顺序返回图片 + 画板的 token 列表。

    每个 item: {"kind": "image"|"board", "token": "<file_or_whiteboard_token>"}
    返回 (items, error)。permission 不够时 error 非 None，items=[]。

    access_token：推荐传上传者的 user_access_token（他对文档必有权限）。
    缺省回到 tenant token（app 身份，多数文档会 forBidden）。
    进程内缓存 5 分钟。
    """
    now = time.time()
    cached = _DOCX_BLOCKS_CACHE.get(doc_id)
    if cached and cached[1] > now:
        return cached[0], None

    token = access_token or _get_tenant_token()
    if not token:
        return [], "no_token"

    items: list[dict] = []
    page_token = ""
    # 飞书 block 接口 page_size 上限 500；绝大多数会议纪要一页够用
    for _ in range(10):  # 防死循环，最多 10 页（5000 blocks）
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = http_requests.get(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=15,
            )
            data = resp.json()
        except Exception:
            logger.exception("docx blocks: request failed doc_id=%s", doc_id)
            return [], "network_error"

        if data.get("code", -1) != 0:
            logger.error(
                "docx blocks failed: code=%s msg=%s doc_id=%s",
                data.get("code"), data.get("msg"), doc_id,
            )
            return [], data.get("msg") or f"code_{data.get('code')}"

        payload = data.get("data") or {}
        raw_blocks = payload.get("items") or []
        # 诊断：统计每种 block_type 出现多少次
        from collections import Counter
        type_counts = Counter((b.get("block_type") for b in raw_blocks))
        logger.info(
            "docx blocks doc_id=%s total=%d types=%s",
            doc_id[:12], len(raw_blocks), dict(type_counts),
        )
        for block in raw_blocks:
            btype = block.get("block_type")
            if btype == BLOCK_TYPE_IMAGE:
                tok = (block.get("image") or {}).get("token")
                if tok:
                    items.append({"kind": "image", "token": tok})
            elif btype == BLOCK_TYPE_BOARD:
                tok = (block.get("board") or {}).get("token")
                if tok:
                    items.append({"kind": "board", "token": tok})

        if not payload.get("has_more"):
            break
        page_token = payload.get("page_token") or ""
        if not page_token:
            break

    _DOCX_BLOCKS_CACHE[doc_id] = (items, now + _DOCX_BLOCKS_TTL)
    return items, None


def stream_docx_image(file_token: str, access_token: str | None = None):
    """拉 docx 内嵌的图片（drive/v1/medias/{token}/download）。

    返回 (iterator, content_type) 或 (None, None)。
    access_token：推荐 user_access_token（文档拥有者身份），否则 tenant fallback。
    """
    token = access_token or _get_tenant_token()
    if not token:
        return None, None

    resp = http_requests.get(
        f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download",
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=30,
    )
    if resp.status_code != 200:
        # 不截断 —— 需要看到飞书完整的"required privilege"清单
        logger.error(
            "stream_docx_image: status=%s token=%s body=%s",
            resp.status_code, file_token[:12], resp.text,
        )
        return None, None
    ctype = resp.headers.get("Content-Type", "application/octet-stream")
    return resp.iter_content(chunk_size=8192), ctype


def stream_board_image(whiteboard_token: str, access_token: str | None = None):
    """把画板（whiteboard/画布）导出为 PNG 返回。

    飞书 API 路径：GET /open-apis/board/v1/whiteboards/{id}/download_as_image
    权限：board:whiteboard:node:read + board:whiteboard:node:content:read （审批中）。
    审批前这里会返回 None → 调用方忽略该条目。

    access_token：推荐 user_access_token；tenant fallback 对绝大多数画板也会 403。
    """
    token = access_token or _get_tenant_token()
    if not token:
        return None, None

    resp = http_requests.get(
        f"https://open.feishu.cn/open-apis/board/v1/whiteboards/{whiteboard_token}/download_as_image",
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(
            "stream_board_image: status=%s whiteboard=%s body=%s",
            resp.status_code, whiteboard_token[:12], resp.text,
        )
        return None, None
    ctype = resp.headers.get("Content-Type", "image/png")
    return resp.iter_content(chunk_size=8192), ctype


# ------------- 妙记（Minutes）搜索 + 元信息 --------------------------
#
# 搜索用的是飞书官方 lark-cli 用的那个未公开接口：
#     POST /open-apis/minutes/v1/minutes/search
# 源码：github.com/larksuite/cli/shortcuts/minutes/minutes_search.go
# Auth：**仅 user_access_token**（AuthTypes: ["user"]）
# Scope：minutes:minutes.search:read
# 必须至少传 query / owner_ids / participant_ids / create_time 之一

def search_minutes(
    user_access_token: str,
    query: str = "",
    create_time_start: str | None = None,   # ISO 8601
    create_time_end: str | None = None,
    page_size: int = 15,
    page_token: str = "",
) -> tuple[dict | None, str | None]:
    """返回 ({ items, has_more, page_token }, error)。出错时前者 None。"""
    if not user_access_token:
        return None, "no_user_token"

    body: dict = {}
    q = (query or "").strip()
    if q:
        body["query"] = q[:50]

    filter_obj: dict = {}
    if create_time_start or create_time_end:
        ct: dict = {}
        if create_time_start:
            ct["start_time"] = create_time_start
        if create_time_end:
            ct["end_time"] = create_time_end
        if ct:
            filter_obj["create_time"] = ct
    if filter_obj:
        body["filter"] = filter_obj

    if not body:
        return None, "empty_query"

    params: dict = {"page_size": max(1, min(page_size, 30))}
    if page_token:
        params["page_token"] = page_token

    try:
        resp = http_requests.post(
            "https://open.feishu.cn/open-apis/minutes/v1/minutes/search",
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            params=params,
            json=body,
            timeout=15,
        )
        data = resp.json()
    except Exception:
        logger.exception("search_minutes: request failed")
        return None, "network_error"

    if data.get("code", -1) != 0:
        logger.warning(
            "search_minutes failed: code=%s msg=%s",
            data.get("code"), data.get("msg"),
        )
        return None, data.get("msg") or f"code_{data.get('code')}"

    payload = data.get("data") or {}
    return {
        "items": payload.get("items") or [],
        "has_more": bool(payload.get("has_more")),
        "page_token": payload.get("page_token") or "",
    }, None


def get_docx_title(doc_id: str, user_access_token: str) -> str | None:
    """GET /open-apis/docx/v1/documents/{doc_id} → title

    只返回标题（失败返回 None，不抛）。
    Scope: docx:document:readonly（user_access_token）
    """
    if not user_access_token or not doc_id:
        return None
    try:
        resp = http_requests.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}",
            headers={"Authorization": f"Bearer {user_access_token}"},
            timeout=8,
        )
        data = resp.json()
    except Exception:
        logger.exception("get_docx_title: request failed doc=%s", doc_id[:12])
        return None
    if data.get("code", -1) != 0:
        logger.info(
            "get_docx_title: code=%s msg=%s doc=%s",
            data.get("code"), data.get("msg"), doc_id[:12],
        )
        return None
    return ((data.get("data") or {}).get("document") or {}).get("title")


def get_wiki_node_title(node_token: str, user_access_token: str) -> str | None:
    """GET /open-apis/wiki/v2/spaces/get_node?token=X → node.title

    知识库节点 URL 是 /wiki/XXX，XXX 是 node_token（不是 doc_id）。
    Scope: wiki:node:read（user_access_token）
    """
    if not user_access_token or not node_token:
        return None
    try:
        resp = http_requests.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {user_access_token}"},
            params={"token": node_token},
            timeout=8,
        )
        data = resp.json()
    except Exception:
        logger.exception("get_wiki_node_title: request failed token=%s", node_token[:12])
        return None
    if data.get("code", -1) != 0:
        logger.info(
            "get_wiki_node_title: code=%s msg=%s token=%s",
            data.get("code"), data.get("msg"), node_token[:12],
        )
        return None
    return ((data.get("data") or {}).get("node") or {}).get("title")


def get_minute_meta(
    minute_token: str,
    user_access_token: str,
) -> tuple[dict | None, str | None]:
    """GET /open-apis/minutes/v1/minutes/{minute_token}

    返回 ({ title, duration, create_time, owner_id, url, ... }, error)。
    Scope: minutes:minutes.basic:read
    """
    if not user_access_token:
        return None, "no_user_token"

    try:
        resp = http_requests.get(
            f"https://open.feishu.cn/open-apis/minutes/v1/minutes/{minute_token}",
            headers={"Authorization": f"Bearer {user_access_token}"},
            timeout=10,
        )
        data = resp.json()
    except Exception:
        logger.exception("get_minute_meta: request failed token=%s", minute_token[:12])
        return None, "network_error"

    if data.get("code", -1) != 0:
        logger.info(
            "get_minute_meta: code=%s msg=%s token=%s",
            data.get("code"), data.get("msg"), minute_token[:12],
        )
        return None, data.get("msg") or f"code_{data.get('code')}"

    # 飞书返的 data.minute = { token, owner_id, create_time, title, cover, duration, url }
    minute = (data.get("data") or {}).get("minute") or {}
    return minute, None


# ------------- 用户身份 token（OAuth 拿的 user_access_token）---------
# 存在 user_tokens 表，OAuth 回调时由 auth.py 写入。
# 这里只负责：按需取出 + 过期 refresh + 透传给调用方。

from datetime import datetime, timedelta  # noqa: E402
from src.db.connection import connect, transaction  # noqa: E402

# 过期前多少秒就刷新（避免卡在边界）
_USER_TOKEN_REFRESH_SKEW = 60


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _load_user_tokens(open_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM user_tokens WHERE open_id = ?", (open_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_user_tokens(
    open_id: str, access_token: str, refresh_token: str,
    expires_in: int, refresh_expires_in: int,
) -> None:
    now = datetime.now()
    access_exp = (now + timedelta(seconds=expires_in)).isoformat(timespec="seconds")
    refresh_exp = (now + timedelta(seconds=refresh_expires_in)).isoformat(timespec="seconds")
    now_iso = now.isoformat(timespec="seconds")

    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO user_tokens
                    (open_id, access_token, refresh_token,
                     access_expires_at, refresh_expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(open_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    access_expires_at = excluded.access_expires_at,
                    refresh_expires_at = excluded.refresh_expires_at,
                    updated_at = excluded.updated_at
                """,
                (open_id, access_token, refresh_token, access_exp, refresh_exp, now_iso),
            )
    finally:
        conn.close()


def _refresh_user_token(row: dict) -> str | None:
    """用 refresh_token 换新的 access_token。失败返回 None。"""
    try:
        resp = http_requests.post(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": LARK_APP_ID,
                "client_secret": LARK_APP_SECRET,
                "refresh_token": row["refresh_token"],
            },
            timeout=10,
        )
        data = resp.json()
    except Exception:
        logger.exception("refresh user token: http failed")
        return None

    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token") or row["refresh_token"]
    if not new_access:
        logger.error(
            "refresh user token failed: code=%s msg=%s",
            data.get("code"), data.get("msg") or data.get("error_description"),
        )
        return None

    _save_user_tokens(
        open_id=row["open_id"],
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=int(data.get("expires_in") or 7200),
        refresh_expires_in=int(
            data.get("refresh_token_expires_in") or data.get("refresh_expires_in") or 2592000
        ),
    )
    return new_access


def get_user_access_token(open_id: str) -> str | None:
    """拿当前用户的 user_access_token，过期自动 refresh。

    返回 None 意味着：token 根本没有（用户从没 OAuth 过，或 refresh 也过期）。
    调用方应当返回 401 让前端重新走 OAuth。
    """
    row = _load_user_tokens(open_id)
    if not row:
        return None

    now = datetime.now()
    try:
        access_exp = _parse_iso(row["access_expires_at"])
        refresh_exp = _parse_iso(row["refresh_expires_at"])
    except Exception:
        logger.warning("user_tokens corrupt iso for %s, forcing re-auth", open_id)
        return None

    # access 未过期 → 直接用（这个判断放最前，refresh_token 可能是空的）
    if now + timedelta(seconds=_USER_TOKEN_REFRESH_SKEW) < access_exp:
        return row["access_token"]

    # access 过期 or 快过期
    # 没有 refresh_token 就没救（未申请 offline_access scope 的情况）
    if not row.get("refresh_token"):
        logger.info(
            "user %s access_token expired and no refresh_token; "
            "needs re-OAuth (offline_access not granted?)", open_id,
        )
        return None

    # refresh_token 也过期 → 没救
    if now >= refresh_exp:
        logger.info("refresh_token expired for %s, re-auth needed", open_id)
        return None

    # access 过期 + refresh 还有效 → refresh
    return _refresh_user_token(row)


def search_feishu_users(user_token: str, query: str, limit: int = 20) -> list[dict]:
    """用用户身份搜公司员工。

    需要权限：contact:user:search（user_access_token）。
    返回 [{open_id, name, department_path, avatar_url, ...}]。
    失败返回 []（打日志）。
    """
    # 官方端点：GET /open-apis/search/v1/user（对应 scope contact:user:search）
    try:
        resp = http_requests.get(
            "https://open.feishu.cn/open-apis/search/v1/user",
            headers={"Authorization": f"Bearer {user_token}"},
            params={"query": query, "page_size": limit},
            timeout=10,
        )
    except Exception:
        logger.exception("search_feishu_users: http exception")
        return []

    # 把响应概况打出来便于诊断（400/403/200 都要看）
    body_preview = resp.text[:500] if resp.text else "(empty body)"
    try:
        data = resp.json()
    except Exception:
        logger.warning(
            "search_feishu_users: non-JSON response  status=%d  content-type=%s  body[:500]=%r",
            resp.status_code, resp.headers.get("Content-Type"), body_preview,
        )
        return []

    if data.get("code", -1) != 0:
        logger.warning(
            "search_feishu_users: code=%s msg=%s  data=%r",
            data.get("code"), data.get("msg"), data,
        )
        return []

    users = (data.get("data") or {}).get("users") or []
    return users


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
