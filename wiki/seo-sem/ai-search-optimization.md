# AI 搜索优化 (ASO)

> Sources: 邓鑫, 张心如(Janice Zhang), 2026-04
> Raw: [视频会议](../../raw/meetings/2026-04-02-视频会议.md)
> Updated: 2026-04-16

## 背景

传统 SEO 针对 Google 搜索引擎优化，但 AI 搜索工具（ChatGPT、Claude、Perplexity 等）正在改变用户搜索行为。需要同时针对 Google 和 AI agent 进行优化。

## AI Agent 优化 vs 传统 SEO

| 维度 | 传统 SEO | ASO |
|---|---|---|
| 目标 | Google 爬虫 | AI agent（ChatGPT、Claude、Perplexity） |
| 内容形式 | 关键词密度、标题标签 | 结构化数据、FAQ schema |
| 发现渠道 | Google 索引 | Wikipedia/Wikidata、专业网站引用 |
| 交互方式 | 用户点击链接 | Agent 直接提取信息、完成交易 |

## 四类关键词策略

1. **纯产品词** — 产品名称、型号
2. **品牌词** — 公司/品牌名称
3. **产品+业务描述词** — 产品应用场景组合
4. **对比类词** — "A vs B"、替代品比较

## 技术实现要求

- robots.txt 允许所有 AI 爬虫访问
- 向所有 AI 搜索工具提交 sitemap（Brave、Perplexity 等）
- 实现 schema.org 结构化数据
- 添加 FAQ schema 标记
- 创建 API 端点供 agent 交互
- 单独追踪 AI agent 流量（与 Google Analytics 分开）

## Agentic 电商

讨论了让 AI agent 直接完成交易的可能性：

- 实现 "book demo"、"get quote"、"contact us" 能力
- 产品 API + WhatsApp 集成
- 多语言支持
- Agent 可直接为用户下单、询价

## 内容分发策略

不再局限于网站内部内容，扩展到多渠道：

- 专业行业网站发布 PR 内容
- 利用展会（广交会等）产出权威内容
- TikTok、YouTube、微博、LinkedIn 同步分发
- Wikipedia/Wikidata 引用建立专业权威

## See Also

- [SEO 技术优化](seo-technical-optimization.md)
- [网站基础设施问题](../website-infra/website-infrastructure-issues.md)
