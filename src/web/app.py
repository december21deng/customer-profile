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
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.db.connection import connect

PAGE_SIZE = 30
Tab = Literal["customers", "records"]

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="客户助手")


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
) -> list[dict]:
    """
    取一页客户。按 (crm_updated_at, id) DESC 排（对齐设计文档 §5）。
    返回字段命名对齐模板消费。
    """
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
        "  (SELECT COUNT(*) FROM followup_records r",
        "     WHERE r.customer_id = c.id",
        "       AND r.meeting_date >= date('now','-30 days')) AS recent_count",
        "FROM customers c",
        "LEFT JOIN crm_users u ON u.id = c.crm_owner_id",
        "LEFT JOIN crm_value_list v ON v.field='行业' AND v.id = CAST(c.crm_industry_id AS TEXT)",
        "WHERE c.crm_is_deleted = 0",
    ]
    params: list = []

    if owner:
        sql.append("AND c.crm_owner_id = ?")
        params.append(owner)

    if q:
        sql.append("AND c.name LIKE ?")
        params.append(f"%{q}%")

    if cursor is not None:
        ts, cid = cursor
        sql.append(
            "AND (c.crm_updated_at < ? "
            "     OR (c.crm_updated_at = ? AND c.id < ?))"
        )
        params.extend([ts, ts, cid])

    sql.append("ORDER BY c.crm_updated_at DESC, c.id DESC")
    sql.append("LIMIT ?")
    params.append(limit + 1)  # +1 判断是否有下一页

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
    return _encode_cursor(last["updated_at"], last["id"])


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
    if tab == "records":
        # 跟进记录 tab：followup_records 还没建，直接渲空态
        return templates.TemplateResponse("customers.html", {
            "request": request,
            "tab": "records",
            "rows": [],
            "next_cursor": None,
            "owner": owner or "",
            "q": q or "",
        })

    rows, has_next = _fetch_page(None, owner, q, PAGE_SIZE)
    return templates.TemplateResponse("customers.html", {
        "request": request,
        "tab": "customers",
        "rows": rows,
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
    if tab == "records":
        return templates.TemplateResponse("_rows.html", {
            "request": request, "tab": "records",
            "rows": [], "next_cursor": None,
            "owner": owner or "", "q": q or "",
        })

    cur = _decode_cursor(cursor) if cursor else None
    rows, has_next = _fetch_page(cur, owner, q, PAGE_SIZE)
    return templates.TemplateResponse("_rows.html", {
        "request": request,
        "tab": "customers",
        "rows": rows,
        "next_cursor": _next_cursor(rows) if has_next else None,
        "owner": owner or "",
        "q": q or "",
    })


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(request: Request, customer_id: str):
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

    return templates.TemplateResponse(
        "customer_detail.html",
        {"request": request, "c": dict(row)},
    )
