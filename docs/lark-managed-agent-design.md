# 飞书群聊 AI 助手 — Claude Managed Agents 设计文档

## 1. 项目概述

在飞书群聊中接入 AI 助手，团队成员在话题（Thread）中 @bot 即可与 Claude 对话。基于 Claude Managed Agents API，Anthropic 托管 agent 运行时，我们只开发 Bot 网关。

### 使用方式

```
飞书话题群 → 话题（Thread）
┌─────────────────────────────────────┐
│  用户 A: 帮我分析这个数据            │
│  Bot: [JSON 卡片] 正在分析...        │
│  用户 B: 再加个按月分组的图表        │
│  Bot: [JSON 卡片] 已生成图表...      │
│  用户 A: 导出为 CSV                 │
│  Bot: [JSON 卡片] 已保存...         │
└─────────────────────────────────────┘
```

### 核心规则

- 仅在话题群中使用，普通群不可用（Bot 只加入话题群）
- 话题群中所有消息都触发，不需要 @bot
- 一个话题（Thread） = 一个 session，多人共用，不做用户隔离
- 使用 Claude Opus 4.6（1M context）

---

## 2. 架构

```
飞书用户
  │
  ▼
飞书 Bot 网关（我们部署）
  ├── 消息接收（飞书 Event Subscription）
  ├── Thread → Session 路由
  ├── 消息队列（per-session 串行）
  └── 错误处理 / 重试
  │
  ▼
Claude Managed Agents API（Anthropic 托管）
  ├── Agent（Opus 4.6 + 内置工具集）
  ├── Environment（per-thread 容器沙箱）
  ├── Session（per-thread 对话）
  └── MCP Servers（远程工具）
```

### 部署边界

| 组件 | 部署方 | 说明 |
|---|---|---|
| Agent 运行时（容器、代码执行、文件系统） | Anthropic | 零运维 |
| Bot 网关（接收飞书消息、转发 API） | 我们 | 轻量 Python 服务 |
| 远程 MCP Server（可选） | 我们 | 飞书 API 包装等 |

---

## 3. 数据流

```
1. 用户在飞书话题群的 thread 中发消息
2. 飞书推送事件到 Bot 网关（Event Subscription）
3. 网关检查：是否话题群？是否在 thread 中？否则忽略
4. 注入当前用户的偏好（从网关侧 DB 读取）
5. 查找该 thread 绑定的 session（无则新建 session + environment）
6. 消息入队（同一 thread 串行处理）
7. 调用 Managed Agents API 发送 user event + 流式接收响应
8. 通过飞书 JSON 卡片回复到 thread 中（先发卡片，后续 PATCH 更新内容）
```

---

## 4. Session 管理

### 映射关系

一个飞书 thread = 一个 Managed Agents session + 一个 environment。

| 飞书概念 | Managed Agents 概念 |
|---|---|
| Thread（话题） | Session + Environment |
| Thread 内的消息 | Session Event |
| 不同 Thread | 完全隔离的 Session |

### 生命周期

```
首次发消息 ──→ 新建 environment + session + 绑定 thread
               ──→ thread 内持续追加消息
               ──→ 用户发消息时，网关尝试用已绑定的 session
               ──→ 如果 API 报错（session 不可用）→ 新建 session 重新绑定
```

网关不主动管理 session 过期。策略是"用到报错再新建"，保持简单。

### 并发控制

- 同一 thread 内消息严格串行（Managed Agents 不支持并发写同一 session）
- 不同 thread 可并行
- 实现方式：per-thread asyncio.Lock 或消息队列

---

## 5. Context 管理

基于 [Anthropic 官方推荐方案](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：

| 层 | 策略 | 谁负责 | 说明 |
|---|---|---|---|
| L1 | Tool Result Clearing | Managed Agents 内置 | 自动清除旧工具结果 |
| L2 | Compaction | Managed Agents 内置 | 对话接近上限时自动压缩 |

L1 和 L2 完全由 Managed Agents 处理，网关不需要干预。

### 用户偏好（后续迭代）

简单版不做用户偏好。后续如果要加，方向是让 agent 从对话中自动学习用户偏好并记录，用户不需要做任何额外操作。参考 OpenClaw 的 USER.md 方案和 Mem0。

---

## 6. 飞书 Bot 接入

### 前置准备

1. 在飞书开放平台创建企业自建应用，获取 App ID 和 App Secret
2. 开通 Bot 能力，配置 Event Subscription 回调地址（指向我们的网关）
3. 申请权限：接收群消息、发送消息、更新消息卡片、获取群信息
4. 配置事件订阅：订阅 `im.message.receive_v1`（接收消息）
5. 飞书后台发布应用，管理员审批
6. 将 Bot 添加到目标话题群

### 回调验证

飞书首次配置回调 URL 时会发送 verification 请求，网关需返回 challenge 值完成验证。后续每条事件推送都带签名，网关需用 Verification Token 校验防伪造。

### 凭证管理

| 凭证 | 用途 | 存储 |
|---|---|---|
| App ID / App Secret | 获取 tenant_access_token | 环境变量 |
| Verification Token | 验证飞书回调签名 | 环境变量 |
| Encrypt Key（可选） | 解密飞书加密推送 | 环境变量 |
| Anthropic API Key | 调用 Managed Agents API | 环境变量 |

---

## 7. MCP 工具接入

Managed Agents 支持远程 MCP，在 Agent 定义中配置 URL 即可。

| MCP 类型 | 支持 | 说明 |
|---|---|---|
| 远程 MCP | 支持 | Agent 定义中配 `mcp_servers` URL |
| 本地 MCP | 不支持 | Agent 运行在 Anthropic 云端，访问不到本地。需包装为远程服务 |

### 飞书 MCP Server

Agent 不仅被动接收消息，还可以主动调用飞书 API。通过部署一个飞书 MCP Server，让 agent 具备以下能力：

| 能力 | 飞书 API | 场景举例 |
|---|---|---|
| 查文档 | 云文档 Open API | "帮我找一下上周的周报" |
| 查日历 | 日历 API | "我今天下午有什么会？" |
| 查群成员 | 群组 API | "这个群里有谁？" |
| 发消息 | 消息 API | "帮我通知一下 @张三" |
| 查审批 | 审批 API | "我有几个待审批的？" |

飞书 MCP Server 本质是一个 HTTP 服务，把飞书 Open API 包装成 MCP 协议。需要我们自己部署，用 Bot 的 tenant_access_token 调用飞书 API。

### 其他 MCP

同理可接入 GitHub、Jira、数据库查询等。凭证通过环境变量或 Vault 管理。

---

## 8. 飞书适配要点

| 项目 | 说明 |
|---|---|
| SDK | lark-oapi（飞书官方 Python SDK） |
| 事件订阅 | 接收 `im.message.receive_v1` 事件 |
| 话题群判断 | 检查 chat_type 是否为话题群，非话题群不响应 |
| Thread 判断 | 消息体中 `root_id` 不为空表示在 thread 中 |
| 回复方式 | JSON 模板卡片（interactive card），支持富文本样式 |
| 流式更新 | 先发 JSON 卡片（"思考中..."），后续通过 PATCH card_id 更新卡片内容 |
| 消息长度 | JSON 卡片约 30KB 上限 |

### JSON 卡片流式更新

飞书只有交互式 JSON 卡片支持发送后更新内容（通过 PATCH `card_id`）。流程：

1. Agent 开始响应 → 发送一张 JSON 卡片（内容为"思考中..."）
2. 流式接收 agent 响应 → 定期 PATCH 更新卡片内容（每 1-2s）
3. Agent 完成 → 最终 PATCH 为完整内容

普通文本消息不支持编辑，必须用 JSON 卡片。

---

## 9. 错误处理

| 错误 | 应对 |
|---|---|
| Anthropic 429 | 指数退避重试（最多 3 次） |
| Anthropic 5xx | 指数退避重试 |
| Session NotFound / 已归档 | 新建 session 绑定到同一 thread |
| Environment 被回收 | 新建 environment + session |
| 飞书消息发送失败 | 降级为纯文本回复 |
| 所有错误 | 最终给用户一个友好提示，不静默失败 |

---

## 10. 成本估算（Opus 4.6）

10 人团队，每人每天 10 次 thread 交互：

| 场景 | 月成本（估） |
|---|---|
| 轻度（简单问答，~5K tokens/次） | ~$550-1600 |
| 重度（代码生成/分析，~30K tokens/次） | ~$3200-9600 |

> 如成本敏感，可将模型改为 Sonnet 4.6（约 1/5 价格），或混合使用。

---

## 11. 部署

Bot 网关是一个轻量 Python 服务：

- 接收飞书 Webhook 事件
- 转发到 Managed Agents API
- 持久化 thread → session 映射（SQLite）
- 部署在任意能被飞书回调到的服务器上（云主机 / Docker）

Agent 运行时完全由 Anthropic 托管。

---

## 12. 后续扩展

| 功能 | 说明 |
|---|---|
| 个人私聊场景 | 1:1 私聊 + 话题检测 + USER.md 偏好 |
| Discord / Slack 适配 | 核心层复用，新增适配器 |
| 跨 thread 记忆 | 引入 Memory Tool，Environment 中维护 summary.md |
| 用量限制 | per-user 频率和 token 上限 |
| 文件上传 | 飞书附件 → Environment 文件系统 |
| 混合模型 | 简单任务用 Sonnet，复杂任务用 Opus |
