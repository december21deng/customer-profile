"""AI 摘要：跟进记录创建后异步生成 1 句话摘要。

输入：用户写的 background + 会议纪要 raw_content（拉不到就只用 background）
输出：写入 followup_records.summary
失败不抛，写日志，summary 保持 NULL，列表页降级显示 background 首行。
"""

from __future__ import annotations

import logging

from anthropic import Anthropic

from src.config import ANTHROPIC_API_KEY
from src.db.connection import connect, transaction
from src.lark_client import fetch_docx_raw

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_MINUTES_CHARS = 8000  # 纪要截断，避免超长


_SYS = (
    "你是 CRM 跟进记录助手。用户会给你本次客户沟通的背景和会议纪要原文，"
    "请用 1 句话（不超过 30 个汉字）精准概括这次跟进的核心主题。"
    "直接输出摘要，不加任何前缀、引号或解释。"
)


def _build_user_msg(background: str, minutes_text: str | None) -> str:
    parts = [f"【客户】{{customer}}", f"【会议背景】\n{background.strip()}"]
    if minutes_text and minutes_text.strip():
        truncated = minutes_text.strip()[:MAX_MINUTES_CHARS]
        parts.append(f"【会议纪要】\n{truncated}")
    return "\n\n".join(parts)


def _generate(background: str, minutes_text: str | None, customer: str) -> str | None:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    user_msg = _build_user_msg(background, minutes_text).replace("{customer}", customer or "")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=80,
            system=_SYS,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception:
        logger.exception("anthropic messages.create failed")
        return None

    # content: list[TextBlock]
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text or ""
    text = text.strip().strip('"').strip("“”").strip()
    # 掐到 40 字硬上限（30 字 soft + 余量）
    if len(text) > 40:
        text = text[:40].rstrip()
    return text or None


def generate_and_save(record_id: str) -> None:
    """BackgroundTasks 入口：按 record_id 重跑一次摘要。"""
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT r.id, r.background, r.minutes_doc_id, c.name AS customer
            FROM followup_records r
            JOIN customers c ON c.id = r.customer_id
            WHERE r.id = ?
            """,
            (record_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        logger.warning("AI summary: record %s not found", record_id)
        return

    background = row["background"] or ""
    customer = row["customer"] or ""
    minutes_text = None
    if row["minutes_doc_id"]:
        minutes_text, err = fetch_docx_raw(row["minutes_doc_id"])
        if err:
            logger.info("AI summary: docx fetch failed (%s), using background only", err)

    summary = _generate(background, minutes_text, customer)
    if not summary:
        logger.warning("AI summary: generation returned empty for %s", record_id)
        return

    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE followup_records SET summary = ? WHERE id = ?",
                (summary, record_id),
            )
    finally:
        conn.close()
    logger.info("AI summary set for %s: %s", record_id, summary)
