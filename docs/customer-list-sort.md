# 客户列表：按「最近查看」个性化排序

**状态**：设计 → 实施中（终版）
**日期**：2026-04-24
**作者**：kevin + Claude

## 背景

客户列表当前按 `crm_updated_at DESC` 排。销售每天要扫 50+ 客户，"我正在跟进的"应该浮顶。但"最近修改的"对销售价值很低（可能是同步脚本改的电话/地址）。

## 目标

客户列表**单一排序**：我看过的客户浮顶，没看过的按最近真实活动排。**不加任何切换控件**（tab / 下拉都不要）。

## 最终排序规则

```sql
ORDER BY
  COALESCE(
    uv.last_viewed_at,                          -- 我看过的：按我的查看时间
    c.crm_recent_activity_at,                   -- 没看过的：按最近跟进时间
    '1970-01-01'                                -- 都没有：沉底
  ) DESC,
  c.id DESC
```

分层效果：

```
━━━━━━━━━━━━━━━━━━━━━━━━
刚才查看的客户      ← viewed 5 分钟前
小时前查看的客户    ← viewed 2 小时前
昨天查看的客户      ← viewed 昨天
━━━━━━━━━━━━━━━━━━━━━━━━（我看过的结束）
今天有跟进的客户    ← activity 今天
昨天有跟进的客户    ← activity 昨天
上周活动的客户      ← activity 上周
━━━━━━━━━━━━━━━━━━━━━━━━
从未活动的客户      ← NULL → 1970，沉底
```

视觉上是**一个连续列表**，没有分隔线，但顺序自然合理。

## 核心设计决策

### 为什么 fallback 用 `crm_recent_activity_at` 而不是 `crm_updated_at`

| 字段 | 含义 | 对销售的价值 |
|---|---|---|
| `crm_recent_activity_at` | 最近活动时间 | **高** —— 反映真实业务热度 |
| `crm_updated_at` | 字段最后修改时间 | 低 —— 可能是同步脚本/自动任务改的 |

销售扫列表想知道"这个客户热不热"，不是"档案今天有没有被改"。

### 为什么不做下拉 / tab

推敲了四种方案：

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 单视图 + 搜索 | 极简 | "我的客户"视图丢失 | 基础，本设计基于 A |
| B. 双 tab（最近查看 / 我的客户）| 覆盖两种心智 | 需要完整 owner 映射、多 36px UI | 推过但被本方案取代 |
| C. 下拉切换 | 节省空间 | 手机端发现性差 | 已实现后被替换 |
| D. 单视图"我相关"过滤 | 简洁 | 新客户/冷客户发现不了 | 放弃 |

**最终方案 = A 的精神 + 更好的 fallback 字段**。

这个方案比 B 少一个"我的客户"视图，代价是：**销售不能一键看到自己名下所有客户**。接受这个代价因为：
- 销售日常就围着活跃客户转，"最近查看"本来就浮出了他们
- 月末盘点这种场景可通过搜索应对（未来搜索可扩展"搜 owner"）
- UI 保持极简，减少选择疲劳

### 为什么保留顶部静态 label "最近查看"

个性化排序是新特性，不能让用户意识不到：

- 写 label → 用户知道"这个列表是按我的习惯排的"
- 不写 label → 功能技术上存在但用户感知不到 = 无价值

做法：**静态灰字 `最近查看`，无交互、无箭头**。只是 subtitle，不是按钮。

### 为什么卡片右下显示 "上次查看 X 前"

光有 label 不够具象。在用户看过的卡片上加相对时间戳 `上次查看 3 天前`：
- 给 label 语义提供**视觉证据**
- 用户一眼理解"哦顶上是我刚看过的"
- 没看过的卡片不显示 timestamp（避免"3 个月前查看"这种负面信号，对数据卫生也没贡献）

### 为什么不加"未读小点"

中文手机 app 里小点（红/蓝）几乎 100% 等于"未读消息"约定。
- 红点：太强，用户以为有通知
- 蓝点：太弱，用户不知道是什么意思

所以"没看过但最近有活动"这个信号**不做指示灯**。这些客户自然会出现在"我看过"下方，按跟进时间排，本身就会浮到醒目位置。

## 数据层

### 新表：`user_customer_views`

```sql
CREATE TABLE user_customer_views (
    user_id        TEXT NOT NULL,    -- 飞书 open_id（dev 模式是 "pwd-user"）
    customer_id    TEXT NOT NULL,
    last_viewed_at TEXT NOT NULL,
    view_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, customer_id)
);
CREATE INDEX idx_uv_user_time
  ON user_customer_views(user_id, last_viewed_at DESC);
```

### 写入：详情页访问时 UPSERT

`GET /customers/{id}` 加一行（失败静默）：

```python
INSERT INTO user_customer_views(...)
VALUES (?, ?, ?, 1)
ON CONFLICT(user_id, customer_id) DO UPDATE SET
  last_viewed_at = excluded.last_viewed_at,
  view_count = user_customer_views.view_count + 1
```

### 读取：列表查询

`_fetch_page` 始终 LEFT JOIN views 表，始终按 COALESCE 排：

```sql
SELECT c.*, ..., uv.last_viewed_at,
       COALESCE(uv.last_viewed_at, c.crm_recent_activity_at, '1970-01-01') AS sort_key
FROM customers c
LEFT JOIN user_customer_views uv ON uv.user_id = ? AND uv.customer_id = c.id
...
ORDER BY sort_key DESC, c.id DESC
```

## URL / 分页

- 不加 `sort` 参数（只有一种排序）
- cursor 编码保持 `base64(sort_key|id)`，sort_key 含义稳定

## UI 改动

### 列表顶部加轻量 label

```html
<div class="list-sort-label">最近查看</div>
```

灰色 13px 字，左对齐，`padding: 8px 16px 0`。无箭头、无交互。

### 卡片右下角相对时间

模板里每个卡片加：
```html
{% if row.viewed_at_display %}
  <span class="card-view-time">上次查看 {{ row.viewed_at_display }}</span>
{% endif %}
```

`viewed_at_display` 由后端 `_relative_time(last_viewed_at)` 计算。

灰色 12px 字，绝对定位到卡片右下角 / flex 贴右。

## 清理（从之前的尝试回退）

| 文件 | 要删的东西 |
|---|---|
| `src/web/app.py` | `SortMode`, `VALID_SORTS`, `DEFAULT_SORT`, `_normalize_sort`, `_count_user_owned_customers` |
| `src/web/app.py` | `customers_page` / `customer_list_partial` 的 `sort` 参数 |
| `src/web/app.py` | `_fetch_page` 里的 sort 分支（mine/viewed） |
| `src/web/templates/customers.html` | `.sort-bar` / `.sort-menu` 块 + dropdown JS |
| `src/web/templates/base.html` | `.sort-menu` / `.sort-trigger` / `.sort-panel` / `.sort-item` CSS |
| `src/web/templates/_rows.html` | `sort` 参数在 sentinel URL 里的传递（没 sort 了） |

**保留的东西**（以后可能还用）：
- `src/db/schema.py` 的 `user_customer_views` 表
- `src/web/auth.py` 的 OAuth auto-map feishu_open_id（以后做"我的客户"功能用）
- `src/web/app.py` 的 `_track_view` 函数 + 详情页调用

## 实施清单

| 项 | 行数 |
|---|---|
| `src/web/app.py`: 简化 `_fetch_page`（去 sort，fallback 改）| -40 / +15 |
| `src/web/app.py`: route 去掉 sort 参数 | -10 |
| `src/web/app.py`: `_decorate_rows` 加 `viewed_at_display` 计算 | +15 |
| `src/web/templates/customers.html`: 删 dropdown，加静态 label | -30 / +3 |
| `src/web/templates/base.html`: 删 dropdown CSS，加 label CSS + 卡片 timestamp CSS | -55 / +15 |
| `src/web/templates/_rows.html`: 加 `上次查看 X 前` + 删 sort 传递 | +3 / -1 |

净 +~0 行（删改比例差不多）。

## 已知局限 / 未来

- **密码登录所有人共用 `uid="pwd-user"`** → view 记录会串。dev 无所谓，prod 用 OAuth 没事
- **月末盘点"我所有客户"场景无直接入口** → 未来可考虑搜索支持"搜 owner"，或在详情页加"同负责人其他客户"入口
- **view 表清理** → 暂不做。单人数据量小
- **`feishu_open_id` 自动 map** → 代码保留，数据也会继续积累。以后做"我的客户"feature 可直接启用

## 决策演进（便于以后回顾）

1. 初版：4 个下拉选项（最近查看/更新/跟进/全部）— 过于复杂，选择疲劳
2. V2：2 个下拉（最近查看/我的客户）— 管理者会空列表，"我的客户"label 不通用
3. V3：双 tab（最近查看/我的客户）— 更清晰但多 36px UI
4. V4：单视图"我有权限"过滤 — 冷启和发现性问题
5. **终版**：单视图 + 个性化排序 + fallback 用最近跟进 — 简洁、自洽、可感知
