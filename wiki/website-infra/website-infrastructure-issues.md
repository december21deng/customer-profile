# 网站基础设施问题

> Sources: 邓鑫, 张心如(Janice Zhang), 2026-04; 邓鑫, 王琦琦(Patti Wang), 顾波(Byron), 2026-03
> Raw: [视频会议](../../raw/meetings/2026-04-02-视频会议.md); [SEO事情讨论](../../raw/meetings/2026-03-16-seo事情讨论.md)
> Updated: 2026-04-16

## 当前问题

### 技术债务

- 多个公司网站存在严重技术债务
- 某网站曾受恶意软件攻击
- 代码未纳入 Git 版本控制
- 内网访问需通过跳板机，限制了开发效率

### CMS 限制

- 当前 CMS 不提供 API 接口
- 无法支持现代集成需求（AI agent 交互、自动内容发布）
- 需要重建为支持 API 的现代 CMS

### 迁移问题

- 网站迁移时未实现 301 重定向 → 流量损失 80%
- 一个站点从 475 页降至 110+ 页
- 删除旧页面导致 SEO 排名永久丧失

## 重建方向

1. 使用现代 CMS 重建，支持 API 层
2. 获取完整代码和数据库访问权限
3. 实现 API 端点支持现代集成
4. 产品 API 端点支持 agent 交易
5. 实现 schema.org 结构化数据
6. 正确的版本控制（Git）

## AI 工具讨论

会议中讨论了开发工具选型：

- **Claude CLI** — 更好的 agentic 框架，本地 MCP 支持，流程控制能力强
- **OpenClaw** — 更大的生态系统，更多社区 skill
- 两者的比较和适用场景

## See Also

- [SEO 技术优化](../seo-sem/seo-technical-optimization.md)
- [AI 搜索优化](../seo-sem/ai-search-optimization.md)
