"""通用已登录请求：登录后能碰的接口，理论上都能通过它触达。

防线（不是建议，是硬拦）：
1. 域名白名单（http.check_url），非 Steam 官方域名直接拒绝；
2. 只允许 https；
3. 涉及花钱 / 改账号安全设置 / 接受交易 的接口，必须显式 confirm=true，
   或者配置里把 behavior.allow_sensitive 打开；
4. 响应体截断，避免把整页 HTML 灌进模型上下文。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from . import auth, config, http

SENSITIVE_PATTERNS = [
    r"/purchase",
    r"/checkout",
    r"/finalizepurchase",
    r"/transaction",
    r"/account/changepassword",
    r"/account/email",
    r"/account/security",
    r"/account/deactivate",
    r"/deleteaccount",
    r"/tradeoffer/\d+/(accept|decline)",
    r"/market/(buy|sellitem|removelisting)",
    r"/market/sellitem",
    r"/actions/ajaxmodifymarketlisting",
]

MAX_BODY = 12000


def _is_sensitive(method: str, url: str, data: dict[str, Any] | None) -> bool:
    if method.upper() in ("GET", "HEAD"):
        return False
    target = url.lower()
    if data:
        target += " " + " ".join(f"{k}={v}" for k, v in data.items())
    return any(re.search(p, target) for p in SENSITIVE_PATTERNS)


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any = None,
    confirm: bool = False,
    raw_html: bool = False,
) -> dict[str, Any]:
    method = (method or "GET").upper()
    if not url.startswith("https://"):
        if url.startswith("/"):
            raise http.SteamError("请给出完整 URL（含域名），或用 host+path 形式。", code="bad_input")
        raise http.SteamError("只接受 https:// 开头的完整 URL。", code="bad_input")

    host = http.check_url(url).lower()

    if _is_sensitive(method, url, data) and not confirm:
        if not config.load()["behavior"].get("allow_sensitive"):
            raise http.SteamError(
                f"{method} {urlsplit(url).path} 被判定为不可逆操作（花钱 / 改安全设置 / 动交易），已拦截。",
                code="confirmation_required",
                hint="确认要执行就再调一次并传 confirm=true；想长期放行把 behavior.allow_sensitive 设为 true。",
            )

    result: dict[str, Any] = {
        "ok": True,
        "request": {"method": method, "url": url, "host": host},
    }
    if not auth.is_logged_in():
        result["warning"] = "当前未登录，本次请求未携带任何登录凭证（等价于匿名访问）。"

    kwargs: dict[str, Any] = {"params": params, "allow_redirects": False}
    if auth.is_logged_in():
        if method not in ("GET", "HEAD"):
            auth.warm_session()  # 写操作需要 Steam 下发的 sessionid，十分钟内只 warm 一次
        kwargs["cookies"] = auth.cookies_for(host)
    if data:
        kwargs["data"] = data
        kwargs["headers"] = {"X-Requested-With": "XMLHttpRequest", "Referer": f"https://{host}/"}
    if json_body is not None:
        kwargs["json"] = json_body

    resp = http.request(method, url, **kwargs)

    result["status"] = resp.status
    result["content_type"] = resp.headers.get("content-type", "")
    loc = resp.headers.get("location")
    if loc:
        result["location"] = loc
        if "login" in loc.lower():
            result["warning"] = "响应被重定向到登录页：cookie 可能已过期（用 steam_auth_status 复核）。"

    body = resp.text or ""
    if "json" in result["content_type"] or body.strip().startswith(("{", "[")):
        try:
            result["json"] = resp.json()
        except http.SteamError:
            result["body"] = body[:MAX_BODY]
    elif raw_html:
        result["body"] = body[:MAX_BODY]
    else:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        result["text_preview"] = text[:2000]
        result["body_length"] = len(body)
        if len(body) > MAX_BODY:
            result["truncated"] = True

    return result
