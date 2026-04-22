# 客户列表页设计

## 1. 设计原则

- **极简**：不提供任何筛选、排序、新建客户入口
- **手机优先**：H5 跑在飞书小程序容器，单列布局
- **现代化视觉**：无边框、轻阴影、大圆角、中间点分隔、字段两端对齐
- **SSR + HTMX**：服务端渲染，搜索通过 HTMX 局部刷新
- **客户只读**：数据从外部 CRM 同步，本页面不新建、不删除客户

## 2. 整体布局

```
┌──────────────────────────────────┐
│                                  │
│  客户助手                         │ ← header，只有标题
│                                  │
│  🔍  搜索                         │ ← 胶囊搜索框
│                                  │
│  客户       跟进记录              │ ← 下划线式 tab
│  ━━                              │
│                                  │
│  ╭────────────────────────────╮ │
│  │                        ⊕    │ │ ← + 按钮（右上）
│  │  UPE                        │ │
│  │  方案沟通 · 未付费           │ │ ← stage · 其他 tag
│  │                             │ │
│  │  近 30 天跟进     4 次       │ │
│  │  客户所有人    🙂 张三       │ │
│  ╰────────────────────────────╯ │
│                                  │
│  ╭────────────────────────────╮ │
│  │                             │ │
│  │  字节跳动                    │ │
│  │  合同签约 · 已付费           │ │
│  │                             │ │
│  │  近 30 天跟进     2 次       │ │
│  │  客户所有人    🙂 李四       │ │
│  ╰────────────────────────────╯ │
└──────────────────────────────────┘
```

## 3. 元素规格

### 3.1 Header

| 项 | 值 |
|---|---|
| 标题 | "客户助手" |
| 字号 | `text-[20px] font-semibold` |
| 颜色 | `text-neutral-900` |
| 对齐 | 左对齐 |
| 内边距 | `px-5 py-4` |
| 背景 | 透明（沿用页面背景） |
| 按钮 | 无 |

### 3.2 搜索框

| 项 | 值 |
|---|---|
| 宽度 | 满宽（container 内） |
| 圆角 | `rounded-full` |
| 背景 | 默认 `bg-neutral-100`，focus `bg-white` |
| 阴影 | focus 时 `shadow-[0_0_0_4px_rgba(51,112,255,0.1)]` |
| 内边距 | `px-5 py-3` |
| 图标 | 左侧 search icon，`text-neutral-400`，`size-5` |
| 占位符 | 跟 tab 变：<br/>• 客户 tab：`搜索客户名 / 行业`<br/>• 跟进记录 tab：`搜索记录内容 / 标题 / 客户名` |
| 触发 | HTMX `hx-trigger="input changed delay:300ms"` → `GET /_p/customer-list?q=...` |

### 3.3 Tab

| 项 | 值 |
|---|---|
| 布局 | 两个 tab 横向等宽 |
| 字号 | `text-[15px]` |
| active 态 | `font-semibold text-neutral-900` + 底部 2px 主色下划线 |
| inactive 态 | `text-neutral-500` |
| 切换动画 | 下划线用 `transform transition-transform duration-200` 滑动 |
| URL | `?tab=customers` / `?tab=records`，保持 tab 状态可深链 |

### 3.4 客户卡片

**容器：**
| 项 | 值 |
|---|---|
| 圆角 | `rounded-3xl` (24px) |
| 背景 | `bg-white` |
| 阴影 | `shadow-[0_1px_3px_rgba(0,0,0,0.04)]` |
| 边框 | 无 |
| 内边距 | `p-5` |
| 上下间距 | `space-y-3` |
| 点击反馈 | `active:bg-neutral-50` 瞬闪 |

**客户名（第 1 行）：**
| 项 | 值 |
|---|---|
| 字号 | `text-[17px] font-semibold` |
| 颜色 | `text-neutral-900` |
| 截断 | `truncate`（过长省略号） |

**Tag 行（第 2 行，紧跟客户名）：**
- 格式：`{stage} · {其他 tag}`（中间点分隔）
- 字号：`text-[13px]`
- Stage 颜色编码：

| Stage | 颜色 |
|-------|------|
| 线索、初步接触 | `text-neutral-500` |
| 需求确认、方案沟通 | `text-blue-600` |
| 报价商务、合同签约 | `text-amber-600` |
| 交付验收、运营维护 | `text-emerald-600` |
| 流失冻结 | `text-red-500` |

- 其他 tag（付费状态等）：`text-neutral-500`

**字段行（第 3-4 行）：**
| 项 | 值 |
|---|---|
| 布局 | `flex justify-between`，标签左、值右 |
| 标签字号 | `text-[13px] text-neutral-400` |
| 值字号 | `text-[14px] text-neutral-700` |
| 行间距 | `space-y-2`，首行与 tag 行 `mt-3` |

显示字段：
1. `近 30 天跟进` → `N 次`（N=0 时显示 `暂无`，灰色）
2. `客户所有人` → 小头像（16px）+ 名字；无 owner 时显示 `企业公共客户池`（灰 pill）

**+ 按钮（卡片右上）：**
| 项 | 值 |
|---|---|
| 定位 | `absolute top-4 right-4` |
| 尺寸 | `w-9 h-9` (36×36) |
| 形状 | `rounded-full` |
| 默认态 | `bg-neutral-100 text-neutral-500` |
| 按下态 | `bg-blue-600 text-white scale-95 transition-transform` |
| 图标 | Plus（lucide 细线条风格，`size-4`） |
| 点击行为 | **待定：A 或 B**（见第 7 节） |

## 4. 搜索行为

- **触发**：输入变化 300ms 防抖
- **端点**：`GET /_p/customer-list?q={query}&tab={customers|records}`
- **服务端逻辑**：
  - 客户 tab：`name LIKE '%q%' OR aliases LIKE '%q%' OR industry LIKE '%q%'`
  - 跟进记录 tab：`records.summary LIKE '%q%' OR records.source_title LIKE '%q%' OR customer.name LIKE '%q%'`
- **返回**：HTMX 片段 HTML，替换 `<div id="list">` 的内容
- **URL 同步**：`history.replaceState` 把 `q` 同步到地址栏，便于分享/刷新

## 5. 排序

**默认（且唯一）：**
```sql
SELECT * FROM customers
ORDER BY updated_at DESC
```

不暴露给用户切换。`updated_at` 由 ingest pipeline 维护（每次跟进记录写入后更新）。

## 6. 边界情况

### 6.1 搜索无结果

```
┌─────────────────────────┐
│                          │
│    未找到 "某某公司"      │
│                          │
└─────────────────────────┘
```

- 居中显示，灰色文字
- **不提供"新建客户"入口**（客户来自 CRM）
- 提示文案：`未找到 "{q}"，请确认名称或去 CRM 查询`

### 6.2 客户列表为空（初次使用，CRM 未同步）

```
┌─────────────────────────┐
│                          │
│    暂无客户              │
│    请确认 CRM 已同步数据  │
│                          │
└─────────────────────────┘
```

### 6.3 跟进记录 tab 为空

```
┌─────────────────────────┐
│                          │
│    暂无跟进记录           │
│                          │
└─────────────────────────┘
```

### 6.4 Loading 态（首次加载 / 搜索中）

骨架屏（Skeleton），显示 3 个灰色占位卡：

```
┌─────────────────────────┐
│ ▓▓▓▓▓▓                  │
│ ▓▓▓                      │
│ ▓▓  ▓▓                  │
└─────────────────────────┘
```

- 每个占位卡用 `animate-pulse bg-neutral-100`
- 不用 spinner

## 7. + 按钮行为（待定）

两种方案，需要产品拍板：

**方案 A：直达详情页，自动打开加记录面板**
- 点击 → 跳 `/customers/{id}?action=add-record`
- 详情页检测到 `action` 参数，页面滚动到"加跟进记录"区域，输入框获得焦点
- 优点：和普通进入详情页一致，只是多一步焦点
- 缺点：多一次页面跳转

**方案 B：卡片内就地展开加记录表单**
- 点击 → 卡片高度增长，下方展开一个 URL 输入框 + 提交按钮
- 提交后卡片内显示进度条，ingest 完成后卡片恢复原样并显示"✅ 已添加"
- 优点：无跳页，体验最顺
- 缺点：实现复杂度高（HTMX 片段 + 进度轮询嵌在卡片内）

**当前推荐：方案 A**（简单、可靠，视觉上有跳页但流程短）。

## 8. 路由与端点

### 页面路由
```
GET /customers
    → 渲染整个列表页（含 header、搜索框、tabs、卡片列表）
    query: ?q= &tab=customers|records
```

### HTMX 片段端点
```
GET /_p/customer-list
    → 只返回卡片列表部分 HTML
    query: ?q= &tab=customers|records
    用于搜索输入时局部刷新
```

### 详情页（+ 按钮跳转目标）
```
GET /customers/{id}?action=add-record
    → 客户详情页
    action=add-record 时自动定位到加记录区域
```

## 9. SQL 查询（关键语句）

### 9.1 默认列表（客户 tab，无搜索）
```sql
SELECT
  c.id,
  c.name,
  c.journey_stage,
  c.payment_status,                       -- TBD: 是否加此字段
  c.primary_owner,
  u.name     AS owner_name,
  u.avatar_url AS owner_avatar,
  (SELECT COUNT(*) FROM followup_records r
    WHERE r.customer_id = c.id
      AND r.meeting_date >= date('now', '-30 days')) AS recent_count
FROM customers c
LEFT JOIN users u ON u.open_id = c.primary_owner
ORDER BY c.updated_at DESC
LIMIT 50 OFFSET ?;
```

### 9.2 搜索（客户 tab）
```sql
SELECT ...
FROM customers c
LEFT JOIN users u ON u.open_id = c.primary_owner
WHERE c.name LIKE '%' || ? || '%'
   OR c.aliases LIKE '%' || ? || '%'
   OR c.industry LIKE '%' || ? || '%'
ORDER BY c.updated_at DESC
LIMIT 50;
```

### 9.3 搜索（跟进记录 tab）
```sql
SELECT
  r.id, r.customer_id, r.summary, r.source_title, r.meeting_date,
  c.name AS customer_name, c.journey_stage
FROM followup_records r
JOIN customers c ON c.id = r.customer_id
WHERE r.summary LIKE '%' || ? || '%'
   OR r.source_title LIKE '%' || ? || '%'
   OR c.name LIKE '%' || ? || '%'
ORDER BY r.created_at DESC
LIMIT 50;
```

## 10. 分页（无限滚动）

### 10.1 交互规则

- **初次加载**：50 条
- **触发**：列表末尾放一个 `<div class="sentinel">`，用 HTMX `hx-trigger="revealed"` 自动请求下一页
- **swap 策略**：`hx-swap="outerHTML"` —— 新片段替换 sentinel 本身（新片段里自带下一页的 sentinel，形成链）
- **到底**：服务端判断无更多数据时返回一个"已经到底了"的终止片段（**不含** sentinel），链条自然停止
- **错误**：返回错误片段 + `[重试]` 按钮（`hx-get` 重新拉同一页）
- **不用 spinner**：每次插入 3 张骨架屏占位，被真数据替换

### 10.2 分页方式：cursor（非 offset）

**为什么不用 OFFSET：** 在 `ORDER BY updated_at DESC` 下若有并发同步写入，offset 会漏行或重复。

**cursor 字段**：`(updated_at, id)` 复合 cursor

```sql
-- 第 1 页
SELECT ... FROM customers c
WHERE c.crm_is_deleted = 0
  AND c.crm_owner_id = :me
ORDER BY c.crm_recent_activity_at DESC, c.id DESC
LIMIT 51;     -- 多拉 1 条判断是否有下一页

-- 第 N 页（cursor = 上页最后一条的 (ts, id)）
SELECT ... FROM customers c
WHERE c.crm_is_deleted = 0
  AND c.crm_owner_id = :me
  AND (c.crm_recent_activity_at, c.id) < (:cursor_ts, :cursor_id)   -- 严格小于
ORDER BY c.crm_recent_activity_at DESC, c.id DESC
LIMIT 51;
```

返回 51 条时表示有下一页；cursor = 第 50 条的 `(ts, id)` 编码后给前端。

### 10.3 端点协议

```
GET /_p/customer-list?q=&tab=customers&cursor=<b64>
    → 返回 HTML 片段：N 张卡片 + sentinel（若还有下一页）
```

**cursor 编码**：`base64url(f"{iso_ts}|{id}")`，URL 安全，前后端不需要共识 schema。

### 10.4 HTMX 模板骨架

页面首屏 ssr 渲染：

```html
<div id="list" class="space-y-3">
  {% for c in customers %}
    {{ render_card(c) }}
  {% endfor %}

  {% if has_next %}
    <div
      class="sentinel h-1"
      hx-get="/_p/customer-list?q={{ q }}&tab={{ tab }}&cursor={{ next_cursor }}"
      hx-trigger="revealed"
      hx-swap="outerHTML"
    ></div>
  {% else %}
    <div class="text-center text-neutral-400 text-xs py-6">已经到底了</div>
  {% endif %}
</div>
```

下一页片段就是 `{% for %} + sentinel-or-terminal`，没有外层 `<div id="list">` 包裹。

### 10.5 搜索与翻页的互动

- 搜索框触发时重置分页：`hx-target="#list"`，`hx-swap="innerHTML"`（替换整个列表，cursor 归零）
- 搜索中的翻页同样用 cursor，URL query `?q=xxx&cursor=...`

### 10.6 滚动位置保持

页面刷新 / 从详情页返回时，希望保留原滚动位置：

- HTMX `hx-history="false"`（翻页片段不进 history stack）
- 从详情页返回：用浏览器原生 bfcache 即可；若被卸载，URL query 保留 `scroll=<Y>`，onload 读出后 `window.scrollTo`

## 11. 待决事项

1. **+ 按钮动作**：方案 A（跳详情页 + action 参数）还是 B（卡片内展开表单）？
2. **"近 30 天跟进次数"的时间基准**：`meeting_date >= date('now', '-30 days')` 还是 `created_at >= ...`？
   - 推荐 `meeting_date`（真实会议日期，允许补录历史）
3. **"客户所有人" 是谁**：`primary_owner`、`csm_owner`、`co_owners` 哪些字段？
   - 推荐只显示 `primary_owner`
4. **Tags 除了 stage 还显示哪些**：付费状态（未付费 / 已付费 / 续约 / 流失）？行业？风险级别？
   - 推荐 v0.2 只显示 payment_status（需要加字段）
5. **CRM 同步方案**（影响 schema 和数据流）：
   - CRM 类型（自建 / Salesforce / Hubspot / 飞书）
   - 同步方式（webhook / 定时拉取）
   - 同步字段清单
   - customers.id 是用 CRM id 还是自有 id + crm_id 映射
   - 冲突处理策略
