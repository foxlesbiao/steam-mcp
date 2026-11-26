"""登录态管理。

三条路径，优先推荐第一条：

1. cookie 导入（推荐，最稳）
   浏览器登录 Steam 后，从开发者工具复制 `steamLoginSecure`（有 `steamRefresh_steam` 更好，
   可以自动续期）。不需要密码、不经过 api.steampowered.com，国内网络直接可用。

2. 账号密码登录（走 IAuthenticationService）
   需要能访问 api.steampowered.com。密码只在内存中做 RSA 加密后发出，绝不落盘、绝不写日志。

3. 刷新
   有 refresh token / steamRefresh_steam 时可静默续期；否则提示重新导入。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import config, http, secrets

API = "https://api.steampowered.com"
STORE = "https://store.steampowered.com"
COMMUNITY = "https://steamcommunity.com"
LOGIN = "https://login.steampowered.com"

AUTH_WEBSITE_ID = "Community"

# Steam EAuthSessionGuardType（BeginAuthSessionViaCredentials 返回的 allowed_confirmations）
GUARD_UNKNOWN = 0
GUARD_NONE = 1
GUARD_EMAIL_CODE = 2           # 邮箱验证码，需输入
GUARD_DEVICE_CODE = 3          # 手机令牌 TOTP 动态码，需输入
GUARD_DEVICE_CONFIRMATION = 4  # 手机 Steam App 点「确认登录」，无需输码
GUARD_EMAIL_CONFIRMATION = 5   # 邮箱确认链接，无需输码
GUARD_MACHINE_TOKEN = 6

# 只需「等待对方确认」的类型（不用输码，靠 PollAuthSessionStatus 轮询）
CONFIRMATION_TYPES = {GUARD_DEVICE_CONFIRMATION, GUARD_EMAIL_CONFIRMATION}
# 需要「输入验证码」的类型
CODE_TYPES = {GUARD_EMAIL_CODE, GUARD_DEVICE_CODE}

GUARD_LABELS = {
    2: "邮箱验证码（需输入）",
    3: "手机令牌动态码（需输入）",
    4: "手机 Steam App 确认（无需输码）",
    5: "邮箱确认（无需输码）",
    6: "本机令牌",
}


# ---------------------------------------------------------------- 状态


def state() -> dict[str, Any]:
    return secrets.load_credentials()


def is_logged_in() -> bool:
    data = state()
    return bool(data.get("cookies", {}).get("steamLoginSecure"))


def steamid() -> str | None:
    data = state()
    return data.get("steamid") or _steamid_from_cookie(data.get("cookies", {}).get("steamLoginSecure"))


def sessionid() -> str:
    """返回当前会话的 sessionid：优先 Steam 下发的（写操作校验 cookie 与表单一致），
    没有才生成随机值。表单里的 sessionid 和 cookie 里的必须是同一个。"""
    data = state()
    sid = (data.get("cookies") or {}).get("sessionid") or data.get("sessionid")
    if sid:
        return sid
    sid = os.urandom(12).hex()
    data["sessionid"] = sid
    secrets.save_credentials(data)
    return sid


_SESSION_COOKIE_EXACT = {"steamLoginSecure", "steamRefresh_steam", "sessionid", "steamCountry", "browserid"}


def _keep_cookie(name: str) -> bool:
    """登录态相关 cookie：固定名字 + steamMachineAuth<steamid> 这类设备令牌。

    设备令牌（steamMachineAuth…）决定 Steam 是否把这台机器当可信设备：
    缺了它每次登录都要手机确认，refresh token 也容易被判不可信而轮换失败。
    按前缀匹配，因为名字里带 steamid。
    """
    return name in _SESSION_COOKIE_EXACT or name.startswith("steamMachineAuth")


def cookies_for(host: str) -> dict[str, str]:
    """按目标域名挑 cookie，避免把无关 cookie 发给别的域名。

    sessionid 优先用 Steam 下发的（写操作校验它）；没有才用自己生成的随机值。
    domain_cookies 是登录 transfer 流程给各域名（如 checkout.steampowered.com）
    单独下发的 cookie，checkout 这类域名的登录态只认它们。
    """
    data = state()
    jar = data.get("cookies", {})
    out: dict[str, str] = {}
    if host == "login.steampowered.com":
        if jar.get("steamRefresh_steam"):
            out["steamRefresh_steam"] = jar["steamRefresh_steam"]
        out["sessionid"] = jar.get("sessionid") or sessionid()
        return out
    if "steamLoginSecure" in jar:
        out["steamLoginSecure"] = jar["steamLoginSecure"]
    out["sessionid"] = jar.get("sessionid") or sessionid()
    for extra in ("steamCountry", "browserid"):
        if jar.get(extra):
            out[extra] = jar[extra]
    for name, value in jar.items():
        if name.startswith("steamMachineAuth") and value:
            out[name] = value
    for name, value in (data.get("domain_cookies", {}).get(host) or {}).items():
        out[name] = value
    return out


def warm_session(force: bool = False) -> dict[str, Any]:
    """从 store 首页拿 Steam 下发的 sessionid / browserid / steamCountry 并缓存。

    写操作（加愿望单、加购物车等）会校验 sessionid 与登录会话绑定，
    自己生成的随机值会被拒绝（success:false），所以写之前必须先 warm 一次。
    force=True 强制重取（sessionid 会轮换，缓存里的旧值会让写操作静默失败）。
    """
    data = state()
    login_secure = data.get("cookies", {}).get("steamLoginSecure")
    if not login_secure:
        raise NotLoggedIn()
    if (
        not force
        and data.get("warmed_at")
        and time.time() - data["warmed_at"] < 600
        and {"sessionid", "browserid", "steamCountry"} <= set(data.get("cookies", {}))
    ):
        return {"ok": True, "cached": True, "sessionid_present": True}
    resp = http.request("GET", f"{STORE}/", cookies={"steamLoginSecure": login_secure})
    if resp.status != 200 or resp.looks_like_login_page():
        raise NotLoggedIn("warm session 失败：store 首页拒绝了当前登录态（cookie 可能过期）。")
    jar = data.setdefault("cookies", {})
    collected = []
    for line in resp.headers.get_list("set-cookie"):
        name, _, rest = line.partition("=")
        value = rest.split(";")[0]
        if name in ("sessionid", "browserid", "steamCountry") and value:
            # cookie 值按原样存：unquote 会吃掉 %7C 这类转义，发回 Steam 时值就非法了
            jar[name] = value
            collected.append(name)
    data["warmed_at"] = int(time.time())
    secrets.save_credentials(data)
    return {"ok": True, "collected": sorted(set(collected)), "sessionid_present": bool(jar.get("sessionid"))}


def _steamid_from_cookie(value: str | None) -> str | None:
    if not value:
        return None
    head = unquote(value).split("||")[0].strip()
    return head if head.isdigit() and len(head) == 17 else None


def _token_from_cookie(value: str | None) -> str | None:
    if not value:
        return None
    parts = unquote(value).split("||", 1)
    return parts[1] if len(parts) == 2 and parts[1] else None


# ---------------------------------------------------------------- cookie 导入


def parse_cookie_blob(blob: str) -> dict[str, str]:
    """兼容三种粘贴格式：Cookie 头、DevTools 复制出的 TSV、纯 steamLoginSecure 值。"""
    text = (blob or "").strip()
    if not text:
        raise http.SteamError("cookie 内容为空", code="bad_input")

    if text.startswith("{"):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return {str(k): str(v) for k, v in loaded.items()}
        except json.JSONDecodeError:
            pass

    pairs: dict[str, str] = {}
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:  # DevTools 表格：name \t value \t domain ...
            cols = line.split("\t")
            if len(cols) >= 2:
                pairs[cols[0].strip()] = cols[1].strip()
            continue
        for chunk in line.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            pairs[name.strip()] = value.strip()

    if not pairs and "||" in unquote(text):
        pairs["steamLoginSecure"] = text
    return pairs


def import_cookie(blob: str, *, verify: bool = True) -> dict[str, Any]:
    pairs = parse_cookie_blob(blob)
    login_secure = unquote(pairs.get("steamLoginSecure", "")).strip()
    if "||" not in login_secure:
        raise http.SteamError(
            "没找到有效的 steamLoginSecure（形如 7656119xxxxxxxxxx||eyJhbGci...）。",
            code="bad_input",
            hint="浏览器 F12 → Application → Cookies → https://store.steampowered.com → 复制 steamLoginSecure 整行。",
        )
    sid = steamid_from = _steamid_from_cookie(login_secure)
    if not sid:
        raise http.SteamError("无法从 steamLoginSecure 解析出 SteamID。", code="bad_input")

    data = state()
    jar = data.get("cookies", {})
    jar["steamLoginSecure"] = login_secure
    if pairs.get("steamRefresh_steam"):
        # 原样保留：值里含 %7C 编码，unquote 会让 Steam 拒绝
        jar["steamRefresh_steam"] = pairs["steamRefresh_steam"].strip()
    if pairs.get("steamCountry"):
        jar["steamCountry"] = pairs["steamCountry"].strip()
    if pairs.get("sessionid"):
        data["sessionid"] = pairs["sessionid"].strip()
    elif not data.get("sessionid"):
        data["sessionid"] = os.urandom(12).hex()

    data.update(
        {
            "steamid": sid,
            "cookies": jar,
            "access_token": _token_from_cookie(login_secure),
            "method": "cookie",
            "obtained_at": int(time.time()),
        }
    )
    if pairs.get("account_name"):
        data["account"] = pairs["account_name"]
    secrets.save_credentials(data)

    result: dict[str, Any] = {
        "ok": True,
        "method": "cookie",
        "steamid": sid,
        "has_refresh_cookie": bool(jar.get("steamRefresh_steam")),
        "credential_fingerprint": secrets.fingerprint(login_secure),
    }
    if verify:
        result["verification"] = verify_session()
    return result


# ---------------------------------------------------------------- 校验


def verify_session() -> dict[str, Any]:
    """访问 store 的 /account/ 判断会话是否有效。"""
    if not is_logged_in():
        return {"valid": False, "reason": "no_cookie"}
    try:
        resp = http.request(
            "GET",
            f"{STORE}/account/",
            cookies=cookies_for("store.steampowered.com"),
        )
    except http.SteamError as exc:
        return {"valid": None, "reason": "network", "error": exc.as_dict()}

    if resp.status in (301, 302, 303) and "login" in (resp.headers.get("location") or ""):
        return {"valid": False, "reason": "expired", "status": resp.status}
    if resp.status == 200 and not resp.looks_like_login_page():
        account = None
        for marker in ('data-account-name="', 'account_name&quot;:&quot;'):
            idx = resp.text.find(marker)
            if idx >= 0:
                frag = resp.text[idx + len(marker) : idx + len(marker) + 60]
                account = frag.split('"')[0].split("&quot;")[0] or None
                break
        return {"valid": True, "status": 200, "account_name": account}
    return {"valid": False, "reason": "unexpected", "status": resp.status}


def status() -> dict[str, Any]:
    data = state()
    if not data.get("cookies", {}).get("steamLoginSecure"):
        return {
            "ok": True,
            "logged_in": False,
            "hint": "先登录：steam_auth_import_cookie（推荐）或 steam_auth_login。",
            "config_path": str(config.config_path()),
        }
    info: dict[str, Any] = {
        "ok": True,
        "logged_in": True,
        "steamid": data.get("steamid"),
        "account": data.get("account"),
        "method": data.get("method"),
        "obtained_at": data.get("obtained_at"),
        "has_refresh_token": bool(data.get("refresh_token") or data.get("cookies", {}).get("steamRefresh_steam")),
        "access_token": secrets.fingerprint(data.get("access_token")),
        "session_cookie": secrets.fingerprint(data.get("cookies", {}).get("steamLoginSecure")),
    }
    if data.get("account") is None and info["steamid"]:
        info["account"] = None
    info["renew"] = renew_status()
    info["verification"] = verify_session()
    return info


def require_login() -> dict[str, Any]:
    """所有需要登录的工具都从这里过一道：先保证登录态可用（过期会自动续期）。

    续期成功会直接写回凭证库，调用方不需要关心；续期失败会抛 NotLoggedIn，
    并如实说明 refresh token 已失效、需要重新登录——不做静默降级。
    """
    if not is_logged_in():
        raise http.NotLoggedIn()
    ensure_session()
    return state()


# ---------------------------------------------------------------- 密码登录


def _rsa_encrypt(password: str, mod_hex: str, exp_hex: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    n = int(mod_hex, 16)
    e = int(exp_hex, 16)
    key = rsa.RSAPublicNumbers(e, n).public_key()
    cipher = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def login_with_password(username: str, password: str) -> dict[str, Any]:
    """第一步：提交账号密码。返回是否需要 Steam Guard 验证码。"""
    if not username or not password:
        raise http.SteamError("账号或密码为空", code="bad_input")

    try:
        key_resp = http.get_json(
            f"{API}/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
            params={"account_name": username},
        )
    except http.NetworkError as exc:
        raise http.NetworkError(
            "无法访问 api.steampowered.com，密码登录不可用。",
            hint=exc.hint,
        ) from exc

    key = key_resp.get("response", {}) if isinstance(key_resp, dict) else {}
    if not key.get("publickey_mod"):
        raise http.Rejected("获取 RSA 公钥失败（账号名可能不存在）。")

    encrypted = _rsa_encrypt(password, key["publickey_mod"], key["publickey_exp"])
    # 注意：不要带 device_details —— Steam 现行接口对该参数一律返回
    # 400 "Please verify that all required parameters are being sent"（已实测多种序列化写法）。
    payload = {
        "account_name": username,
        "encrypted_password": encrypted,
        "encryption_timestamp": key["timestamp"],
        "remember_login": "true",
        "platform_type": "SteamClient",
        "persistence": 1,
        "website_id": AUTH_WEBSITE_ID,
        "language": "schinese",
    }
    begin = http.post_json(f"{API}/IAuthenticationService/BeginAuthSessionViaCredentials/v1/", data=payload)
    resp = begin.get("response", {}) if isinstance(begin, dict) else {}
    if not resp.get("client_id"):
        detail = resp.get("extended_error_message") or ""
        if not resp:
            raise http.Rejected(
                "登录失败：账号名不存在（Steam 未返回任何会话）。",
                code="invalid_credentials",
            )
        raise http.Rejected(
            "登录失败：账号名或密码不正确" + (f"（{detail}）" if detail else "。"),
            code="invalid_credentials",
        )

    pending = {
        "client_id": resp["client_id"],
        "request_id": resp.get("request_id"),
        "steamid": resp.get("steamid"),
        "interval": resp.get("interval", 5),
        "account": username,
        "created_at": int(time.time()),
    }
    raw_hints = [c.get("confirmation_type") for c in resp.get("allowed_confirmations", []) if isinstance(c, dict)]
    hints: list[int] = []
    for h in raw_hints:
        try:
            hints.append(int(h))
        except (TypeError, ValueError):
            continue
    confirm_types = sorted({h for h in hints if h in CONFIRMATION_TYPES})
    code_types = sorted({h for h in hints if h in CODE_TYPES})
    # 输码时优先手机令牌动态码(3)，其次邮箱验证码(2)
    suggested = 3 if 3 in code_types else (2 if 2 in code_types else 2)

    data = state()
    data["_pending"] = pending
    secrets.save_credentials(data)

    # 顺序很关键：只要 Steam 给了「确认类」(4/5)，就先让用户在手机 App / 邮箱点确认，
    # 不弹验证码输入框；等不到确认才回落到输入验证码。
    if confirm_types:
        stage = "awaiting_confirmation"
        next_hint = (
            "检测到可在手机/邮箱直接确认（类型 4/5）。先调用 steam_auth_poll 等待确认，无需输验证码；"
            "若等待超时仍需要输码，再调用 steam_auth_guard（不要提交空码）。"
        )
    elif code_types:
        stage = "awaiting_guard"
        next_hint = "调用 steam_auth_guard 提交 Steam Guard 验证码（2=邮箱验证码，3=手机令牌动态码）。"
    else:
        stage = "awaiting_poll"
        next_hint = "调用 steam_auth_poll 等待在手机/邮箱上确认。"

    out: dict[str, Any] = {
        "ok": True,
        "stage": stage,
        "steamid": pending["steamid"],
        "allowed_confirmations": hints,
        "confirmation_types": confirm_types,
        "code_types": code_types,
        "guard_labels": {str(h): GUARD_LABELS.get(h, f"未知类型 {h}") for h in hints},
        "suggested_code_type": suggested,
        "next": next_hint,
    }
    return out


def submit_guard_code(code: str, code_type: int = 2) -> dict[str, Any]:
    data = state()
    pending = data.get("_pending")
    if not pending:
        raise http.Rejected("没有进行中的登录，请先调用 steam_auth_login。")
    if not code or not code.strip():
        # 空码不是错误：用户可能只是想跳过输入、回头去手机 App 上点确认。
        # 返回可操作提示而不是 bad_input，避免「回车就报错」。
        return {
            "ok": True,
            "stage": "guard_skipped",
            "message": "未输入验证码，未提交任何验证；登录会话仍保留。",
            "hint": "请在手机 Steam App / 邮箱完成确认后调用 steam_auth_poll 继续；"
                    "若确实要输码，重新调用并填入动态码（2=邮箱码，3=手机令牌）。",
        }

    http.post_json(
        f"{API}/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/",
        data={
            "client_id": pending["client_id"],
            "steamid": pending["steamid"],
            "code": code.strip(),
            "code_type": int(code_type),
        },
    )
    return poll_login()


def poll_login(timeout: float | None = None) -> dict[str, Any]:
    """轮询登录会话，等手机 App / 邮箱确认或验证码生效。

    timeout=None 用默认 90 秒；传秒数可自定义（最少 5 秒）。超时返回 stage=pending，
    _pending 仍保留，调用方可以继续轮询，或改用 submit_guard_code 输验证码。
    """
    data = state()
    pending = data.get("_pending")
    if not pending:
        raise http.Rejected("没有进行中的登录，请先调用 steam_auth_login。")

    limit = 90.0 if timeout is None else max(5.0, float(timeout))
    started = time.time()
    deadline = started + limit
    interval = max(1.0, float(pending.get("interval") or 5))
    while True:
        # 注意：PollAuthSessionStatus 必须 POST，GET 一律 405（已实测）
        result = http.post_json(
            f"{API}/IAuthenticationService/PollAuthSessionStatus/v1/",
            data={"client_id": pending["client_id"], "request_id": pending.get("request_id")},
        )
        body = result.get("response", {}) if isinstance(result, dict) else {}
        if body.get("new_client_id"):
            pending["client_id"] = body["new_client_id"]
            data["_pending"] = pending
            secrets.save_credentials(data)
        if body.get("refresh_token"):
            return _finish_login(body, pending)
        if time.time() > deadline:
            return {
                "ok": True,
                "stage": "pending",
                "waited_seconds": round(time.time() - started, 1),
                "message": "等待手机/邮箱确认超时，登录会话仍保留。",
                "hint": "在手机 Steam App 里点确认后再调用 steam_auth_poll 继续；"
                        "或改用 steam_auth_guard 提交验证码（2=邮箱码，3=手机令牌动态码）。",
            }
        time.sleep(interval)


def _finish_login(body: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    """关键顺序：先换 access_token，再 finalize，最后逐域名收集 transfer cookie。

    实测 finalizelogin 会消费掉这次登录会话，之后 GenerateAccessTokenForApp 只会回空，
    所以换 token 必须排在最前面。steamLoginSecure 的格式就是 `<steamid>||<access_token>`，
    优先自己拼（本地反代会吞掉 settoken 的 Set-Cookie）。

    但即使自拼的 cookie 已通过校验，也**必须继续跑 finalizelogin 的 transfer_info**：
    结账域名 checkout.steampowered.com 只认登录时单独下发给它的 sessionid，
    跳过这一步会导致结账永远 302 到登录页（本次排查的根因）。
    """
    refresh_token = body["refresh_token"]
    sid = pending.get("steamid") or body.get("steamid")
    warnings: list[str] = []

    access_token = body.get("access_token")
    if not access_token:
        try:
            app_tok = http.post_json(
                f"{API}/IAuthenticationService/GenerateAccessTokenForApp/v1/",
                data={"refresh_token": refresh_token, "steamid": sid},
            )
            access_token = (app_tok.get("response") or {}).get("access_token")
            if not access_token:
                warnings.append("GenerateAccessTokenForApp 返回空（refresh_token 可能已被消费）")
        except http.SteamError as exc:
            warnings.append(f"GenerateAccessTokenForApp 失败：{exc.message}")

    jar: dict[str, str] = {}
    if access_token:
        jar["steamLoginSecure"] = f"{sid}||{access_token}"

    # finalizelogin 的 transfer_info 会给每个域名单独下发 cookie（含 checkout 专属 sessionid）。
    # 失败不阻塞登录，但要如实记进 warnings，让调用方知道结账可能不可用。
    transfer = _finalize_web_session(refresh_token, sid)
    by_host: dict[str, dict[str, str]] = transfer.get("by_host") or {}
    if transfer.get("cookies"):
        for name, value in transfer["cookies"].items():
            jar.setdefault(name, value)
    warnings.extend(transfer.get("warnings") or [])

    data = state()
    data.update(
        {
            "steamid": sid,
            "account": pending.get("account"),
            "cookies": jar,
            "domain_cookies": by_host,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "method": "password",
            "obtained_at": int(time.time()),
        }
    )
    secrets.save_credentials(data)

    verification = verify_session() if jar.get("steamLoginSecure") else {"valid": False, "reason": "no_token"}
    data.pop("_pending", None)
    secrets.save_credentials(data)
    return {
        "ok": bool(verification.get("valid")),
        "stage": "logged_in" if verification.get("valid") else "need_cookie",
        "steamid": sid,
        "account": pending.get("account"),
        "cookies_collected": sorted(jar),
        "domain_cookies_collected": sorted(by_host),
        "verification": verification,
        "warnings": warnings,
        "hint": None if verification.get("valid") else "浏览器复制 steamLoginSecure 后调用 steam_auth_import_cookie。",
    }


def _finalize_web_session(refresh_token: str, sid: str) -> dict[str, Any]:
    """finalizelogin + 逐域名 POST transfer URL，按域名分别收集 cookie。

    transfer_info 里每个条目形如 {"url": "https://xxx/login/settoken", "params": {...}}，
    POST 之后该域名会下发 sessionid / steamLoginSecure。必须按 host 分开存，
    因为 checkout.steampowered.com 的 sessionid 与 store 的不通用。
    """
    payload = {
        "nonce": refresh_token,
        "sessionid": sessionid(),
        "redir": f"{COMMUNITY}/login/home/?goto=",
    }
    collected: dict[str, str] = {}
    by_host: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    try:
        resp = http.request(
            "POST",
            f"{LOGIN}/jwt/finalizelogin",
            data=payload,
            cookies=cookies_for("login.steampowered.com"),
            # Steam 校验来源站：缺 Origin/Referer 会被当成异常客户端
            headers={"Origin": COMMUNITY, "Referer": f"{COMMUNITY}/"},
            multipart=True,
        )
        body = resp.json()
    except http.SteamError as exc:
        return {"cookies": collected, "by_host": by_host, "warnings": [f"finalizelogin 失败：{exc.message}"]}

    # finalizelogin 顶层响应会下发 steamRefresh_steam —— 这是真正长期可用的续期凭证
    # （登录得来的 refresh_token 会被 finalize 消费掉，只有它能在之后继续 ajaxrefresh）。
    # 之前只从 transfer 子请求收 cookie，把它漏了，导致自动续期没有可用输入。
    for name, value in resp.raw.cookies.items():
        if _keep_cookie(name) and value:
            collected[name] = value

    if not body.get("success", True):
        return {
            "cookies": collected,
            "by_host": by_host,
            "warnings": [f"finalizelogin 拒绝：error={body.get('error')} {body.get('message') or ''}".strip()],
        }
    transfers = body.get("transfer_info") or []
    if not transfers:
        warnings.append("finalizelogin 未返回 transfer_info（拿不到 checkout 专属 cookie）")

    for item in transfers:
        url = item.get("url")
        params = item.get("params") or {}
        if not url:
            continue
        host = (urlsplit(url).hostname or "").lower()
        try:
            # 浏览器做 settoken 时会带上该域已有的 cookie，缺了可能不回下发新的
            sub = http.request("POST", url, data={**params, "steamID": sid}, cookies=cookies_for(host))
        except http.SteamError as exc:
            warnings.append(f"{host or url}: {exc.code}")
            continue
        host_jar = by_host.setdefault(host, {})
        for name, value in sub.raw.cookies.items():
            if _keep_cookie(name):
                host_jar[name] = value
                collected.setdefault(name, value)
    return {"cookies": collected, "by_host": by_host, "warnings": warnings, "steamid": body.get("steamID") or sid}


def fetch_webapi_key() -> dict[str, Any]:
    """从 /dev/apikey 页面抓已注册的 Web API Key 并存进凭证库。"""
    require_login()
    jar = cookies_for("steamcommunity.com")
    resp = http.request("GET", f"{COMMUNITY}/dev/apikey", cookies=jar, allow_redirects=True)
    if resp.looks_like_login_page():
        raise NotLoggedIn("apikey 页面要求登录。")
    match = re.search(r"(?:密钥|Key)[:：]\s*([0-9A-F]{32})", resp.text)
    if not match:
        return {
            "ok": True,
            "has_key": False,
            "hint": "账号还没有 Web API Key，可调用 steam_webapi_key(action=\"register\") 注册（会同意 Steam Web API 使用条款）。",
        }
    key = match.group(1)
    data = state()
    data["webapi_key"] = key
    secrets.save_credentials(data)
    return {"ok": True, "has_key": True, "key": key, "note": "key 已存入凭证库，成就/新闻/档案等接口即刻解锁。"}


def register_webapi_key(domain: str = "localhost") -> dict[str, Any]:
    """注册 Web API Key。POST /dev/requestkey 会代表账号同意 Steam Web API 使用条款。"""
    require_login()
    warm_session()
    jar = cookies_for("steamcommunity.com")
    resp = http.request(
        "POST",
        f"{COMMUNITY}/dev/requestkey",
        data={"domain": domain, "request_id": 0, "sessionid": sessionid(), "agreeToTerms": "true"},
        cookies=jar,
        headers={"Referer": f"{COMMUNITY}/dev/apikey", "X-Requested-With": "XMLHttpRequest"},
    )
    try:
        body = resp.json()
    except http.SteamError:
        body = {"raw": resp.text[:200]}
    return {"ok": bool(body.get("success") == 1), "response": body, "retry_fetch": fetch_webapi_key() if body.get("success") == 1 else None}


# ---------------------------------------------------------------- 续期 / 登出

# 进程内缓存：避免每个工具调用都多打一次 verify_session 的网络往返。
_ensure_lock = threading.Lock()
_ensure_cache: dict[str, Any] = {"checked_at": 0.0, "valid": False}


def _refresh_jwt(raw: str | None) -> str:
    """从续期凭证里取出真正的 refresh JWT。

    磁盘上可能有三种形态：纯 JWT（登录 API 返回）、
    `<steamid>%7C%7C<jwt>`（steamRefresh_steam cookie 原值）、
    以及被 unquote 过的 `<steamid>||<jwt>`。
    ajaxrefresh / finalizelogin 都只认 JWT 本身，传整串一律被拒。
    """
    s = unquote(raw or "").strip()
    return s.split("||", 1)[1] if "||" in s else s


def _renew(force: bool = False) -> dict[str, Any]:
    """用 refresh token 换一整套新的登录态，并把各域名的完整 cookie 收回来。

    顺序不可调换（每一步都是实测踩出来的）：
    1. `/jwt/ajaxrefresh`：nonce 必须是 refresh_token；成功会轮换出一个新的 refresh token。
    2. `GenerateAccessTokenForApp`：**必须排在 finalizelogin 之前**，
       否则 token 被消费后该接口只回空 response。
    3. 自拼 `steamLoginSecure = "<steamid>||<access_token>"`（本地反代常吞掉 settoken 的 Set-Cookie）。
    4. `finalizelogin` → 逐个 POST transfer_info 里的 settoken，把
       store / community / help / checkout / steam.tv 各自的 cookie 收全，
       按 host 写进 `domain_cookies`。checkout 域只认这一份，缺了就永远 302 到登录页。

    只写凭证库，不写日志、不回传任何 token 明文。
    """
    data = state()
    jar = data.get("cookies") or {}
    # 续期凭证的优先级：steamRefresh_steam（长期、可反复轮换）> 登录得到的 refresh_token
    # （后者会被 finalizelogin 消费，只能用一次）。两个都试，能用哪个用哪个。
    candidates: list[str] = []
    for candidate in (jar.get("steamRefresh_steam"), data.get("refresh_token")):
        jwt = _refresh_jwt(candidate) if candidate else ""
        if jwt and jwt not in candidates:
            candidates.append(jwt)
    if not candidates:
        return {
            "ok": False,
            "error": "no_refresh_token",
            "message": "凭证库里没有 refresh token，无法静默续期。",
            "hint": "重新导入浏览器 cookie（要带上 steamRefresh_steam）或用密码登录一次。",
        }

    sid = data.get("steamid") or _steamid_from_cookie(jar.get("steamLoginSecure"))
    warnings: list[str] = []

    # ---- 1) ajaxrefresh：拿一次性 transfer nonce（顺带可能轮换 refresh token）
    new_refresh: str | None = None
    new_access: str | None = None
    new_nonce: str | None = None
    body: dict[str, Any] = {}
    refresh_token = candidates[0]
    for candidate in candidates:
        body = {}
        try:
            resp = http.request(
                "POST",
                f"{LOGIN}/jwt/ajaxrefresh",
                # nonce 必须是 refresh token 本身；发空 body 是早期实现缺陷，必失败
                data={"nonce": candidate, "sessionid": sessionid(), "redir": f"{STORE}/login/?goto="},
                cookies=cookies_for("login.steampowered.com"),
            )
            if resp.text.strip().startswith(("{", "[")):
                body = resp.json() or {}
            # 响应里也可能顺手轮换 steamRefresh_steam
            for name, value in resp.raw.cookies.items():
                if name == "steamRefresh_steam" and value:
                    jar["steamRefresh_steam"] = value
        except http.SteamError as exc:
            warnings.append(f"ajaxrefresh 失败：{exc.code}")
            continue

        if isinstance(body, dict) and body.get("success") is False:
            warnings.append(f"ajaxrefresh 拒绝：error={body.get('error')}")
            continue
        refresh_token = candidate
        if isinstance(body, dict):
            new_refresh = body.get("refresh_token") or body.get("token") or body.get("new_token") or None
            new_access = body.get("access_token") or None
            # ajaxrefresh 的响应体里没有 access/refresh token，只有一次性 nonce（+ auth 串）。
            # finalizelogin 必须拿这个新 nonce，用旧的 refresh_token 会被直接拒。
            new_nonce = body.get("nonce") or None
        if new_refresh or new_access or new_nonce:
            break

    # finalizelogin 的 nonce 就是 refresh JWT 本身（ajaxrefresh 返回的 nonce 是另一条链路用的，传它会 error=8）
    work_refresh = new_refresh or refresh_token
    if isinstance(body, dict):
        new_refresh = new_refresh or None
        new_access = new_access or body.get("access_token") or None

    # ---- 2) 换 access token（必须在 finalize 之前）
    if not new_access and sid:
        try:
            app_tok = http.post_json(
                f"{API}/IAuthenticationService/GenerateAccessTokenForApp/v1/",
                data={"refresh_token": work_refresh, "steamid": sid},
            )
            new_access = ((app_tok or {}).get("response") or {}).get("access_token") or None
        except http.SteamError as exc:
            warnings.append(f"GenerateAccessTokenForApp 失败：{exc.code}")

    # ---- 3) 自拼登录 cookie
    merged_jar = dict(jar)
    if new_access and sid:
        merged_jar["steamLoginSecure"] = f"{sid}||{new_access}"
        got_new_login_secure = True
    else:
        # 浏览器源的 refresh token 换不到 access token 是正常的：
        # finalizelogin 的 settoken 会直接下发各域 steamLoginSecure，是否真拿到看下一步
        got_new_login_secure = False

    # ---- 4) finalizelogin transfer，逐域名收完整 cookie
    transfer: dict[str, Any] = {}
    if work_refresh:
        transfer = _finalize_web_session(work_refresh, sid or "")
    warnings.extend(transfer.get("warnings") or [])
    # 设备令牌 / 续期凭证必须覆盖（这是长期登录的关键）；
    # 其余沿用 setdefault —— transfer 里混着 checkout 域的 steamLoginSecure，不能写进 store 的 jar。
    for name, value in (transfer.get("cookies") or {}).items():
        if name.startswith("steamMachineAuth") or name == "steamRefresh_steam":
            merged_jar[name] = value
        else:
            merged_jar.setdefault(name, value)

    # 合并而不是覆盖：某个域名这次没下发，不至于把上次收好的 cookie 抹掉
    merged_domains: dict[str, dict[str, str]] = {
        host: dict(cookies) for host, cookies in (data.get("domain_cookies") or {}).items()
    }
    for host, cookies in (transfer.get("by_host") or {}).items():
        merged_domains.setdefault(host, {}).update(cookies)

    if not got_new_login_secure and not merged_domains.get("store.steampowered.com", {}).get("steamLoginSecure"):
        warnings.append("续期没拿到新的 steamLoginSecure，沿用旧值（可能已过期）")

    data.update(
        {
            "steamid": sid,
            "cookies": merged_jar,
            "domain_cookies": merged_domains,
            "access_token": new_access or data.get("access_token"),
            # 只存真正的 refresh token；ajaxrefresh 的 nonce 是一次性的，存进去会毁掉续期能力
            "refresh_token": new_refresh or data.get("refresh_token"),
            "refreshed_at": int(time.time()),
        }
    )
    secrets.save_credentials(data)

    verification = verify_session()
    ok = bool(verification.get("valid"))
    if ok:
        _ensure_cache.update({"checked_at": time.time(), "valid": True})

    out: dict[str, Any] = {
        "ok": ok,
        "refreshed": ok,
        "steamid": sid,
        "rotated_refresh_token": bool(new_refresh),
        "new_access_token": bool(new_access),
        "session_cookie": secrets.fingerprint(merged_jar.get("steamLoginSecure")),
        "domain_cookies": {host: sorted(cookies) for host, cookies in sorted(merged_domains.items())},
        "checkout_ready": bool(merged_domains.get("checkout.steampowered.com")),
        "verification": verification,
        "warnings": warnings,
    }
    if not ok:
        out["error"] = "refresh_rejected"
        out["message"] = "续期链路跑完但会话仍无效（refresh token 通常只能用一次，可能已被消费）。"
        out["hint"] = "重新登录一次（steam_auth_login 或 steam_auth_import_cookie），之后自动续期即可正常工作。"
    return out


def refresh() -> dict[str, Any]:
    """手动续期入口：强制跑一次完整续期链路（不受节流窗口限制）。"""
    return _renew(force=True)


def ensure_session(force: bool = False, *, verify_seconds: int | None = None) -> dict[str, Any]:
    """保证登录态可用：发现过期就用 refresh token 自动续期（幂等 + 节流）。

    行为：
    - 进程内缓存校验结果，默认 10 分钟内不重复校验，避免每个工具调用都多一次网络往返。
    - 只有**明确过期**（302 到登录页）才续期；网络异常不会被误判成过期。
    - 续期成功后 cookies 与 domain_cookies 全量写回凭证库，下次冷启动直接可用。
    - 续期失败不静默降级：抛 NotLoggedIn，把真实原因和「需要重新登录」写清楚。
    """
    if not is_logged_in():
        raise http.NotLoggedIn()

    cfg = config.load()["behavior"]
    if not cfg.get("auto_renew", True):
        return {"ok": True, "checked": False, "reason": "auto_renew_disabled"}

    window = float(cfg.get("renew_check_seconds", 600) if verify_seconds is None else verify_seconds)
    now = time.time()

    with _ensure_lock:
        if not force and _ensure_cache["valid"] and now - _ensure_cache["checked_at"] < window:
            return {"ok": True, "checked": False, "cached": True}

        verification = verify_session()
        if verification.get("valid"):
            _ensure_cache.update({"checked_at": now, "valid": True})
            return {"ok": True, "checked": True, "verification": verification}
        if verification.get("valid") is None:
            # 网络问题，不能当成过期，否则会把好好的凭证丢掉
            return {
                "ok": True,
                "checked": True,
                "skipped": "network",
                "verification": verification,
                "note": "网络异常，无法判定会话是否过期，跳过自动续期。",
            }

        _ensure_cache.update({"checked_at": now, "valid": False})
        result = _renew(force=force)

    if result.get("ok"):
        _ensure_cache.update({"checked_at": time.time(), "valid": True})
        return {
            "ok": True,
            "checked": True,
            "renewed": True,
            "verification": result.get("verification"),
            "domain_cookies": result.get("domain_cookies"),
            "warnings": result.get("warnings") or [],
        }

    raise http.NotLoggedIn(
        "登录态已过期，自动续期失败：" + str(result.get("message") or result.get("error") or ""),
        hint=result.get("hint")
        or "重新登录一次（steam_auth_login 或 steam_auth_import_cookie），之后自动续期即可正常工作。",
    )


def renew_status() -> dict[str, Any]:
    """只读：看一眼自动续期是否可用（不发起续期，最多校验一次会话）。"""
    data = state()
    jar = data.get("cookies") or {}
    token = data.get("refresh_token") or jar.get("steamRefresh_steam")
    cfg = config.load()["behavior"]
    out: dict[str, Any] = {
        "ok": True,
        "logged_in": bool(jar.get("steamLoginSecure")),
        "auto_renew": bool(cfg.get("auto_renew", True)),
        "check_window_seconds": cfg.get("renew_check_seconds", 600),
        "has_refresh_token": bool(token),
        "refresh_token_fingerprint": secrets.fingerprint(token),
        "refreshed_at": data.get("refreshed_at"),
        "last_check_valid": _ensure_cache.get("valid"),
        "domain_cookies": {host: sorted(cookies) for host, cookies in sorted((data.get("domain_cookies") or {}).items())},
    }
    if not out["has_refresh_token"]:
        out["hint"] = "没有 refresh token，无法自动续期。导入带 steamRefresh_steam 的 cookie，或用密码登录一次。"
    elif not out["auto_renew"]:
        out["hint"] = "behavior.auto_renew 已关闭；可手动调用 steam_auth_refresh。"
    return out


def logout() -> dict[str, Any]:
    removed = secrets.clear_credentials()
    _ensure_cache.update({"checked_at": 0.0, "valid": False})
    return {"ok": True, "removed": removed}
