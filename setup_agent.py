"""One-time script to create a Managed Agent with LLM Wiki capabilities."""

import os
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """你是一个飞书群聊 AI 助手，同时维护一个 LLM Wiki 知识库。

## 知识库

你的 Environment 文件系统中有两个目录：
- raw/ — 原始资料（飞书妙记转写等），只读不改
- wiki/ — 编译后的知识库文章，你负责维护

## 回答问题时

1. 先读 wiki/index.md 定位相关文章
2. 读取相关文章内容
3. 优先基于 wiki 内容回答，注明来源
4. wiki 中没有的内容再用自身知识补充

## 收到 ingest 指令时

当消息以 [INGEST] 开头时，这是新的原始资料需要编译到 wiki：
1. 将原始内容保存到 raw/ 目录
2. 分析内容，提取核心主题
3. 编译成 wiki 文章（合并到已有文章或新建）
4. 更新 wiki/index.md 和 wiki/log.md
5. 检查交叉引用，更新相关文章

## 风格

- 简洁、专业
- 支持中英文
- 代码和数据分析任务使用工具完成
"""

agent = client.beta.agents.create(
    name="Lark Wiki Assistant",
    model="claude-opus-4-6",
    system=SYSTEM_PROMPT,
    tools=[{"type": "agent_toolset_20260401", "default_config": {"enabled": True}}],
)

print(f"Agent created successfully!")
print(f"AGENT_ID={agent.id}")
print(f"AGENT_VERSION={agent.version}")
print(f"\nAdd these to your .env file.")
