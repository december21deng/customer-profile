# 客户列表：个性化排序（最近查看 / 我的客户）

**状态**：设计 → 实施中
**日期**：2026-04-24
**作者**：kevin + Claude

## 背景

客户列表当前只有一种排序：`crm_updated_at DESC`。销售每天要扫 50+ 客户，"和我有关的"应该浮到顶 —— 需要个人化。

## 目标

列表顶部加一个轻量下拉，两个选项：

| 选项 | 语义 | 排序/过滤 |
|---|---|---|
| **最近查看**（默认） | 我最近点过的客户浮顶，其他按 CRM 更新时间 | `COALESCE(views.last_viewed_at, c.crm_updated_at) DESC` |
| **我的客户** | 我直接负责的客户（管理者/新人兜底显示全部） | 按名下数量分流，按 `c.crm_recent_activity_at DESC` 排 |

## 关键设计

### "我的客户" 的角色自适应

**问题**：管理者通常没有直接挂名在他名下的客户 —— 硬按 `owner_id = me` 过滤会返回空列表。"我的客户"这个词对管理者就成了谎言。

**方案**：分流逻辑

```
1. 当前用户 open_id 映射到 crm_users.id（下详）
2. COUNT(customers WHERE crm_owner_id = 映射到的 id)
3a. > 0  → 销售 → 过滤到自己名下
3b. = 0  → 管理者 / 新人 / 映射失败 → 不过滤，显示全部
4. 无论分支：ORDER BY c.crm_recent_activity_at DESC
```

这样"我的客户"对所有人都有意义：
- **销售**：看到自己名下的客户，按最近跟进时间排
- **管理者**：看到全量客户，按最近跟进时间排（自然突出"有热度"的客户）
- **新人 / 未映射**：看到全量客户，不会空

Label 的"我的" 在中文里够口语化，销售会自然理解为"我名下的"，管理者会自然理解为"我业务范围内的"，不别扭。

### 个人 view 历史存储

新表：

```sql
CREATE TABLE user_customer_views (
    user_id        TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    last_viewed_at TEXT NOT NULL,
    view_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, customer_id)
);
CREATE INDEX idx_uv_user_time
  ON user_customer_views(user_id, last_viewed_at DESC);
```

- PK `(user_id, customer_id)` UPSERT 更新时间
- 索引 `(user_id, last_viewed_at DESC)` 查最近看的走索引
- `view_count` 预留（以后可能做"最常看"）

### 写入：详情页访问时 UPSERT

`GET /customers/{id}` 加一行：

```python
conn.execute(
    "INSERT INTO user_customer_views(user_id, customer_id, last_viewed_at, view_count) "
    "VALUES(?, ?, ?, 1) "
    "ON CONFLICT(user_id, customer_id) DO UPDATE SET "
    "  last_viewed_at = excluded.last_viewed_at, "
    "  view_count = user_customer_views.view_count + 1",
    (uid, customer_id, now_iso()),
)
```

失败静默（不能让详情页因写 view 失败而挂）。

### 读取 SQL

**最近查看**（默认）：

```sql
SELECT c.*, v.last_viewed_at
FROM customers c
LEFT JOIN user_customer_views v
       ON v.user_id = ? AND v.customer_id = c.id
WHERE c.crm_is_deleted = 0 ...
ORDER BY COALESCE(v.last_viewed_at, c.crm_updated_at) DESC, c.id DESC
```

零记录时 `v.last_viewed_at` 全 NULL，COALESCE 回退到 `crm_updated_at` → **视觉上和原来一致**，用户无感。点过几个客户后，它们自然浮顶。

**我的客户**：

先查当前用户 owned count：

```sql
SELECT COUNT(*) FROM customers c
JOIN crm_users u ON u.id = c.crm_owner_id
WHERE c.crm_is_deleted = 0 AND u.feishu_open_id = ?
```

然后分流：

```sql
-- 销售分支（owned > 0）
SELECT c.* FROM customers c
JOIN crm_users u ON u.id = c.crm_owner_id
WHERE u.feishu_open_id = ?
  AND c.crm_is_deleted = 0 ...
ORDER BY COALESCE(c.crm_recent_activity_at, '1970-01-01') DESC, c.id DESC

-- 管理者/兜底分支（owned = 0）
SELECT c.* FROM customers c
WHERE c.crm_is_deleted = 0 ...
ORDER BY COALESCE(c.crm_recent_activity_at, '1970-01-01') DESC, c.id DESC
```

### open_id → crm_users.id 映射

问题：`crm_users.feishu_open_id` 列存在但 0/184 填了。

方案：**OAuth 登录时 auto-map**。

```python
# OAuth 回调里拿到 open_id + feishu_name 之后
conn.execute(
    "UPDATE crm_users SET feishu_open_id = ? "
    "WHERE display_name = ? "
    "  AND (feishu_open_id IS NULL OR feishu_open_id = '') "
    "  AND 1 = (SELECT COUNT(*) FROM crm_users WHERE display_name = ?)",
    (open_id, feishu_name, feishu_name),
)
```

规则：
- 同名**唯一**才写（两个"张伟"就不 map，防串）
- 不覆盖已有映射
- 失败静默（写不上不影响登录）
- 用户下次 OAuth 重登自动触发

### URL 状态

`/customers?sort=viewed|mine&owner=xxx&q=keyword`

- `sort` 缺省 = `viewed`
- 无效值回退到 `viewed`
- 切 sort 时 cursor 丢弃，回第 1 页（方案 B）

### Cursor 设计

切 sort 从第 1 页开始（方案 B），cursor 编码保持 `base64(ts|id)`，`ts` 含义随 sort 变化：

| sort | ts 含义 |
|---|---|
| `viewed` | `COALESCE(v.last_viewed_at, c.crm_updated_at)` |
| `mine` | `COALESCE(c.crm_recent_activity_at, '1970-01-01')` |

## 下拉 UI

原生 `<select>` 在飞书 webview 里样式丑，自己做轻量 dropdown：

```html
<div class="sort-menu">
  <button class="sort-trigger" id="sort-btn">
    最近查看 <span class="sort-caret">▾</span>
  </button>
  <div class="sort-panel" id="sort-panel" hidden>
    <a class="sort-item active" data-sort="viewed">最近查看</a>
    <a class="sort-item"        data-sort="mine">我的客户</a>
  </div>
</div>
```

- 点按钮 → 浮层打开
- 点选项 → 带 `?sort=xxx` 整页刷（不走 HTMX，交互少不值得）
- 点浮层外 → 关闭

## 实施清单

| 文件 | 动作 | 行数 |
|---|---|---|
| `src/db/schema.py` | 加 `user_customer_views` 表 + 索引 | 10 |
| `src/web/auth.py` | OAuth 回调里 auto-map feishu_open_id | 18 |
| `src/web/app.py` | `customer_detail` UPSERT view | 12 |
| `src/web/app.py` | `_fetch_page` 加 `sort` 参数 + 两分支 | 35 |
| `src/web/app.py` | `/customers` 和 `/_p/customer-list` 带 `sort` | 10 |
| `src/web/templates/customers.html` | sort 下拉 widget | 20 |
| `src/web/templates/base.html` | `.sort-menu` CSS | 25 |
| inline JS | 下拉点击逻辑 | 18 |

合计 ~148 行。

## 已记录的决策（便于以后回顾）

1. **砍掉了"最近更新 / 最近跟进 / 全部"** —— 4 选项对用户是噪音，2 个选项已覆盖核心场景
2. **"我负责的" 改名"我的客户"** —— 更口语，不"销售化"
3. **"我的客户" 角色自适应** —— 管理者空列表问题用 owned=0 兜底解决
4. **默认不加引导 / 提示** —— "最近查看" 的规则自洽，视觉上第一天和"最近更新"一致，用户看不出差异
5. **排序切换丢弃 cursor** —— 简化实现，UX 可接受
6. **feishu_open_id 自动 map** —— OAuth 时 display_name 唯一匹配才写，防串
7. **view_count 预留但不用** —— 未来做"最常看"入口

## 已知局限 / 未来

- **密码登录模式所有人共用 uid="pwd-user"** → view 记录会串。dev 无所谓，prod 走 OAuth 没事
- **同名冲突不 map** → 用户切"我的客户"走管理者兜底（显示全部）。合理兜底
- **无 manager hierarchy 数据** → "我的团队"这种过滤做不了。目前用 owned=0 兜底足够
- **view 表清理** → 单人数据量小，先不清。以后 scheduler 可加一个 weekly 清老的
