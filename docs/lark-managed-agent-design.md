# 飞书多角色 AI 助手 — Claude Managed Agents 设计文档

## 1. 项目概述

面向团队不同角色（Marketing / Sales / 董事长 / 部门负责人）的 AI 助手系统。每个角色有独立的飞书 Bot 和专属 Agent，Agent 拥有该角色所需的所有工具（MCP）。基于 Claude Managed Agents，Agent 运行时由 Anthropic 托管，我们只维护 Bot 网关和共享基础设施。

### 核心原则

- **1 Bot = 1 Agent = 1 角色** — 不同角色用不同 Bot，进入不同 Agent
- **Agent 自带多工具** — 一个 Agent 可调多个 MCP，无需跨 Agent 编排
- **配置即扩展** — 新增角色只需配置新 Agent + Bot，不改代码
- **两层存储** — Supabase 存事实（结构化），LLM Wiki 存理解（叙事+关系）

### 三个核心业务场景

| 场景 | 入口 | 输出 |
|---|---|---|
| 1. 录入客户进展 | 卡片表单 + 妙记链接 | 写入 Supabase + 更新客户 Wiki 页面 |
| 2. 查询客户信息 | 话题群/私聊提问 | 卡片回复（基本信息、进展、风险） |
| 3. 浏览/搜索客户 | H5 页面（飞书内打开） | 客户列表 + 详情 + 客户旅程 |

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│  飞书客户端                                                    │
│  ├─ Marketing Bot / Sales Bot / Chairman Bot / Dept Head Bot │
│  └─ H5 页面（在飞书内打开，用于场景 3）                           │
└────────────┬───────────────────────────────┬─────────────────┘
             │ webhook                        │ REST/HTTP
             ▼                               ▼
┌──────────────────────────────┐  ┌────────────────────────────┐
│  Render Web Service          │  │  Render Static Site        │
│  Bot 网关                     │  │  H5 前端（Vite + React）    │
│  ├─ 根据 App ID 路由到 Agent   │  │                            │
│  ├─ Thread → Session 管理    │  │  读 Supabase + Wiki MCP     │
│  └─ 卡片流式更新              │  │                            │
└───────┬───────────────┬──────┘  └──────────┬─────────────────┘
        │               │                    │
        ▼               ▼                    ▼
┌───────────────┐  ┌────────────────────────────────────────────┐
│  Anthropic    │  │  Supabase（托管）                           │
│  Managed      │  │  ├─ customers / contacts                   │
│  Agents       │  │  ├─ progress（进展，含妙记链接）              │
│               │  │  └─ risks                                  │
│  Sales Agent  │  │  用于：H5 列表、精准 SQL 查询、统计            │
│  Marketing    │  └────────────────────────────────────────────┘
│  Agent        │
│  Chairman     │  ┌────────────────────────────────────────────┐
│  Agent ...    │  │  Wiki MCP Server（Render Web Service）      │
│               │─▶│  ├─ 读写 /data/wiki/*.md                   │
└───────┬───────┘  │  └─ HTTP API 给 H5 和 Agent 用               │
        │          │                                            │
        │          │  Render 持久磁盘                             │
        │          │  /data/wiki/                                │
        │          │  ├─ customers/XYZ.md（每客户一页，含链接）    │
        │          │  ├─ contacts/Alice.md                      │
        │          │  ├─ risks/budget-limited.md                │
        │          │  └─ sop/identify-decision-chain.md         │
        │          │  用于：Agent 理解客户全貌、Wiki 语义搜索        │
        │          └────────────────────────────────────────────┘
        ▼
┌─────────────────────────────────────────────────┐
│  共享 MCP 工具池（远程 HTTP）                     │
│  ├─ 妙记 MCP（拉 transcript）                     │
│  ├─ Supabase MCP（结构化数据 CRUD）                │
│  ├─ Wiki MCP（读写 md 文件）                       │
│  ├─ 飞书 MCP（发消息、云文档、日历）                 │
│  ├─ Clay MCP / Google Ads MCP / PostHog MCP      │
│  └─ 产品库 MCP                                   │
└─────────────────────────────────────────────────┘
```

### 部署边界

| 组件 | 部署方 | 成本 |
|---|---|---|
| Agent 运行时 | Anthropic | 按 token 计费 |
| Bot 网关 | Render Web Service | 免费/$7 |
| H5 前端 | Render Static Site | 免费 |
| Wiki MCP + 持久磁盘 | Render Web Service + Disk | $7 + $1 |
| Supabase 数据库 | Supabase 托管 | 免费（500MB） |
| 其他 MCP Servers | Render / 按需 | 按需 |

---

## 3. 多角色 Bot-Agent 映射

### Agent 定义

每个 Agent 在 Anthropic 后台独立创建，配置：
- **system_prompt** — 该角色的职责、语气、边界
- **mcp_servers** — 允许访问的 MCP 工具列表
- **model** — Opus 4.6（复杂分析）或 Sonnet 4.6（轻任务）

### 角色配置

| Bot / Agent | system_prompt 要点 | MCP 工具集 |
|---|---|---|
| **Sales Agent** | 客户跟进、竞品情报、销售方法论 | 妙记、Supabase、Wiki、Clay、产品库、飞书 |
| **Marketing Agent** | 广告分析、用户行为、内容营销 | Google Ads、PostHog、Supabase、Wiki、飞书 |
| **Chairman Agent** | 高管视角、跨部门汇总 | 所有只读 MCP、Wiki、飞书 |
| **Dept Head Agent** | 部门协作、任务分配 | 部门专属工具、飞书 |

### Bot 网关路由

```
消息到达 → 根据 App ID 识别 Bot → 查配置拿 Agent ID → 调用对应 Agent
```

新增角色 = 加一行配置 + 创建 Agent，不改代码。

---

## 4. 两层存储设计（核心）

### 分工

| | Supabase（结构化） | LLM Wiki（叙事） |
|---|---|---|
| **角色** | 真相源（source of truth） | 理解层（understanding layer） |
| **形态** | 数据库表 + 字段 | markdown 文件 + 交叉引用 |
| **写入** | 每次进展直接写入 | 从 Supabase 派生生成 |
| **查询** | SQL / REST | 文件读取 + grep + 语义搜索 |
| **用于** | H5 列表筛选、Bot 精准查询、统计 | Agent 理解客户全貌、发现关联 |

### Supabase 表结构

**customers**
- id, 名称, 行业, 规模, 官网, 创建时间, owner（飞书 ID）, stage, tags

**contacts**
- id, customer_id, 姓名, 职位, 邮箱, 电话

**progress**（进展记录，核心）
- id, customer_id, contact_id, 发生时间
- 妙记链接, transcript_id, 来源类型
- LLM 提取字段：summary, stage_change, risks, next_actions, amount
- 原始内容（备份）
- 录入人

**risks**
- id, customer_id, level, description, detected_at, resolved

### LLM Wiki 结构（Karpathy 风格）

**文件形态，部署在 Render 持久磁盘：**

```
/data/wiki/
├─ index.md                                # 总索引
├─ customers/
│   ├─ XYZ.md                              # 每客户一页
│   └─ ABC.md
├─ contacts/
│   ├─ alice.md
│   └─ bob.md
├─ risks/
│   ├─ budget-limited.md                   # 风险类型沉淀
│   └─ decision-unclear.md
├─ sop/
│   ├─ identify-decision-chain.md          # SOP 和方法论
│   └─ handle-budget-objection.md
├─ industries/
│   └─ saas.md                             # 行业知识
└─ products/
    └─ realtime-sync.md                    # 产品知识
```

### 单个 Wiki 页面示例（customers/XYZ.md）

```markdown
---
entity: customer
name: XYZ
industry: SaaS
stage: 深入沟通
amount: 200K
owner: kevin
key_contacts: [Alice, Bob]
related_customers: [ABC, DEF]
linked_risks: [budget-limited, decision-unclear]
updated_at: 2026-04-17
---

# 客户 XYZ

## 基本信息
SaaS 行业，200 人规模，北京...

## 决策链
- CEO [[contacts/alice|Alice]]
- CTO [[contacts/bob|Bob]]
- 采购 Carol（待确认）

## 客户旅程
### 2026-04-10 技术演示
CTO Bob 表示架构契合，关注 [[products/realtime-sync]]

### 2026-04-15 需求对齐
Alice 提出预算约束，见 [[risks/budget-limited]]

## 相关案例
- [[customers/ABC]] 同行业，通过分期方案签单
- [[customers/DEF]] 类似规模，参考报价模型

## 下一步
1. 联系 Carol 明确采购流程（参考 [[sop/identify-decision-chain]]）
2. 准备分期方案
```

### 为什么 Supabase 存表 + Wiki 存 md（两者都要）

- **Supabase 强**在：精准查询、H5 列表筛选、数据关联
- **Wiki 强**在：LLM 读懂客户全貌、跨实体语义关联、知识沉淀
- **两者互补**，不能互相替代

---

## 5. Wiki 部署方案

### 方案：Render 持久磁盘 + Wiki MCP

```
┌──────────────────────────────────────────────┐
│  Render Web Service: wiki-mcp                │
│  ├─ 读写 /data/wiki/*.md                     │
│  ├─ HTTP API（给 H5 用）                     │
│  └─ MCP 接口（给 Agent 用）                  │
│                                              │
│  挂载：Render Persistent Disk $1/月          │
│  /data/wiki/（所有 md 文件）                  │
└──────────────────────────────────────────────┘
```

**为什么不放 Managed Agents Environment：**
- 每个 Agent 独立 Environment，无法跨 Agent 共享
- H5 前端访问不到 Environment
- 不能让团队直接查看

**为什么不用 GitHub：**
- Phase 1 不需要版本历史，简化部署
- 隐私考虑：数据不上代码仓库
- Phase 2 可以再加 Git 作为备份

### Wiki MCP 提供的能力

```
Agent 和 H5 都通过 Wiki MCP 访问：

读:
  - get_wiki(slug)           → 返回 md 内容
  - list_wiki(entity_type)   → 列出某类所有页面
  - search_wiki(query)       → 语义搜索
  - get_linked(entity)       → 跟 X 相关联的所有页面

写:
  - write_wiki(slug, content)
  - regenerate_customer(customer_id)
    → 读 Supabase 该客户全部数据
    → LLM 生成新 md
    → 覆盖 /data/wiki/customers/{name}.md
```

### Wiki 的数据流向

```
场景 1 录入新 progress
  │
  ├─ Supabase 写入结构化数据（真相源）
  └─ 触发 Wiki MCP: regenerate_customer(customer_id)
     └─ 读 Supabase → LLM 生成 → 写 /data/wiki/customers/XYZ.md

场景 2 Agent 查询
  │
  └─ Agent 调用 Wiki MCP: get_wiki("customers/XYZ")
     └─ 得到完整 md（含链接到 contacts、risks、sop）

场景 3 H5 浏览
  │
  ├─ 列表/筛选：直接查 Supabase（快）
  └─ 客户详情：调 Wiki MCP HTTP API → 渲染 md
```

---

## 6. 三个场景的详细流程

### 场景 1：录入客户进展

```
用户（Sales Bot）触发卡片表单
  │
  │ 字段：客户、联系人、妙记链接、进展类型、备注
  ▼
提交 → Bot 网关 → Sales Agent
  │
  │ Agent 依次调用：
  ├─ 妙记 MCP：拉 transcript
  ├─ LLM 提取：summary / stage / risks / next_actions / amount
  ├─ Supabase MCP：写入 progress 表
  ├─ Wiki MCP: regenerate_customer → 更新 customers/XYZ.md
  └─ 返回"已录入"卡片
```

**关键点：**
- 提取失败时保留原始内容，标记 needs_review
- H5 支持人工修正
- Wiki 重生成是异步的，不阻塞回复

### 场景 2：查询客户信息

```
用户（Sales Bot）："客户 XYZ 最近怎么样？"
  │
  ▼
Bot 网关 → Sales Agent
  │
  │ Agent 按需调用：
  ├─ Wiki MCP: get_wiki("customers/XYZ")   ← 首选，全貌+关系
  ├─ Supabase MCP：补充最新数据（可选）
  └─ 返回 JSON 卡片（基本信息 + 进展 + 风险 + 下一步）
```

**为什么 Agent 优先读 Wiki：**
- Wiki 已经是"故事"，LLM 理解快
- 自带交叉链接，能顺藤摸瓜找关联
- 减少多次 SQL 查询拼数据的成本

### 场景 3：H5 页面浏览

```
用户点击 Bot 消息链接 / 飞书应用菜单
  │
  ▼
H5 页面（Render Static Site）
  │
  │ 飞书 JSAPI 拿 user_id（免登）
  │
  ├─ 客户列表：查 Supabase REST API（速度快，适合筛选排序）
  └─ 客户详情：
     ├─ 基础信息 + 进展时间线：查 Supabase
     └─ 完整档案：调 Wiki MCP HTTP API → 渲染 markdown
```

**数据修正：**
- 用户在 H5 编辑字段 → 写 Supabase
- 触发 Wiki MCP 重生成该客户 wiki

---

## 7. Session 管理

### 映射

| 入口 | Session 归属 |
|---|---|
| 话题群 thread | 一个 thread = 一个 session |
| 私聊 | 每个用户一个 session |

不同 Bot / Agent 的 session 完全隔离。

### 生命周期

- **不主动过期** — 用到报错再新建
- Managed Agents 内置 L1/L2 自动管理上下文
- Session not_found → 新建并更新 thread→session 映射

### 并发控制

- 同一 thread 内消息串行（asyncio.Lock）
- 不同 thread 并行

---

## 8. Bot 网关职责

**只做最薄的转发层：**

1. 验证飞书回调签名
2. 根据 App ID 路由到对应 Agent
3. 维护 thread/chat → session 映射（存 Supabase）
4. 消息串行化
5. 流式卡片更新（收 Agent 流 → PATCH 卡片）
6. 错误降级

**不做：LLM 调用、业务逻辑、数据转换、权限控制**（全交给 Agent + MCP + Supabase RLS）

---

## 9. MCP 工具池

| MCP | 功能 | 用户 |
|---|---|---|
| 飞书 MCP | 发消息、查文档、查日历 | 所有 Agent |
| Supabase MCP | 读写结构化表 | Sales / Marketing / Chairman |
| Wiki MCP | 读写 md 文件、语义搜索 | 所有 Agent + H5 |
| 妙记 MCP | 拉 transcript | Sales / Marketing |
| Clay MCP | 第三方数据丰富 | Sales |
| Google Ads MCP | 广告数据 | Marketing |
| PostHog MCP | 用户行为 | Marketing |
| 产品库 MCP | 产品规格 | Sales |

**所有 MCP 必须是远程 HTTP 服务**（Managed Agents 运行在 Anthropic 云端，访问不到本地）。

---

## 10. 飞书接入

### Bot 配置（每个角色一次）

1. 开放平台创建自建应用
2. 开通机器人能力
3. 申请权限（接收消息、发送消息、更新卡片）
4. 配置 Event Subscription 回调 → Render 网关
5. 发布审批
6. 添加到话题群

### H5 接入

1. 应用 → 添加"网页应用"能力
2. 主页 URL → Render Static Site
3. 配置应用底部菜单（客户列表、搜索）
4. Bot 卡片中放跳转链接

### JSON 卡片

- 所有 Bot 回复用 JSON 交互卡片（支持 PATCH 更新、按钮、表单）
- 30KB 上限

---

## 11. 错误处理

| 错误 | 处理 |
|---|---|
| Anthropic 429/5xx | 指数退避重试 |
| Session 失效 | 新建并重试 |
| MCP 调用失败 | Agent 选择降级回答 |
| 妙记限流 | 队列化 + 重试 |
| LLM 提取失败 | 保存原始内容 + needs_review，提示人工修正 |
| Wiki 生成失败 | 保留旧版本，后台重试 |
| 飞书卡片失败 | 降级为纯文本 |

---

## 12. 部署拓扑

```
Render
├─ lark-bot-gateway        （Web Service）
├─ lark-frontend           （Static Site）
├─ wiki-mcp + 持久磁盘       （Web Service + Disk $1）
├─ mcp-lark                （Web Service）
└─ mcp-supabase            （可选）

外部依赖
├─ Anthropic Managed Agents
├─ Supabase（结构化数据）
├─ 飞书开放平台
└─ 第三方 API（Google Ads / PostHog / Clay / 妙记）
```

---

## 13. 成本估算（Sales Bot 单角色，10 人团队）

| 项目 | 月成本 |
|---|---|
| Render（bot + frontend + wiki + mcps） | $15-30 |
| Render Disk（wiki 持久化） | $1 |
| Supabase | $0（免费额度） |
| Anthropic Opus 4.6 | $500-1500 |
| Anthropic Sonnet 4.6（替代） | $100-300 |

**优化建议：**
- 场景 1 提取用 Sonnet，场景 2 分析用 Opus
- Wiki 重生成用 Sonnet

---

## 14. 实施阶段

### Phase 1（MVP）：Sales Agent 跑通全链路

- [ ] Supabase schema 建立（customers / contacts / progress / risks）
- [ ] Sales Bot 接入，路由到 Sales Agent
- [ ] Wiki MCP 部署（Render + Disk）
- [ ] Sales Agent 配置（Supabase + 妙记 + Wiki MCP）
- [ ] 场景 1：录入进展 + 自动生成 customer wiki
- [ ] 场景 2：Agent 读 wiki 回答
- [ ] 场景 3：H5 客户列表 + 详情（读 Supabase + wiki）

### Phase 2：扩展

- [ ] Marketing Bot + Agent（Google Ads / PostHog MCP）
- [ ] Chairman Bot + Agent（跨部门汇总）
- [ ] H5 完善：搜索、筛选、编辑、客户旅程可视化
- [ ] Wiki 跨实体沉淀（SOP、风险、方法论）

### Phase 3：高级

- [ ] Wiki Git 备份（Private repo）
- [ ] 多 Agent 协作（Orchestrator 模式）
- [ ] 离线任务（定时风险扫描）
- [ ] 细粒度权限（部门/角色过滤）

---

## 15. 关键设计决策汇总

| 决策点 | 选择 | 理由 |
|---|---|---|
| Bot-Agent 映射 | 1:1 按角色 | 简单、权限清晰 |
| 结构化数据 | Supabase Postgres | 查询强、免费额度够、H5 直连 |
| Wiki 形态 | Karpathy llm-wiki（md 文件 + 交叉引用） | 符合 LLM 阅读习惯 |
| Wiki 存储 | Render 持久磁盘 + Wiki MCP | 简单、数据完全私有 |
| Wiki 共享 | 所有 Agent 通过 MCP 访问同一份 | 多 Agent 一致性 |
| H5 框架 | Vite + React | 轻量、打包快 |
| H5 认证 | 飞书 JSAPI 免登 | 无感体验 |
| 工具接入 | MCP（远程 HTTP） | 标准协议、跨 Agent 共享 |
| 本地 MCP | 不支持 | Managed Agents 在云端 |
| Session 策略 | 不主动过期，失败新建 | 简单可靠 |
| Context 管理 | Managed Agents 内置 L1/L2 | 零成本 |
| 模型 | Opus 4.6（主）+ Sonnet 4.6（提取） | 效果/成本平衡 |

---

## 16. 未决问题

1. 飞书免登实现细节（JSAPI → Supabase JWT）
2. 客户去重/合并逻辑
3. 提取 schema 版本化（字段调整时如何迁移）
4. 团队数据隔离（多公司/多团队）
5. H5 编辑权限（谁能改客户数据）
6. Wiki 页面何时全量重生成（每次进展？每日一次？）

这些留到 Phase 1 跑起来后根据实际使用反馈决定。
