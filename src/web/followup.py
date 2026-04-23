"""跟进记录：手动录入 + 图片代理。

路由：
    GET  /customers/{id}/followup/new   → 表单页（SSR）
    POST /customers/{id}/followup       → 提交（multipart）
    GET  /api/image/{image_key}         → 图片代理（从 Lark 流式透传）

设计文档：docs/followup-record-design.md
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from src.db.connection import connect, transaction
from src.ingest.pipeline import run as run_ingest_pipeline
from src.lark_client import (
    fetch_docx_raw,
    get_user_access_token,
    search_feishu_users,
    sign_jssdk,
)
from src import photo_storage

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---- 限制 ----------------------------------------------------------
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10MB（Lark im/v1/images 上限）
ALLOWED_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/gif"}
MAX_LOCATION = 100
MAX_BACKGROUND = 500
MAX_CLIENT_ATTENDEE_NAME = 40


# docx URL 里抽 doc_id
DOC_ID_PATTERNS = [
    re.compile(r"/docx/([A-Za-z0-9]+)"),
    re.compile(r"/wiki/([A-Za-z0-9]+)"),
    re.compile(r"/docs/([A-Za-z0-9]+)"),
]


def _extract_doc_id(url: str) -> str | None:
    for pat in DOC_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _parse_meeting_date(s: str) -> str | None:
    """`datetime-local` 输入 'YYYY-MM-DDTHH:MM'。不超过现在。"""
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt > datetime.now():
        return None
    return dt.isoformat(timespec="minutes")


def _fetch_customer(customer_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, name FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _render_form(
    request: Request,
    customer: dict,
    errors: dict[str, str] | None = None,
    values: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "followup_new.html",
        {
            "request": request,
            "customer": customer,
            "errors": errors or {},
            "values": values or {},
        },
        status_code=status_code,
    )


# ---- 路由 ---------------------------------------------------------
@router.get("/customers/{customer_id}/followup/new", response_class=HTMLResponse)
def followup_new(request: Request, customer_id: str):
    customer = _fetch_customer(customer_id)
    if customer is None:
        return HTMLResponse(
            "<p style='padding:40px;text-align:center;color:#756E62'>未找到该客户</p>",
            status_code=404,
        )
    return _render_form(request, customer)


@router.post("/customers/{customer_id}/followup")
async def followup_submit(
    request: Request,
    customer_id: str,
    background_tasks: BackgroundTasks,
    minutes_url: str = Form(...),
    transcript_url: str = Form(...),
    meeting_date: str = Form(...),
    location: str = Form(...),
    our_attendees: str = Form(...),      # JSON: [{"open_id":..,"name":..}]
    client_attendees: str = Form(...),   # JSON: ["name1", "name2"]
    background: str = Form(...),
    photo: UploadFile = File(...),
):
    customer = _fetch_customer(customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")

    values = {
        "minutes_url": minutes_url,
        "transcript_url": transcript_url,
        "meeting_date": meeting_date,
        "location": location,
        "our_attendees": our_attendees,
        "client_attendees": client_attendees,
        "background": background,
    }
    errors: dict[str, str] = {}

    # 1. 会议纪要 URL → doc_id
    doc_id = _extract_doc_id(minutes_url.strip())
    if not doc_id:
        errors["minutes_url"] = "请填写有效的飞书会议纪要链接（docx/wiki）"

    # 1b. 妙记 URL（只校验非空 + 是 http(s)）
    transcript_s = transcript_url.strip()
    if not transcript_s:
        errors["transcript_url"] = "必填"
    elif not (transcript_s.startswith("http://") or transcript_s.startswith("https://")):
        errors["transcript_url"] = "请填写有效的妙记链接"

    # 2. 时间
    meeting_iso = _parse_meeting_date(meeting_date.strip())
    if not meeting_iso:
        errors["meeting_date"] = "请选择有效时间（不可晚于当前）"

    # 3. 地点
    location_s = location.strip()
    if not location_s:
        errors["location"] = "必填"
    elif len(location_s) > MAX_LOCATION:
        errors["location"] = f"不超过 {MAX_LOCATION} 字"

    # 4. 我方参会人员（JSON）
    try:
        our_list = json.loads(our_attendees) if our_attendees else []
    except json.JSONDecodeError:
        our_list = []
    if not isinstance(our_list, list) or not our_list:
        errors["our_attendees"] = "至少选择 1 位同事"

    # 5. 客户参会人员（JSON array of strings）
    try:
        client_list = json.loads(client_attendees) if client_attendees else []
    except json.JSONDecodeError:
        client_list = []
    if not isinstance(client_list, list):
        client_list = []
    client_list = [str(n).strip() for n in client_list if str(n).strip()]
    client_list = [n for n in client_list if len(n) <= MAX_CLIENT_ATTENDEE_NAME]
    if not client_list:
        errors["client_attendees"] = "至少填 1 位客户参会人"

    # 6. 会议背景
    bg_s = background.strip()
    if not bg_s:
        errors["background"] = "必填"
    elif len(bg_s) > MAX_BACKGROUND:
        errors["background"] = f"不超过 {MAX_BACKGROUND} 字"

    # 7. 照片（读字节以便校验大小 + 后续上传）
    photo_bytes = await photo.read()
    if not photo_bytes:
        errors["photo"] = "必填"
    elif len(photo_bytes) > MAX_PHOTO_BYTES:
        errors["photo"] = "图片不得超过 10MB"
    elif photo.content_type and photo.content_type.lower() not in ALLOWED_MIMES:
        errors["photo"] = "仅支持 JPEG / PNG / GIF"

    if errors:
        return _render_form(request, customer, errors=errors, values=values, status_code=400)

    # 8. 保存图片（dev 本地 / prod 飞书）
    image_key = photo_storage.save(
        photo_bytes,
        filename=photo.filename,
        content_type=photo.content_type,
    )
    if not image_key:
        errors["photo"] = "图片保存失败，请重试"
        return _render_form(request, customer, errors=errors, values=values, status_code=500)

    # 9. 写库
    owner_id = getattr(request.state, "uid", None)
    record_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")

    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO followup_records (
                    id, customer_id, owner_id, meeting_date,
                    location, our_attendees, client_attendees, background,
                    minutes_doc_url, minutes_doc_id, transcript_url, photo_image_key,
                    source_type, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id, customer_id, owner_id, meeting_iso,
                    location_s,
                    json.dumps(our_list, ensure_ascii=False),
                    json.dumps(client_list, ensure_ascii=False),
                    bg_s,
                    minutes_url.strip(), doc_id, transcript_s, image_key,
                    "manual", now,
                ),
            )
    finally:
        conn.close()

    logger.info("Created followup %s for customer %s by %s", record_id, customer_id, owner_id)

    # 后台跑 ingest pipeline：Fetch → Ingest (agent+skill) → Extract → Commit
    # 失败只写日志，H5 端看列表时 summary 暂时为空，允许用户点 regen-wiki 重跑
    background_tasks.add_task(run_ingest_pipeline, record_id)

    # 跳到详情页（而不是列表）：用户立刻能看到刚提交的记录 + 照片 + 参会人员
    return RedirectResponse(url=f"/followup/{record_id}", status_code=302)


@router.post("/followup/{record_id}/regen-wiki")
def regen_wiki(record_id: str, background_tasks: BackgroundTasks):
    """手动重跑完整 ingest pipeline（fetch+ingest+extract+commit）。立即返回，后台跑。"""
    background_tasks.add_task(run_ingest_pipeline, record_id)
    return RedirectResponse(url=f"/followup/{record_id}", status_code=302)


def _format_meeting_date(s: str | None) -> str:
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return s[:16]
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.strftime('%H:%M')}"


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


@router.get("/followup/{record_id}", response_class=HTMLResponse)
def followup_detail(request: Request, record_id: str):
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT r.*,
                   c.name AS customer_name,
                   u.display_name AS owner_display_name
            FROM followup_records r
            JOIN customers c ON c.id = r.customer_id
            LEFT JOIN crm_users u ON u.feishu_open_id = r.owner_id
            WHERE r.id = ?
            """,
            (record_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return HTMLResponse(
            "<p style='padding:40px;text-align:center;color:#756E62'>未找到该跟进记录</p>",
            status_code=404,
        )

    r = dict(row)
    our_list = _safe_json_list(r.get("our_attendees"))
    client_list = _safe_json_list(r.get("client_attendees"))

    # 尝试拉纪要文字（权限未开通时 error 非 None）
    minutes_text = None
    minutes_error = None
    if r.get("minutes_doc_id"):
        minutes_text, minutes_error = fetch_docx_raw(r["minutes_doc_id"])

    return templates.TemplateResponse(
        "followup_detail.html",
        {
            "request": request,
            "r": r,
            "our_list": our_list,
            "client_list": client_list,
            "date_display": _format_meeting_date(r.get("meeting_date")),
            "owner_name": r.get("owner_display_name") or (r.get("owner_id") or "—"),
            "minutes_text": minutes_text,
            "minutes_error": minutes_error,
        },
    )


@router.get("/api/users/search")
def users_search(request: Request, q: str = "", limit: int = 20):
    """按姓名搜飞书 Directory 员工。

    - 用当前登录用户的 user_access_token 调 /contact/v3/user/search
    - 权限 scope：contact:user:search（需要在飞书后台开通 + 版本审核）
    - 401 → 前端提示用户重新登录（cookie 里的 uid 对应的 user_tokens 表没有或已失效）
    """
    q = (q or "").strip()
    if not q:
        return {"items": []}
    if len(q) > 40:
        q = q[:40]
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 20

    open_id = getattr(request.state, "uid", None)
    # 密码登录的 uid 是 'pwd-user'，搜不了
    if not open_id or not open_id.startswith("ou_"):
        raise HTTPException(status_code=401, detail="feishu_login_required")

    token = get_user_access_token(open_id)
    if not token:
        raise HTTPException(status_code=401, detail="reauth_required")

    users = search_feishu_users(token, q, limit=limit)
    items = []
    for u in users:
        name = u.get("name") or u.get("en_name")
        oid = u.get("open_id")
        if not (name and oid):
            continue
        # 次要文本：优先英文名（区别同名）；没有就留空。
        # department_ids 是内部 ID，直接显示没意义，要名字得另调部门接口，先不展示。
        sub = ""
        en = u.get("en_name") or ""
        if en and en != name:
            sub = en
        items.append({
            "id": oid,
            "name": name,
            "sub": sub,
            "avatar": (u.get("avatar") or {}).get("avatar_72") or "",
        })
    return {"items": items}


@router.get("/api/jssdk/config")
def jssdk_config(url: str):
    """前端传当前页面 URL，返回 h5sdk.config 需要的签名参数。

    URL 需 去 hash；前端传 location.href.split('#')[0] 即可。
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="invalid url")
    cfg = sign_jssdk(url)
    if cfg is None:
        raise HTTPException(status_code=502, detail="failed to sign jssdk config")
    return cfg


@router.get("/api/image/{image_key}")
def proxy_image(image_key: str):
    """代理照片：local_* 读本地，img_v* 走飞书。cookie 鉴权由 AuthMiddleware 负责。"""
    if not photo_storage.is_valid_key(image_key):
        raise HTTPException(status_code=400, detail="invalid image_key")

    it, ctype = photo_storage.stream(image_key)
    if it is None:
        raise HTTPException(status_code=502, detail="image fetch failed")

    return StreamingResponse(
        it,
        media_type=ctype,
        headers={"Cache-Control": "private, max-age=86400"},
    )
