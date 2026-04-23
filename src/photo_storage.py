"""照片存储抽象：dev 写本地磁盘，prod 上传飞书 im/v1/images。

返回的 key 作为 `followup_records.photo_image_key` 存库：
    - 本地：  `local_<uuid>`，文件落在 PHOTO_DIR/<uuid>.<ext>
    - 飞书：  `img_v*`（飞书原生 image_key）

读取时根据 key 前缀分流（见 web/followup.py /api/image/{key}）。
历史数据（只有 img_v*）自动走飞书路径，无需迁移。
"""

from __future__ import annotations

import logging
import mimetypes
import re
import uuid
from pathlib import Path

from src.config import APP_ENV, PHOTO_DIR
from src.lark_client import stream_im_image, upload_im_image

logger = logging.getLogger(__name__)

LOCAL_KEY_RE = re.compile(r"^local_[A-Za-z0-9]+$")
LARK_KEY_RE = re.compile(r"^img_v[0-9]_[A-Za-z0-9_\-]+$")

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
}


def is_valid_key(key: str) -> bool:
    return bool(LOCAL_KEY_RE.match(key) or LARK_KEY_RE.match(key))


def _ext_from(filename: str | None, content_type: str | None) -> str:
    if content_type and content_type.lower() in _EXT_BY_MIME:
        return _EXT_BY_MIME[content_type.lower()]
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif"}:
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def save(file_bytes: bytes, filename: str | None = None, content_type: str | None = None) -> str | None:
    """保存照片，返回 storage key。失败返回 None。"""
    if APP_ENV == "prod":
        return upload_im_image(file_bytes, filename=filename or "photo.jpg")

    # dev：写本地
    uid = uuid.uuid4().hex
    ext = _ext_from(filename, content_type)
    path = PHOTO_DIR / f"{uid}{ext}"
    try:
        path.write_bytes(file_bytes)
    except OSError:
        logger.exception("photo_storage.save: failed to write %s", path)
        return None
    logger.info("photo_storage.save: local %s (%d bytes)", path.name, len(file_bytes))
    return f"local_{uid}"


def _local_file_for(key: str) -> Path | None:
    """按 key 找到本地文件（任意扩展名）。"""
    uid = key[len("local_"):]
    if not re.fullmatch(r"[A-Za-z0-9]+", uid):
        return None
    for ext in (".jpg", ".png", ".gif"):
        p = PHOTO_DIR / f"{uid}{ext}"
        if p.exists():
            return p
    return None


def stream(key: str):
    """返回 (iterator, content_type) 或 (None, None)。"""
    if LOCAL_KEY_RE.match(key):
        path = _local_file_for(key)
        if path is None:
            return None, None
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        def _iter():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    yield chunk
        return _iter(), ctype

    if LARK_KEY_RE.match(key):
        return stream_im_image(key)

    return None, None
