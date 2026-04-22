"""Claude Agent SDK wrapper with per-thread session resumption."""

import logging

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk._errors import ProcessError

from src import session_store
from src.config import WIKI_DIR

logger = logging.getLogger(__name__)

INGEST_PROMPT = """你是知识库维护者。用户给你一份飞书会议纪要文档（标题+正文），你要把信息整理进 wiki。

当前工作目录就是 wiki 根目录。结构：
  customers/<客户名>.md   — 每个客户一页
  contacts/<姓名>-<客户>.md  — 重要联系人（可选）

流程：
1. Glob/Grep 找相关客户页面（客户名归一："字节" → "字节跳动"）
2. 如果页面不存在，Write 新建；存在则 Read 后 Edit
3. 提取：客户、关键联系人、进展、待办
4. 时间线只追加，不删除；事实冲突标注日期

新客户页面模板：
# <客户名>

## 基本信息
<行业/规模/背景>

## 关键联系人
- <姓名>（职位）— <特点>

## 进展时间线
- <YYYY-MM-DD> <事件>

## 待办
- [ ] <事项>

回复简洁：说明更新了哪个文件、追加了什么要点。"""

QUERY_PROMPT = """你是知识助手。用户会问你客户、联系人、进展相关的问题。

当前工作目录是 wiki 根目录，里面有 customers/、contacts/ 等子目录。

流程：
1. Glob/Grep 定位相关文件
2. Read 出来
3. 基于文件内容回答，不编造
4. wiki 里没有的，明确说"wiki 里没有相关记录"

回答简洁，用 markdown。"""


async def send_and_stream(thread_id: str, message: str, mode: str = "query"):
    """Send a user message through Claude Agent SDK; yield response text chunks.

    mode: "query" (read-only wiki) or "ingest" (write to wiki)
    """
    session_id = await session_store.get_session_id(thread_id)

    if mode == "ingest":
        system_prompt = INGEST_PROMPT
        tools = ["Read", "Write", "Edit", "Grep", "Glob"]
    else:
        system_prompt = QUERY_PROMPT
        tools = ["Read", "Grep", "Glob"]

    def build_options(resume_id: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            model="claude-sonnet-4-6",
            allowed_tools=tools,
            resume=resume_id,
            cwd=str(WIKI_DIR),
        )

    new_session_id: str | None = None

    async def run(opts: ClaudeAgentOptions):
        nonlocal new_session_id
        async for msg in query(prompt=message, options=opts):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                new_session_id = msg.data.get("session_id")
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        yield block.text
            elif isinstance(msg, ResultMessage):
                logger.info(
                    "Agent done thread=%s mode=%s stop_reason=%s",
                    thread_id, mode, msg.stop_reason,
                )

    try:
        async for chunk in run(build_options(session_id)):
            yield chunk
    except ProcessError:
        # Most commonly a stale session_id (cwd changed, session expired, etc).
        # ProcessError only says "exit code 1"; the actual "No conversation found"
        # is on stderr which we can't easily inspect. If we had a session_id, retry
        # once without resume.
        if session_id:
            logger.warning("Session %s failed, retrying with new session", session_id)
            await session_store.save_session_id(thread_id, "")  # clear stale
            async for chunk in run(build_options(None)):
                yield chunk
        else:
            raise

    if new_session_id:
        await session_store.save_session_id(thread_id, new_session_id)
