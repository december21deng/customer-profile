"""H5 登录与鉴权。

一个签名 cookie（uid）= 登录凭证。校验失败就去 Lark OAuth 或 /login（兜底）。

登录入口：
  - /auth/lark：Lark 网页应用 OAuth（在飞书内自动静默授权）
  - /login：密码登录（APP_ACCESS_PASSWORD，兜底 / 外部调试用）

Lark OAuth 流程（重定向式）：
  1. 用户无 cookie 访问受保护路径
  2. 中间件 302 到 /auth/lark?next=<原路径>
  3. /auth/lark 无 code → 302 到飞书 authorize 页（state=signed(next)）
  4. 飞书静默回调 /auth/lark?code=xxx&state=xxx
  5. 换 open_id → 种 cookie → 302 到 next
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import requests as http_requests
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import APP_ACCESS_PASSWORD, APP_SECRET_KEY, LARK_APP_ID, LARK_APP_SECRET
from src.db.connection import connect, transaction

logger = logging.getLogger(__name__)

COOKIE_NAME = "uid"
MAX_AGE = 60 * 60 * 24 * 30  # 30 天
OPEN_PATHS = {"/login", "/logout", "/auth/lark", "/healthz"}

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")

LARK_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
LARK_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
LARK_USERINFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
STATE_MAX_AGE = 300  # 5 分钟，足够 Lark 回跳


def _lark_enabled() -> bool:
    return bool(LARK_APP_ID and LARK_APP_SECRET and APP_BASE_URL)


def _safe_next(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") else "/customers"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(APP_SECRET_KEY, salt="uid-cookie")


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(APP_SECRET_KEY, salt="lark-oauth-state")


def _sign_state(next_path: str) -> str:
    return _state_serializer().dumps(next_path)


def _verify_state(token: str) -> Optional[str]:
    try:
        return _state_serializer().loads(token, max_age=STATE_MAX_AGE)
    except Exception:
        return None


def sign(uid: str) -> str:
    return _serializer().dumps(uid)


def verify(token: str) -> Optional[str]:
    try:
        return _serializer().loads(token, max_age=MAX_AGE)
    except BadSignature:
        return None
    except Exception:
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    """所有路径默认需要 uid cookie；白名单路径 + /static/* 放行。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/static"):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        uid = verify(token) if token else None

        if not uid:
            nxt = path
            if request.url.query:
                nxt = f"{path}?{request.url.query}"
            # 配好了 Lark 走 OAuth；否则走密码（兜底）
            login_url = "/auth/lark" if _lark_enabled() else "/login"
            return RedirectResponse(url=f"{login_url}?next={nxt}", status_code=302)

        request.state.uid = uid
        return await call_next(request)


router = APIRouter()


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>登录 · 客户助手</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font: 14px -apple-system, "PingFang SC", sans-serif;
    background: #F5F6F7; color: #1F2329;
    margin: 0; padding: 0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }}
  .box {{
    background: #fff; border-radius: 16px;
    padding: 32px 28px;
    width: min(360px, calc(100vw - 32px));
    box-shadow: 0 4px 16px rgba(31,35,41,0.08);
  }}
  h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 6px; }}
  .sub {{ font-size: 12px; color: #8F959E; margin-bottom: 20px; }}
  input {{
    width: 100%; padding: 10px 12px;
    border: 1px solid #E5E6EB; border-radius: 8px;
    font-size: 14px; font-family: inherit;
  }}
  input:focus {{ outline: none; border-color: #3370FF; box-shadow: 0 0 0 3px rgba(51,112,255,0.1); }}
  button {{
    width: 100%; padding: 11px; margin-top: 14px;
    background: #3370FF; color: #fff; border: 0;
    border-radius: 8px; font-size: 14px; font-weight: 500;
    cursor: pointer;
  }}
  button:active {{ background: #2B5EDB; }}
  .err {{ color: #E54545; font-size: 12px; margin: 10px 0 0; }}
</style></head><body>
<form class="box" method="post" action="/login">
  <h1>客户助手</h1>
  <div class="sub">内部系统，请输入访问密码</div>
  <input name="password" type="password" placeholder="密码" autofocus required>
  <input name="next" type="hidden" value="{next}">
  <button type="submit">登录</button>
  {err}
</form></body></html>"""


@router.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/customers"):
    return HTMLResponse(_LOGIN_HTML.format(next=next, err=""))


@router.post("/login")
def login_submit(
    password: str = Form(...),
    next: str = Form("/customers"),
):
    if not APP_ACCESS_PASSWORD or not secrets.compare_digest(password, APP_ACCESS_PASSWORD):
        return HTMLResponse(
            _LOGIN_HTML.format(next=next, err='<p class="err">密码错误</p>'),
            status_code=401,
        )
    # 防 open redirect：只允许以 / 开头（站内路径）
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/customers"
    resp = RedirectResponse(url=safe_next, status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        sign("pwd-user"),
        max_age=MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


def _err_html(msg: str, status: int = 401) -> HTMLResponse:
    return HTMLResponse(
        f"<p style='padding:40px;text-align:center;color:#E54545;font-family:-apple-system,sans-serif'>"
        f"{msg}</p>",
        status_code=status,
    )


def _store_user_tokens(
    open_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_expires_in: int,
) -> None:
    """OAuth 拿到的 token 存库，后续调飞书 user_access_token 身份的 API 用。"""
    now = datetime.now()
    access_expires_at = (now + timedelta(seconds=expires_in)).isoformat(timespec="seconds")
    refresh_expires_at = (now + timedelta(seconds=refresh_expires_in)).isoformat(timespec="seconds")
    now_iso = now.isoformat(timespec="seconds")

    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO user_tokens
                    (open_id, access_token, refresh_token,
                     access_expires_at, refresh_expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(open_id) DO UPDATE SET
                    access_token       = excluded.access_token,
                    refresh_token      = excluded.refresh_token,
                    access_expires_at  = excluded.access_expires_at,
                    refresh_expires_at = excluded.refresh_expires_at,
                    updated_at         = excluded.updated_at
                """,
                (open_id, access_token, refresh_token,
                 access_expires_at, refresh_expires_at, now_iso),
            )
    finally:
        conn.close()


@router.get("/auth/lark")
def auth_lark(code: str = "", state: str = "", next: str = "/customers"):
    """Lark 网页应用 OAuth 入口 + 回调。

    - 无 code：302 到飞书 authorize 页（state 里带 next）
    - 有 code：POST /open-apis/authen/v2/oauth/token 换 open_id → 种 cookie → 302 next
    """
    if not _lark_enabled():
        return _err_html(
            "Lark OAuth 未配置（缺 LARK_APP_ID / LARK_APP_SECRET / APP_BASE_URL），请用 /login 登录",
            status=501,
        )

    redirect_uri = f"{APP_BASE_URL}/auth/lark"

    # Step 1: 没 code → 跳飞书
    # scope 必须显式请求才能把权限注入 user_access_token：
    #   - contact:user:search：搜同事（1.0.7 已发布 ✅）
    #   - offline_access：拿 refresh_token（1.0.8 审核中，暂不加，等批了再加）
    if not code:
        params = {
            "app_id": LARK_APP_ID,
            "redirect_uri": redirect_uri,
            "state": _sign_state(_safe_next(next)),
            "scope": "contact:user:search",
        }
        return RedirectResponse(
            url=f"{LARK_AUTHORIZE_URL}?{urlencode(params)}",
            status_code=302,
        )

    # Step 2: code → access_token
    next_path = _safe_next(_verify_state(state) or "/customers")
    try:
        tok_resp = http_requests.post(
            LARK_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": LARK_APP_ID,
                "client_secret": LARK_APP_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
        tok_data = tok_resp.json()
    except Exception:
        logger.exception("Lark token exchange failed")
        return _err_html("登录失败：无法连接飞书授权服务器", status=502)

    access_token = tok_data.get("access_token")
    refresh_token = tok_data.get("refresh_token") or ""
    expires_in = int(tok_data.get("expires_in") or 7200)
    refresh_expires_in = int(
        tok_data.get("refresh_token_expires_in") or tok_data.get("refresh_expires_in") or 2592000
    )
    if not access_token:
        logger.error("Lark token exchange: no access_token, response=%s", tok_data)
        return _err_html(
            f"登录失败：{tok_data.get('error_description') or tok_data.get('msg') or '未获取到 access_token'}",
            status=401,
        )

    # Step 3: access_token → open_id（v2 token 接口不返回 open_id，要再调 user_info）
    try:
        info_resp = http_requests.get(
            LARK_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        info_data = info_resp.json()
    except Exception:
        logger.exception("Lark user_info failed")
        return _err_html("登录失败：拉取用户信息失败", status=502)

    open_id = (info_data.get("data") or {}).get("open_id") or info_data.get("open_id")
    if not open_id:
        logger.error("Lark user_info: no open_id, response=%s", info_data)
        return _err_html(
            f"登录失败：{info_data.get('msg') or info_data.get('error_description') or '未获取到 open_id'}",
            status=401,
        )

    # 存 token，供 user_access_token 身份的 API 调用（如搜同事）
    # 没 refresh_token 也存（只是 2h 后要重登），保证本地测试能跑。
    try:
        _store_user_tokens(
            open_id=open_id,
            access_token=access_token,
            refresh_token=refresh_token or "",
            expires_in=expires_in,
            refresh_expires_in=refresh_expires_in if refresh_token else 0,
        )
        logger.info(
            "stored user_tokens for open_id=%s (access_exp=%ds, has_refresh=%s)",
            open_id, expires_in, bool(refresh_token),
        )
    except Exception:
        logger.exception("store user tokens failed for %s (continue anyway)", open_id)
    if not refresh_token:
        logger.warning(
            "Lark token response missing refresh_token for %s (tok_data keys=%s); "
            "user will need to re-OAuth in %d seconds",
            open_id, list(tok_data.keys()), expires_in,
        )

    resp = RedirectResponse(url=next_path, status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        sign(open_id),
        max_age=MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    logger.info("Lark login success: open_id=%s next=%s", open_id, next_path)
    return resp
