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

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from src.db.connection import connect, transaction
from src.ingest.pipeline import run as run_ingest_pipeline
from src.lark_client import (
    get_docx_title,
    get_minute_meta,
    get_user_access_token,
    get_wiki_node_title,
    search_feishu_users,
    search_minutes,
    sign_jssdk,
)
from src.web.auth import require_csrf_form
from src import photo_storage
from src.web import title_cache

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---- 限制 ----------------------------------------------------------
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10MB（Lark im/v1/images 上限）
MAX_PHOTOS = 6                       # 最多上传张数
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


_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


def _parse_meeting_time_range(
    date_s: str, start_s: str, end_s: str,
) -> tuple[str | None, str | None, str | None]:
    """把（YYYY-MM-DD, HH:MM, HH:MM）组合成 (meeting_date_iso, end_hhmm, error)。

    规则:
    - 日期格式 YYYY-MM-DD；开始/结束 HH:MM
    - 日期不能晚于今天（允许今天的任意时间，哪怕是将来几小时 —— 方便填会议刚结束场景）
    - 结束时间严格大于开始时间
    """
    try:
        date_only = datetime.strptime(date_s, "%Y-%m-%d").date()
    except Exception:
        return None, None, "请选择有效日期"

    if not _HHMM_RE.match(start_s) or not _HHMM_RE.match(end_s):
        return None, None, "请选择开始和结束时间"

    try:
        start_h, start_m = map(int, start_s.split(":"))
        end_h, end_m = map(int, end_s.split(":"))
        start_dt = datetime.combine(date_only, datetime.min.time()).replace(hour=start_h, minute=start_m)
        end_dt = datetime.combine(date_only, datetime.min.time()).replace(hour=end_h, minute=end_m)
    except Exception:
        return None, None, "时间格式不正确"

    if end_dt <= start_dt:
        return None, None, "结束时间必须晚于开始时间"

    # 日期本身不能是未来（允许今天）
    today = datetime.now().date()
    if date_only > today:
        return None, None, "日期不能晚于今天"

    return (
        start_dt.isoformat(timespec="minutes"),
        end_s,
        None,
    )


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
    meeting_date: str = Form(...),        # YYYY-MM-DD
    meeting_start_time: str = Form(...),  # HH:MM
    meeting_end_time: str = Form(...),    # HH:MM
    location: str = Form(...),
    our_attendees: str = Form(...),      # JSON: [{"open_id":..,"name":..}]
    client_attendees: str = Form(...),   # JSON: ["name1", "name2"]
    other_attendees: str = Form(""),     # JSON: ["name1", ...]；可选，默认空
    background: str = Form(...),
    photos: list[UploadFile] = File(default_factory=list),
    _csrf: None = Depends(require_csrf_form),
):
    customer = _fetch_customer(customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")

    values = {
        "minutes_url": minutes_url,
        "transcript_url": transcript_url,
        "meeting_date": meeting_date,
        "meeting_start_time": meeting_start_time,
        "meeting_end_time": meeting_end_time,
        "location": location,
        "our_attendees": our_attendees,
        "client_attendees": client_attendees,
        "other_attendees": other_attendees,
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

    # 2. 时间：日期 + 起止
    meeting_iso, end_hhmm, time_err = _parse_meeting_time_range(
        meeting_date.strip(), meeting_start_time.strip(), meeting_end_time.strip(),
    )
    if time_err:
        errors["meeting_date"] = time_err

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

    # 5b. 其他人员（可选）
    try:
        other_list = json.loads(other_attendees) if other_attendees else []
    except json.JSONDecodeError:
        other_list = []
    if not isinstance(other_list, list):
        other_list = []
    other_list = [str(n).strip() for n in other_list if str(n).strip()]
    other_list = [n for n in other_list if len(n) <= MAX_CLIENT_ATTENDEE_NAME]

    # 6. 会议背景
    bg_s = background.strip()
    if not bg_s:
        errors["background"] = "必填"
    elif len(bg_s) > MAX_BACKGROUND:
        errors["background"] = f"不超过 {MAX_BACKGROUND} 字"

    # 7. 照片（0-MAX_PHOTOS 张，至少 1 张）—— 先校验，再保存
    valid_photos: list[tuple[bytes, str | None, str | None]] = []
    # 过滤空的 UploadFile（FastAPI 对空 input[type=file] 会塞一个空条目进来）
    real_photos = [p for p in (photos or []) if p and p.filename]
    if not real_photos:
        errors["photos"] = "至少上传 1 张照片"
    elif len(real_photos) > MAX_PHOTOS:
        errors["photos"] = f"最多上传 {MAX_PHOTOS} 张"
    else:
        for p in real_photos:
            b = await p.read()
            if not b:
                errors["photos"] = "图片为空，请重新上传"
                break
            if len(b) > MAX_PHOTO_BYTES:
                errors["photos"] = f"{p.filename} 超过 10MB"
                break
            if p.content_type and p.content_type.lower() not in ALLOWED_MIMES:
                errors["photos"] = f"{p.filename} 不是 JPEG/PNG/GIF"
                break
            valid_photos.append((b, p.filename, p.content_type))

    if errors:
        return _render_form(request, customer, errors=errors, values=values, status_code=400)

    # 8. 保存每张图片（dev 本地 / prod 飞书）
    image_keys: list[str] = []
    for (b, fname, ctype) in valid_photos:
        key = photo_storage.save(b, filename=fname, content_type=ctype)
        if not key:
            errors["photos"] = "图片保存失败，请重试"
            return _render_form(request, customer, errors=errors, values=values, status_code=500)
        image_keys.append(key)
    # 第一张也写进旧的 photo_image_key，保持 legacy 兼容（老代码路径还在读这列）
    first_key = image_keys[0] if image_keys else None

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
                    id, customer_id, owner_id,
                    meeting_date, meeting_end_time,
                    location, our_attendees, client_attendees, other_attendees,
                    background,
                    minutes_doc_url, minutes_doc_id, transcript_url,
                    photo_image_key, photo_image_keys,
                    source_type, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id, customer_id, owner_id,
                    meeting_iso, end_hhmm,
                    location_s,
                    json.dumps(our_list, ensure_ascii=False),
                    json.dumps(client_list, ensure_ascii=False),
                    json.dumps(other_list, ensure_ascii=False),
                    bg_s,
                    minutes_url.strip(), doc_id, transcript_s,
                    first_key, json.dumps(image_keys, ensure_ascii=False),
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
def regen_wiki(record_id: str, request: Request, background_tasks: BackgroundTasks):
    """手动重跑完整 ingest pipeline（fetch+ingest+extract+commit）。

    这个接口会花 Claude token，必须把调用权限收得比普通登录用户更紧：
    - DEV_REGEN_PASSWORD 未配置 → 关闭（403）
    - 请求头 X-Dev-Password 必须匹配
    - 即使知道 URL 的登录用户也不能直接打
    """
    from src.config import DEV_REGEN_PASSWORD  # 延迟 import，避免循环

    if not DEV_REGEN_PASSWORD:
        raise HTTPException(status_code=403, detail="regen-wiki disabled")
    supplied = request.headers.get("X-Dev-Password", "")
    # 常量时间比较，避免 timing attack 泄露密码长度/前缀
    import hmac
    if not hmac.compare_digest(supplied, DEV_REGEN_PASSWORD):
        raise HTTPException(status_code=403, detail="bad dev password")

    logger.warning(
        "regen-wiki triggered by uid=%s record_id=%s",
        getattr(request.state, "uid", "?"), record_id,
    )
    background_tasks.add_task(run_ingest_pipeline, record_id)
    return {"ok": True, "record_id": record_id}


def _format_meeting_date(s: str | None, end_hhmm: str | None = None) -> str:
    """返回 "2026-04-24 09:05 - 10:30"；没 end_hhmm 就回退到老单点格式。"""
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return s[:16]
    base = f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.strftime('%H:%M')}"
    if end_hhmm and _HHMM_RE.match(end_hhmm):
        return f"{base} - {end_hhmm}"
    return base


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_INGEST_IN_PROGRESS = {"queued", "fetching", "ingesting", "extracting", "committing"}


def _format_meeting_date_parts(s: str | None) -> dict:
    """'2026-04-24T09:00' → {month:'4月', day:24, weekday:'周四', time:'09:00', year:2026}."""
    if not s:
        return {"year": "", "month": "", "day": "", "weekday": "", "time": ""}
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return {"year": "", "month": "", "day": s[:10], "weekday": "", "time": ""}
    return {
        "year": dt.year,
        "month": f"{dt.month}月",
        "day": dt.day,
        "weekday": _WEEKDAY_CN[dt.weekday()],
        "time": dt.strftime("%H:%M"),
    }


def _needs_reauth(request) -> bool:
    """用户 cookie 有效（已登录），但 user_access_token 不可恢复。
    典型场景：access_token 过期 + refresh_token 也过期/为空。
    这种情况下拉飞书 API 的字段（文档标题 / 妙记元信息）都会失败，
    需要引导用户重新 OAuth 授权。"""
    open_id = getattr(request.state, "uid", "") or ""
    # 密码登录（uid != ou_*）不走 OAuth，没有 user_access_token 概念
    if not open_id.startswith("ou_"):
        return False
    return get_user_access_token(open_id) is None


def _lookup_doc_title(url: str | None, request, *, kind: str) -> str | None:
    """实时拉 docx / wiki / minute 标题。kind: 'doc' | 'minute'。

    URL 类型自动识别：
      - /docx/XXX → docx API
      - /wiki/XXX → wiki node API（XXX 是 node_token，不是 doc_id）
      - /docs/XXX → 老式 doc（暂不支持，返 None）
      - /minutes/XXX → minute API

    走 5 分钟 TTL 内存缓存。失败/无权限/无登录 → 返回 None，模板用 fallback。
    """
    if not url:
        return None

    # 识别 URL 类型 + 抽 token
    sub_kind: str
    token: str | None
    if kind == "minute":
        sub_kind = "minute"
        token = _parse_minute_token(url)
    else:
        # doc 类：区分 docx / wiki
        if "/wiki/" in url:
            sub_kind = "wiki"
            m = re.search(r"/wiki/([A-Za-z0-9]+)", url)
            token = m.group(1) if m else None
        elif "/docx/" in url:
            sub_kind = "docx"
            m = re.search(r"/docx/([A-Za-z0-9]+)", url)
            token = m.group(1) if m else None
        else:
            return None

    if not token:
        return None

    # 缓存命中（cache key 含 sub_kind，避免 wiki 和 docx token 冲突）
    hit, cached = title_cache.get(sub_kind, token)
    if hit:
        return cached

    # 需要 user_access_token 调飞书 API
    open_id = getattr(request.state, "uid", "") or ""
    if not open_id.startswith("ou_"):
        return None
    user_token = get_user_access_token(open_id)
    if not user_token:
        return None

    title: str | None
    if sub_kind == "docx":
        title = get_docx_title(token, user_token)
    elif sub_kind == "wiki":
        title = get_wiki_node_title(token, user_token)
    else:  # minute
        meta, _err = get_minute_meta(token, user_token)
        title = (meta or {}).get("title") if meta else None

    # 即使 None 也缓存一下，避免 5 分钟内反复拉
    title_cache.put(sub_kind, token, title)
    return title


def _parse_our_people_detail(our_attendees_raw: str | None) -> list[dict]:
    """our_attendees JSON → [{id, name, avatar, initial}, ...]. 详情页复用。
    avatar 为空时从 user_tokens.avatar 按 open_id 补回（存量 bug backfill）。"""
    if not our_attendees_raw:
        return []
    try:
        v = json.loads(our_attendees_raw)
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for it in v:
        if isinstance(it, dict):
            name = (it.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "id": it.get("id") or it.get("open_id") or "",
                "name": name,
                "avatar": it.get("avatar") or "",
                "initial": name[:1],
            })
        elif isinstance(it, str):
            s = it.strip()
            if s:
                out.append({"id": "", "name": s, "avatar": "", "initial": s[:1]})
    # backfill from user_tokens
    needed = [p["id"] for p in out if p["id"] and not p["avatar"]]
    if needed:
        conn = connect()
        try:
            placeholders = ",".join("?" * len(needed))
            rows = conn.execute(
                f"SELECT open_id, avatar FROM user_tokens WHERE open_id IN ({placeholders})",
                needed,
            ).fetchall()
        finally:
            conn.close()
        lookup = {r["open_id"]: r["avatar"] for r in rows if r["avatar"]}
        for p in out:
            if not p["avatar"] and p["id"] in lookup:
                p["avatar"] = lookup[p["id"]]
    return out


@router.get("/followup/{record_id}", response_class=HTMLResponse)
def followup_detail(request: Request, record_id: str):
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT r.*,
                   c.name AS customer_name,
                   u.display_name AS crm_owner_name,
                   ut.display_name AS fs_owner_name,
                   ut.avatar AS fs_owner_avatar,
                   ij.status AS ingest_status
            FROM followup_records r
            JOIN customers c ON c.id = r.customer_id
            LEFT JOIN crm_users u ON u.feishu_open_id = r.owner_id
            LEFT JOIN user_tokens ut ON ut.open_id = r.owner_id
            LEFT JOIN ingest_jobs ij ON ij.record_id = r.id
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
    our_people = _parse_our_people_detail(r.get("our_attendees"))
    client_list = _safe_json_list(r.get("client_attendees"))
    other_list = _safe_json_list(r.get("other_attendees"))
    # photo_image_keys（多图 JSON 数组）优先；否则 fallback 到 legacy 单图
    photo_keys = _safe_json_list(r.get("photo_image_keys"))
    if not photo_keys and r.get("photo_image_key"):
        photo_keys = [r["photo_image_key"]]
    r["photo_keys"] = photo_keys
    # 从 docx ingest 时物化到本地的画板 / 图片（{kind, key} 列表）
    r["minutes_media_list"] = _safe_json_list(r.get("minutes_media"))
    r["board_items"] = [m for m in r["minutes_media_list"] if m.get("kind") == "board"]

    # AI 处理状态：让模板根据 ai_state 渲染 skeleton / 失败 / 正常
    ingest_status = (r.get("ingest_status") or "").lower()
    if ingest_status in _INGEST_IN_PROGRESS:
        r["ai_state"] = "processing"
    elif ingest_status == "failed":
        r["ai_state"] = "failed"
    else:
        r["ai_state"] = "done"
    # 画板单独判一个状态：还在 ingest 且尚未出画板 → processing；否则根据结果决定
    if ingest_status in _INGEST_IN_PROGRESS and not r["board_items"]:
        r["board_state"] = "processing"
    elif ingest_status == "failed" and not r["board_items"]:
        r["board_state"] = "failed"
    else:
        r["board_state"] = "done"

    r["date_parts"] = _format_meeting_date_parts(r.get("meeting_date"))
    r["date_end_time"] = r.get("meeting_end_time") or ""

    # 实时拉 docx / 妙记标题（飞书文档标题可能会改，不存 DB）
    # TTL 内存缓存 5 分钟避免每次刷新都打 API
    r["minutes_title"] = _lookup_doc_title(
        r.get("minutes_doc_url"), request, kind="docx",
    )
    r["transcript_title"] = _lookup_doc_title(
        r.get("transcript_url"), request, kind="minute",
    )
    # user_access_token 失效检测：用户 cookie 还在（uid 是 ou_），但 token
    # 已过期且 refresh_token 也失效 → 挂个提示让用户主动重新登录
    r["needs_reauth"] = _needs_reauth(request) and bool(
        r.get("minutes_doc_url") or r.get("transcript_url")
    )

    # 总参会人数（hero meta pill）
    r["attendee_total"] = len(our_people) + len(client_list) + len(other_list)

    owner_name = (
        r.get("crm_owner_name")
        or r.get("fs_owner_name")
        or "—"
    )
    owner_avatar = r.get("fs_owner_avatar") or ""

    return templates.TemplateResponse(
        "followup_detail.html",
        {
            "request": request,
            "r": r,
            "our_people": our_people,
            "client_list": client_list,
            "other_list": other_list,
            "date_display": _format_meeting_date(
                r.get("meeting_date"), r.get("meeting_end_time"),
            ),
            "owner_name": owner_name,
            "owner_avatar": owner_avatar,
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
        # 次要文本：飞书 en_name 偶尔被设成员工号/ID 乱码（脏数据），
        # 展示出来对用户毫无意义，反而像个 bug。
        # 只在 en_name 看起来是"正常英文名"（纯字母 + 可能的空格/点/短横）时才展示。
        sub = ""
        en = (u.get("en_name") or "").strip()
        if en and en != name:
            looks_like_real_name = all(
                c.isalpha() or c in " .-'" for c in en
            )
            if looks_like_real_name:
                sub = en
        # avatar 可能来自两种 shape：
        #  - /search/v1/user 旧端点：扁平 {avatar_url: "..."}
        #  - /contact/v3/* 新端点：嵌套 {avatar: {avatar_72: "..."}}
        av = u.get("avatar")
        if isinstance(av, dict):
            avatar_url = av.get("avatar_72") or av.get("avatar_origin") or av.get("avatar_url") or ""
        elif isinstance(av, str):
            avatar_url = av
        else:
            avatar_url = u.get("avatar_url") or ""
        items.append({
            "id": oid,
            "name": name,
            "sub": sub,
            "avatar": avatar_url,
        })
    return {"items": items}


@router.get("/api/jssdk/config")
def jssdk_config(url: str):
    """前端传当前页面 URL，返回 h5sdk.config 需要的签名参数。

    URL 需 去 hash；前端传 location.href.split('#')[0] 即可。
    响应必须 no-store —— 每次签名只能用一次（飞书会判重 333443）。
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="invalid url")
    cfg = sign_jssdk(url)
    if cfg is None:
        raise HTTPException(status_code=502, detail="failed to sign jssdk config")
    # 防止浏览器 / CDN / 中间层缓存 —— 每次必须拿新的签名
    return JSONResponse(
        content=cfg,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
        },
    )


# ---- 妙记 picker 代理接口 --------------------------------------------
# 后端用**当前登录用户的 user_access_token** 调飞书 minutes API。
# 浏览器拿到的已是脱敏后的精简 JSON。

# 妙记 URL 里 minute_token 的正则（和文档的一致：24+ 字母数字）
MINUTE_TOKEN_RE = re.compile(r"/minutes/([A-Za-z0-9]{12,})")


def _parse_minute_token(s: str) -> str | None:
    """接受 URL 或 token 本身；URL 里抽 token。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("http"):
        m = MINUTE_TOKEN_RE.search(s)
        return m.group(1) if m else None
    # 裸 token：粗校验
    if re.fullmatch(r"[A-Za-z0-9]{12,}", s):
        return s
    return None


def _need_feishu_user_token(request: Request) -> str:
    """从 request.state.uid 取用户的 user_access_token；失败抛 HTTPException。"""
    open_id = getattr(request.state, "uid", None)
    if not open_id or not open_id.startswith("ou_"):
        raise HTTPException(status_code=401, detail="feishu_login_required")
    token = get_user_access_token(open_id)
    if not token:
        raise HTTPException(status_code=401, detail="reauth_required")
    return token


@router.get("/api/minutes/search")
def minutes_search(request: Request, q: str = "", page_token: str = "", page_size: int = 15):
    """搜索当前用户有权限的妙记。

    - 前端每次打开 picker / 搜索关键字变化时调这里
    - 没 query 时用"最近 30 天创建"作为默认 filter（API 不允许完全空 filter）
    """
    user_token = _need_feishu_user_token(request)

    q = (q or "").strip()[:50]
    # API 必须至少给 query / filter 之一；query 为空时用时间范围
    # 飞书要求 ISO 8601 with Z 后缀（UTC），例如 2024-01-01T00:00:00Z
    create_start = create_end = None
    if not q:
        from datetime import timedelta, timezone
        now = datetime.now(timezone.utc)
        create_start = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        create_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    data, err = search_minutes(
        user_token,
        query=q,
        create_time_start=create_start,
        create_time_end=create_end,
        page_size=page_size,
        page_token=page_token,
    )
    if err:
        # 常见错误：scope 未批（FORBIDDEN）、用户没权限
        if "forbid" in err.lower() or "99991663" in err or "scope" in err.lower():
            raise HTTPException(status_code=403, detail="minutes_scope_required")
        raise HTTPException(status_code=502, detail=f"upstream: {err}")

    # 透传给前端，只保留展示字段
    items = []
    for it in (data or {}).get("items", []):
        meta = it.get("meta_data") or {}
        items.append({
            "token": it.get("token"),
            "title": it.get("display_info") or "未命名",
            "description": meta.get("description") or "",
            "url": meta.get("app_link") or "",
            "avatar": meta.get("avatar") or "",
        })
    return {
        "items": items,
        "has_more": (data or {}).get("has_more", False),
        "page_token": (data or {}).get("page_token", ""),
    }


@router.get("/api/minutes/meta")
def minutes_meta(request: Request, token: str = "", url: str = ""):
    """取单条妙记元信息，用于前端验证卡片。token 或 url 任填一个。"""
    user_token = _need_feishu_user_token(request)

    minute_token = _parse_minute_token(token or url)
    if not minute_token:
        raise HTTPException(status_code=400, detail="invalid_url")

    meta, err = get_minute_meta(minute_token, user_token)
    if err:
        if "forbid" in err.lower() or "99991663" in err:
            raise HTTPException(status_code=403, detail="forbidden")
        raise HTTPException(status_code=502, detail=f"upstream: {err}")
    if not meta:
        raise HTTPException(status_code=404, detail="not_found")

    # 飞书 create_time 是 Unix 秒字符串
    ct = meta.get("create_time")
    try:
        ct_iso = datetime.fromtimestamp(int(ct)).isoformat(timespec="seconds") if ct else None
    except Exception:
        ct_iso = None

    return {
        "token": meta.get("token"),
        "title": meta.get("title") or "未命名",
        "duration_secs": int(meta.get("duration") or 0),
        "create_time": ct_iso,
        "owner_id": meta.get("owner_id"),
        "url": meta.get("url") or "",
        "cover": meta.get("cover") or "",
    }


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
