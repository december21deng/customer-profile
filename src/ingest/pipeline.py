"""Ingest pipeline: Fetch → Ingest (agent) → Extract (structured) → Commit.

对 `followup_records` 表里一条记录跑完整流程。被两个入口调用：
    - scripts/try_ingest.py      手工/自动化测试
    - src/web/followup.py        表单提交后的 BackgroundTask

落盘写入：
    raw/customers/<date>-<id>-<hash>.md
    wiki/customers/<customer_id>.md
    wiki/index.md, wiki/log.md
    db: followup_records.summary
    db: customers.summary, customers.wiki_path, customers.local_updated_at

Stage 2 简化版（见 docs/ingest-implementation-plan.md §4）：
    - 无 CustomerLocks（Fly 单机 BackgroundTask，并发冲突概率低；Stage 3 再加）
    - 无 ingest_jobs 状态表（失败只写日志）
    - 无 /data/.git commit（Stage 5 在 Fly 上再加）
    - journey_stage / contacts_delta / next_actions 抽出来但暂不落库（缺列）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import textwrap
from datetime import datetime
from pathlib import Path

import anthropic

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from src.config import PROJECT_ROOT
from src.db.connection import connect, transaction
from src.ingest import jobs, lock
from src.lark_client import (
    fetch_docx_media,
    fetch_docx_raw,
    get_user_access_token,
    resolve_wiki_node,
    stream_board_image,
    stream_docx_image,
)
from src import photo_storage

logger = logging.getLogger(__name__)

RAW_DIR = PROJECT_ROOT / "raw"
WIKI_DIR = PROJECT_ROOT / "wiki"

INGEST_MODEL = "claude-sonnet-4-6"   # wiki 写作，保叙述/表格/金句质量
EXTRACT_MODEL = "claude-haiku-4-5"   # schema 填空，Haiku 足够

# 每百万 tokens 的定价（input, output），用于 Extract 成本估算。
# Ingest 成本由 claude-agent-sdk 自己返回，不走这张表。
_MODEL_PRICING = {
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-sonnet-4-5":  (3.00, 15.00),
    "claude-haiku-4-5":   (1.00,  5.00),
    "claude-opus-4-6":   (15.00, 75.00),
}


# ---------------------------------------------------------------------------
# Fetch / raw 落盘
# ---------------------------------------------------------------------------

def save_raw(
    text: str, source: str, customer_id: str, meeting_date: str,
) -> Path:
    """把 raw 文本写成带 frontmatter 的 md 文件，返回绝对路径。"""
    date = meeting_date[:10]
    short = hashlib.sha256(f"{source}{meeting_date}".encode()).hexdigest()[:8]
    raw_dir = RAW_DIR / "customers"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{date}-{customer_id}-{short}.md"

    header = textwrap.dedent(f"""\
        ---
        source: {source}
        customer_id: {customer_id}
        meeting_date: {meeting_date}
        collected: {datetime.now().isoformat(timespec='seconds')}
        published: Unknown
        ---

        """)
    raw_path.write_text(header + text, encoding="utf-8")
    logger.info("raw saved: %s (%d chars)", raw_path.relative_to(PROJECT_ROOT), len(text))
    return raw_path


def fetch_and_save(
    doc_id: str, customer_id: str, meeting_date: str,
    access_token: str | None = None,
) -> tuple[Path, str] | None:
    """拉飞书 docx 纯文字，落 raw markdown。access_token 为上传者的 user_access_token
    （必有文档权限）；缺省回 tenant token（多数文档会 forBidden）。"""
    text, err = fetch_docx_raw(doc_id, access_token=access_token)
    if err or not text:
        logger.warning("fetch_docx_raw failed: %r", err)
        return None
    raw_path = save_raw(
        text, source=f"https://feishu.cn/docx/{doc_id}",
        customer_id=customer_id, meeting_date=meeting_date,
    )
    return raw_path, text


def fetch_media_and_save(doc_id: str, access_token: str | None) -> list[dict]:
    """把 docx 里的图片 + 画板下载到本地存储，返回 [{kind, key}] 列表。

    失败 / 权限不足的条目静默跳过。全部失败返回 []。
    画板权限未审批前画板条目会全部失败 → 该部分返回空。
    """
    items, err = fetch_docx_media(doc_id, access_token=access_token)
    if err:
        logger.warning("fetch_docx_media failed: %r (doc=%s)", err, doc_id[:12])
        return []
    if not items:
        return []

    result: list[dict] = []
    for idx, it in enumerate(items):
        kind = it["kind"]
        tok = it["token"]
        try:
            if kind == "image":
                stream, _ctype = stream_docx_image(tok, access_token=access_token)
            elif kind == "board":
                stream, _ctype = stream_board_image(tok, access_token=access_token)
            else:
                continue
            if stream is None:
                logger.info(
                    "media skip (permission/fetch): doc=%s idx=%d kind=%s tok=%s…",
                    doc_id[:12], idx, kind, tok[:8],
                )
                continue
            buf = b"".join(stream)
            if not buf:
                continue
            # 存本地（dev） / im/v1/images（prod），复用同一套 photo_storage
            key = photo_storage.save(
                buf,
                filename=f"{kind}-{tok[:8]}.png",
                content_type="image/png",
            )
            if key:
                result.append({"kind": kind, "key": key})
        except Exception as e:
            logger.warning(
                "media fetch crashed doc=%s idx=%d kind=%s: %r",
                doc_id[:12], idx, kind, e,
            )
            continue
    return result


# ---------------------------------------------------------------------------
# Ingest（Claude Agent SDK + karpathy-llm-wiki skill）
# ---------------------------------------------------------------------------

_INGEST_PROMPT = """按 karpathy-llm-wiki skill 的 Ingest 流程处理这份客户跟进。

客户：{customer_name}（id={customer_id}）
目标 wiki 页：{wiki_abs_path}（不存在就新建）
新 raw 文件路径（已落盘，按 skill 要求 Sources 字段引用）：{raw_abs_path}
会议时间：{meeting_date}
项目根目录：{project_root}

新 raw 文件内容（**已为你读好，不需要再 Read 这个文件**）：

<raw>
{raw_text}
</raw>

硬约束：
- wiki/ 只用 customers/ 一个 topic 子目录，不要创建其他 topic
- 客户页文件名必须用 "{customer_id}"（slug），不是显示名
- 不要级联到其他客户的文章
- 不要读 raw/customers/ 下其他客户的文件
- 所有 Read/Write/Edit 必须用上面给出的绝对路径
"""


async def run_ingest_agent(
    customer_id: str,
    customer_name: str,
    raw_abs_path: str,
    meeting_date: str,
    log_prefix: str = "ingest",
) -> None:
    """调 claude-agent-sdk，让 skill 自己读写 wiki/。

    优化：把 raw 内容预读进 prompt，省掉 agent 一次 Read roundtrip。
    其他 Read/Edit（wiki/index/log/template）仍由 skill 驱动 agent 自主做。
    """
    opts = ClaudeAgentOptions(
        model=INGEST_MODEL,
        allowed_tools=["Read", "Write", "Edit", "Grep", "Glob"],
        cwd=str(PROJECT_ROOT),
        permission_mode="bypassPermissions",
    )

    raw_text = Path(raw_abs_path).read_text(encoding="utf-8")
    wiki_abs = str(WIKI_DIR / "customers" / f"{customer_id}.md")
    prompt = _INGEST_PROMPT.format(
        customer_id=customer_id,
        customer_name=customer_name,
        raw_abs_path=raw_abs_path,
        wiki_abs_path=wiki_abs,
        meeting_date=meeting_date,
        project_root=str(PROJECT_ROOT),
        raw_text=raw_text,
    )

    logger.info("[%s] ingest start  model=%s", log_prefix, INGEST_MODEL)

    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            logger.info("[%s] session=%s", log_prefix, msg.data.get("session_id"))
        elif isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text:
                    logger.debug("[%s] assistant: %s", log_prefix, b.text[:200])
                elif isinstance(b, ToolUseBlock):
                    preview = str(b.input)[:120].replace("\n", " ")
                    logger.info("[%s] tool %s  %s", log_prefix, b.name, preview)
        elif isinstance(msg, ResultMessage):
            logger.info(
                "[%s] ingest done  stop=%s duration_ms=%s cost=$%s",
                log_prefix,
                msg.stop_reason,
                getattr(msg, "duration_ms", "?"),
                getattr(msg, "total_cost_usd", "?"),
            )


# ---------------------------------------------------------------------------
# Extract（一次 anthropic tool_use 强制）
# ---------------------------------------------------------------------------

JOURNEY_STAGES = [
    "线索", "初步接触", "需求确认", "方案沟通",
    "报价商务", "合同签约", "交付验收", "运营维护", "流失冻结",
]

_EXTRACT_TOOL = {
    "name": "extract",
    "description": "从 wiki 文章全文和一份新 raw 纪要提取结构化字段",
    "input_schema": {
        "type": "object",
        "required": [
            "summary", "journey_stage",
            "meeting_title", "progress_line",
            "next_actions", "contacts_delta",
        ],
        "properties": {
            "summary": {
                "type": "string",
                "description": "300-800 字的客户整体概况，从 wiki 全文提炼，"
                               "包含行业、合作阶段、主推产品、关键联系人、当前风险/机会。",
            },
            "journey_stage": {
                "type": "string",
                "enum": JOURNEY_STAGES,
                "description": "客户当前所处阶段。以最新一次跟进为准。",
            },
            "meeting_title": {
                "type": "string",
                "maxLength": 20,
                "description": (
                    "6-14 字的会议主题短语，动宾或名词结构。"
                    "显示在 CRM 列表每条记录的「会议主题」标签右侧。"
                    "硬规则："
                    "(1) 不含客户名（客户名已经在卡片头部显示）；"
                    "(2) 不含日期、时长；"
                    "(3) 不以'会议''纪要''沟通''交流'收尾；"
                    "(4) 不要抄文档标题。"
                    "正例：'供应链合作探索'、'产品性能需求澄清'、"
                    "'合同条款二次谈判'、'样品实测结果复盘'、'2026 年度采购规划'。"
                    "反例：'会议纪要'(废话)、'ABENA 的会议'(带客户名)、"
                    "'双方友好会谈'(套话)、'产品讨论'(太泛)。"
                ),
            },
            "progress_line": {
                "type": "string",
                "maxLength": 80,
                "description": (
                    "20-40 字的一句话进展。"
                    "显示在 CRM 列表每条记录的「一句话进展」标签右侧，"
                    "让销售 1 秒判断这个客户推进到哪一步。"
                    "必须包含以下四类之一："
                    "(a) 明确结论（'双方同意/客户拒绝/达成共识'）；"
                    "(b) 可验证的下一步（'下周提供报价/三天内发样/本月签合同'）；"
                    "(c) 具体阻碍（'客户对 X 有异议/待法务审合同/缺 Y 参数'）；"
                    "(d) 金额/数量/时间里程碑（'首单 50 万/1000 件/Q3 落地'）。"
                    "正例：'双方确认合作意向，下周提供 BOM 报价'、"
                    "'客户对防火等级存疑，需补 B1 级检测报告'、"
                    "'达成首单意向 50 万，待客户法务审合同'。"
                    "反例：'双方进行了沟通'、'气氛融洽'、"
                    "'交换了名片'、'深入讨论了多个方面'——一律禁止。"
                    "纪要信息不足时可写'纪要信息不足，需补会议要点'，不准留空。"
                ),
            },
            "next_actions": {
                "type": "array",
                "description": "本次纪要中的可执行 Action Items（3-10 条）。",
                "items": {
                    "type": "object",
                    "required": ["action", "owner"],
                    "properties": {
                        "action": {"type": "string"},
                        "owner": {"type": "string"},
                        "due": {"type": "string"},
                    },
                },
            },
            "contacts_delta": {
                "type": "array",
                "description": "本次纪要中出现的客户侧联系人（不含我方）。",
                "items": {
                    "type": "object",
                    "required": ["action", "name"],
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "update"]},
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
        },
    },
}


_TITLE_TRIM_SUFFIXES = ("会议纪要", "拜访纪要", "纪要", "会议", "沟通", "交流", "会谈")


def _clean_title(t: str) -> str:
    """清洗 AI 生成的 meeting_title：去空白、剥尾巴、限长。"""
    t = (t or "").strip()
    changed = True
    while changed and t:
        changed = False
        for suffix in _TITLE_TRIM_SUFFIXES:
            if t.endswith(suffix) and len(t) > len(suffix):
                t = t[: -len(suffix)].rstrip("·-—— ：: ")
                changed = True
    return t[:20]


def _clean_progress(p: str) -> str:
    return (p or "").strip()[:80]

_EXTRACT_SYSTEM = (
    "你是一个结构化提取助手。输入是一篇客户 wiki 文章（已由上游 LLM 沉淀）"
    "和一份新的飞书纪要 raw 文本。你只做信息抽取和分类，不做润色或编造。"
    "【语言要求】不管输入是什么语言，所有输出字段（summary / "
    "meeting_title / progress_line / next_actions / contacts_delta 里的文字）"
    "必须全部使用简体中文。人名、公司名、产品名可以保留原文不翻译。"
    "如果字段在输入中找不到，就留空字符串或空数组。"
)


def run_extract(wiki_text: str, raw_text: str, log_prefix: str = "extract") -> dict:
    """同步调用 anthropic。被 async 调用者请用 asyncio.to_thread 包裹。"""
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=4096,
        system=_EXTRACT_SYSTEM,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"# Wiki（客户整体档案，可能为空表示首条）\n\n"
                    f"{wiki_text or '（空）'}\n\n"
                    f"# Raw（本次新纪要）\n\n"
                    f"{raw_text}"
                ),
            },
        ],
    )

    usage = resp.usage
    in_p, out_p = _MODEL_PRICING.get(EXTRACT_MODEL, (3.00, 15.00))
    cost = (usage.input_tokens * in_p + usage.output_tokens * out_p) / 1_000_000
    logger.info(
        "[%s] done  model=%s in=%d out=%d cost=$%.4f",
        log_prefix, EXTRACT_MODEL, usage.input_tokens, usage.output_tokens, cost,
    )

    for block in resp.content:
        if block.type == "tool_use" and block.name == "extract":
            return block.input

    raise RuntimeError(f"extract tool not returned; stop_reason={resp.stop_reason}")


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def _commit_sql(
    record_id: str,
    customer_id: str,
    wiki_path: Path,
    extract_result: dict,
    minutes_media: list[dict],
) -> None:
    """把 Extract 结果 + 从 docx 拉下来的画板/图片写回 DB（单事务）。"""
    wiki_rel = str(wiki_path.relative_to(PROJECT_ROOT))
    now = datetime.now().isoformat(timespec="seconds")

    conn = connect()
    try:
        with transaction(conn):
            # followup_records.summary 已废弃（UI 不再显示长摘要），不再写入
            conn.execute(
                "UPDATE followup_records "
                "SET meeting_title = ?, progress_line = ?, "
                "    minutes_media = ? "
                "WHERE id = ?",
                (
                    _clean_title(extract_result.get("meeting_title") or ""),
                    _clean_progress(extract_result.get("progress_line") or ""),
                    json.dumps(minutes_media, ensure_ascii=False),
                    record_id,
                ),
            )
            conn.execute(
                """
                UPDATE customers
                SET summary = ?, wiki_path = ?, local_updated_at = ?
                WHERE id = ?
                """,
                (
                    extract_result.get("summary") or "",
                    wiki_rel,
                    now,
                    customer_id,
                ),
            )
    finally:
        conn.close()

    logger.info(
        "[commit] record=%s customer=%s wiki=%s",
        record_id, customer_id, wiki_rel,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _load_record(record_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT r.id, r.customer_id, r.meeting_date, r.minutes_doc_id,
                   r.minutes_doc_url, r.owner_id, r.background,
                   c.name AS customer_name
            FROM followup_records r
            JOIN customers c ON c.id = r.customer_id
            WHERE r.id = ?
            """,
            (record_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _short_err(exc: BaseException, limit: int = 300) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    return msg if len(msg) <= limit else msg[:limit] + "…"


async def run(record_id: str) -> None:
    """一条跟进记录跑完整 ingest pipeline。失败只写日志 + ingest_jobs，不抛。

    状态机：queued → fetching → ingesting → extracting → committing → done
    任一阶段失败 → failed（带 error 字段）。
    """
    log_prefix = f"pipeline {record_id[:8]}"

    # --- 载入记录 + 起 job（在 lock 之外做，这样失败也能记账） ---
    try:
        rec = _load_record(record_id)
    except Exception:
        logger.exception("[%s] load record failed", log_prefix)
        return

    if not rec:
        logger.warning("[%s] record not found", log_prefix)
        return

    customer_id = rec["customer_id"]
    jobs.init(record_id, customer_id)

    if not rec.get("minutes_doc_id"):
        msg = "no minutes_doc_id, nothing to ingest"
        logger.info("[%s] %s", log_prefix, msg)
        jobs.set_status(record_id, "failed", error=msg)
        return

    # 拿上传者的 user_access_token（有 docx:document:readonly + wiki:node:read scope）
    # 没拿到（如：密码登录、OAuth scope 没加、token 过期且没 refresh）→ None，
    # fetch_docx_* 会 fallback 到 tenant token（对大部分文档会 forBidden，但至少不挂）
    owner_user_token: str | None = None
    owner_id = rec.get("owner_id") or ""
    if owner_id.startswith("ou_"):
        try:
            owner_user_token = get_user_access_token(owner_id)
        except Exception:
            logger.exception("[%s] get_user_access_token failed (continue anyway)", log_prefix)

    # wiki URL 需要先解析成真实 docx id（/wiki/<token> 不能直接调 docx API）
    effective_doc_id = rec["minutes_doc_id"]
    if "/wiki/" in (rec.get("minutes_doc_url") or ""):
        try:
            resolved = await asyncio.to_thread(
                resolve_wiki_node, rec["minutes_doc_id"], owner_user_token,
            )
        except Exception:
            logger.exception("[%s] resolve_wiki_node crashed", log_prefix)
            resolved = None
        if resolved and resolved != rec["minutes_doc_id"]:
            logger.info(
                "[%s] wiki → docx resolved: %s → %s",
                log_prefix, rec["minutes_doc_id"][:10], resolved[:10],
            )
            effective_doc_id = resolved
        elif not resolved:
            logger.warning(
                "[%s] wiki resolve returned nothing; falling back to raw token (likely to fail)",
                log_prefix,
            )

    # --- 串行化：同客户一把锁 + 全局并发 3 ---
    async with lock.acquire(customer_id):
        # ---- Fetch -----------------------------------------------------
        jobs.set_status(record_id, "fetching")
        try:
            fetched = await asyncio.to_thread(
                fetch_and_save,
                effective_doc_id,
                customer_id,
                rec["meeting_date"],
                owner_user_token,
            )
        except Exception as e:
            logger.exception("[%s] fetch crashed", log_prefix)
            jobs.set_status(record_id, "failed", error=f"fetch: {_short_err(e)}")
            return

        if not fetched:
            jobs.set_status(record_id, "failed",
                            error="fetch: empty response (permission or bad doc_id)")
            return
        raw_path, raw_text = fetched

        # 媒体（画板/图片）不再由 pipeline 下载 —— 用户点"打开原文"在飞书里看，
        # 避免额外申请 drive / board scope。minutes_media 永远为空数组。
        minutes_media: list[dict] = []

        # ---- Ingest (agent) -------------------------------------------
        jobs.set_status(record_id, "ingesting")
        try:
            await run_ingest_agent(
                customer_id=customer_id,
                customer_name=rec["customer_name"],
                raw_abs_path=str(raw_path),
                meeting_date=rec["meeting_date"],
                log_prefix=log_prefix,
            )
        except Exception as e:
            logger.exception("[%s] ingest agent crashed", log_prefix)
            jobs.set_status(record_id, "failed", error=f"ingest: {_short_err(e)}")
            return

        # ---- Extract --------------------------------------------------
        jobs.set_status(record_id, "extracting")
        wiki_path = WIKI_DIR / "customers" / f"{customer_id}.md"
        wiki_text = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""

        try:
            extract_result = await asyncio.to_thread(
                run_extract, wiki_text, raw_text, log_prefix,
            )
        except Exception as e:
            logger.exception("[%s] extract failed", log_prefix)
            jobs.set_status(record_id, "failed", error=f"extract: {_short_err(e)}")
            return

        # ---- Commit ---------------------------------------------------
        jobs.set_status(record_id, "committing")
        try:
            await asyncio.to_thread(
                _commit_sql,
                record_id, customer_id, wiki_path, extract_result, minutes_media,
            )
        except Exception as e:
            logger.exception("[%s] commit failed", log_prefix)
            jobs.set_status(record_id, "failed", error=f"commit: {_short_err(e)}")
            return

        # extract 结果顺手落盘一份，调试用（失败不影响主流程）
        try:
            extract_path = raw_path.with_suffix(".extract.json")
            extract_path.write_text(
                json.dumps(extract_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[%s] extract.json sidecar save failed", log_prefix)

        jobs.set_status(record_id, "done")
        logger.info("[%s] ✓ pipeline done", log_prefix)
