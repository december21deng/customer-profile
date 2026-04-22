"""End-to-end test: fetch docx via lark-cli, then ingest via Claude Agent SDK."""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from src import docx_client, agent_client, session_store

DOC_ID = "E3fFdZQI3ozWjMxKVe1cPsronTg"
URL = f"https://global-intco.feishu.cn/docx/{DOC_ID}"
THREAD_ID = "test_thread_upe"


async def main():
    await session_store.init_db()

    print(f"\n=== Step 1: fetch docx {DOC_ID} via lark-cli ===")
    doc, err = await asyncio.to_thread(docx_client.fetch_raw_content, DOC_ID, URL)
    if err:
        print(f"FETCH FAILED: {err}")
        sys.exit(1)
    print(f"title={doc.title!r}")
    print(f"content length={len(doc.raw_content)} chars")
    print(f"first 200 chars: {doc.raw_content[:200]!r}")

    print(f"\n=== Step 2: ingest via Claude Agent SDK (sonnet-4-6) ===")
    prompt_text = (
        f"下面是一份飞书会议纪要文档的内容，请整理到 wiki。\n\n"
        f"标题：{doc.title or '(无标题)'}\n"
        f"文档 ID：{doc.doc_id}\n"
        f"URL：{doc.url}\n\n"
        f"正文：\n{doc.raw_content}"
    )

    full = ""
    async for chunk in agent_client.send_and_stream(THREAD_ID, prompt_text, mode="ingest"):
        sys.stdout.write(chunk)
        sys.stdout.flush()
        full += chunk

    print(f"\n\n=== Done. full response length={len(full)} chars ===")


if __name__ == "__main__":
    asyncio.run(main())
