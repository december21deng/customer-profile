"""H5 客户列表 Web app。

    uvicorn src.web.app:app --reload --port 8001

设计对齐 docs/h5-customer-list-design.md。

路由：
    GET /                                    → 302 /customers
    GET /customers?tab=customers&q=&cursor=  → 列表页（SSR 首屏）
    GET /_p/customer-list?...                → HTMX 分页 / 搜索片段
    GET /customers/{id}                      → 详情
    GET /healthz

分页用 (crm_updated_at, id) DESC 复合 cursor。
索引 idx_customers_updated 已在 schema.py 建好。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.db.connection import connect
from src.web.auth import AuthMiddleware, router as auth_router
from src.web.followup import router as followup_router

logger = logging.getLogger(__name__)

# ---- Sentry（可选）--------------------------------------------------
# 设置了 SENTRY_DSN 才启用；没设就静悄悄跳过，本地开发不受影响。
_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_sentry_dsn,
            environment=os.environ.get("APP_ENV", "dev"),
            release=os.environ.get("FLY_MACHINE_VERSION") or os.environ.get("GIT_SHA", "unknown"),
            traces_sample_rate=0.0,      # 性能追踪先关（免费额度有限）
            profiles_sample_rate=0.0,
            send_default_pii=False,      # 不自动带 cookies / 请求体
        )
        logger.info("Sentry initialized (env=%s)", os.environ.get("APP_ENV", "dev"))
    except Exception:
        logger.exception("Sentry init failed; continuing without error reporting")

DetailTab = Literal["info", "followup"]

PAGE_SIZE = 30
FOLLOWUP_PAGE_SIZE = 20
Tab = Literal["customers", "records"]

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="客户助手")
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
app.include_router(followup_router)


# ---- cursor 编解码 -------------------------------------------------
def _encode_cursor(ts: str, cid: str) -> str:
    raw = f"{ts}|{cid}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(s: str) -> tuple[str, str] | None:
    if not s:
        return None
    try:
        pad = "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(s + pad).decode("utf-8")
        ts, cid = raw.split("|", 1)
        return ts, cid
    except Exception:
        return None


# ---- 数据层 -------------------------------------------------------
def _fetch_page(
    cursor: tuple[str, str] | None,
    owner: str | None,
    q: str | None,
    limit: int,
    uid: str = "",
) -> tuple[list[dict], bool]:
    """取一页客户。按个性化"最近查看"排序。

    排序规则：
      COALESCE(我对这条的 last_viewed_at,
               客户的 crm_recent_activity_at,
               '1970-01-01') DESC

    效果：我看过的按查看时间浮顶，没看过的按最近跟进活动排。
    uid 为空（未登录）时 LEFT JOIN 产生全 NULL，退化为 recent_activity_at 排序。
    """
    SORT_EXPR = "COALESCE(uv.last_viewed_at, c.crm_recent_activity_at, '1970-01-01')"

    sql = [
        "SELECT",
        "  c.id, c.name,",
        "  c.crm_is_customer   AS is_customer,",
        "  c.crm_state         AS state,",
        "  c.crm_sale_stage_id AS stage_id,",
        "  c.crm_owner_id      AS owner_id,",
        "  c.crm_recent_activity_at AS recent_at,",
        "  c.crm_updated_at    AS updated_at,",
        "  u.display_name      AS owner_name,",
        "  v.label             AS industry,",
        "  uv.last_viewed_at   AS last_viewed_at,",
        f" {SORT_EXPR}          AS sort_key,",
        "  (SELECT COUNT(*) FROM followup_records r",
        "     WHERE r.customer_id = c.id",
        "       AND r.meeting_date >= date('now','-30 days')) AS recent_count",
        "FROM customers c",
        "LEFT JOIN crm_users u ON u.id = c.crm_owner_id",
        "LEFT JOIN crm_value_list v ON v.field='行业' AND v.id = CAST(c.crm_industry_id AS TEXT)",
        "LEFT JOIN user_customer_views uv ON uv.user_id = ? AND uv.customer_id = c.id",
        "WHERE c.crm_is_deleted = 0",
    ]
    params: list = [uid or ""]

    if owner:
        sql.append("AND c.crm_owner_id = ?")
        params.append(owner)

    if q:
        sql.append("AND c.name LIKE ?")
        params.append(f"%{q}%")

    if cursor is not None:
        ts, cid = cursor
        sql.append(
            f"AND ({SORT_EXPR} < ? "
            f"     OR ({SORT_EXPR} = ? AND c.id < ?))"
        )
        params.extend([ts, ts, cid])

    sql.append(f"ORDER BY {SORT_EXPR} DESC, c.id DESC")
    sql.append("LIMIT ?")
    params.append(limit + 1)

    conn = connect()
    try:
        cur = conn.execute("\n".join(sql), params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    has_next = len(rows) > limit
    return rows[:limit], has_next


def _next_cursor(rows: list[dict]) -> str:
    last = rows[-1]
    # sort_key 是 SELECT 里按当前 sort 计算出来的值，encode 它而不是固定 updated_at
    return _encode_cursor(last.get("sort_key") or last["updated_at"] or "", last["id"])


# ---- followup 分页 -------------------------------------------------
def _fetch_followup_page(
    cursor: tuple[str, str] | None,
    customer_id: str | None,
    q: str | None,
    limit: int,
) -> tuple[list[dict], bool]:
    """取一页 followup。按 (meeting_date, id) DESC 排。

    customer_id 为空 → 全局跟进记录列表。
    q 匹配客户名 / background / summary / location。
    """
    sql = [
        "SELECT",
        "  r.id, r.customer_id, r.meeting_date, r.location,",
        "  r.our_attendees, r.client_attendees, r.other_attendees,",
        "  r.background, r.photo_image_key,",
        "  r.meeting_title, r.progress_line,",
        "  c.name AS customer_name,",
        "  ij.status AS ingest_status",
        "FROM followup_records r",
        "JOIN customers c ON c.id = r.customer_id",
        "LEFT JOIN ingest_jobs ij ON ij.record_id = r.id",
        "WHERE 1=1",
    ]
    params: list = []

    if customer_id:
        sql.append("AND r.customer_id = ?")
        params.append(customer_id)

    if q:
        # followup_records.summary 已废弃，搜索不再包含
        sql.append(
            "AND (c.name LIKE ? OR r.meeting_title LIKE ? "
            "     OR r.progress_line LIKE ? "
            "     OR r.background LIKE ?)"
        )
        kw = f"%{q}%"
        params.extend([kw, kw, kw, kw])

    if cursor is not None:
        md, rid = cursor
        sql.append(
            "AND (r.meeting_date < ? "
            "     OR (r.meeting_date = ? AND r.id < ?))"
        )
        params.extend([md, md, rid])

    sql.append("ORDER BY r.meeting_date DESC, r.id DESC")
    sql.append("LIMIT ?")
    params.append(limit + 1)

    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute("\n".join(sql), params).fetchall()]
    finally:
        conn.close()

    has_next = len(rows) > limit
    return rows[:limit], has_next


def _next_followup_cursor(rows: list[dict]) -> str:
    last = rows[-1]
    return _encode_cursor(last["meeting_date"], last["id"])


def _decorate_customers(rows: list[dict]) -> list[dict]:
    """给客户卡片加派生字段：viewed_at_display（人读的"上次查看"相对时间）。"""
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        lv = d.get("last_viewed_at")
        d["viewed_at_display"] = _relative_time(lv) if lv else ""
        out.append(d)
    return out


_INGEST_IN_PROGRESS = {"queued", "fetching", "ingesting", "extracting", "committing"}


def _parse_attendee_names(raw: str | None) -> list[str]:
    """our_attendees: [{id,name,avatar}...]   client/other: ['name1', 'name2']"""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    names: list[str] = []
    for it in v:
        if isinstance(it, str):
            s = it.strip()
            if s:
                names.append(s)
        elif isinstance(it, dict):
            n = (it.get("name") or "").strip()
            if n:
                names.append(n)
    return names


def _decorate_followups(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["our_names"] = _parse_attendee_names(d.get("our_attendees"))
        d["client_names"] = _parse_attendee_names(d.get("client_attendees"))
        d["other_names"] = _parse_attendee_names(d.get("other_attendees"))
        d["date_display"] = _format_meeting_date(d.get("meeting_date"))
        # AI 状态：pipeline 还在跑 → "AI 处理中…"；失败 → 提示失败；成功（字段有值）→ 不用标签
        status = (d.get("ingest_status") or "").lower()
        if status in _INGEST_IN_PROGRESS:
            d["ai_state"] = "processing"
        elif status == "failed":
            d["ai_state"] = "failed"
        else:
            d["ai_state"] = "done"
        out.append(d)
    return out


# ---- 路由 ---------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/customers", status_code=302)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    tab: Tab = Query("customers"),
    owner: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    uid = getattr(request.state, "uid", "") or ""

    if tab == "records":
        rows, has_next = _fetch_followup_page(None, None, q, FOLLOWUP_PAGE_SIZE)
        return templates.TemplateResponse("customers.html", {
            "request": request,
            "tab": "records",
            "followups": _decorate_followups(rows),
            "next_cursor": _next_followup_cursor(rows) if has_next else None,
            "show_customer": True,
            "customer_id": "",
            "owner": owner or "",
            "q": q or "",
        })

    rows, has_next = _fetch_page(None, owner, q, PAGE_SIZE, uid=uid)
    return templates.TemplateResponse("customers.html", {
        "request": request,
        "tab": "customers",
        "rows": _decorate_customers(rows),
        "next_cursor": _next_cursor(rows) if has_next else None,
        "owner": owner or "",
        "q": q or "",
    })


@app.get("/_p/customer-list", response_class=HTMLResponse)
def customer_list_partial(
    request: Request,
    tab: Tab = Query("customers"),
    cursor: str = Query(""),
    owner: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """HTMX 片段：用于翻页（追加）或搜索（替换整个列表）。"""
    uid = getattr(request.state, "uid", "") or ""

    if tab == "records":
        cur = _decode_cursor(cursor) if cursor else None
        rows, has_next = _fetch_followup_page(cur, None, q, FOLLOWUP_PAGE_SIZE)
        return templates.TemplateResponse("_rows.html", {
            "request": request,
            "tab": "records",
            "followups": _decorate_followups(rows),
            "next_cursor": _next_followup_cursor(rows) if has_next else None,
            "show_customer": True,
            "customer_id": "",
            "owner": owner or "",
            "q": q or "",
        })

    cur = _decode_cursor(cursor) if cursor else None
    rows, has_next = _fetch_page(cur, owner, q, PAGE_SIZE, uid=uid)
    return templates.TemplateResponse("_rows.html", {
        "request": request,
        "tab": "customers",
        "rows": _decorate_customers(rows),
        "next_cursor": _next_cursor(rows) if has_next else None,
        "owner": owner or "",
        "q": q or "",
    })


@app.get("/_p/customer-followups", response_class=HTMLResponse)
def customer_followups_partial(
    request: Request,
    customer_id: str = Query(...),
    cursor: str = Query(""),
):
    """客户详情 followup tab 的分页片段（追加）。"""
    cur = _decode_cursor(cursor) if cursor else None
    rows, has_next = _fetch_followup_page(cur, customer_id, None, FOLLOWUP_PAGE_SIZE)
    return templates.TemplateResponse("_followup_rows.html", {
        "request": request,
        "followups": _decorate_followups(rows),
        "next_cursor": _next_followup_cursor(rows) if has_next else None,
        "customer_id": customer_id,
        "show_customer": False,
    })


def _parse_ts(ts: str) -> datetime | None:
    """crm_* 时间字段大多是 'YYYY-MM-DD HH:MM:SS'，也兼容 ISO。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _relative_time(ts: str | None) -> str:
    dt = _parse_ts(ts) if ts else None
    if dt is None:
        return "—"
    # 去掉 tz 方便和 now 相减
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    sec = int((datetime.now() - dt).total_seconds())
    if sec < 0:
        return ts[:10]
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    if sec < 86400 * 30:
        return f"{sec // 86400} 天前"
    if sec < 86400 * 365:
        return f"{sec // (86400 * 30)} 个月前"
    return f"{sec // (86400 * 365)} 年前"


def _count_json_list(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        import json as _json
        v = _json.loads(raw)
        return len(v) if isinstance(v, list) else 0
    except Exception:
        return 0


def _first_line(s: str, max_len: int = 40) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    head = s.splitlines()[0].strip()
    if len(head) > max_len:
        head = head[:max_len] + "…"
    return head


def _format_meeting_date(s: str | None) -> str:
    """'2026-04-20T14:30' → '4月20日 · 14:30'。"""
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return s[:16]
    return f"{dt.month}月{dt.day}日 · {dt.strftime('%H:%M')}"


def _format_amount(v) -> str | None:
    """累计订单额：为 0 / NULL / 非数字 → None（模板里显示 —）。"""
    if v in (None, "", 0, "0"):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"¥{n:,.0f}"


def _track_view(uid: str, customer_id: str) -> None:
    """记录一次"查看"。失败静默，不让详情页因为记 view 失败而挂。"""
    if not uid or not customer_id:
        return
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO user_customer_views(user_id, customer_id, last_viewed_at, view_count) "
                "VALUES(?, ?, ?, 1) "
                "ON CONFLICT(user_id, customer_id) DO UPDATE SET "
                "  last_viewed_at = excluded.last_viewed_at, "
                "  view_count = user_customer_views.view_count + 1",
                (uid, customer_id, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("track view failed for uid=%s cid=%s", uid, customer_id)


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(
    request: Request,
    customer_id: str,
    tab: DetailTab = Query("info"),
):
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT c.*, u.display_name AS owner_name,
                   v.label AS industry
            FROM customers c
            LEFT JOIN crm_users u ON u.id = c.crm_owner_id
            LEFT JOIN crm_value_list v
                   ON v.field='行业' AND v.id = CAST(c.crm_industry_id AS TEXT)
            WHERE c.id = ?
            """,
            (customer_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return HTMLResponse(
            "<p style='padding:40px;text-align:center;color:#756E62'>未找到该客户</p>",
            status_code=404,
        )

    # 记录这次访问，驱动"最近查看"排序
    uid = getattr(request.state, "uid", "")
    _track_view(uid, customer_id)

    c = dict(row)

    followups: list[dict] = []
    followup_next_cursor: str | None = None
    if tab == "followup":
        rows, has_next = _fetch_followup_page(None, customer_id, None, FOLLOWUP_PAGE_SIZE)
        followups = _decorate_followups(rows)
        followup_next_cursor = _next_followup_cursor(rows) if has_next else None

    return templates.TemplateResponse(
        "customer_detail.html",
        {
            "request": request,
            "c": c,
            "tab": tab,
            "recent_at_display": _relative_time(c.get("crm_recent_activity_at")),
            "order_amount_display": _format_amount(c.get("crm_total_order_amount")),
            "followups": followups,
            "followup_next_cursor": followup_next_cursor,
        },
    )
