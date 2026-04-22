"""v0.2 schema 定义。

改动 schema 就改这里，`migrate()` 会把所有 CREATE IF NOT EXISTS 跑一遍。
真正破坏性改动（ALTER / DROP）后续走手写迁移脚本，别放这里。
"""

SCHEMA = [
    # =========================================================
    # 客户镜像（从 ByteHouse ODS_YL.crm_account 同步）+ 本地增量
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS customers (
        id                        TEXT PRIMARY KEY,           -- CRM id（数字字符串）
        name                      TEXT NOT NULL,              -- accountName
        -- CRM 镜像字段（同步覆盖）
        crm_owner_id              TEXT,
        crm_level                 INTEGER,
        crm_industry_id           INTEGER,
        crm_dim_depart            TEXT,
        crm_is_customer           INTEGER NOT NULL DEFAULT 0, -- '1'→1
        crm_is_deleted            INTEGER NOT NULL DEFAULT 0,
        crm_recent_activity_at    TEXT,                       -- ISO
        crm_total_order_amount    REAL,
        crm_account_score         REAL,
        crm_state                 TEXT,
        crm_shared_tags           TEXT,
        crm_channel               INTEGER,
        crm_parent_id             TEXT,
        crm_sale_stage_id         TEXT,                       -- 由 sync_stages 更新
        crm_created_at            TEXT,
        crm_updated_at            TEXT NOT NULL,              -- watermark 字段
        -- 本地增量（sync 不覆盖）
        summary                   TEXT NOT NULL DEFAULT '',
        wiki_path                 TEXT NOT NULL DEFAULT '',
        local_updated_at          TEXT,
        -- 元信息
        synced_at                 TEXT NOT NULL               -- 上次 sync 写入时间
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_customers_owner_recent "
    "ON customers(crm_owner_id, crm_recent_activity_at DESC, id DESC) "
    "WHERE crm_is_deleted = 0",
    "CREATE INDEX IF NOT EXISTS idx_customers_recent "
    "ON customers(crm_recent_activity_at DESC, id DESC) "
    "WHERE crm_is_deleted = 0",
    "CREATE INDEX IF NOT EXISTS idx_customers_updated "
    "ON customers(crm_updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_customers_name "
    "ON customers(name)",

    # =========================================================
    # CRM 枚举字典（从 crm_ValueList 同步）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS crm_value_list (
        id          TEXT PRIMARY KEY,
        entity      TEXT,           -- customItem2（如 '客户'）
        field       TEXT,           -- customItem3（如 '行业' / '国家'）
        code        TEXT,           -- name 字段（如 'ZLB-0008'）
        label       TEXT,           -- customItem1（中文标签）
        synced_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_value_list_entity "
    "ON crm_value_list(entity, field)",

    # =========================================================
    # CRM 用户镜像（负责人展示）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS crm_users (
        id                 TEXT PRIMARY KEY,
        display_name       TEXT,       -- customItem1
        dim_depart         TEXT,
        feishu_open_id     TEXT,       -- 手工映射，sync 不覆盖
        created_at         TEXT,
        updated_at         TEXT,
        synced_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_crm_users_feishu "
    "ON crm_users(feishu_open_id) WHERE feishu_open_id IS NOT NULL",

    # =========================================================
    # 跟进记录（AI 从飞书聊天 / 会议链接 ingest）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS followup_records (
        id             TEXT PRIMARY KEY,
        customer_id    TEXT NOT NULL,
        owner_id       TEXT,
        source_type    TEXT,
        source_url     TEXT,
        source_title   TEXT,
        summary        TEXT,
        meeting_date   TEXT NOT NULL,
        created_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fr_customer_date "
    "ON followup_records(customer_id, meeting_date DESC)",

    # =========================================================
    # 同步状态（watermark）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        scope         TEXT PRIMARY KEY,   -- 'crm_account' / 'crm_user' / 'crm_value_list' / 'crm_stage'
        watermark     TEXT NOT NULL,      -- CRM updatedAt 的最大值（ISO）
        last_run_at   TEXT NOT NULL,
        last_run_ok   INTEGER NOT NULL,
        last_error    TEXT,
        rows_total    INTEGER NOT NULL DEFAULT 0,
        rows_last     INTEGER NOT NULL DEFAULT 0
    )
    """,
]
