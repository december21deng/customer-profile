"""Stage 0/1 smoke test — 单条 docx 跑一遍 Ingest + Extract，不动 DB。

用法：
    # 路子 A：走应用身份 fetch（等 docx:document:readonly 发版）
    python scripts/try_ingest.py \\
        --doc https://xxx.feishu.cn/docx/ABCdef123 \\
        --customer-id upe --customer-name UPE --meeting-date 2026-04-23

    # 路子 B：本地 md 文件（lark-cli 用户身份导出，绕开版本发布）
    lark-cli api GET /open-apis/docx/v1/documents/<id>/raw_content \\
        --as user -q '.data.content' -r > /tmp/upe.md
    python scripts/try_ingest.py \\
        --raw-file /tmp/upe.md \\
        --customer-id upe --customer-name UPE --meeting-date 2026-04-21

注意：这里的 --raw-file 只影响本测试脚本；src/lark_client.py 和 web 应用
的 ingest 代码仍然走 tenant_access_token（应用身份），不受影响。

流程（Stage 0 + Stage 1）：
    Fetch    - lark docx raw 落盘                    raw/customers/*.md
    Ingest   - claude-agent-sdk + karpathy skill     wiki/customers/<id>.md
    Extract  - anthropic SDK structured output       打印到 stdout，不落库

产出：
    raw/customers/<YYYY-MM-DD>-<customer-id>-<hash>.md
    wiki/customers/<customer-id>.md
    wiki/index.md, wiki/log.md
    [extract] 结构化 JSON 打印在控制台

验证：
    1. wiki 质量（是 Stage 0 的事）
    2. Extract 出来的 summary/stage/record_summary/contacts_delta 是否靠谱
    3. 两步总耗时和 cost 是否可接受

失败就改这个脚本，不碰其他任何东西。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

# 先加载 .env，.env 里的 key 只影响本进程
load_dotenv(PROJECT_ROOT / ".env", override=True)

from src.ingest.pipeline import (  # noqa: E402
    RAW_DIR, WIKI_DIR,
    fetch_and_save, run_extract, run_ingest_agent, save_raw,
)


DOC_ID_PATTERNS = [
    re.compile(r"/docx/([A-Za-z0-9]+)"),
    re.compile(r"/wiki/([A-Za-z0-9]+)"),
    re.compile(r"/docs/([A-Za-z0-9]+)"),
]


def parse_doc_id(s: str) -> str:
    s = s.strip()
    if "://" not in s:
        return s  # already a doc_id
    for pat in DOC_ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    raise ValueError(f"can't extract doc_id from: {s}")


def load_raw_file(
    raw_file: Path, customer_id: str, meeting_date: str,
) -> tuple[Path, str]:
    """仅测试用旁路：从本地 md 文件读（lark-cli 用户身份导出）。"""
    print(f"[raw-file] reading {raw_file} ...")
    if not raw_file.exists():
        raise FileNotFoundError(raw_file)
    text = raw_file.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"{raw_file} is empty")
    raw_path = save_raw(
        text, source=f"lark-cli:{raw_file.name}",
        customer_id=customer_id, meeting_date=meeting_date,
    )
    return raw_path, text


def fetch_via_app(
    doc_id: str, customer_id: str, meeting_date: str,
) -> tuple[Path, str]:
    """生产代码路径（应用身份）。"""
    print(f"[fetch] pulling docx {doc_id} via tenant_access_token ...")
    res = fetch_and_save(doc_id, customer_id, meeting_date)
    if res is None:
        raise RuntimeError("fetch_docx_raw failed (see logs)")
    return res


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--doc",
                     help="飞书 docx URL 或 doc_id（应用身份，走生产 fetch_docx_raw）")
    src.add_argument("--raw-file", type=Path,
                     help="本地 md 文件（测试旁路，lark-cli 用户身份导出用）")
    parser.add_argument("--customer-id", required=True,
                        help="客户 slug（文件名用）")
    parser.add_argument("--customer-name", required=True,
                        help="客户显示名（喂给 LLM）")
    parser.add_argument("--meeting-date", required=True,
                        help="YYYY-MM-DD 或 ISO8601")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="跳过 Ingest 阶段（wiki 已生成，只想重跑 Extract）")
    parser.add_argument("--skip-extract", action="store_true",
                        help="跳过 Extract 阶段（只跑 Stage 0）")
    args = parser.parse_args()

    try:
        if args.raw_file:
            raw_path, raw_text = load_raw_file(
                args.raw_file, args.customer_id, args.meeting_date,
            )
        else:
            doc_id = parse_doc_id(args.doc)
            raw_path, raw_text = fetch_via_app(
                doc_id, args.customer_id, args.meeting_date,
            )
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[error] fetch failed: {e}", file=sys.stderr)
        return 3

    if not args.skip_ingest:
        try:
            await run_ingest_agent(
                customer_id=args.customer_id,
                customer_name=args.customer_name,
                raw_abs_path=str(raw_path),
                meeting_date=args.meeting_date,
                log_prefix=f"try {args.customer_id}",
            )
        except Exception as e:
            print(f"[error] ingest failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 4

    wiki_path = WIKI_DIR / "customers" / f"{args.customer_id}.md"
    print("\n" + "=" * 60)
    print(f"=== WIKI RESULT: {wiki_path.relative_to(PROJECT_ROOT)}")
    print("=" * 60)
    if wiki_path.exists():
        print(wiki_path.read_text(encoding="utf-8"))
    else:
        print(f"(no file at {wiki_path}; agent 可能没写成功)")

    # ----- Stage 1: Extract -------------------------------------------------
    if args.skip_extract:
        print("\n[extract] skipped by --skip-extract")
        return 0

    wiki_text = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""
    try:
        extract_result = run_extract(wiki_text, raw_text, log_prefix=f"try {args.customer_id}")
    except Exception as e:
        print(f"[error] extract failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 5

    print("\n" + "=" * 60)
    print("=== EXTRACT RESULT (structured JSON) ===")
    print("=" * 60)
    print(json.dumps(extract_result, ensure_ascii=False, indent=2))

    # 保存一份 JSON 到 raw 旁，方便人工比对
    extract_path = raw_path.with_suffix(".extract.json")
    extract_path.write_text(
        json.dumps(extract_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[extract] saved to {extract_path.relative_to(PROJECT_ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
