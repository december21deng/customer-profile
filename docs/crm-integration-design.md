# CRM 集成设计建议

> 基于对 ByteHouse `ODS_YL` / `DWD_YL` 真实 schema 的探索。

## 1. CRM 侧真实情况

### 1.1 主数据概览

| 维度 | 真实数值 |
|------|---------|
| 数据库 | ByteHouse (ClickHouse 21.8 协议)，TLS 端口 19000 |
| 主库 | `ODS_YL`（raw）/ `DWD_YL`（清洗）/ `ADS_YL`（应用层）/ `AI_DW_YL`（AI） |
| 行业 | 医疗耗材（英科医疗） |
| CRM 系统特征 | `customItemNNN__c` 命名 + `entityType` + `ValueList` 字典 → 判断为**销售易 NeoCRM**（或类似国产 CRM） |
| 账号数 | `ODS_YL.crm_account` 69,953 行 |
| 活动（跟进记录）数 | `crm_activityrecord_json` 4,895,512 行 |
| 联系人数 | `crm_contact_json` 30,244 行 |
| 单销售持有账号峰值 | 9,451 个（Top Owner） |

### 1.2 核心字段清单（`ODS_YL.crm_account`，约 300 列中的关键）

| 字段 | 类型 | 含义 | 备注 |
|------|------|------|------|
| `id` | String | CRM 主键 | 用作我们的 `customer.crm_id` |
| `accountName` | String? | 客户名 | 如 `MaiMed GmbH` |
| `ownerId` | String? | 负责人 id | 关联 `crm_User__c_json.id` |
| `level` | Int8? | 客户等级 1-6 | 分布：L2=30K, L1=12K, L3=3K, 其他少 |
| `parentAccountId` | String? | 父账号 | 客户层级 |
| `industryId` | Int8? | 行业枚举 | Top: 25(9.6K), 16(8K), 18(3.9K)；需 join `crm_ValueList` 才能拿文案 |
| `state`, `fState`, `fCity`, `fDistrict` | 多种 | 地理 | `state`=原文，`fState/fCity/fDistrict`=编码 |
| `isCustomer` | String? | '1'=正式客户, '0'=线索 | 70K 里只 19K 是正式客户 |
| `sharedTags` | String? | CRM 自定义 tag | |
| `accountChannel` | Int8? | 来源渠道 | |
| `recentActivityRecordTime` | DateTime64 | 最近活动时间 | **H5 默认排序字段候选** |
| `recentActivityCreatedBy` | String? | 最近活动创建人 | |
| `createdAt`/`updatedAt` | DateTime64 | | |
| `totalOrderAmount` | String? | 订单总额（字符串存数字） | 如 `'27564143'` |
| `totalWonOpportunities`/`totalWonOpportunityAmount` | String? | 赢单 | |
| `totalContract`/`totalActiveOrders` | String? | 合同/活跃订单 | |
| `accountScore` | String? | 账号评分（字符串存 float） | 如 `'100.0'` |
| `visitTotalCount`/`visitLatestTime`/`visitInplanCount` | 多种 | 拜访统计 | |
| `employeeNumber` | Int32? | 员工数 | |
| `annualRevenue` | String? | 年营收 | |
| `dimDepart` | String? | 所属部门 | 权限过滤用 |
| `territoryHighSeaStatus` | Int8? | 公海池状态 | |
| `loss` | Int8? | 流失标记 | **全 NULL，未启用** |
| `is_crm_deleted` | Int8 | 软删除 | 查询必须加 `= 0` |
| `LOAD_TIMESTAMP` | DateTime | 同步时间 | |
| `customItemNNN__c` | 各种 | 自定义字段 ~250 个 | 大部分稀疏 |

### 1.3 活动记录（`DWD_YL.crm_activityrecord`，68 列精简版）

| 字段 | 说明 |
|------|------|
| `id` | 活动记录 PK |
| `content` | **正文**（我们要 ingest 的那段文字） |
| `activityRecordFrom` / `activityRecordFrom_data` | 关联实体类型 + 实体 id（=11 时指向 account） |
| `ownerId`, `contactId`, `contactName`, `contactPhone` | 参与人 |
| `saleStageId` | **销售阶段** ←→ 我们的 `journey_stage`（需 join `crm_ValueList`） |
| `intentionalDegree` | 意向度（Int8 枚举） |
| `needFollow`, `nextCallTime` | 待办 |
| `location`, `longitude`, `latitude` | 打卡地点 |
| `createdAt`, `startTime`, `updatedAt` | 时间轴 |

### 1.4 用户（`ODS_YL.crm_User__c_json`）

- `id` = ownerId 外键
- `customItem1` = **显示名**（如 `刘方毅Frank`）—— 注意**不是** `name` 字段
- `name` = 是数字字符串（CRM 内部 ID，不是人名）
- `dimDepart` = 部门 id

### 1.5 ValueList（枚举字典）

`ODS_YL.crm_ValueList` 是统一枚举表，980 行，用 `(id, name, customItem2, customItem3)` 表示：
- `customItem1` = 租户名（英科医疗）
- `customItem2` = 枚举所属实体（如"客户"）
- `customItem3` = 枚举名（"区域"、"国家"）
- `name` = 具体编码（如 `ZLB-0008`）

**Industry / Level / saleStageId 的中文标签都在这里。** 首次同步时建议拉一次快照到我们的 SQLite。

---

## 2. 对我们 v0.2 设计的影响

### 2.1 必须修改的决策

| 原设计 | 新决策 | 原因 |
|--------|--------|------|
| `customers.id = slug（"upe", "bytedance"）` | `customers.id = CRM id`（数字字符串如 `"11218635"`） | 6.9 万客户无法 slug 化；CRM id 天然稳定 |
| `name TEXT NOT NULL UNIQUE` | 去掉 UNIQUE | CRM 允许同名（不同 owner/region） |
| `journey_stage` 9 枚举 | 直接用 CRM 的 `saleStageId`，同步 ValueList 到本地枚举表 | 不要自己造一套 stage 再做映射 |
| `customers.summary` 由我们生成 | 保留；这是我们的**增量价值**，CRM 里没有 | |
| H5 一次拉全表 | **必须**按 `ownerId = 当前销售` 或 `dimDepart IN (...)` 过滤 | 单用户 9.4K 客户已是上限 |

### 2.2 新增字段（SQLite `customers` 表）

```sql
CREATE TABLE customers (
    id                TEXT PRIMARY KEY,         -- = CRM id，不是 slug
    name              TEXT NOT NULL,
    crm_level         INTEGER,                  -- 1..6
    crm_industry_id   INTEGER,                  -- 对照 value_list
    crm_owner_id      TEXT,                     -- CRM user id
    crm_dim_depart    TEXT,                     -- 部门 id
    crm_is_customer   INTEGER NOT NULL,         -- 0=线索, 1=客户
    crm_is_deleted    INTEGER NOT NULL DEFAULT 0,
    crm_recent_activity_at TEXT,                -- ISO；= recentActivityRecordTime
    crm_total_order_amount REAL,                -- 转 REAL 存，CRM 给的是 String
    crm_account_score REAL,
    crm_state         TEXT,
    crm_sale_stage_id TEXT,                     -- 最近一次 activity 的 saleStageId
    crm_updated_at    TEXT NOT NULL,            -- CRM 的 updatedAt（判断谁较新）
    -- ↓ 我们自己的增量
    summary           TEXT NOT NULL DEFAULT '',
    wiki_path         TEXT NOT NULL,
    local_updated_at  TEXT NOT NULL,            -- 本地 summary / wiki 最后更新
    synced_at         TEXT NOT NULL             -- 上一次从 CRM 同步的时间
);

CREATE TABLE crm_value_list (
    id             TEXT PRIMARY KEY,            -- CRM valueList id
    entity         TEXT NOT NULL,               -- '客户' / '活动' 等
    field          TEXT NOT NULL,               -- '行业' / '销售阶段' 等
    code           TEXT,                        -- 如 'ZLB-0008'
    label          TEXT,                        -- 中文标签
    value_int      INTEGER                      -- 若是数字枚举，记下数字
);
CREATE INDEX idx_value_list ON crm_value_list(entity, field);

CREATE TABLE crm_users (
    id          TEXT PRIMARY KEY,               -- ownerId
    display_name TEXT,                          -- from customItem1
    dim_depart   TEXT,
    feishu_open_id TEXT,                        -- 手工 or LDAP 对照表
    synced_at    TEXT NOT NULL
);
```

### 2.3 同步策略：**全量镜像**（改用）

重新评估：70K 行 × ~30 字段（~300 字节/行）= **~20MB**。SQLite 完全扛得住。**活动记录 4.9M 不搬**，只按需拉。

对 `crm_account` 采用**全量镜像 + 增量更新**：

| 阶段 | 触发 | 查询 | 频率 |
|------|------|------|------|
| **初始化** | 手动 `python -m src.crm.sync_account --init` | `SELECT * ... WHERE is_crm_deleted=0` 分批拉（每批 5000） | 一次 |
| **增量** | 内置调度器（APScheduler） | `WHERE updatedAt > :watermark OR is_crm_deleted=1` | 每 15 分钟 |
| **软删除清单** | 同上 | 同上 `is_crm_deleted` 分支 → 本地打 `crm_is_deleted=1` | |
| **夜间对账** | cron（0 3 * * *） | `SELECT count(), max(updatedAt)` 对比本地 | 每天 |

**不过滤部门 / owner**：全量镜像，UI 层再过滤。这样 H5 所有查询零网络，也支持 admin 看全局。

**不拉的字段：** ~250 个 `customItemNNN__c`（稀疏 + 含义未知）、`wxUnionID` 等微信相关、`CustomerInfoSearchDetails__c`（大文本）。**只搬** 2.4 字段映射表里的白名单字段。

### 2.3.1 字段映射表（ODS → 本地）

| ByteHouse 字段 | 类型 | 本地字段 | 转换 |
|---------------|------|---------|------|
| `id` | String | `customers.id` | 直取 |
| `accountName` | Nullable(String) | `customers.name` | `COALESCE(nm, '(未命名)')` |
| `ownerId` | Nullable(String) | `customers.crm_owner_id` | 直取 |
| `level` | Nullable(Int8) | `customers.crm_level` | 直取 |
| `industryId` | Nullable(Int8) | `customers.crm_industry_id` | 直取 |
| `dimDepart` | Nullable(String) | `customers.crm_dim_depart` | 直取 |
| `isCustomer` | Nullable(String) | `customers.crm_is_customer` | `'1'→1 / else→0` |
| `is_crm_deleted` | Int8 | `customers.crm_is_deleted` | 直取 |
| `recentActivityRecordTime` | DateTime64(3) | `customers.crm_recent_activity_at` | `.isoformat()` |
| `totalOrderAmount` | Nullable(String) | `customers.crm_total_order_amount` | `float(x) if x else None` |
| `accountScore` | Nullable(String) | `customers.crm_account_score` | 同上 |
| `state` | Nullable(String) | `customers.crm_state` | 直取 |
| `sharedTags` | Nullable(String) | `customers.crm_shared_tags` | 直取（逗号分隔） |
| `updatedAt` | DateTime64(3) | `customers.crm_updated_at` | `.isoformat()`；**watermark 字段** |
| `createdAt` | DateTime64(3) | `customers.crm_created_at` | 直取 |
| `accountChannel` | Nullable(Int8) | `customers.crm_channel` | 直取 |
| `parentAccountId` | Nullable(String) | `customers.crm_parent_id` | 直取 |

**`saleStageId` 不在这个表里**——它在 `crm_activityrecord`。每次同步完 account 后，跑一个子查询拿每个账号的**最近一次 saleStageId**：

```sql
SELECT activityRecordFrom_data AS account_id,
       argMax(saleStageId, createdAt) AS latest_stage_id
FROM DWD_YL.crm_activityrecord
WHERE activityRecordFrom = 11    -- 11 = 关联 account
  AND is_crm_deleted = 0
  AND saleStageId IS NOT NULL
  AND createdAt >= :watermark
GROUP BY account_id
```

然后 UPDATE 对应 customers 行的 `crm_sale_stage_id`。

### 2.3.2 本地新增字段

`customers` 表新增 `crm_shared_tags TEXT`、`crm_channel INTEGER`、`crm_parent_id TEXT`、`crm_created_at TEXT`（之前 schema 漏列，本节补齐）。

### 2.3.3 Watermark 表

```sql
CREATE TABLE sync_state (
    scope     TEXT PRIMARY KEY,        -- 'crm_account' / 'crm_stage' / 'crm_user' / 'crm_value_list'
    watermark TEXT NOT NULL,           -- CRM updatedAt 的最大值（ISO）
    last_run_at TEXT NOT NULL,
    last_run_ok INTEGER NOT NULL,      -- 0/1
    last_error TEXT,
    rows_total INTEGER NOT NULL,       -- 累计处理行
    rows_last INTEGER NOT NULL         -- 本次处理行
);
```

### 2.3.4 同步脚本骨架

```python
# src/crm/sync_account.py

BATCH = 5000
FIELDS = [
    "id", "accountName", "ownerId", "level", "industryId", "dimDepart",
    "isCustomer", "is_crm_deleted", "recentActivityRecordTime",
    "totalOrderAmount", "accountScore", "state", "sharedTags",
    "updatedAt", "createdAt", "accountChannel", "parentAccountId",
]

async def sync_accounts(init: bool = False) -> SyncResult:
    ch = bytehouse.client()
    watermark = "1970-01-01T00:00:00" if init else await sync_state.get("crm_account")

    total = 0
    max_ts = watermark
    while True:
        rows = ch.execute(f"""
            SELECT {','.join(FIELDS)}
            FROM ODS_YL.crm_account
            WHERE updatedAt > toDateTime64(%(ts)s, 3)
            ORDER BY updatedAt ASC
            LIMIT {BATCH}
        """, {"ts": watermark})

        if not rows:
            break

        async with db.transaction():
            for r in rows:
                await upsert_customer(dict(zip(FIELDS, r)))
                max_ts = max(max_ts, r[FIELDS.index("updatedAt")].isoformat())
        total += len(rows)
        watermark = max_ts

        if len(rows) < BATCH:
            break

    await refresh_latest_stages(since=watermark_before)
    await sync_state.commit("crm_account", watermark=max_ts, rows_last=total)
    return SyncResult(rows=total, watermark=max_ts)

async def upsert_customer(r: dict):
    # 关键：只覆盖 crm_* 字段，summary / wiki_path / local_updated_at 保留
    await db.execute("""
        INSERT INTO customers (id, name, crm_owner_id, crm_level, crm_industry_id,
            crm_dim_depart, crm_is_customer, crm_is_deleted,
            crm_recent_activity_at, crm_total_order_amount, crm_account_score,
            crm_state, crm_shared_tags, crm_updated_at, crm_created_at,
            crm_channel, crm_parent_id,
            summary, wiki_path, local_updated_at, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                '', ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            crm_owner_id = excluded.crm_owner_id,
            crm_level = excluded.crm_level,
            crm_industry_id = excluded.crm_industry_id,
            crm_dim_depart = excluded.crm_dim_depart,
            crm_is_customer = excluded.crm_is_customer,
            crm_is_deleted = excluded.crm_is_deleted,
            crm_recent_activity_at = excluded.crm_recent_activity_at,
            crm_total_order_amount = excluded.crm_total_order_amount,
            crm_account_score = excluded.crm_account_score,
            crm_state = excluded.crm_state,
            crm_shared_tags = excluded.crm_shared_tags,
            crm_updated_at = excluded.crm_updated_at,
            crm_channel = excluded.crm_channel,
            crm_parent_id = excluded.crm_parent_id,
            synced_at = excluded.synced_at
            -- summary / wiki_path / local_updated_at / crm_created_at 不覆盖
    """, [...])
```

### 2.3.5 初始化流程（首次全量）

```
1. python -m src.db.migrate                    # 建表
2. python -m src.crm.sync_valuelist            # 拉 980 行枚举，秒级
3. python -m src.crm.sync_users                # 拉 crm_User__c，~1 分钟
4. python -m src.crm.sync_account --init       # 全量拉 70K，~14 批，预计 2-5 分钟
5. python -m src.crm.sync_stages --init        # 拉 4.9M 活动里每账号最近 stage，耗时看 BH 性能
6. python -m src.server                        # 启动 H5，内置 15 分钟增量调度
```

**首次全量 TPS 估算：** BH ClickHouse 拉 5000 行 Nullable 列 × 17 字段 ≈ 2-4 秒；70K / 5000 = 14 批 × 3s ≈ 1 分钟（网络良好时）。

### 2.3.6 调度器（APScheduler）

```python
# src/jobs/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler

def start():
    sched = AsyncIOScheduler()
    sched.add_job(sync_accounts, "interval", minutes=15,
                  max_instances=1, coalesce=True, id="crm_account")
    sched.add_job(sync_stages,   "interval", minutes=15,
                  max_instances=1, coalesce=True, id="crm_stage")
    sched.add_job(sync_users,    "interval", hours=6, id="crm_user")
    sched.add_job(sync_valuelist,"interval", hours=24, id="crm_value_list")
    sched.add_job(reconcile,     "cron", hour=3, id="crm_reconcile")
    sched.start()
```

### 2.3.7 对账（每天凌晨 3 点）

```python
async def reconcile():
    # 1. 远端总数
    ch = bytehouse.client()
    remote_count = ch.execute("SELECT count() FROM ODS_YL.crm_account WHERE is_crm_deleted=0")[0][0]
    local_count  = await db.scalar("SELECT count() FROM customers WHERE crm_is_deleted=0")

    # 2. 远端 max(updatedAt)
    remote_max = ch.execute("SELECT max(updatedAt) FROM ODS_YL.crm_account")[0][0]
    local_max  = await db.scalar("SELECT max(crm_updated_at) FROM customers")

    # 3. 差异记录
    if abs(remote_count - local_count) > 50 or str(remote_max) > local_max:
        log.warning("drift detected", remote=remote_count, local=local_count)
        # 主动触发一次强同步：把 watermark 往前倒 1 小时，重拉
        await sync_state.rewind("crm_account", hours=1)
        await sync_accounts()
```

### 2.3.8 失败处理

- **单次同步失败**：`sync_state.last_run_ok=0 + last_error=msg`。watermark 不推进，下次仍从上次成功位置开始
- **BH 连接断**：`ClickHouseError` 捕获 → 记日志 → 不抛出到调度器（避免 APScheduler miss）
- **事务内部失败**：SQLite 回滚，watermark 不推进
- **单行转换失败**（如 `float('???')`）：跳过该行，累计 skip 计数，watermark 仍按该 batch 的 max 推进（避免无限卡住）

### 2.4 连接层

**独立只读 CH 连接池**（和 SQLite 完全隔离）：

```python
# src/crm/bytehouse.py
from clickhouse_driver import Client

_cli: Client | None = None
def client() -> Client:
    global _cli
    if _cli is None:
        _cli = Client(
            host=settings.BH_HOST,
            port=19000,
            user=settings.BH_USER,
            password=settings.BH_PASSWORD,
            secure=True, verify=False,
            settings={"virtual_warehouse": settings.BH_WAREHOUSE},
            send_receive_timeout=30,
        )
    return _cli
```

**绝对不把 CH 连接暴露给 FastAPI 路由——只让 sync worker 用。**

---

## 3. 后端设计建议

### 3.1 模块划分调整

```
src/
├── crm/
│   ├── bytehouse.py        # CH client 单例
│   ├── sync_account.py     # 增量拉 account
│   ├── sync_activity.py    # （可选）历史活动导入
│   ├── sync_valuelist.py   # 字典同步
│   ├── sync_users.py       # owner 同步
│   └── mapping.py          # CRM id ↔ 本地概念的翻译
├── jobs/
│   ├── scheduler.py        # asyncio cron（APScheduler 或自写）
│   └── sync_runner.py
└── ...（其他同 v0.2）
```

### 3.2 推荐新增路由

| 路由 | 作用 |
|------|------|
| `POST /internal/sync/crm-account` | 手动触发增量同步（admin） |
| `GET /internal/sync/status` | 看各 watermark |
| `GET /customers?owner=me` | 只看我名下的（默认） |
| `GET /customers?scope=depart` | 我部门名下 |

### 3.3 查询层（列表页 SQL 调整）—— 配合 cursor 分页

配合列表页 §10.2 的 cursor 分页（不用 OFFSET）：

```sql
-- 首页
SELECT
  c.id, c.name, c.crm_level, c.crm_is_customer,
  c.crm_recent_activity_at,
  il.label AS industry_name,
  sl.label AS stage_name,
  u.display_name AS owner_name,
  (SELECT count() FROM followup_records r
    WHERE r.customer_id = c.id
      AND r.meeting_date >= date('now','-30 days')) AS recent_count
FROM customers c
LEFT JOIN crm_value_list il ON il.entity='客户' AND il.field='行业' AND il.value_int=c.crm_industry_id
LEFT JOIN crm_value_list sl ON sl.id=c.crm_sale_stage_id
LEFT JOIN crm_users u        ON u.id=c.crm_owner_id
WHERE c.crm_is_deleted = 0
  AND c.crm_owner_id = :current_user_crm_id
ORDER BY c.crm_recent_activity_at DESC, c.id DESC
LIMIT 51;        -- 多拉 1 条判断 has_next

-- 下一页
... AND (c.crm_recent_activity_at, c.id) < (:cursor_ts, :cursor_id) ...
```

**索引：**

```sql
CREATE INDEX idx_customers_owner_recent
  ON customers(crm_owner_id, crm_recent_activity_at DESC, id DESC)
  WHERE crm_is_deleted = 0;
```

### 3.4 H5 用户态和 CRM 用户映射

**问题：** H5 登录拿到的是飞书 `open_id`，而 CRM 里是 `ownerId`（数字）。两者要映射。

**方案：**

1. **配置驱动**：`crm_users.feishu_open_id` 字段手工维护（CSV 导入 + admin 面板）
2. **邮箱匹配**：若 CRM 存了邮箱，拿飞书拉邮箱对上
3. **名字模糊匹配**：把 `customItem1`（"刘方毅Frank"）清洗后和飞书姓名对
4. **用户自助绑定**：首次登录时未匹配 → 显示下拉让用户选自己的 CRM 账号 → 保存映射

**推荐：2 + 4 组合**。

### 3.5 Ingest 流水线与 CRM 的关系

Ingest 的职责**不变**：docx → wiki → 结构化提取 → 写 `followup_records`。

**但新增一步**：ingest 完成后，异步往 CRM 回写一条活动记录（如果需要）：

```
完成 SQL 写入
  ↓
（可选）POST CRM API: 创建 activityRecord（content = 会议摘要 + 链接）
  ↓
记录 crm_activity_id 到 followup_records 以便追溯
```

**注意：** 这一步要走 CRM 的 REST/写 API，不能走 ByteHouse（BH 是只读数仓）。需要拿 CRM 应用凭证。先做单向（CRM→我们），回写放 Phase 4。

---

## 4. 前端设计建议

### 4.1 列表卡片字段调整

基于真实字段重排：

```
╭─────────────────────────────╮
│                         ⊕    │
│  MaiMed GmbH                 │ ← accountName (17px semibold)
│  L1客户 · 医疗耗材 · 已成交   │ ← level label · industry · isCustomer
│                              │
│  近 30 天跟进      4 次       │
│  客户所有人    🙂 刘方毅Frank │ ← crm_users.display_name
│  订单金额         ¥27.5M     │ ← 新增：totalOrderAmount（可选）
╰─────────────────────────────╯
```

**变化：**
- Tag 行从 `方案沟通 · 未付费` 改为 `L{level} · {industry} · {isCustomer 1→成交 / 0→线索}`
- 保留 stage 颜色映射，但来源换成 `crm_sale_stage_id` join ValueList 后的 label
- 新增可选"订单金额"行（视觉上可收起）

### 4.2 Header 权限 Scope 切换（新增）

因为单用户可能持有 9K 客户，列表默认按"我"过滤。建议顶部加一个极小 scope 切换：

```
客户助手                 [我 ▾]
```

下拉：`我的` / `我部门` / `全部（admin only）`

只在账号数 > 500 时才显露，否则隐藏。

### 4.3 搜索字段扩充

原设计：`name | aliases | industry`

改为：
```sql
WHERE c.name LIKE %q%
   OR c.crm_state LIKE %q%        -- 地域搜索
   OR EXISTS (SELECT 1 FROM crm_value_list v
              WHERE v.value_int = c.crm_industry_id
                AND v.label LIKE %q%)
```

（`aliases` 暂时移除——CRM 没这个字段；若要加得我们在 wiki 里维护）

### 4.4 详情页新增 CRM 只读区

详情页在 summary 之上加一个"CRM 快照"灰色区：

```
┌─ CRM 快照（来自 ODS_YL）──────────┐
│ L1 客户 · 医疗耗材                │
│ 订单总额 ¥27.5M · 评分 100        │
│ 最近活动 2026-04-22 21:01         │
│ 最近拜访 —                        │
│ 公海状态：未入公海                 │
│                 [在 CRM 中打开 ↗] │
└──────────────────────────────────┘
```

`[在 CRM 中打开 ↗]` 深链到 CRM Web（URL 模板需销售易提供）。

### 4.5 "跟进记录" Tab 的双来源

CRM 里已经有 4.9M 活动记录了。我们的 `followup_records` 只是其中**由 docx ingest 来的那部分**。

两种展示策略：

| 策略 | UI |
|------|-----|
| **分 tab** | "会议纪要（本地）" / "所有活动（CRM）" 两个子 tab |
| **统一时间线** | 一条时间线，CRM 原生活动是灰色小条，我们 ingest 的是卡片 |

**推荐策略 2（统一）**，视觉层级靠颜色/图标区分：

```
📅 2026-04-22 21:01    [CRM]
   电话沟通
   意向度：中 · 阶段：方案沟通

📝 2026-04-21 15:30    [会议纪要]
   UPE 客户来访纪要
   确认核心需求，要求 Q2 报价
   阶段：需求确认 → 方案沟通
   🔗 原文链接
```

### 4.6 空状态文案更新

| 场景 | 原 | 新 |
|------|----|----|
| 列表空 | "暂无客户，请确认 CRM 已同步" | "暂无客户在你名下" + 副文案 "CRM 上次同步：{HH:mm}" + [立即同步] 按钮（可选） |
| 详情页无 followup | "这个客户还没有跟进记录" | "这个客户还没有会议纪要"（因为 CRM 侧可能有活动，只是我们没 ingest） |

---

## 5. 安全 / 运维建议

### 5.1 凭证管理

- **立即 rotate** 这次聊天里贴的 ByteHouse 密码（已暴露）
- 新密码放 `.env` + `.gitignore`，不要再贴
- 生产环境用 secret manager（若有 k8s），开发环境 `.env.local`

### 5.2 CH 只读账号

申请一个**仅 SELECT** 权限的 BH 账号，不给 INSERT/CREATE，对 `ODS_YL`/`DWD_YL` 只读。

### 5.3 拉数据量控制

- 单次同步 `LIMIT 5000`，翻页 watermark
- 全量夜间跑一次做对账（`count()` 核对）
- 失败回退到上一个 watermark，不半提交

### 5.4 PII 与合规

- `crm_account` 里有手机号、邮箱、联系人姓名 → 属于个人数据
- H5 日志不要打这些字段
- wiki/ 仓库**不要** push 到外网——现已要求私有，继续保持

---

## 6. 需向产品确认的问题

1. **同步频率**：15 分钟增量能接受？还是要实时（webhook）？销售易是否提供 webhook？
2. **权限 scope**：销售只看自己 vs 看部门，admin 看全部的阈值
3. **写回 CRM**：ingest 出的 summary 要不要自动在 CRM 里创建一条 activityRecord？
4. **覆盖哪个 isCustomer**：只管 `isCustomer='1'`（19K 正式客户）还是包括线索（整 70K）？
5. **ownerId ↔ 飞书 open_id 的对照来源**：HR 系统 / 飞书通讯录 / 手工维护？
6. **CRM 深链 URL 模板**：`https://crm.intco.com/account/{id}`？
7. **是否需要 `ADS_YL` / `AI_DW_YL` 这些层**：它们可能有已经聚合好的"客户画像"视图，比我们在 SQLite 再算一遍划算
8. **流失 `loss` 字段全 NULL**：是弃用还是未来要启用？若启用我们要跟着加过滤

---

## 7. 下一步行动

建议拆两步：

**Step A（1 天）**：
- 申请 BH 只读账号 + rotate 密码
- 跑一次全量 `crm_ValueList` / `crm_User__c_json` 同步脚本，核对枚举
- 确认问题 1-8

**Step B（接 v0.2 Phase 1）**：
- 按本文档调整 `customers` schema
- 实现 `src/crm/sync_account.py` + `sync_users.py` + `sync_valuelist.py`
- 改造列表查询 SQL
- Phase 1 验收再加一条："增量同步 1 天数据无误 + 详情页展示 CRM 快照"
