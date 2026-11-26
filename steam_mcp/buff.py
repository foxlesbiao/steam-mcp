"""网易 BUFF（buff.163.com）集成：登录态 + 市场行情。

登录方式：走 BUFF 官方提供的「Steam 登录」（OpenID 2.0）——
BUFF 302 到 steamcommunity.com/openid，借用本机已登录的 Steam 会话完成授权，
回跳后 BUFF 下发自己的 session cookie。全程不碰网易账号密码。

安全约束（与主模块一致）：
- BUFF cookie 只进 DPAPI 加密凭证库（credentials.bin 的 "buff" 命名空间），不落明文、不进日志；
- 域名白名单只放行 buff.163.com（见 http.py），不做任何转发；
- 只读行情。购买/求购/上架等资金操作不在本模块实现，也不允许通过通用请求放行。
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import auth, http, secrets

BASE = "https://buff.163.com"
OPENID_START = f"{BASE}/account/login/steam"
OPENID_VERIFY_PREFIX = f"{BASE}/account/login/steam/verification"

# BUFF 的 openid 表单需要带一个它自己生成的 session 上下文，先走首页拿 cookie
_UA_PAGE = {"Referer": f"{BASE}/", "X-Requested-With": "XMLHttpRequest"}


# ---------------------------------------------------------------- 状态存储


def _buff_state() -> dict[str, Any]:
    return auth.state().get("buff") or {}


def _save_buff_state(buff: dict[str, Any]) -> None:
    data = auth.state()
    data["buff"] = buff
    secrets.save_credentials(data)


def is_logged_in() -> bool:
    return bool(_buff_state().get("cookies", {}).get("session"))


def cookies() -> dict[str, str]:
    return dict(_buff_state().get("cookies", {}))


def require_login() -> dict[str, str]:
    jar = cookies()
    if not jar.get("session"):
        raise http.SteamError(
            "未登录 BUFF。",
            code="buff_not_logged_in",
            hint="调用 steam_buff_login（走 Steam OpenID，无需网易账号密码）。",
        )
    return jar


# ---------------------------------------------------------------- 登录


def login_via_steam() -> dict[str, Any]:
    """用当前 Steam 会话完成 BUFF 的 Steam OpenID 登录。

    实测链路（与浏览器完全一致，顺序不能乱）：
    1. GET buff /account/login/steam → 302 到 steamcommunity openid loginform
    2. GET loginform → 302 到 /openid/login?<参数>（此跳下发 browserid）
    3. GET /openid/login → 200 批准页（下发 sessionidSecureOpenIDNonce，页内含 openidForm）
    4. POST /openid/login（**必须 multipart/form-data**，urlencoded 会被静默打回 200 错误页）→ 302 回跳 BUFF
    5. GET BUFF verification 地址 → BUFF 下发 session
    """
    auth.require_login()

    # 1) BUFF 侧起手
    step1 = http.request("GET", OPENID_START)
    jar = _collect(step1, {})
    loc = step1.headers.get("location") or ""
    if step1.status not in (301, 302, 303, 307) or "steamcommunity.com/openid" not in loc:
        raise http.Rejected(
            f"BUFF 未跳转到 Steam OpenID（HTTP {step1.status}）。",
            code="buff_login_failed",
        )

    # 2)+3) 跟随 steamcommunity 内部跳转直到拿到批准页（收集沿途 cookie）
    steam_jar = auth.cookies_for("steamcommunity.com")
    approval: http.Response | None = None
    page_url = loc
    for _hop in range(4):
        resp = http.request("GET", page_url, cookies=steam_jar)
        _collect(resp, steam_jar)
        if resp.status in (301, 302, 303, 307, 308):
            nxt = resp.headers.get("location") or ""
            page_url = nxt if nxt.startswith("https://") else "https://steamcommunity.com" + nxt
            continue
        approval = resp
        break
    if approval is None or approval.status != 200:
        raise http.Rejected("未能到达 Steam OpenID 批准页。", code="buff_login_failed")

    fields = _extract_openid_form(approval.text)
    if fields is None:
        raise http.SteamError(
            "Steam 没有认可当前登录会话（渲染的是账号密码登录页）。",
            code="steam_session_invalid",
            hint="先续期/重新登录 Steam（steam_auth_status 查看）。",
        )

    # 4) 批准（multipart 是硬要求）
    approved = http.request(
        "POST",
        "https://steamcommunity.com/openid/login",
        data=fields,
        multipart=True,
        cookies=steam_jar,
        headers={"Referer": page_url, "Origin": "https://steamcommunity.com"},
    )
    verify_url = approved.headers.get("location") or ""
    if not verify_url.startswith(OPENID_VERIFY_PREFIX):
        raise http.SteamError(
            f"Steam OpenID 批准失败（HTTP {approved.status}）。",
            code="buff_login_failed",
            hint="重试一次；仍失败则 Steam 会话可能已被风控，稍后再试。",
        )

    # 5) 回跳 BUFF，完成登录，收集 session cookie
    final = http.request("GET", verify_url, cookies=jar, headers={"Referer": "https://steamcommunity.com/"})
    jar = _collect(final, jar)

    if not jar.get("session"):
        raise http.Rejected(
            "BUFF 未下发 session cookie，登录未完成。",
            code="buff_login_failed",
            hint="若该 Steam 账号首次登录 BUFF，可能需要在浏览器里补一次手机号绑定。",
        )

    buff = {
        "cookies": jar,
        "method": "steam_openid",
        "obtained_at": int(time.time()),
    }
    _save_buff_state(buff)

    check = status(check_remote=True)
    out: dict[str, Any] = {
        "ok": True,
        "stage": "logged_in",
        "method": "steam_openid",
        "cookies_collected": sorted(jar),
        "session_fingerprint": secrets.fingerprint(jar.get("session")),
    }
    out["verification"] = check
    if check.get("nickname"):
        out["nickname"] = check["nickname"]
    return out


def _collect(resp: http.Response, jar: dict[str, str]) -> dict[str, str]:
    """从 Set-Cookie 里收集 BUFF 域名 cookie（httpx 的 raw.cookies 只保留最后一组，改用头解析）。"""
    for line in resp.headers.get_list("set-cookie"):
        name, _, rest = line.partition("=")
        value = rest.split(";")[0].strip()
        name = name.strip()
        if name and value:
            jar[name] = value
    return jar


def _extract_openid_form(html: str) -> dict[str, str] | None:
    """解析批准页的 openidForm（action/openid.mode/openidparams/nonce 四个 hidden input）。

    返回 None 表示页面不是批准页（通常是账号密码登录页，即 Steam 会话没被认可）。
    """
    m = re.search(r'<form[^>]*id="openidForm"[^>]*>(.*?)</form>', html, re.S | re.I)
    if not m:
        return None
    fields: dict[str, str] = {}
    for inp in re.findall(r"<input[^>]*>", m.group(1), re.I):
        name = re.search(r'name="([^"]*)"', inp)
        value = re.search(r'value="([^"]*)"', inp)
        if name:
            fields[name.group(1)] = (value.group(1) if value else "").replace("&amp;", "&")
    return fields


# ---------------------------------------------------------------- 状态 / 登出


def status(check_remote: bool = True) -> dict[str, Any]:
    buff = _buff_state()
    jar = buff.get("cookies", {})
    if not jar.get("session"):
        return {
            "ok": True,
            "logged_in": False,
            "hint": "调用 steam_buff_login 通过 Steam OpenID 登录 BUFF（无需网易账号密码）。",
        }
    out: dict[str, Any] = {
        "ok": True,
        "logged_in": True,
        "method": buff.get("method"),
        "obtained_at": buff.get("obtained_at"),
        "session_fingerprint": secrets.fingerprint(jar.get("session")),
    }
    if check_remote:
        out["verification"] = verify()
    return out


def verify() -> dict[str, Any]:
    """用一个只有登录态才能访问的接口探测 session 是否仍有效，并顺手拿昵称。"""
    jar = cookies()
    if not jar.get("session"):
        return {"valid": False, "reason": "no_cookie"}
    try:
        resp = http.request(
            "GET",
            f"{BASE}/account/api/user/info",
            cookies=jar,
            headers={"Referer": f"{BASE}/", "X-Requested-With": "XMLHttpRequest"},
        )
        if resp.status == 200 and resp.text.lstrip().startswith("{"):
            body = resp.json()
            if body.get("code") == "OK":
                data = body.get("data") or {}
                return {"valid": True, "nickname": data.get("nickname"), "steamid": data.get("steamid")}
    except http.SteamError:
        pass
    # 回落：sell_order 接口未登录时会整页 302 到登录
    try:
        resp = http.request(
            "GET",
            f"{BASE}/api/market/goods/sell_order",
            params={"game": "csgo", "goods_id": 42592, "page_num": 1},
            cookies=jar,
            headers={"Referer": f"{BASE}/market/goods?goods_id=42592", "X-Requested-With": "XMLHttpRequest"},
        )
        if resp.status in (301, 302, 303) and "login" in (resp.headers.get("location") or ""):
            return {"valid": False, "reason": "expired"}
        if resp.status == 200:
            body = resp.json()
            return {"valid": body.get("code") == "OK", "reason": None if body.get("code") == "OK" else body.get("code")}
    except http.SteamError as exc:
        return {"valid": None, "reason": "network", "error": exc.as_dict()}
    return {"valid": False, "reason": "unexpected"}


def logout() -> dict[str, Any]:
    data = auth.state()
    had = "buff" in data
    data.pop("buff", None)
    secrets.save_credentials(data)
    return {"ok": True, "removed": had}


# ---------------------------------------------------------------- 行情查询


def search_goods(keyword: str, game: str = "csgo", limit: int = 10) -> dict[str, Any]:
    """按名字搜商品，拿 goods_id（BUFF 各接口都用 goods_id 而不是市场名）。

    注意：/api/market/search/suggest 要求额外的身份验证（Steam Logged In Needs
    Verification），不可用；这里用市场商品列表接口，登录态即可。
    """
    jar = require_login()
    resp = http.request(
        "GET",
        f"{BASE}/api/market/goods",
        params={"game": game, "page_num": 1, "search": keyword, "page_size": max(1, min(limit, 20))},
        cookies=jar,
        headers={"Referer": f"{BASE}/market/{game}", "X-Requested-With": "XMLHttpRequest"},
    )
    body = resp.json()
    if body.get("code") != "OK":
        raise http.Rejected(f"BUFF 搜索失败：{body.get('code')} {body.get('msg') or body.get('error') or ''}")
    items = []
    for it in ((body.get("data") or {}).get("items") or []):
        items.append(
            {
                "goods_id": it.get("id"),
                "name": it.get("name"),
                "market_hash_name": it.get("market_hash_name"),
                "sell_min_price": it.get("sell_min_price"),
                "sell_num": it.get("sell_num"),
            }
        )
    return {"ok": True, "keyword": keyword, "count": len(items), "items": items}


def market_price(goods_id: int, game: str = "csgo", page_num: int = 1) -> dict[str, Any]:
    """某件商品的在售列表（含每单的 float / 图案模板）+ 求购列表。"""
    jar = require_login()
    headers = {
        "Referer": f"{BASE}/market/goods?goods_id={goods_id}",
        "X-Requested-With": "XMLHttpRequest",
    }

    sell = http.request(
        "GET",
        f"{BASE}/api/market/goods/sell_order",
        params={"game": game, "goods_id": int(goods_id), "page_num": max(1, int(page_num)), "sort_by": "price"},
        cookies=jar,
        headers=headers,
    )
    if sell.status in (301, 302, 303):
        raise http.SteamError("BUFF 会话已过期，请重新 steam_buff_login。", code="buff_not_logged_in")
    body = sell.json()
    if body.get("code") != "OK":
        raise http.Rejected(f"BUFF 在售查询失败：{body.get('code')} {body.get('msg') or ''}")
    data = body.get("data") or {}

    items = []
    for it in (data.get("items") or [])[:20]:
        asset = it.get("asset_info") or {}
        info = asset.get("info") or {}
        items.append(
            {
                "price": it.get("price"),
                "float": asset.get("paintwear"),
                "paint_seed": info.get("paintseed"),
                "paint_index": info.get("paintindex"),
                "name_tag": (info.get("fraudwarnings") or "").replace("名称标签: ", "") or None,
                "seller_id": it.get("user_id"),
                "has_bargain": bool(it.get("allow_bargain")),
            }
        )
    goods_info = (data.get("goods_infos") or {}).get(str(goods_id)) or {}

    buy_orders: list[dict[str, Any]] = []
    buy_err = None
    try:
        buy = http.request(
            "GET",
            f"{BASE}/api/market/goods/buy_order",
            params={"game": game, "goods_id": int(goods_id), "page_num": 1},
            cookies=jar,
            headers=headers,
        )
        bbody = buy.json()
        if bbody.get("code") == "OK":
            for it in ((bbody.get("data") or {}).get("items") or [])[:10]:
                buy_orders.append({"price": it.get("price"), "num": it.get("num") or it.get("real_num")})
        else:
            buy_err = bbody.get("code")
    except http.SteamError as exc:
        buy_err = exc.code

    return {
        "ok": True,
        "goods_id": int(goods_id),
        "name": goods_info.get("name"),
        "market_hash_name": goods_info.get("market_hash_name"),
        "sell_count": data.get("total_count"),
        "sell_min_price": goods_info.get("sell_min_price"),
        "sell_listings": items,
        "buy_orders": buy_orders,
        "buy_order_error": buy_err,
        "note": "price 为买家实付（BUFF 手续费由卖家承担）；float 越低越新，紫罗兰只看 float、不看 paint_seed。",
    }
