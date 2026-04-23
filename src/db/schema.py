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
        id                TEXT PRIMARY KEY,
        customer_id       TEXT NOT NULL,
        owner_id          TEXT,                   -- 创建人 open_id
        meeting_date      TEXT NOT NULL,          -- ISO 'YYYY-MM-DDTHH:MM'
        -- 手动录入字段（manual 必填，ingest 场景为 NULL）
        location          TEXT,
        our_attendees     TEXT,                   -- JSON: [{"open_id":..,"name":..}]
        client_attendees  TEXT,                   -- 逗号/顿号分隔原文
        background        TEXT,
        minutes_doc_url   TEXT,                   -- 会议纪要 docx URL 原文
        minutes_doc_id    TEXT,                   -- 从 URL 提取的 doc_id
        transcript_url    TEXT,                   -- 妙记 URL（存档，AI ingest 时用）
        photo_image_key   TEXT,                   -- Lark im/v1/images 的 image_key
        -- 共用字段（ingest 也会用）
        source_type       TEXT,                   -- 'manual' | 'chat' | 'meeting_link'
        source_url        TEXT,
        source_title      TEXT,
        summary           TEXT,                   -- 详情页的长摘要（80-200 字）
        meeting_title     TEXT NOT NULL DEFAULT '',  -- 列表用，≤20 字主题
        progress_line     TEXT NOT NULL DEFAULT '',  -- 列表用，20-40 字一句话进展
        created_at        TEXT NOT NULL
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

    # =========================================================
    # Ingest Jobs（跟进记录 ingest pipeline 的状态）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS ingest_jobs (
        record_id     TEXT PRIMARY KEY,          -- FK followup_records.id
        customer_id   TEXT NOT NULL,
        status        TEXT NOT NULL,             -- queued|fetching|ingesting|extracting|committing|done|failed
        error         TEXT,                      -- 失败原因（最后一条）
        attempts      INTEGER NOT NULL DEFAULT 0,
        started_at    TEXT NOT NULL,             -- ISO 第一次进入 pipeline 的时间
        updated_at    TEXT NOT NULL,             -- ISO 每次 set_status 更新
        finished_at   TEXT,                      -- done/failed 时写入
        cost_usd      REAL                       -- 本次 pipeline 估算成本（agent + extract）
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status "
    "ON ingest_jobs(status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ingest_jobs_customer "
    "ON ingest_jobs(customer_id, started_at DESC)",

    # =========================================================
    # 用户飞书 token（OAuth 回调后存，用于调用 user_access_token 身份的飞书 API
    # 例如 contact:user:search 搜同事）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS user_tokens (
        open_id              TEXT PRIMARY KEY,
        access_token         TEXT NOT NULL,
        refresh_token        TEXT NOT NULL,
        access_expires_at    TEXT NOT NULL,   -- ISO，2h 有效期
        refresh_expires_at   TEXT NOT NULL,   -- ISO，30d 有效期
        updated_at           TEXT NOT NULL
    )
    """,

    # =========================================================
    # 个人"最近查看"历史（列表个性化排序用）
    # =========================================================
    """
    CREATE TABLE IF NOT EXISTS user_customer_views (
        user_id        TEXT NOT NULL,            -- 飞书 open_id（或 dev 的 "pwd-user"）
        customer_id    TEXT NOT NULL,
        last_viewed_at TEXT NOT NULL,            -- ISO
        view_count     INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (user_id, customer_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_uv_user_time "
    "ON user_customer_views(user_id, last_viewed_at DESC)",
]
