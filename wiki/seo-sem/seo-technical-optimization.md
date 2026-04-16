# SEO 技术优化

> Sources: 邓鑫, 王琦琦(Patti Wang), 顾波(Byron), 2026-03; 邓鑫, 庞云召, 王琦琦, 2026-03
> Raw: [SEO事情讨论](../../raw/meetings/2026-03-16-seo事情讨论.md); [SEM SEO讨论](../../raw/meetings/2026-03-17-sem_seo讨论.md); [SEO和SEM的事情](../../raw/meetings/2026-03-21-seo和sem的事情.md)
> Updated: 2026-04-16

## 核心问题

网站存在多个 SEO 基础配置问题，严重影响搜索引擎索引效果：

### Sitemap

- 2025 年首次提交 sitemap 即失败（无法抓取，0 索引）
- 2026 年 1-2 月多次提交仍然报错，原因是 PHP 格式不被识别
- 最终转为 XML 格式后成功提交，但覆盖不足（49 vs 93 页被索引）
- robots.txt 中缺少 sitemap 引用

### 静态页面 vs 动态页面

- 网站使用动态页面，搜索引擎无法正确解析
- 需要转换为静态页面以支持 Google 索引
- 当前共识别出 6 类页面结构

### 多语言

- 70 个页面仅有中文版本，缺少英文版（面向海外市场致命）
- 缺少 hreflang 标签声明多语言关系
- 中英文页面之间没有正确的语言关联

### 网站迁移

- 从旧网站迁移到新网站时未实现 301 重定向
- 导致 Google 流量损失约 80%
- 已删除的页面丧失了 SEO 排名（页面不应被删除）

## 优化优先级

1. 修复 robots.txt，添加 sitemap 配置
2. 提交优化后的 sitemap 到 Google API
3. 静态页面转换
4. 实现 hreflang 标签
5. 将 70 个中文页面翻译为英文
6. 修复网站模板和移动端适配

## 内容生成计划

- 使用 AI 批量生成关键词对应内容
- 目标：首周 20 页，后续每周 100-200 页，最终达到 5000+ 页
- 团队仅负责审核 AI 生成内容
- 自动提交到 CMS 并更新 sitemap
- SEO 效果预计 2 个月后显现

## See Also

- [SEM 广告投放策略](sem-advertising-strategy.md)
- [AI 搜索优化](ai-search-optimization.md)
- [网站基础设施问题](../website-infra/website-infrastructure-issues.md)
