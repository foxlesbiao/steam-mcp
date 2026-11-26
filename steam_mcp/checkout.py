"""结账 / 购买游戏（checkout.steampowered.com）。

分阶段，钱只在最后一步动：

    cart     读/加/清购物车（store 域）
    preview  只读：购物车明细 + 预估总价 + 钱包余额，不产生任何交易
    init     POST /checkout/inittransaction/，创建交易拿 transid（不扣款）
    price    GET  /checkout/getfinalprice/，核对最终金额（钱包扣多少 / 外部付多少）
    finalize POST /checkout/finalizetransaction/，**真正完成购买（扣款）**
    cancel   POST /checkout/canceltransaction/
    status   GET  /checkout/transactionstatus/

硬门槛（不是建议）：
1. finalize 必须 confirm=true，且 behavior.allow_sensitive=true；
2. finalize 必须带 expected_total（来自 price 阶段），并会重新核对一次当前价格，
   价格对不上就拒绝，防止误买或价格变动；
3. 所有请求走 http.check_url 白名单，只允许官方域名；
4. 凭证不进日志，状态输出只带指纹；
5. 外部支付（支付宝等）只返回支付链接，绝不代付、绝不自动 finalize。

前置条件：checkout.steampowered.com 需要登录时 transfer_info 单独下发的 cookie。
密码登录会自动收集；cookie 导入方式需调用 steam_checkout_session(action="import"/"refresh")。
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from . import auth, config, http, secrets

CHECKOUT = "https://checkout.steampowered.com"
STORE = "https://store.steampowered.com"
CHECKOUT_HOST = "checkout.steampowered.com"
# 新版购物车的真实写入接口（protobuf 服务），跑在 api 域：
#   POST https://api.steampowered.com/IAccountCartService/<Method>/v1/?access_token=<购物车页 SSR 里的 strWebAPIToken>
#   表单字段 input_protobuf_encoded=<base64 protobuf>
# 旧的 /cart/removelineitem、/cart/emptycart、/api/removefromcart 全部已失效（实测只回购物车页 HTML）。
ACCOUNT_CART_API = "https://api.steampowered.com/IAccountCartService"

# inittransaction 的完整表单模板（Steam 前端会提交全部字段，缺了会被拒）
_INIT_BASE: dict[str, Any] = {
    "gidShoppingCart": -1,
    "gidReplayOfTransID": -1,
    "bUseAccountCart": 1,
    "abortPendingTransactions": 0,
    "bHasCardInfo": 0,
    "CardNumber": "",
    "CardExpirationYear": "",
    "CardExpirationMonth": "",
    "FirstName": "",
    "LastName": "",
    "Address": "",
    "AddressTwo": "",
    "City": "",
    "State": "",
    "PostalCode": "",
    "Phone": "",
    "ShippingFirstName": "",
    "ShippingLastName": "",
    "ShippingAddress": "",
    "ShippingAddressTwo": "",
    "ShippingCity": "",
    "ShippingState": "",
    "ShippingPostalCode": "",
    "ShippingPhone": "",
    "bIsGift": 0,
    "GifteeAccountID": 0,
    "GifteeEmail": "",
    "GifteeName": "",
    "GiftMessage": "",
    "Sentiment": "",
    "Signature": "",
    "ScheduledSendOnDate": 0,
    "BankAccount": "",
    "BankCode": "",
    "BankIBAN": "",
    "BankBIC": "",
    "TPBankID": "",
    "BankAccountID": "",
    "bSaveBillingAddress": 1,
    "gidPaymentID": "",
    "bPreAuthOnly": 0,
}


# ---------------------------------------------------------------- 会话


def checkout_session(action: str = "status", cookie: str = "") -> dict[str, Any]:
    """管理 checkout.steampowered.com 的独立登录态。

    status  查看是否已有 checkout 专属 cookie，并试探访问结账页确认是否登录态有效
    import  粘贴浏览器里 checkout.steampowered.com 的 cookie（Cookie 头 / DevTools 表格）
    refresh 用当前 refresh_token 重跑 finalizelogin transfer，补齐各域名 cookie
    """
    action = (action or "status").lower()
    data = auth.state()
    domains: dict[str, dict[str, str]] = data.get("domain_cookies") or {}
    jar = dict(domains.get(CHECKOUT_HOST) or {})

    if action == "import":
        if not cookie:
            raise http.SteamError("cookie 为空", code="bad_input")
        pairs = auth.parse_cookie_blob(cookie)
        keep = ("sessionid", "steamLoginSecure", "steamRefresh_steam", "steamCountry", "browserid")
        imported = {k: v for k, v in pairs.items() if k in keep}
        if not imported:
            raise http.SteamError(
                "没找到 checkout 域需要的 cookie（sessionid / steamLoginSecure）。",
                code="bad_input",
                hint="F12 → Application → Cookies → https://checkout.steampowered.com → 复制整行。",
            )
        jar.update(imported)
        data.setdefault("domain_cookies", {})[CHECKOUT_HOST] = jar
        secrets.save_credentials(data)
        return {
            "ok": True,
            "action": "import",
            "imported": sorted(imported),
            "cookie_fingerprint": {k: secrets.fingerprint(v) for k, v in imported.items()},
        }

    if action == "refresh":
        rt = data.get("refresh_token") or (data.get("cookies") or {}).get("steamRefresh_steam")
        if not rt:
            raise http.SteamError(
                "凭证库里没有 refresh_token，无法补发 checkout cookie。",
                code="checkout_session_missing",
                hint="重新密码登录一次，或改用 action=\"import\" 从浏览器粘贴 checkout 域 cookie。",
            )
        transfer = auth._finalize_web_session(rt, data.get("steamid"))
        by_host = transfer.get("by_host") or {}
        if by_host:
            data["domain_cookies"] = {**domains, **{k: v for k, v in by_host.items() if v}}
            secrets.save_credentials(data)
        return {
            "ok": bool((by_host.get(CHECKOUT_HOST) or {}).get("sessionid")),
            "action": "refresh",
            "hosts": sorted(by_host),
            "warnings": transfer.get("warnings") or [],
            "hint": None if (by_host.get(CHECKOUT_HOST) or {}).get("sessionid") else
                    "finalizelogin 未能下发 checkout cookie（refresh_token 可能已被消费）。改用 action=\"import\"。",
        }

    # status
    probe = _probe_checkout()
    return {
        "ok": True,
        "action": "status",
        "has_checkout_cookie": bool(jar.get("sessionid")),
        "cookie_names": sorted(jar),
        "session_fingerprint": secrets.fingerprint(jar.get("sessionid")),
        "checkout_page": probe,
        "note": "checkout 域不需要单独的 cookie：store 的 steamLoginSecure + 同一个 sessionid 即可下单"
                "（已实测 init 成功）。domain_cookies 里 checkout 为空不代表不可用。",
        "hint": None if probe.get("logged_in") else
                "结账域名未登录：先 steam_auth_status 确认整体会话，仍不行再用 action=\"import\" 粘贴 checkout 域 cookie。",
    }


def _probe_checkout() -> dict[str, Any]:
    """判断 checkout 域是否可用。

    **旧实现的坑**：靠 `data-userinfo` 里的 `logged_in` 判断，而 Steam 新版结账页
    已经不再输出这个字段，于是正常会话被误判成未登录（实测：probe 说 logged_in:false，
    紧接着 init 却成功拿到了 transid）。现在改成：
    - 302 到登录页 / 回登录表单 → 未登录；
    - 302 到结账首页或购物车 → 正常（只是当前没有待结账交易）；
    - 200 且不是登录页 → 视为已登录。
    真正权威的判据是 init 能否拿到 transid。
    """
    jar = auth.cookies_for(CHECKOUT_HOST)
    try:
        resp = http.request(
            "GET",
            f"{CHECKOUT}/checkout/?purchasetype=self&checkout=1",
            cookies=jar,
            headers={"Referer": f"{STORE}/cart/"},
        )
    except http.SteamError as exc:
        return {"logged_in": False, "error": exc.as_dict()}
    location = resp.headers.get("location") or ""
    if resp.status in (301, 302, 303):
        if "login" in location:
            return {"logged_in": False, "status": resp.status, "redirect": location[:120]}
        return {
            "logged_in": True,
            "status": resp.status,
            "redirect": location[:120],
            "note": "重定向到结账首页/购物车属正常（当前没有待结账交易）。",
        }
    if resp.status != 200:
        return {"logged_in": False, "status": resp.status}
    if resp.looks_like_login_page():
        return {"logged_in": False, "status": 200, "reason": "login_page"}
    title = re.search(r"<title>(.*?)</title>", resp.text, re.S)
    return {
        "logged_in": True,
        "status": 200,
        "title": (title.group(1).strip()[:40] if title else None),
        "note": "最终以 init 能否拿到 transid 为准。",
    }


def _require_checkout() -> dict[str, str]:
    jar = auth.cookies_for(CHECKOUT_HOST)
    if not jar.get("sessionid"):
        raise http.SteamError(
            "缺少 checkout.steampowered.com 的专属 sessionid，结账不可用。",
            code="checkout_session_missing",
            hint="先 steam_checkout_session(action=\"import\") 粘贴浏览器 cookie，或重新密码登录。",
        )
    return jar


# ---------------------------------------------------------------- 购物车


# ---------------------------------------------------------------- 新版 AccountCart 服务
#
# 购物车页是 React SSR，写操作走 protobuf 服务而不是表单接口：
#   POST https://api.steampowered.com/IAccountCartService/RemoveItemFromCart/v1/
#        ?access_token=<购物车页 SSR 里的 strWebAPIToken>
#   form: input_protobuf_encoded=<base64>
# RemoveItemFromCart 请求体字段：line_item_id = field1 (uint64)，user_country = field2 (string)

_TOKEN_CACHE: dict[str, Any] = {"token": "", "fetched_at": 0.0}
_TOKEN_TTL = 600


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _webapi_token(force: bool = False) -> str:
    """取购物车页 SSR 数据里的 web API token（strWebAPIToken）。

    这是购物车服务调用必需的 access_token，与登录 cookie 分开、短期有效，所以按需重取。
    """
    now = time.time()
    if not force and _TOKEN_CACHE["token"] and now - _TOKEN_CACHE["fetched_at"] < _TOKEN_TTL:
        return str(_TOKEN_CACHE["token"])
    resp = http.request("GET", f"{STORE}/cart/", cookies=auth.cookies_for("store.steampowered.com"))
    match = re.search(r'strWebAPIToken\\+"\s*:\s*\\+"([^"\\]+)', resp.text)
    if not match:
        raise http.SteamError(
            "购物车页里没找到 web API token，无法调用购物车服务。",
            code="cart_token_missing",
            hint="多半是登录态失效（页面被重定向到登录页）。先 steam_auth_status 确认会话。",
        )
    _TOKEN_CACHE.update({"token": match.group(1), "fetched_at": now})
    return match.group(1)


def _account_cart(method: str, body: bytes = b"", version: int = 1) -> dict[str, Any]:
    """调用 AccountCart 服务（protobuf 入参）。是否生效以调用后重读购物车为准。"""
    last: Any = None
    for attempt in (1, 2):
        token = _webapi_token(force=attempt == 2)
        resp = http.request(
            "POST",
            f"{ACCOUNT_CART_API}/{method}/v{version}/",
            params={"access_token": token},
            data={"input_protobuf_encoded": base64.b64encode(body).decode()},
            cookies=auth.cookies_for("store.steampowered.com"),
            headers={"Origin": STORE, "Referer": f"{STORE}/cart/"},
        )
        last = resp
        if resp.status == 200:
            return {"ok": True, "status": resp.status, "method": method}
        if resp.status in (401, 403) and attempt == 1:
            continue  # token 过期，换一个再试一次
        break
    return {
        "ok": False,
        "status": getattr(last, "status", None),
        "method": method,
        "hint": "购物车服务调用失败。常见原因：api.steampowered.com 不可达、token 与 Origin 不匹配。",
    }


def remove_line_item(line_item_id: str) -> dict[str, Any]:
    """按 line_item_id 移除购物车行（新版 protobuf 服务）。"""
    if not line_item_id:
        raise http.SteamError("remove 需要 line_item_id（view 会返回）", code="bad_input")
    try:
        gid = int(str(line_item_id))
    except (TypeError, ValueError):
        raise http.SteamError("line_item_id 必须是数字", code="bad_input")
    auth.require_login()
    call = _account_cart("RemoveItemFromCart", b"\x08" + _pb_varint(gid))
    remaining = [str(item.get("line_item_id")) for item in _cart_view().get("line_items") or []]
    still_there = str(line_item_id) in remaining
    return {
        "ok": not still_there,
        "action": "remove",
        "line_item_id": str(line_item_id),
        "service": call,
        "remaining": remaining,
        "hint": "服务返回成功但条目还在，稍后重试或手动移除。" if still_there else None,
    }


def empty_cart() -> dict[str, Any]:
    """清空购物车：先 DeleteCart，仍有残留就逐行移除。"""
    auth.require_login()
    call = _account_cart("DeleteCart", b"")
    remaining = [str(item.get("line_item_id")) for item in _cart_view().get("line_items") or []]
    removed_by_lines: list[str] = []
    if remaining:
        for lid in remaining:
            if remove_line_item(lid).get("ok"):
                removed_by_lines.append(lid)
        remaining = [str(item.get("line_item_id")) for item in _cart_view().get("line_items") or []]
    return {
        "ok": not remaining,
        "action": "clear",
        "service": call,
        "removed_by_lines": removed_by_lines,
        "remaining": remaining,
    }


def cart(action: str = "view", appid: int | None = None, subid: int | None = None,
         line_item_id: str = "") -> dict[str, Any]:
    """购物车：view（读明细）/ add（按 appid 或 subid 加购）/ remove / clear。"""
    auth.require_login()
    action = (action or "view").lower()
    if action == "view":
        return _cart_view()

    if action == "add":
        auth.warm_session()
        jar = auth.cookies_for("store.steampowered.com")
        sid = auth.sessionid()
        if not subid and appid:
            subid = _resolve_subid(int(appid))
        if not subid:
            raise http.SteamError("add 需要 appid 或 subid", code="bad_input")
        def _add_once(sid_value: str) -> Any:
            return http.request(
                "POST",
                f"{STORE}/cart/addtocart?redirect=1",
                data={
                    "snr": "1_5_9__403",
                    "originating_snr": "1_direct-navigation__",
                    "action": "add_to_cart",
                    "sessionid": sid_value,
                    "subid": int(subid),
                },
                cookies=auth.cookies_for("store.steampowered.com"),
                headers={"Referer": f"{STORE}/cart/"},
            )

        def _confirm() -> list[dict[str, Any]]:
            # 购物车页是 SSR，加完立刻读经常读不到，短轮询；且只看目标 subid
            for attempt in range(4):
                items = _cart_view().get("line_items") or []
                if any(str(i.get("packageid")) == str(subid) for i in items):
                    return items
                if attempt < 3:
                    time.sleep(0.8)
            return _cart_view().get("line_items") or []

        resp = _add_once(sid)
        items = _confirm()
        if not items:
            # sessionid 会随会话轮换，缓存里的旧值会让加车静默失败（302 但购物车没变）：
            # 强制重取一次 sessionid 再试。
            auth.warm_session(force=True)
            resp = _add_once(auth.sessionid())
            items = _confirm()
        return {
            "ok": resp.status in (301, 302, 303) or resp.status == 200,
            "action": "add",
            "subid": int(subid),
            "status": resp.status,
            "verified": any(str(i.get("packageid")) == str(subid) for i in items),
            "cart_line_items": items,
            "note": "加购不扣款；随后用 steam_checkout(action=\"preview\") 核对金额。",
        }

    if action == "remove":
        return remove_line_item(line_item_id)

    if action == "clear":
        return empty_cart()

    raise http.SteamError("action 只能是 view / add / remove / clear", code="bad_input")


def _resolve_subid(appid: int) -> int | None:
    """从商店页的 add_to_cart 表单里抓 subid（比 /api/addtocart 稳定）。"""
    try:
        resp = http.request("GET", f"{STORE}/app/{appid}/", allow_redirects=True)
        m = re.search(r'<form name="add_to_cart_(\d+)"', resp.text)
        if m:
            return int(m.group(1))
    except http.SteamError:
        pass
    return None


def _parse_cart_html(text: str) -> tuple[list[dict[str, Any]], Any]:
    """从购物车页 HTML 提取行项目。纯解析、不联网，便于离线测试。"""
    items: list[dict[str, Any]] = []
    cart_id = None
    m = re.search(r'<script[^>]*>(\{"route":"/cart/".*?)</script>', text, re.S)
    if m:
        try:
            ssr = json.loads(m.group(1))
            for chunk in ssr.get("loaderData") or []:
                obj = json.loads(chunk) if isinstance(chunk, str) else chunk
                if not isinstance(obj, dict):
                    continue
                if isinstance(obj.get("cartID"), dict):
                    cart_id = obj["cartID"]
                items.extend(_find_line_items(obj))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if not items:
        # 回落：新版购物车 SSR 是双层转义，JSON 路径常解析不到，直接从原文抓。
        # 页面里的 "购物车是空的" 只是空状态组件文案，有货时同样存在，不能拿它判空。
        for mm in re.finditer(
            r'line_item_id\\+"\s*:\s*\\+"(\d+)\\+"\s*,\s*\\+"item_id\\+"\s*:\s*\{\\+"packageid\\+"\s*:\s*(\d+)',
            text,
        ):
            items.append({"line_item_id": mm.group(1), "packageid": int(mm.group(2))})
        if not items:  # 兜底：只拿到 id 也算有货
            for mm in re.finditer(r'line_item_id\\+"\s*:\s*\\+"(\d+)', text):
                items.append({"line_item_id": mm.group(1)})

    # 多个 loaderData chunk 会重复给出同一行项目，按 id 去重并合并字段
    deduped: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("line_item_id") or item.get("lineItemId") or item.get("packageid") or "")
        if key and key in seen:
            for k, v in item.items():
                seen[key].setdefault(k, v)
            continue
        record = dict(item)
        deduped.append(record)
        if key:
            seen[key] = record
    return deduped, cart_id


def _cart_view() -> dict[str, Any]:
    """解析购物车页 SSR 的 loaderData，尽力提取行项目与价格。"""
    jar = auth.cookies_for("store.steampowered.com")
    resp = http.request("GET", f"{STORE}/cart/", cookies=jar)
    if resp.status in (301, 302, 303):
        raise http.NotLoggedIn("购物车页重定向到登录页，cookie 可能已失效。")
    deduped, cart_id = _parse_cart_html(resp.text)

    return {
        "ok": True,
        "action": "view",
        "empty": not deduped,
        "cart_id": cart_id,
        "line_items": deduped,
        "source": "cart SSR loaderData",
        "warnings": ["购物车是 React SSR，字段随 Steam 前端变动；金额以 steam_checkout(action=\"price\") 返回的最终价为准。"],
    }


def _find_line_items(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """递归找带 line_item_id / packageid 的字典，兼容双层转义的 SSR 结构。"""
    out: list[dict[str, Any]] = []
    if depth > 8:
        return out
    if isinstance(node, str):
        s = node.replace('\\"', '"')
        if s.lstrip().startswith(("{", "[")):
            try:
                return _find_line_items(json.loads(s), depth + 1)
            except json.JSONDecodeError:
                return out
        return out
    if isinstance(node, list):
        for v in node:
            out.extend(_find_line_items(v, depth + 1))
        return out
    if not isinstance(node, dict):
        return out
    keys = set(node)
    if keys & {"line_item_id", "lineItemId", "packageid", "subid"}:
        item: dict[str, Any] = {}
        for k in ("line_item_id", "lineItemId", "name", "packageid", "subid", "appid",
                  "formatted_final_price", "final_price_in_cents", "price", "quantity"):
            if k in node and isinstance(node[k], (str, int, float)):
                item[k] = node[k]
        if item:
            out.append(item)
    for k, v in node.items():
        if k in ("line_items", "lineItems", "items", "cart", "data", "response"):
            out.extend(_find_line_items(v, depth + 1))
    return out


# ---------------------------------------------------------------- 结账主流程


def checkout(
    action: str = "preview",
    *,
    transid: str = "",
    expected_total: int | None = None,
    confirm: bool = False,
    payment_method: str = "steamaccount",
    country: str = "",
    count: int = 1,
) -> dict[str, Any]:
    """结账统一入口。默认 preview（只读）；finalize 才真正扣款且必须 confirm=true。"""
    auth.require_login()
    action = (action or "preview").lower()
    country = country or config.load()["behavior"].get("country", "CN")

    if action == "preview":
        return _preview()
    if action == "init":
        return _init_transaction(payment_method, country)
    if action == "price":
        return final_price(transid)
    if action == "finalize":
        return finalize(transid, expected_total=expected_total, confirm=confirm)
    if action == "cancel":
        return cancel_transaction(transid)
    if action == "status":
        return transaction_status(transid, count)
    if action == "pay_link":
        return pay_link(transid)
    raise http.SteamError(
        "action 只能是 preview / init / price / finalize / cancel / status / pay_link",
        code="bad_input",
    )


def _preview() -> dict[str, Any]:
    """只读预览：购物车 + 钱包余额 + checkout 会话可用性。不产生任何交易。"""
    cart_info = _cart_view()
    wallet = _wallet_balance()
    session = _probe_checkout()
    return {
        "ok": True,
        "action": "preview",
        "readonly": True,
        "cart": cart_info,
        "wallet": wallet,
        "checkout_session": session,
        "next": "确认购物车内容后，调用 steam_checkout(action=\"init\") 创建交易（不扣款），"
                "再用 price 核对金额，最后才用 finalize(confirm=true, expected_total=...) 完成购买。",
    }


def _parse_money_cents(text: str) -> int | None:
    """把 "Rp 1 857.17" / "¥ 8.70" 这类展示金额换算成最小单位（分）。"""
    cleaned = re.sub(r"[^0-9.,]", "", text or "")
    if not cleaned:
        return None
    tail = re.search(r"[.,](\d{1,2})$", cleaned)
    if tail:
        whole = re.sub(r"[.,]", "", cleaned[: tail.start()]) or "0"
        return int(whole) * 100 + int(tail.group(1).ljust(2, "0"))
    return int(re.sub(r"[^0-9]", "", cleaned)) * 100


def _wallet_balance() -> dict[str, Any]:
    """读取 Steam 钱包余额（账户页），返回展示串 + 分值。

    下单前必须先看这个：`total` 与余额同币种（都来自账号所在区域），可直接比较。
    """
    jar = auth.cookies_for("store.steampowered.com")
    try:
        resp = http.request("GET", f"{STORE}/account/", cookies=jar, allow_redirects=True)
    except http.SteamError as exc:
        return {"found": False, "error": exc.as_dict()}
    m = re.search(r'wallet_balance"[^>]*>([^<]{1,40})</a>', resp.text)
    text = (m.group(1).strip() if m else "")
    if not text:
        return {
            "found": False,
            "hint": "账户页没读到钱包余额（可能登录态失效或页面结构变了）；可用 steam_checkout(action=\"price\") 返回的余额复核。",
        }
    return {"found": True, "formatted": text, "cents": _parse_money_cents(text)}


def _headers() -> dict[str, str]:
    return {
        "Origin": CHECKOUT,
        "Referer": f"{CHECKOUT}/checkout/?accountcart=1",
        "X-Requested-With": "XMLHttpRequest",
    }


def _init_transaction(payment_method: str, country: str) -> dict[str, Any]:
    """创建交易，返回 transid。此步不扣款。"""
    jar = _require_checkout()
    data = dict(_INIT_BASE)
    data.update({
        "PaymentMethod": payment_method,
        "Country": country,
        "ShippingCountry": country,
        # 钱包支付时把余额用满；外部支付由 pay_link 单独处理
        "bUseRemainingSteamAccount": 1 if payment_method == "steamaccount" else 0,
        "sessionid": jar.get("sessionid") or auth.sessionid(),
    })
    resp = http.request("POST", f"{CHECKOUT}/checkout/inittransaction/", data=data,
                        cookies=jar, headers=_headers())
    body = resp.json() if resp.text.strip().startswith("{") else {"raw": resp.text[:200]}
    if body.get("success") != 1 or not body.get("transid"):
        return {
            "ok": False, "action": "init", "status": resp.status, "response": body,
            "hint": "success!=1 通常是钱包币种/地区不符、有待处理订单、购物车为空或登录态失效。"
                    "先用 steam_checkout_session 复核 checkout 域登录态。",
        }
    tid = str(body["transid"])
    return {
        "ok": True,
        "action": "init",
        "transid": tid,
        "payment_method": payment_method,
        "charged": False,
        "next": f"调用 steam_checkout(action=\"price\", transid=\"{tid}\") 核对最终金额，"
                f"确认无误后再 finalize(confirm=true, expected_total=<price 返回的 total>)。",
    }


# getfinalprice 是**一次性**接口：同一个 transid 第二次调用会回 success!=1（实测 resultdetail=7），
# 所以 finalize 里「再核一次价」不能重新请求，只能用 price 阶段缓存下来的结果比对。
_PRICE_CACHE: dict[str, dict[str, Any]] = {}


def final_price(transid: str, refresh: bool = True) -> dict[str, Any]:
    """查询交易的最终金额。total 单位为分。

    Steam 的 getfinalprice 对一个 transid 只成功一次，因此结果会缓存在进程内，
    finalize 复核时用 `refresh=False` 直接取缓存，避免第二次请求必然失败。
    """
    if not transid:
        raise http.SteamError("price 需要 transid", code="bad_input")
    if not refresh and transid in _PRICE_CACHE:
        cached = dict(_PRICE_CACHE[transid])
        cached["cached"] = True
        return cached
    jar = _require_checkout()
    resp = http.request(
        "GET",
        f"{CHECKOUT}/checkout/getfinalprice/",
        params={"count": 1, "transid": transid, "purchasetype": "self",
                "microtxnid": -1, "cart": -1, "gidReplayOfTransID": -1},
        cookies=jar,
        headers=_headers(),
    )
    body = resp.json() if resp.text.strip().startswith("{") else {"raw": resp.text[:200]}
    if body.get("success") != 1:
        return {"ok": False, "action": "price", "transid": transid, "response": body,
                "hint": "getfinalprice 对同一 transid 只成功一次；这条多半是已经核过价或交易已过期/取消。"
                        "需要重新核价就重新 init 一单。"}
    total = body.get("total")
    wallet = _wallet_balance()
    out: dict[str, Any] = {
        "ok": True,
        "action": "price",
        "transid": transid,
        "total": total,
        # 注意：这是本单「由钱包抵扣的金额」，不是余额余额
        "wallet_charge": body.get("formattedSteamAccountTotal"),
        "external_charge": body.get("formattedProviderTotal"),
    }
    if wallet.get("found"):
        out["wallet_balance"] = wallet["formatted"]
        if isinstance(total, int) and wallet.get("cents") is not None:
            out["wallet_sufficient"] = wallet["cents"] >= total
            if wallet["cents"] < total:
                out["shortfall_cents"] = total - wallet["cents"]
                out["hint"] = (
                    f"钱包余额不足：余额 {wallet['formatted']}，本单 {total / 100:.2f}（分制）。"
                    "finalize 会被拒绝，先充值或换更便宜的商品；不要反复 init。"
                )
    if "hint" not in out:
        out["hint"] = (
            "total 为最终金额（分）。external_charge 非空表示钱包不够、需外部支付，"
            "此时用 pay_link 拿链接自行付款，不要 finalize。"
        )
    _PRICE_CACHE[transid] = dict(out)
    return out


def finalize(transid: str, expected_total: int | None, confirm: bool) -> dict[str, Any]:
    """真正完成购买（扣款）。三重校验：confirm、allow_sensitive、expected_total。"""
    if not transid:
        raise http.SteamError("finalize 需要 transid", code="bad_input")
    if not confirm:
        raise http.SteamError(
            "finalize 会真实扣款，已拦截。",
            code="confirmation_required",
            hint="确认无误后再调用并传 confirm=true，同时带上 price 阶段返回的 expected_total。",
        )
    if not config.load()["behavior"].get("allow_sensitive"):
        raise http.SteamError(
            "behavior.allow_sensitive 未开启，禁止执行扣款操作。",
            code="confirmation_required",
            hint="先 steam_config_set(allow_sensitive=true)，再带 confirm=true 调用。",
        )
    if expected_total is None:
        raise http.SteamError(
            "finalize 需要 expected_total（price 阶段返回的 total），防止价格变动误买。",
            code="bad_input",
        )

    # 下单前复核价格：用缓存（getfinalprice 一次性，重新请求必失败）；没有缓存就现查一次
    price = final_price(transid, refresh=transid not in _PRICE_CACHE)
    if not price.get("ok"):
        return {"ok": False, "action": "finalize", "blocked": "price_check_failed", "price": price}
    current = price.get("total")
    if current is None or int(current) != int(expected_total):
        return {
            "ok": False, "action": "finalize", "blocked": "price_changed",
            "expected_total": expected_total, "current_total": current,
            "hint": "最终价与确认价不一致，已拒绝扣款。重新跑 price 确认金额。",
        }
    # 余额护栏放在最前面：Steam 的 formattedProviderTotal 在「钱包全额抵扣」模式下会返回
    # "Rp 0" 这种非空字符串，光判断它是否为空会把所有单子误判成需要外部支付。
    if price.get("wallet_sufficient") is False:
        return {
            "ok": False, "action": "finalize", "blocked": "insufficient_wallet",
            "wallet_balance": price.get("wallet_balance"),
            "total": current,
            "shortfall_cents": price.get("shortfall_cents"),
            "hint": "钱包余额不足，已拦下扣款（没有发出任何支付请求）。先充值，或取消交易换更便宜的商品。",
        }
    external_cents = _parse_money_cents(price.get("external_charge") or "") or 0
    if external_cents > 0:
        return {
            "ok": False, "action": "finalize", "blocked": "external_payment_required",
            "external_charge": price["external_charge"],
            "hint": "钱包余额不足，需要外部支付。用 pay_link 拿链接自行付款，MCP 不代付。",
        }

    jar = _require_checkout()
    browser_info = json.dumps({
        "language": config.load()["behavior"].get("language", "schinese"),
        "javaEnabled": "false", "colorDepth": 24,
        "screenHeight": 1080, "screenWidth": 1920,
    })
    resp = http.request(
        "POST",
        f"{CHECKOUT}/checkout/finalizetransaction/",
        data={"transid": transid, "CardCVV2": "", "browserInfo": browser_info},
        cookies=jar,
        headers=_headers(),
    )
    body = resp.json() if resp.text.strip().startswith("{") else {"raw": resp.text[:200]}
    success = body.get("success") == 22 and body.get("purchaseresultdetail") == 0
    if success:
        _PRICE_CACHE.pop(transid, None)
    return {
        "ok": bool(success),
        "action": "finalize",
        "transid": transid,
        "charged": bool(success),
        "total": current,
        "wallet_charge": price.get("wallet_charge"),
        "response": body,
        "next": "用 steam_checkout(action=\"status\") 轮询交易状态确认到账。" if success else
                "未完成扣款，请用 status 查询状态，或 cancel 取消交易后重试。",
    }


def cancel_transaction(transid: str) -> dict[str, Any]:
    if not transid:
        raise http.SteamError("cancel 需要 transid", code="bad_input")
    jar = _require_checkout()
    resp = http.request(
        "POST",
        f"{CHECKOUT}/checkout/canceltransaction/",
        data={"transid": transid, "lock_transaction": "false", "cancel_reason": "user cancelled via MCP"},
        cookies=jar,
        headers=_headers(),
    )
    body = resp.json() if resp.text.strip().startswith("{") else {"raw": resp.text[:200]}
    _PRICE_CACHE.pop(transid, None)
    return {"ok": body.get("success") == 2, "action": "cancel", "transid": transid, "response": body}


def transaction_status(transid: str, count: int = 1) -> dict[str, Any]:
    if not transid:
        raise http.SteamError("status 需要 transid", code="bad_input")
    jar = _require_checkout()
    resp = http.request(
        "GET",
        f"{CHECKOUT}/checkout/transactionstatus/",
        params={"count": max(1, int(count)), "transid": transid},
        cookies=jar,
        headers=_headers(),
    )
    body = resp.json() if resp.text.strip().startswith("{") else {"raw": resp.text[:200]}
    return {"ok": body.get("success") == 1, "action": "status", "transid": transid, "response": body}


def pay_link(transid: str) -> dict[str, Any]:
    """拿外部支付跳转入口（只读，返回链接，不代付）。"""
    if not transid:
        raise http.SteamError("pay_link 需要 transid", code="bad_input")
    jar = _require_checkout()
    resp = http.request(
        "GET",
        f"{CHECKOUT}/checkout/externallink/",
        params={"transid": transid},
        cookies=jar,
        headers=_headers(),
    )
    forms = re.findall(r'<form[^>]*action="([^"]+)"', resp.text)
    fields = {m.group(1): m.group(2) for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', resp.text)}
    return {
        "ok": True,
        "action": "pay_link",
        "transid": transid,
        "status": resp.status,
        "form_actions": forms[:3],
        "form_fields": {k: v for k, v in list(fields.items())[:20]},
        "redirect": (resp.headers.get("location") or "")[:200] or None,
        "note": "外部支付请在浏览器里自行完成，MCP 不代付、不会自动 finalize。",
    }
