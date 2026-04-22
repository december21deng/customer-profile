"""Feishu Docx: detect /docx/ URLs and fetch plain-text content via lark-cli."""

import json
import logging
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Matches: https://xxx.feishu.cn/docx/Whted43vmoewDJxySixcktwpnQc
DOCX_URL_PATTERN = re.compile(
    r"https?://[\w.-]*feishu\.cn/docx/(?P<doc_id>[A-Za-z0-9]+)"
)

LARK_CLI = "lark-cli"


@dataclass
class DocxDocument:
    doc_id: str
    url: str
    title: str | None
    raw_content: str


def extract_docx_url(text: str) -> tuple[str, str] | None:
    """If text contains a /docx/ URL, return (doc_id, url). Else None."""
    m = DOCX_URL_PATTERN.search(text)
    if not m:
        return None
    return m.group("doc_id"), m.group(0)


def _lark_cli_get(path: str, timeout: int = 30) -> tuple[dict | None, str | None]:
    """Call `lark-cli api GET <path> --as user`. Returns (parsed_json, error)."""
    try:
        proc = subprocess.run(
            [LARK_CLI, "api", "GET", path, "--as", "user"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, "未找到 lark-cli，请先安装：npm i -g @larksuite/cli"
    except subprocess.TimeoutExpired:
        return None, f"lark-cli 调用超时（{timeout}s）"

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        return None, f"lark-cli 执行失败：{stderr[:300]}"

    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as e:
        return None, f"lark-cli 输出解析失败：{e}"


def fetch_raw_content(doc_id: str, url: str) -> tuple[DocxDocument | None, str | None]:
    """Fetch the plain-text content of a Feishu Docx document via lark-cli.

    Returns (document, error_message). On success error_message is None.
    """
    # Fetch title (optional; failure is non-fatal)
    title: str | None = None
    meta, meta_err = _lark_cli_get(f"/open-apis/docx/v1/documents/{doc_id}", timeout=10)
    if meta and meta.get("code") == 0:
        title = meta.get("data", {}).get("document", {}).get("title")
    elif meta:
        logger.warning(
            "docx metadata fetch non-zero: code=%s msg=%s",
            meta.get("code"), meta.get("msg"),
        )
    elif meta_err:
        logger.warning("docx metadata fetch error: %s", meta_err)

    # Fetch raw content
    data, err = _lark_cli_get(
        f"/open-apis/docx/v1/documents/{doc_id}/raw_content", timeout=30
    )
    if err:
        return None, err

    code = data.get("code", -1)
    if code != 0:
        msg = data.get("msg", "unknown")
        logger.error("Docx raw_content failed doc_id=%s code=%s msg=%s", doc_id, code, msg)
        if code == 99991672 or code == 1254005 or "permission" in msg.lower():
            return None, f"无权限读取该文档（code={code}）。当前 lark-cli 用户对此文档没有访问权限。"
        if code == 1254006:
            return None, f"文档不存在或已删除（code={code}）"
        return None, f"飞书 API 返回错误 code={code} msg={msg}"

    content = data.get("data", {}).get("content", "")
    if not content.strip():
        return None, "文档内容为空"

    return DocxDocument(doc_id=doc_id, url=url, title=title, raw_content=content), None
