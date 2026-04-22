"""H5 登录与鉴权。

一个签名 cookie（uid）= 登录凭证。校验失败就 302 到 /login。

当前支持：
  - 密码登录（APP_ACCESS_PASSWORD，临时兜底）
  - /auth/lark（Lark 小程序 code → open_id，Step 2 再接入真实交换）

后续：Lark OAuth 打通后，/login 密码登录降级为 fallback（只保留不删，方便直连调试）。
"""

from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE_NAME = "uid"
MAX_AGE = 60 * 60 * 24 * 30  # 30 天
OPEN_PATHS = {"/login", "/logout", "/auth/lark", "/healthz"}


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("APP_SECRET_KEY")
    if not secret:
        raise RuntimeError("APP_SECRET_KEY env var is required")
    return URLSafeTimedSerializer(secret, salt="uid-cookie")


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
            return RedirectResponse(url=f"/login?next={nxt}", status_code=302)

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
    expected = os.environ.get("APP_ACCESS_PASSWORD", "")
    if not expected or not secrets.compare_digest(password, expected):
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


@router.get("/auth/lark")
def auth_lark(code: str = "", redirect: str = "/customers"):
    """Step 2 接入：code → open_id → cookie。

    现在返回 501，防止忘了配置就上线。
    """
    return HTMLResponse(
        "<p style='padding:40px;text-align:center;color:#E54545'>"
        "Lark OAuth 未配置，当前请用 /login 密码登录</p>",
        status_code=501,
    )
