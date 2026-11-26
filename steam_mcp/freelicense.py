"""免费游戏 / 免费许可证领取（store.steampowered.com，与付费结账完全独立）。

Steam 里「免费」和「付费」是两条毫不相同的链路，绝不能混用：

    免费  → POST store/freelicense/addfreelicense/  加 subid 许可证
            无购物车、无 transid、无钱包、无支付、不经过 checkout.steampowered.com
    付费  → checkout.steampowered.com 的 inittransaction / getfinalprice /
            finalizetransaction（见 checkout.py），会真实扣款

「免费」本身还分两种，工具会区分：
1. F2P 常驻免费（appdetails.is_free=true，商店页显示「免费开玩」）
   按钮是 steam://run/<appid>，客户端直接安装，**不需要领取、不入库 license**；
   这种包的 can_get_free_license 通常是 "0"。
2. 限免 / 免费促销包（Limited Free Promotional Package）
   appdetails 的 package_groups[].subs[] 里 is_free_license=true、
   price_in_cents_with_discount=0 且 can_get_free_license 为真，才是可以 claim 的 subid。

安全门槛（不是建议，和付费 finalize 一致）：
1. claim 必须 confirm=true；
2. 必须 behavior.allow_sensitive=true；
3. 必须显式给 subid（packageid），不允许只凭 appid 猜着领；
4. 默认 action=preview 全程只读，不产生任何账号变更；
5. 领取后会再查一次 dynamicstore 复核是否真的入库（幂等验证）。
"""
from __future__ import annotations

from typing import Any

from . import auth, config, http

STORE = "https://store.steampowered.com"

# PurchaseResultDetail 的常见值（Steam 官方枚举的精简映射）
RESULT_DETAIL = {
    0: "无详情", 3: "需要联系客服", 5: "无效的包 (InvalidPackage)", 9: "已拥有 (AlreadyPurchased)",
    13: "地区受限 (RestrictedCountry)", 16: "需改用其他支付方式", 22: "账号无效",
    24: "缺少所需的基础游戏 (DoesNotOwnRequiredApp)", 31: "钱包币种不符", 44: "账号被锁定",
    53: "触发限流 (RateLimited)", 65: "交易不可重放",
}


# ---------------------------------------------------------------- 只读判定


def _app_details(appid: int) -> tuple[dict[str, Any], bool]:
    cfg = config.load()["behavior"]
    jar = auth.cookies_for("store.steampowered.com")
    resp = http.request(
        "GET",
        f"{STORE}/api/appdetails",
        params={"appids": int(appid), "cc": cfg.get("country", "CN"), "l": cfg.get("language", "schinese")},
        cookies=jar,
    )
    try:
        body = resp.json() or {}
    except http.SteamError:
        return {}, False
    entry = body.get(str(appid)) or {}
    return (entry.get("data") or {}), bool(entry.get("success"))


def _owned_apps() -> set[int]:
    jar = auth.cookies_for("store.steampowered.com")
    try:
        resp = http.request("GET", f"{STORE}/dynamicstore/userdata", cookies=jar)
        body = resp.json() or {}
        return {int(x) for x in (body.get("rgOwnedApps") or [])}
    except (http.SteamError, ValueError, TypeError):
        return set()


def _free_subs(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (可领取的免费包, 仅展示用的免费包)。

    Steam 促销期 is_free / price_overview 都不可靠，可靠的信号是
    package_groups[].subs[] 里的 is_free_license + price_in_cents_with_discount=0。
    can_get_free_license 为假说明该包不给普通账号领（F2P 直接开玩即可）。
    """
    claimable: list[dict[str, Any]] = []
    display: list[dict[str, Any]] = []
    for group in data.get("package_groups") or []:
        for sub in group.get("subs") or []:
            is_free_license = sub.get("is_free_license") in (True, "true", "True", 1, "1")
            discount = sub.get("price_in_cents_with_discount")
            zero = discount in (0, "0", None)
            if not (is_free_license and zero):
                continue
            text = (sub.get("option_text") or "").lower()
            item = {
                "packageid": sub.get("packageid"),
                "option_text": sub.get("option_text"),
                "can_get_free_license": sub.get("can_get_free_license"),
                "limited_promotional": "limited free promotional" in text or "限时免费" in (sub.get("option_text") or ""),
            }
            display.append(item)
            if item["can_get_free_license"] in (True, "true", "True", 1, "1"):
                claimable.append(item)
    return claimable, display


def inspect(appid: int | None, subid: int | None = None) -> dict[str, Any]:
    """preview：只读判定，绝不修改账号。"""
    if not appid and not subid:
        raise http.SteamError("preview 需要 appid（或至少 subid）", code="bad_input")
    auth.require_login()

    data: dict[str, Any] = {}
    details_ok = False
    if appid:
        data, details_ok = _app_details(int(appid))
    owned = _owned_apps()
    is_owned = bool(appid and int(appid) in owned)
    claimable, display = _free_subs(data)

    if is_owned:
        classification = "already_owned"
        advice = "已在游戏库中，无需领取。若要安装，直接用客户端或 steam://run/<appid>。"
    elif claimable or subid:
        classification = "claimable"
        advice = ("检测到免费许可证包。确认无误后调用 "
                  "steam_free_license(action=\"claim\", subid=<packageid>, confirm=true)；"
                  "此步不花钱，但会写入游戏库，需 behavior.allow_sensitive=true。")
    elif data.get("is_free"):
        classification = "free_to_play"
        advice = ("这是 F2P 常驻免费游戏，商店页显示「免费开玩」。不需要领取、不入库；"
                  "直接用 steam://run/<appid> 或客户端安装即可，走 steam_checkout 是错的。")
    elif details_ok:
        classification = "paid"
        advice = "这是付费游戏，请走 steam_cart → steam_checkout 流程，不要用免费领取。"
    else:
        classification = "unknown"
        advice = "appdetails 未返回数据（appid 可能无效或区域受限），无法判定，未执行任何操作。"

    return {
        "ok": True,
        "action": "preview",
        "readonly": True,
        "appid": appid,
        "name": data.get("name"),
        "is_free": data.get("is_free"),
        "details_ok": details_ok,
        "owned": is_owned,
        "owned_apps_count": len(owned),
        "classification": classification,
        "claimable_packages": claimable,
        "free_packages": display,
        "suggested_subid": (claimable[0]["packageid"] if claimable else subid),
        "paid_checkout_used": False,
        "advice": advice,
        "flow": {
            "free": "store.steampowered.com/freelicense/addfreelicense/ → 直接加 subid 许可证",
            "paid": "checkout.steampowered.com → inittransaction → getfinalprice → finalizetransaction",
        },
    }


# ---------------------------------------------------------------- 领取


def _parse_response(resp: http.Response) -> dict[str, Any]:
    text = (resp.text or "").strip()
    body: dict[str, Any] = {}
    if text.startswith("{"):
        try:
            body = resp.json() if isinstance(resp.json(), dict) else {}
        except http.SteamError:
            body = {}
    detail = body.get("purchaseresultdetail")
    detail_msg = RESULT_DETAIL.get(detail) if isinstance(detail, int) else None
    # 成功信号：200 且响应为 [] / 空 / success=1，或 detail=9（已拥有，视为幂等成功）
    success = resp.status in (200, 201) and (
        text in ("[]", "", "{}") or body.get("success") in (1, "1", True)
    )
    already = detail == 9
    return {
        "status": resp.status,
        "success": bool(success or already),
        "already_owned": already,
        "purchaseresultdetail": detail,
        "detail_message": detail_msg,
        "raw": text[:200],
    }


def claim(appid: int | None, subid: int | None, confirm: bool) -> dict[str, Any]:
    """真正领取（写入游戏库）。三重门槛：confirm / allow_sensitive / 显式 subid。"""
    if not confirm:
        raise http.SteamError(
            "claim 会修改游戏库，已拦截。",
            code="confirmation_required",
            hint="先跑 preview 确认 classification=claimable，再带 confirm=true 和明确的 subid 调用。",
        )
    if not config.load()["behavior"].get("allow_sensitive"):
        raise http.SteamError(
            "behavior.allow_sensitive 未开启，禁止执行领取操作。",
            code="confirmation_required",
            hint="先 steam_config_set(allow_sensitive=true)，用完建议关回去。",
        )
    if not subid:
        raise http.SteamError(
            "claim 需要显式 subid（packageid），不能只凭 appid 猜。",
            code="bad_input",
            hint="从 steam_free_license(action=\"preview\") 的 claimable_packages[].packageid 取。",
        )
    auth.require_login()
    auth.warm_session()
    jar = auth.cookies_for("store.steampowered.com")
    sid = auth.sessionid()
    referer = f"{STORE}/app/{appid}/" if appid else f"{STORE}/"

    # 主端点：新版 AddFreeLicense，subid 放表单
    resp = http.request(
        "POST",
        f"{STORE}/freelicense/addfreelicense/",
        data={"ajax": "true", "sessionid": sid, "subid": int(subid)},
        cookies=jar,
        headers={"Referer": referer, "Origin": STORE, "X-Requested-With": "XMLHttpRequest"},
    )
    result = _parse_response(resp)

    # 仅在端点明确不存在时回落到旧路径（subid 放在 URL 里），避免重复提交
    if resp.status in (404, 405) and not result["success"]:
        legacy = http.request(
            "POST",
            f"{STORE}/checkout/addfreelicense/{int(subid)}",
            data={"ajax": "true", "sessionid": sid},
            cookies=jar,
            headers={"Referer": referer, "Origin": STORE, "X-Requested-With": "XMLHttpRequest"},
        )
        result = _parse_response(legacy)
        result["endpoint_used"] = "legacy /checkout/addfreelicense/<subid>"
    else:
        result["endpoint_used"] = "/freelicense/addfreelicense/"

    # 幂等复核：再查一次库，确认是否真的写入
    verification = {"checked": False}
    if appid:
        try:
            now_owned = int(appid) in _owned_apps()
            verification = {"checked": True, "now_owned": now_owned}
        except http.SteamError as exc:
            verification = {"checked": False, "error": exc.as_dict()}

    return {
        "ok": bool(result["success"]),
        "action": "claim",
        "appid": appid,
        "subid": int(subid),
        "charged": False,
        "checkout_transaction_created": False,
        "result": result,
        "verification": verification,
        "next": "已写入游戏库。" if result["success"] else
                "未成功领取。看 result.purchaseresultdetail 判断原因；不要盲目重试，避免触发限流。",
    }


# ---------------------------------------------------------------- 钱包充值码

REDEEM_URL = "https://store.steampowered.com/account/ajaxredeemwalletcode"

# Steam 对这些 detail 没有公开文档；下面两条是按实测行为归纳的，都标注了证据来源。
REDEEM_DETAIL_HINTS = {
    9: "该码已被兑换过（Steam 不接受重复兑换）。"
       "实测依据：两枚确认用过的码均返回 9，余额无变化。",
    14: "码无效。常见原因：码打错、或与账号地区不匹配"
        "（Steam 钱包码有区域限制，印尼区码只能充印尼区账号）。"
        "实测依据：格式合法的假码返回 14，与其他错误码可区分。",
}


def _mask_code(code: str) -> str:
    """凭证类字符串只留前 4 位，避免兑换码进日志/上下文。"""
    c = (code or "").strip()
    return (c[:4] + "****") if len(c) > 4 else "****"


def redeem_wallet_code(
    code: str,
    *,
    confirm: bool = False,
    captcha_gid: str = "",
    captcha_text: str = "",
) -> dict[str, Any]:
    """兑换 Steam 钱包充值码 / 礼品卡。三重门槛：confirm / allow_sensitive / 非空码。

    这是**真正加钱**的操作，与免费领取不同；码本身是钱，任何返回值与日志只给掩码。
    """
    raw = (code or "").strip()
    if len(raw) < 8:
        return {
            "ok": False,
            "error": "bad_input",
            "hint": "充值码格式通常是 XXXX-XXXX-XXXX 之类，至少 8 位。",
            "code_hint": _mask_code(raw),
        }
    if not confirm:
        return {
            "ok": False,
            "error": "confirmation_required",
            "message": "兑换会直接改动钱包余额，必须显式 confirm=true。",
            "hint": "确认码和账号地区一致后，用 confirm=true 再调一次。",
            "code_hint": _mask_code(raw),
        }
    if not config.load()["behavior"].get("allow_sensitive"):
        return {
            "ok": False,
            "error": "confirmation_required",
            "message": "behavior.allow_sensitive 未开启，禁止兑换充值码。",
            "hint": "先 steam_config_set(allow_sensitive=true)。",
            "code_hint": _mask_code(raw),
        }

    auth.require_login()
    auth.warm_session()
    data: dict[str, Any] = {"wallet_code": raw, "sessionid": auth.sessionid()}
    if captcha_text:
        data["input_captcha"] = captcha_text
        if captcha_gid:
            data["input_captcha_gid"] = captcha_gid

    resp = http.request(
        "POST",
        REDEEM_URL,
        data=data,
        cookies=auth.cookies_for("store.steampowered.com"),
        headers={
            "Referer": "https://store.steampowered.com/account/redeemwalletcode",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        body = resp.json() or {}
    except http.SteamError:
        return {"ok": False, "error": "bad_payload", "code_hint": _mask_code(raw),
                "hint": f"返回不是 JSON（HTTP {resp.status}）；多半是登录态或地区限制，先跑 steam_net_check。",
                "body_preview": resp.text[:200]}

    success = body.get("success")
    detail = body.get("detail")
    if success == 1:
        return {
            "ok": True,
            "redeemed": True,
            "code_hint": _mask_code(raw),
            "detail": detail,
            "hint": "兑换成功。用 steam_auth_status 或账户页确认新余额。",
        }

    out: dict[str, Any] = {
        "ok": False,
        "redeemed": False,
        "code_hint": _mask_code(raw),
        "success": success,
        "detail": detail,
        "hint": REDEEM_DETAIL_HINTS.get(detail, "兑换被拒；Steam 未给出可读原因，按 detail 码排查。"),
    }
    if body.get("captcha_needed") or body.get("captcha_gid"):
        out["captcha_required"] = True
        out["captcha_gid"] = body.get("captcha_gid") or body.get("input_captcha_gid")
        out["captcha_url"] = "https://store.steampowered.com/account/redeemwalletcode"
        out["hint"] = "Steam 要求验证码：打开 captcha_url 读图后，带 captcha_gid + captcha_text 重试。"
    return out


def free_license(
    action: str = "preview",
    appid: int | None = None,
    subid: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """统一入口。默认 preview（只读）；只有 claim 才会修改游戏库。"""
    action = (action or "preview").lower()
    if action == "preview":
        return inspect(appid, subid)
    if action == "claim":
        return claim(appid, subid, confirm)
    raise http.SteamError("action 只能是 preview / claim", code="bad_input")
