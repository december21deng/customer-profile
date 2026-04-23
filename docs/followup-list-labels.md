# 跟进记录列表：meeting_title + progress_line

**状态**：设计 → 实施中
**日期**：2026-04-24
**作者**：kevin + Claude

## 背景与问题

跟进记录列表（`GET /customers?tab=records`、客户详情页 followup tab）当前的每个 item 只展示一段由 AI 生成的 `summary`（80-200 字），模板里截断成首行显示。

**实际用户反馈**：扫读困难。销售一天要扫 50+ 条，1 秒扫一条，需要两个维度的信息：
1. **这是一场什么会**（主题）—— 用来分类、定位
2. **会后推进到哪一步了**（进展）—— 用来判断客户生命周期

一段 summary 糊在一起，截断后这两个维度都看不到。

## 目标

列表 item 显式展示两个短字段：

```
ABENA ASIA LTD              4月24日
会议主题    供应链合作探索
一句话进展   双方确认合作意向，下周提供 BOM 报价
📍上海  我方1·客户1
```

- 标签灰淡，内容黑字，迷你表格对齐
- 溢出省略号（不换行）
- 详情页保留原 summary 长版本

## 非目标

- 不改 `summary` 字段的语义和风格（详情页继续用）
- 不做列表排序 / 筛选的额外维度（这是下一版的事）
- 不做多语言

## 数据

`followup_records` 增加两列：

```sql
ALTER TABLE followup_records ADD COLUMN meeting_title TEXT NOT NULL DEFAULT '';
ALTER TABLE followup_records ADD COLUMN progress_line TEXT NOT NULL DEFAULT '';
```

| 字段 | 类型 | 长度 | 性质 |
|---|---|---|---|
| `meeting_title` | TEXT | ≤20 字 | AI 提取 |
| `progress_line` | TEXT | ≤80 字 | AI 提取 |
| `summary` | TEXT | 保留 | 详情页用 |

迁移通过 `src/db/migrate.py` 里的 `_add_col_if_missing` 幂等添加。新库由 `schema.py` 一次性建好。

## AI 提取

复用现有 extract 步骤（Haiku + tools API），**在同一次 tool_use 里多拿两个字段**。不新增 LLM 调用，成本几乎不变（多几十 tokens）。

### Tool schema 扩展

在现有 `_EXTRACT_TOOL.input_schema.properties` 里加：

```python
"meeting_title": {
    "type": "string",
    "maxLength": 20,
    "description": "6-14 字的会议主题短语，动宾或名词结构。"
                   "不含客户名/日期/'会议'二字收尾。"
                   "例：'供应链合作探索'、'合同条款二次谈判'。",
},
"progress_line": {
    "type": "string",
    "maxLength": 80,
    "description": "20-40 字一句话进展，必含结论/下一步/阻碍/数字里程碑之一。"
                   "不能写'双方友好交流'这类废话。"
                   "例：'双方确认合作意向，下周提供 BOM 报价'。",
},
```

加入 `required` 列表。

### Prompt 改造

`_EXTRACT_SYSTEM` 保持原意，加几条硬规则；`messages[0].content` 的 user prompt 追加详细的正反例（见 `src/ingest/pipeline.py` 实现）。

正例：
- "供应链合作探索"、"产品性能需求澄清"、"合同条款二次谈判"、"样品实测结果复盘"
- "双方确认有合作意向，下周提供 BOM 报价"
- "客户对材料防火等级存疑，需补 B1 级检测报告"

反例：
- "会议纪要"、"ABENA 的会议"、"双方友好会谈"
- "双方进行了沟通"、"气氛融洽"、"交换了名片"

### 后处理

Haiku 偶尔会在 title 末尾带"会议""纪要"等尾巴。commit 前简单清洗：

```python
def _clean_title(t: str) -> str:
    t = t.strip()
    for suffix in ("会议纪要", "纪要", "会议", "沟通", "交流"):
        if t.endswith(suffix):
            t = t[:-len(suffix)].rstrip("·-— ：:")
    return t[:20]
```

## 存储

`_commit_sql` 里扩 `UPDATE followup_records`：

```python
conn.execute(
    "UPDATE followup_records "
    "SET summary=?, meeting_title=?, progress_line=? "
    "WHERE id=?",
    (extract_result.get("record_summary") or "",
     _clean_title(extract_result.get("meeting_title") or ""),
     (extract_result.get("progress_line") or "").strip()[:80],
     record_id),
)
```

## UI

### 列表模板 `_rows.html` / `_followup_rows.html`

在 item 里把原来的 `{{ r.title }}` 段落换成两行 kv：

```html
<div class="fu-kv">
  <span class="fu-k">会议主题</span>
  <span class="fu-v">{{ r.meeting_title or '—' }}</span>
</div>
<div class="fu-kv">
  <span class="fu-k">一句话进展</span>
  <span class="fu-v">{{ r.progress_line or '—' }}</span>
</div>
```

`_decorate_followups` 里把两个字段透传（默认空串，模板用 `or '—'` 兜底）。

### CSS

```css
.fu-kv {
  display: flex; gap: 8px;
  font-size: 14px; line-height: 1.6;
  min-width: 0;
}
.fu-k {
  flex: 0 0 72px;
  color: var(--neutral-400);
  font-size: 13px;
}
.fu-v {
  flex: 1; min-width: 0;
  color: var(--neutral-900);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

## 回填

历史记录 `meeting_title='' AND progress_line=''`，写一次性脚本：

```
python -m src.scripts.backfill_extract
```

脚本行为：
1. 扫 `followup_records WHERE meeting_title='' AND status='done'`（假设 job 已跑过，有 wiki_path）
2. 读对应 `raw/customers/{cid}/{record_id}.md` 和 `wiki/customers/{cid}.md`
3. 调 `run_extract`，UPDATE 两字段（不动 summary，避免改写用户看过的文本）
4. 失败继续下一条，最后打印成功/失败计数

目前 Fly 上只有 3 条。成本 <$0.01。

通过 ssh 跑：
```
flyctl ssh console -C "python -m src.scripts.backfill_extract"
```

## 风险与兜底

| 风险 | 应对 |
|---|---|
| Haiku 偶发返回带"会议"尾巴 | `_clean_title` 清洗 |
| 进展写废话 | prompt 反例 + maxLength 截断；偶发接受 |
| 回填脚本半路失败 | 每条 try/except；再跑一次会跳过已有的（WHERE 条件过滤） |
| 空字段历史数据先上线看到 `—` | 部署后先跑回填，再通知用户刷新 |
| Haiku tool 返回 schema 不符 | Anthropic tools 模式会强约束；极端情况 fallback 到空串（不让 pipeline 整体挂） |

## 发布步骤

1. 本地跑 `python -m src.db.migrate`，确认迁移幂等
2. `python -c "from src.web.app import app"` 导入检查
3. `git commit` + `flyctl deploy`
4. `flyctl ssh console -C "python -m src.scripts.backfill_extract"`
5. 手机飞书小程序打开列表，验证显示

## 代码改动范围

| 文件 | 动作 |
|---|---|
| `src/db/schema.py` | followup_records CREATE TABLE 加两列 |
| `src/db/migrate.py` | `_run_column_migrations` 加两项 |
| `src/ingest/pipeline.py` | tool schema + prompt + commit SQL + `_clean_title` |
| `src/web/app.py` | `_decorate_followups` 透传两字段 |
| `src/web/templates/_followup_rows.html` | 替换段落为两行 kv |
| `src/web/templates/base.html` | 加 `.fu-kv/.fu-k/.fu-v` 样式 |
| `src/scripts/__init__.py` | 新建（空） |
| `src/scripts/backfill_extract.py` | 新建 |

合计 8 文件、~130 行。
