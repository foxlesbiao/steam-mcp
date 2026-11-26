"""HTTP 层：域名白名单、限速、代理/TLS 策略、错误归类。

设计约束（硬性的）：
1. 只允许访问下面 ALLOWED 里列出的 Steam 官方域名，其他一律拒绝，不做任何转发。
2. 只允许 https。
3. 每个域名有最小请求间隔，避免触发 Steam 的频控导致封禁会话。
"""
from __future__ import annotations

import json as _json
import logging
import ssl
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from . import config

# 硬性要求：凭证绝不进日志。httpx 在 INFO 级会把含 token 的完整 URL 打出来，全部压掉。
for _logger in ("httpx", "httpcore"):
    logging.getLogger(_logger).setLevel(logging.WARNING)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ALLOWED_HOSTS = {
    "store.steampowered.com",
    "api.steampowered.com",
    "login.steampowered.com",
    "help.steampowered.com",
    "steamcommunity.com",
    # 结账流程使用独立子域（steamLoginSecure cookie 域覆盖 *.steampowered.com）
    "checkout.steampowered.com",
    # 登录 finalizelogin 的 transfer_info 里固定包含 steam.tv，
    # 它也会 POST settoken 下发 cookie；不放行会导致续期链路每次都报 Blocked
    "steam.tv",
    # 网易 BUFF（buff.163.com）：Steam 官方域名之外唯一的第三方，
    # 用于 BUFF 登录（走 Steam OpenID）与市场行情查询
    "buff.163.com",
    # IsThereAnyDeal：历史最低价（SteamDB 无公开 API 且 Cloudflare 403，这是合规替代）
    # 需要免费 key；未配置时工具返回 itad_key_missing，不发请求
    "api.isthereanydeal.com",
}
ALLOWED_SUFFIXES = (".steamcommunity.com",)


class SteamError(Exception):
    """所有对外错误的基类，带机器可读 code 和给 agent 的 hint。"""

    def __init__(self, message: str, code: str = "error", hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


class NetworkError(SteamError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, code="network_unreachable", hint=hint)


class NotLoggedIn(SteamError):
    def __init__(self, message: str = "未登录 Steam，请先完成登录或导入 cookie。", hint: str | None = None):
        super().__init__(message, code="not_logged_in", hint=hint or "调用 steam_auth_status 查看状态，或用 steam_auth_import_cookie 导入浏览器 cookie。")


class Blocked(SteamError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, code="blocked", hint=hint)


class Rejected(SteamError):
    """Steam 明确返回了失败（业务层拒绝）。"""

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message, code=code)


# ---------------------------------------------------------------- 网络安全策略


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise Blocked(f"只允许 https 请求，收到 {parts.scheme or '空'}。")
    host = (parts.hostname or "").lower()
    if host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES):
        return host
    raise Blocked(
        f"域名 {host or '(空)'} 不在白名单内。",
        hint="本 MCP 只访问 Steam 官方域名与 buff.163.com：" + "、".join(sorted(ALLOWED_HOSTS)),
    )


# ---------------------------------------------------------------- 客户端


class _Limiter:
    def __init__(self, table: dict[str, float]):
        self.table = table
        self.lock = threading.Lock()
        self.last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        interval = self.table.get(host, 0.5)
        with self.lock:
            now = time.monotonic()
            delta = now - self.last.get(host, 0.0)
            if delta < interval:
                time.sleep(interval - delta)
            self.last[host] = time.monotonic()


def _build_client() -> httpx.Client:
    cfg = config.load()["network"]
    trust = cfg.get("tls_trust", "system")
    if trust == "insecure":
        verify: Any = False
    elif trust == "certifi":
        verify = True
    else:
        try:
            import truststore

            verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            verify = True

    mode = cfg.get("mode", "direct")
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(cfg.get("timeout", 25), connect=12),
        "verify": verify,
        "follow_redirects": False,
        "headers": {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        "transport": httpx.HTTPTransport(retries=int(cfg.get("retries", 2)), verify=verify),
    }
    if mode == "env":
        kwargs["trust_env"] = True
    else:
        kwargs["trust_env"] = False
        if mode == "custom" and cfg.get("proxy"):
            kwargs["proxy"] = cfg["proxy"]
    return httpx.Client(**kwargs)


_client: httpx.Client | None = None
_limiter: _Limiter | None = None
_lock = threading.Lock()


def client() -> httpx.Client:
    global _client
    with _lock:
        if _client is None:
            _client = _build_client()
            cfg = config.load()["network"]
            global _limiter
            _limiter = _Limiter(cfg.get("min_interval", {}))
        return _client


def reset() -> None:
    """配置变更后重建连接。"""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None


# ---------------------------------------------------------------- 请求


class Response:
    def __init__(self, raw: httpx.Response, host: str):
        self.raw = raw
        self.host = host
        self.status = raw.status_code
        self.url = str(raw.url)
        self.headers = raw.headers
        self.text = raw.text

    def json(self) -> Any:
        try:
            return _json.loads(self.text)
        except _json.JSONDecodeError:
            raise Rejected(f"接口返回的不是 JSON（HTTP {self.status}）：{self.text[:200]!r}", code="bad_payload")

    def looks_like_login_page(self) -> bool:
        head = self.text[:4000].lower()
        return ("<title>sign in" in head) or ("<title>登录" in head) or ('id="login_form' in head)


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json: Any = None,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    allow_redirects: bool = False,
    retries: int = 0,
    multipart: bool = False,
) -> Response:
    host = check_url(url)
    if _limiter is None:
        client()
    assert _limiter is not None
    _limiter.wait(host)

    # Steam 的 openid 表单要求 multipart/form-data（urlencoded 会被静默拒绝回 200）
    files = None
    if multipart and data is not None:
        files = {k: (None, str(v)) for k, v in data.items()}
        data = None

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = client().request(
                method.upper(),
                url,
                params=params,
                data=data,
                files=files,
                json=json,
                cookies=cookies,
                headers=headers,
                follow_redirects=allow_redirects,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            raise NetworkError(
                f"连接 {host} 失败：{type(exc).__name__}",
                hint=_net_hint(host),
            ) from exc
        except httpx.ProxyError as exc:
            raise Blocked(
                f"经由代理连接 {host} 失败：{exc}",
                hint="检查 network.mode 设置。已启用本地反代（hosts 指向 127.0.0.1）时应设为 direct。",
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"请求 {host} 出错：{type(exc).__name__}: {exc}") from exc

        if resp.status_code in (429, 503) and attempt <= max(2, retries):
            delay = 3.0 * attempt if attempt <= 2 else 10.0
            time.sleep(delay)
            continue
        break

    return Response(resp, host)


def _net_hint(host: str) -> str:
    if host == "steamcommunity.com" or host.endswith(".steamcommunity.com"):
        return "社区域名在国内常被 DNS 污染。启动 Steamcommunity 302 之类的本地反代后再试，或把 network.proxy 设为可用代理。"
    if host == "api.steampowered.com":
        return "api.steampowered.com 在国内多数网络不可直连，可配置代理（network.mode=custom + proxy）。"
    return "检查网络连通性，或运行 steam_net_check 做逐域名诊断。"


def get_text(url: str, **kwargs: Any) -> str:
    return request("GET", url, **kwargs).text


def get_json(url: str, **kwargs: Any) -> Any:
    return request("GET", url, **kwargs).json()


def post_json(url: str, **kwargs: Any) -> Any:
    return request("POST", url, **kwargs).json()


def describe_hosts() -> Iterable[str]:
    return sorted(ALLOWED_HOSTS)
