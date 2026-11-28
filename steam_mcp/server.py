"""MCP 服务入口：把 Steam 账号能力暴露成一组工具。

约定：
- 所有工具都返回 dict，失败时返回 {"ok": false, "error": ..., "hint": ...}，不抛异常给协议层；
- 工具描述写清楚"需要什么前置条件"，agent 才知道什么时候该调用登录/自检；
- 日志只走 stderr，stdout 留给 MCP 协议。
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import api, auth, buff, checkout, config, extras, freelicense, generic, http, insights, secrets

mcp = MCPServer(
    name="steam",
    version="1.4.1",
    instructions=(
        "Steam 账号能力的通用 MCP。首次使用先 steam_auth_status；未登录则用 steam_auth_import_cookie"
        "（推荐）或 steam_auth_login。密码登录返回 awaiting_confirmation 时，先 steam_auth_poll 等待手机 App / 邮箱确认"
        "（无需输码），等不到再 steam_auth_guard 提交验证码；直接回车/空码不会报错。"
        "登录态过期会自动续期（用 refresh token 换新 cookie 并收齐各域名完整 cookie，含 checkout 域），"
        "无需手动重登；续期失败会明确提示需要重新登录，也可手动 steam_auth_refresh。"
        "社区域名不通时先 steam_net_check 诊断，再 steam_config_set 调整网络模式。"
        "登录后可读档案/库/愿望单/好友/库存/市场/评价，也可用 steam_request 触达任意 Steam 官方接口。"
        "网易 BUFF：先 steam_buff_login（走 Steam OpenID），之后 steam_buff_search / steam_buff_price 查行情。"
        "免费游戏和付费游戏流程不同：先用 steam_free_license(action=\"preview\", appid=...) 判定，"
        "F2P/限免走 steam_free_license(action=\"claim\", subid=..., confirm=true)（不花钱、不进 checkout 域）；"
        "付费才走 steam_cart → steam_checkout(preview→init→price→finalize)。两条链路不要混用。"
        "finalize(confirm=true, expected_total=...) 才真正扣款；external_payment 只给链接，不代付。"
    ),
)


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except http.SteamError as exc:
            return exc.as_dict()
        except ValueError as exc:
            return {"ok": False, "error": "bad_input", "message": str(exc)}
        except Exception as exc:  # 兜底，避免协议层报 internal error 丢上下文
            return {"ok": False, "error": "internal", "message": f"{type(exc).__name__}: {exc}"}

    return wrapper


# ---------------------------------------------------------------- 登录


@mcp.tool(description="查看 Steam 登录状态：是否已登录、SteamID、cookie 是否仍有效、能否静默续期。任何 Steam 操作前先调它。")
@_safe
def steam_auth_status() -> dict[str, Any]:
    return auth.status()


@mcp.tool(
    description=(
        "导入浏览器 cookie 完成登录（推荐方式，不需要密码，国内网络可用）。"
        "从浏览器 F12 → Application → Cookies → store.steampowered.com 复制 steamLoginSecure 整行粘贴进来；"
        "若同时提供 steamRefresh_steam 则可后续自动续期。支持 Cookie 头 / DevTools 表格 / 纯值三种格式。"
    )
)
@_safe
def steam_auth_import_cookie(cookie: str, verify: bool = True) -> dict[str, Any]:
    return auth.import_cookie(cookie, verify=verify)


@mcp.tool(
    description=(
        "用账号密码登录 Steam（走官方 IAuthenticationService）。密码只在内存里 RSA 加密后发出，不落盘、不写日志。"
        "需要 api.steampowered.com 可达。返回 stage：awaiting_confirmation=可先在手机 App 确认（用 steam_auth_poll 等待，"
        "无需输码）；awaiting_guard=需提交验证码；awaiting_poll=直接轮询等待确认。"
        "返回里带 allowed_confirmations / confirmation_types / code_types / guard_labels 说明各数字含义。"
    )
)
@_safe
def steam_auth_login(username: str, password: str) -> dict[str, Any]:
    return auth.login_with_password(username, password)


@mcp.tool(
    description=(
        "提交 Steam Guard 验证码。code_type 用 Steam 官方 EAuthSessionGuardType："
        "2=邮箱验证码，3=手机令牌动态码（TOTP），4=手机 App 确认，5=邮箱确认。"
        "优先用 login 返回的 suggested_code_type。验证码留空不会报错，只返回跳过提示。"
    )
)
@_safe
def steam_auth_guard(code: str, code_type: int = 3) -> dict[str, Any]:
    return auth.submit_guard_code(code, code_type)


@mcp.tool(
    description=(
        "轮询登录会话，等待手机 App / 邮箱确认或验证码生效。timeout 为等待秒数（默认 90，最少 5）。"
        "超时返回 stage=pending，会话保留，可继续轮询或改用 steam_auth_guard 输验证码。"
        "密码登录返回 awaiting_confirmation 时先调这个，不要一上来就输码。"
    )
)
@_safe
def steam_auth_poll(timeout: float = 90) -> dict[str, Any]:
    return auth.poll_login(timeout=timeout)


@mcp.tool(
    description=(
        "手动续期登录态：用 refresh token 换新的 access token，并重新走 finalizelogin 把"
        "store/community/help/checkout/steam.tv 各域名的完整 cookie 收齐写回凭证库。"
        "平时不需要手动调（每次调用需要登录的工具时会自动续期），仅在自动续期被关掉或想立刻刷新时用。"
    )
)
@_safe
def steam_auth_refresh() -> dict[str, Any]:
    return auth.refresh()


@mcp.tool(description="预热网页会话（从 store 拿 Steam 下发的 sessionid/browserid）。写操作会自动调用，一般不需要手动用；写操作返回 success:false 时可先跑一次再重试。")
@_safe
def steam_session_warm() -> dict[str, Any]:
    return auth.warm_session()


@mcp.tool(
    description=(
        "获取 Steam Web API Key。action=fetch 从 /dev/apikey 抓已注册的 key 存入凭证库（有 key 就用这个，成就/新闻/档案即刻解锁）；"
        "action=register 注册新 key（会代表账号同意 Steam Web API 使用条款，domain 随意填）。"
    )
)
@_safe
def steam_webapi_key(action: str = "fetch", domain: str = "localhost") -> dict[str, Any]:
    if action == "register":
        return auth.register_webapi_key(domain)
    return auth.fetch_webapi_key()


@mcp.tool(description="清除本机保存的 Steam 凭证（退出登录）。")
@_safe
def steam_auth_logout() -> dict[str, Any]:
    return auth.logout()


# ---------------------------------------------------------------- 诊断 / 配置


@mcp.tool(description="逐域名探测 Steam 各站点可达性，给出 DNS 污染 / 本地反代 / 代理配置的判断和建议。网络异常时先跑这个。")
@_safe
def steam_net_check() -> dict[str, Any]:
    return api.net_check()


@mcp.tool(description="读取当前配置（网络模式、代理、TLS 信任、语言、国家、敏感操作开关）。webapi.key 恒为 null——key 加密存在凭证库不在配置文件里，实际状态看 webapi_key_configured。")
@_safe
def steam_config_get() -> dict[str, Any]:
    cfg = config.load()
    # key 加密存在凭证库，配置文件里永远是 null。只报状态，不回明文。
    key = (auth.state().get("webapi_key") or "").strip()
    return {
        "ok": True,
        "config_path": str(config.config_path()),
        "config": cfg,
        "webapi_key_configured": bool(key),
        "webapi_key_fingerprint": f"len={len(key)}" if key else None,
    }


@mcp.tool(
    description=(
        "修改配置。常用：network_mode=direct|env|custom、proxy=http://127.0.0.1:7890、"
        "tls_trust=system|insecure、language、country、allow_sensitive、max_items、webapi_key。"
        "全部是具名参数（不是 key/value 对，传 {key:..., value:...} 会被忽略且返回 updated:{}）。"
        "传 webapi_key 会加密存进凭证库，不写配置文件。留空表示不改该项。"
    )
)
@_safe
def steam_config_set(
    network_mode: str | None = None,
    proxy: str | None = None,
    tls_trust: str | None = None,
    language: str | None = None,
    country: str | None = None,
    allow_sensitive: bool | None = None,
    webapi_key: str | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if network_mode:
        if network_mode not in ("direct", "env", "custom"):
            raise ValueError("network_mode 只能是 direct / env / custom")
        patch.setdefault("network", {})["mode"] = network_mode
    if proxy is not None:
        patch.setdefault("network", {})["proxy"] = proxy or None
    if tls_trust:
        if tls_trust not in ("system", "certifi", "insecure"):
            raise ValueError("tls_trust 只能是 system / certifi / insecure")
        patch.setdefault("network", {})["tls_trust"] = tls_trust
    if language:
        patch.setdefault("behavior", {})["language"] = language
    if country:
        patch.setdefault("behavior", {})["country"] = country
    if allow_sensitive is not None:
        patch.setdefault("behavior", {})["allow_sensitive"] = bool(allow_sensitive)
    if max_items is not None:
        patch.setdefault("behavior", {})["max_items"] = max(1, int(max_items))

    if patch:
        config.update(patch)
        http.reset()

    if webapi_key:
        data = auth.state()
        data["webapi_key"] = webapi_key.strip()
        secrets.save_credentials(data)

    out = {"ok": True, "updated": patch}
    if webapi_key:
        out["webapi_key_fingerprint"] = secrets.fingerprint(webapi_key)
    out["config"] = config.load()
    return out


# ---------------------------------------------------------------- 档案


@mcp.tool(description="查看自己的账号概况：昵称、状态、头像、SteamID、等级、是否在游戏中。需已登录。")
@_safe
def steam_me() -> dict[str, Any]:
    return api.me()


@mcp.tool(description="查看任意用户档案。user 可传 SteamID64、个人主页链接、自定义 URL 名，或留空表示自己。")
@_safe
def steam_user_profile(user: str = "") -> dict[str, Any]:
    return api.profile(user or None)


@mcp.tool(description="好友列表。需登录；对方好友列表需公开，否则返回空并说明原因。")
@_safe
def steam_friends(user: str = "", limit: int = 30) -> dict[str, Any]:
    return api.friends(user or None, limit=limit)


# ---------------------------------------------------------------- 游戏库


@mcp.tool(
    description=(
        "游戏库列表（默认按游玩时长倒序）。search 可按名称过滤。"
        "有 api 权限时带游玩时长；否则走 store 的 dynamicstore + 批量详情，只有 appid/名称/价格。"
    )
)
@_safe
def steam_library(user: str = "", search: str = "", limit: int = 30, with_details: bool = True) -> dict[str, Any]:
    return api.library(user or None, search=search or None, limit=limit, with_details=with_details)


@mcp.tool(description="最近两周玩过的游戏（需要 api.steampowered.com 可达）。")
@_safe
def steam_recent_games(user: str = "") -> dict[str, Any]:
    return api.recent_games(user or None)


# ---------------------------------------------------------------- 重组分析（只读）

@mcp.tool(description=(
    "[只读] 游玩时长分析：总时长、Top N、近两周、吃灰榜（有库无时长）、久未启动。"
    "纯重组现有游戏库数据，不发额外请求。"
))
@_safe
def steam_playtime_stats(user: str = "", top: int = 20, idle_days: int = 180) -> dict[str, Any]:
    return insights.playtime_stats(user or None, top=top, idle_days=idle_days)


@mcp.tool(description=(
    "[只读] 愿望单折扣雷达：按折扣率排序的降价项 + 0 元限免。"
    "Steam 不支持批量查价，只能逐个扫（约 0.4s/个），结果缓存 30 分钟；"
    "返回 has_more=true 时再调一次即可继续累积。"
))
@_safe
def steam_deals(
    user: str = "",
    min_discount: int = 0,
    limit: int = 40,
    max_fetch: int = 120,
    refresh: bool = False,
) -> dict[str, Any]:
    return insights.deals(
        user or None, min_discount=min_discount, limit=limit, max_fetch=max_fetch, refresh=refresh
    )


@mcp.tool(description=(
    "[只读] Deck 兼容等级 + 商店资源（封面/平台/媒体评分/推荐数），批量传入 appid。"
    "Deck 数据匿名可取，不需要登录。"
))
@_safe
def steam_app_extras(appids: list[int], limit: int = 20) -> dict[str, Any]:
    return insights.app_extras(appids, limit=limit)


@mcp.tool(description="[只读] 单个 app 的 Steam Deck 兼容等级（verified / playable / unsupported）。")
@_safe
def steam_deck_compat(appid: int) -> dict[str, Any]:
    return insights.deck_compat(appid)


@mcp.tool(description=(
    "[只读] 我当前的市场在售挂单（价格/手续费/状态/待确认数）。"
    "只读视图，不含上架、改价、撤单等交易动作。"
))
@_safe
def steam_market_listings(limit: int = 50) -> dict[str, Any]:
    auth.require_login()
    return insights.market_listings(limit=limit)


@mcp.tool(description="[只读] 两人游戏库对比：共同游戏、各自独有、共同游戏的时长差。不传参数则与自己比（a 为自己）。")
@_safe
def steam_library_diff(a: str = "", b: str = "", limit: int = 30) -> dict[str, Any]:
    return insights.library_diff(a or None, b or None, limit=limit)


@mcp.tool(description=(
    "[只读] 愿望单价格变动：基于 steam_deals 缓存做快照对比，不发额外请求。"
    "commit=true 会把当前价格存为新基线（默认只读）。"
))
@_safe
def steam_price_watch(user: str = "", commit: bool = False, limit: int = 30) -> dict[str, Any]:
    return insights.price_watch(user or None, commit=commit, limit=limit)


@mcp.tool(description="[只读] 账号许可/购买记录（日期、名称、来源）。分页用 offset/limit。")
@_safe
def steam_purchase_history(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    auth.require_login()
    return insights.purchase_history(limit=limit, offset=offset)


@mcp.tool(description=(
    "[只读] 我的徽章进度：每款游戏的卡牌收集数、可合成状态、经验值。"
    "需要登录；来自社区徽章页 HTML 解析，不额外消耗接口配额。"
))
@_safe
def steam_badges(limit: int = 0) -> dict[str, Any]:
    auth.require_login()
    return extras.badges(limit=limit or None)


@mcp.tool(description=(
    "[只读] 我的市场成交历史（买入/卖出的完成记录）。"
    "count 只接受 25 或 50；offset 按 25 递增，返回 has_more 表示还能翻页。"
    "金额给最小货币单位原值，不替你换算汇率。"
))
@_safe
def steam_market_history(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    auth.require_login()
    return extras.market_history(limit=limit, offset=offset)


@mcp.tool(description=(
    "[只读] 库存估值：按当前市场最低挂单价汇总。"
    "市场域 2s/次限速，用 top_n / max_price_checks 控制规模，返回 has_more。"
    "est_total 是按最低挂单价累加，不等于立刻能卖出的金额（有手续费与流动性）。"
))
@_safe
def steam_inventory_value(
    top_n: int = 30,
    appid: int = 753,
    contextid: str = "6",
    currency: str = "CNY",
    max_price_checks: int = 0,
) -> dict[str, Any]:
    return extras.inventory_value(
        top_n=top_n,
        appid=appid,
        contextid=contextid,
        currency=currency,
        max_price_checks=max_price_checks or None,
    )


@mcp.tool(description=(
    "[只读] 全库成就完美度排行：找出「再拿 N 个就全成就」的游戏。"
    "逐游戏查询，max_apps 控制本轮扫描量（默认 60），返回 has_more 提示继续。"
    "无 Web API Key 时拿不到成就总数，会带 warnings 说明。"
))
@_safe
def steam_achievement_hunter(
    limit: int = 10,
    min_percent: float = 0.0,
    max_apps: int = 60,
    user: str = "",
) -> dict[str, Any]:
    return extras.achievement_hunter(
        limit=limit, min_percent=min_percent, max_apps=max_apps, value=user or None
    )


@mcp.tool(description=(
    "[只读] 同一件饰品在 Steam 市场与网易 BUFF 的最低价对比（人民币口径）。"
    "BUFF 需先 steam_buff_login；两侧都取不到会如实返回 error。"
))
@_safe
def steam_market_compare(hash_name: str, appid: int = 730, game: str = "csgo") -> dict[str, Any]:
    return insights.market_compare(hash_name, appid=appid, game=game)


@mcp.tool(description=(
    "配置 IsThereAnyDeal API key（历史最低价数据源，SteamDB 的合规替代）。"
    "去 https://isthereanydeal.com/apps/my/ 注册应用获取；传空字符串清除。"
))
@_safe
def steam_itad_key(key: str = "") -> dict[str, Any]:
    return insights.set_itad_key(key)


@mcp.tool(description=(
    "[只读] 某游戏的历史最低价（IsThereAnyDeal）。返回最低价、折扣、店铺、时间点与 ITAD 链接。"
    "country 用 ISO 两位码（CN/ID/US），默认 CN。未配置 key 时返回 itad_key_missing。"
))
@_safe
def steam_price_history(appid: int, country: str = "CN") -> dict[str, Any]:
    return insights.price_history(appid, country=country)


@mcp.tool(description=(
    "[花钱] 兑换 Steam 钱包充值码 / 礼品卡，直接增加余额。"
    "必须 confirm=true 且 behavior.allow_sensitive=true；码有地区限制（码必须与账号地区一致）。"
    "返回与日志里的码只显示前 4 位。若返回 captcha_required，需带 captcha_gid + captcha_text 重试。"
))
@_safe
def steam_redeem_wallet_code(
    code: str,
    confirm: bool = False,
    captcha_gid: str = "",
    captcha_text: str = "",
) -> dict[str, Any]:
    return freelicense.redeem_wallet_code(
        code, confirm=confirm, captcha_gid=captcha_gid, captcha_text=captcha_text
    )


@mcp.tool(description="某个游戏的成就进度与解锁情况（需要 api 可达或 Web API Key）。")
@_safe
def steam_achievements(appid: int, user: str = "", limit: int = 40) -> dict[str, Any]:
    return api.achievements(appid, user or None, limit=limit)


# ---------------------------------------------------------------- 商店


@mcp.tool(description="商店搜索游戏/DLC，返回 appid、名称、价格、平台。")
@_safe
def steam_store_search(term: str, limit: int = 10) -> dict[str, Any]:
    return api.store_search(term, limit=limit)


@mcp.tool(description="游戏详情：价格、折扣、发售日、开发商、类型、DLC、媒体评分、推荐数。")
@_safe
def steam_app_details(appid: int) -> dict[str, Any]:
    return api.app_details(appid)


@mcp.tool(description="查看愿望单（含价格与优先级）。需登录或愿望单公开。")
@_safe
def steam_wishlist(user: str = "", limit: int = 30) -> dict[str, Any]:
    return api.wishlist(user or None, limit=limit)


@mcp.tool(description="把游戏加入/移出愿望单。action: add 或 remove。需已登录。")
@_safe
def steam_wishlist_edit(action: str, appid: int) -> dict[str, Any]:
    return api.wishlist_edit(action, appid)


# ---------------------------------------------------------------- 免费游戏 / 免费许可证


@mcp.tool(
    description=(
        "免费游戏领取，与付费结账是两条独立链路。默认 preview 只读：判定 appid 是 "
        "F2P 常驻免费（不用领、直接 steam://run）、限免可领取（给出 claimable_packages 的 subid）、"
        "已拥有，还是付费游戏（该走 steam_checkout）。"
        "claim 才真正把免费许可证写入游戏库：必须 confirm=true + behavior.allow_sensitive=true + 显式 subid，"
        "走 store 域 /freelicense/addfreelicense/，不花钱、不创建交易、不经过 checkout 域名；"
        "领取后复核游戏库。付费游戏请勿用本工具。"
    )
)
@_safe
def steam_free_license(
    action: str = "preview",
    appid: int | None = None,
    subid: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    return freelicense.free_license(action, appid=appid, subid=subid, confirm=confirm)


# ---------------------------------------------------------------- 购物车


@mcp.tool(
    description=(
        "购物车：action=view 读明细 / add 加购 / remove 移除 / clear 清空。"
        "add 传 appid（自动查 subid）或直接传 subid；remove 传 view 返回的 line_item_id。"
        "加购不扣款。仅用于付费游戏；免费游戏请用 steam_free_license。"
    )
)
@_safe
def steam_cart(
    action: str = "view",
    appid: int | None = None,
    subid: int | None = None,
    line_item_id: str = "",
) -> dict[str, Any]:
    return checkout.cart(action, appid=appid, subid=subid, line_item_id=line_item_id)


# ---------------------------------------------------------------- 结账 / 购买（仅付费游戏）


@mcp.tool(
    description=(
        "结账/购买游戏。默认 preview（只读，展示购物车 + 钱包余额 + checkout 会话状态）。完整流程："
        "init 创建交易（不扣款）→ price 核对最终金额（total 单位分）→ "
        "finalize(confirm=true, expected_total=<price 返回的 total>) 才真正扣款。"
        "finalize 会二次核价：价格变动、钱包不足需外部支付时一律拒绝。"
        "钱包不足用 action=pay_link 只返回支付链接，MCP 不代付、不自动 finalize。"
        "另有 cancel / status 取消与查询交易。花钱动作需 behavior.allow_sensitive=true 且 confirm=true。"
    )
)
@_safe
def steam_checkout(
    action: str = "preview",
    transid: str = "",
    expected_total: int | None = None,
    confirm: bool = False,
    payment_method: str = "steamaccount",
    country: str = "",
    count: int = 1,
) -> dict[str, Any]:
    return checkout.checkout(
        action,
        transid=transid,
        expected_total=expected_total,
        confirm=confirm,
        payment_method=payment_method,
        country=country,
        count=count,
    )


@mcp.tool(
    description=(
        "管理 checkout.steampowered.com 的独立登录态（结账必需，与 store 域 cookie 不通用）。"
        "status 查看是否就绪并试探结账页；密码登录会自动收集，cookie 导入方式需 "
        "action=\"import\" 粘贴 checkout 域 cookie，或 action=\"refresh\" 用 refresh_token 补发。"
    )
)
@_safe
def steam_checkout_session(action: str = "status", cookie: str = "") -> dict[str, Any]:
    return checkout.checkout_session(action, cookie=cookie)


# ---------------------------------------------------------------- 库存 / 市场


@mcp.tool(
    description=(
        "查库存。默认 appid=753（Steam 社区物品，contextid=6）；查 CS2 用 730/2，Dota2 用 570/2，"
        "TF2 用 440/2。需登录且库存公开。"
    )
)
@_safe
def steam_inventory(steamid: str = "", appid: int = 753, contextid: str = "6", limit: int = 30) -> dict[str, Any]:
    return api.inventory(steamid or None, appid=appid, contextid=contextid, limit=limit)


@mcp.tool(description="查单件市场物品的最低/中位价与成交量。hash_name 必须与市场名称完全一致（含磨损、StatTrak 等前缀）。")
@_safe
def steam_market_price(appid: int, hash_name: str, currency: str = "CNY") -> dict[str, Any]:
    return api.market_price(appid, hash_name, currency)


@mcp.tool(description="在市场里搜索物品，返回挂单价与在售数量。")
@_safe
def steam_market_search(appid: int, query: str = "", limit: int = 10) -> dict[str, Any]:
    return api.market_search(appid, query, limit=limit)


# ---------------------------------------------------------------- 其他内容


@mcp.tool(description="游戏新闻公告。优先官方 Web API，失败时回落到 store 的 RSS 源。")
@_safe
def steam_news(appid: int, count: int = 5) -> dict[str, Any]:
    return api.news(appid, count=count)


@mcp.tool(description="游戏评测：总体好评率 + 若干条评测摘要。")
@_safe
def steam_reviews(appid: int, limit: int = 10, language: str = "all") -> dict[str, Any]:
    return api.reviews(appid, limit=limit, language=language)


# ---------------------------------------------------------------- 通用请求


@mcp.tool(
    description=(
        "通用已登录请求：对 Steam 官方域名发任意请求，自动带上当前账号 cookie（和 api 的 access_token 不自动加，需自己放进 params）。"
        "域名白名单：store/api/login/help.steampowered.com、steamcommunity.com。只允许 https。"
        "花钱/改安全设置/接受交易类接口会被拦，必须显式 confirm=true。"
    )
)
@_safe
def steam_request(
    method: str,
    url: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any = None,
    confirm: bool = False,
    raw_html: bool = False,
) -> dict[str, Any]:
    return generic.request(
        method,
        url,
        params=params,
        data=data,
        json_body=json_body,
        confirm=confirm,
        raw_html=raw_html,
    )


# ---------------------------------------------------------------- 网易 BUFF


@mcp.tool(
    description=(
        "登录网易 BUFF（buff.163.com）：走 BUFF 官方的「Steam 登录」通道（OpenID），"
        "借用本机已登录的 Steam 会话完成授权，不需要网易账号密码。前置条件：Steam 会话有效（先 steam_auth_status 确认）。"
        "BUFF cookie 与 Steam 凭证一样只进加密凭证库，不写日志。"
    )
)
@_safe
def steam_buff_login() -> dict[str, Any]:
    return buff.login_via_steam()


@mcp.tool(description="查看 BUFF 登录状态与 session 是否仍有效。")
@_safe
def steam_buff_status() -> dict[str, Any]:
    return buff.status()


@mcp.tool(description="清除本机保存的 BUFF 凭证（不影响 Steam 登录态）。")
@_safe
def steam_buff_logout() -> dict[str, Any]:
    return buff.logout()


@mcp.tool(
    description=(
        "在 BUFF 按名称搜商品，返回 goods_id 列表（后续查价用 goods_id）。"
        "例：搜「蝴蝶刀 致命紫罗兰 破损不堪」拿它的 goods_id。需已登录 BUFF。"
    )
)
@_safe
def steam_buff_search(keyword: str, game: str = "csgo", limit: int = 10) -> dict[str, Any]:
    return buff.search_goods(keyword, game=game, limit=limit)


@mcp.tool(
    description=(
        "查 BUFF 某件商品（goods_id）的实时行情：在售列表（每条含价格、float 磨损、图案模板）+ 求购列表。"
        "价格为买家实付（手续费卖家承担）。需已登录 BUFF。"
    )
)
@_safe
def steam_buff_price(goods_id: int, game: str = "csgo", page_num: int = 1) -> dict[str, Any]:
    return buff.market_price(goods_id, game=game, page_num=page_num)


# ---------------------------------------------------------------- prompt / resource
#
# MCP 的 prompt 是「用户手动选的预设任务」，resource 是「可读的文档块」。
# 这两样只对带 UI 的客户端有意义；纯 agent 直接调 tool 更快。放在这里是因为
# 客户端会显示这三类能力，空着会被当成服务端没实现。


@mcp.prompt(
    name="wishlist_sale_check",
    title="愿望单折扣检查",
    description="扫描愿望单，把当前折扣按「是否史低」分组，给出买/等的判断。",
)
def prompt_wishlist_sale_check(limit: str = "40") -> str:
    return (
        f"检查我的 Steam 愿望单折扣（最多 {limit} 个）。\n"
        "步骤：\n"
        "1. steam_deals 拉当前折扣，必要时多调几次直到 has_more=false\n"
        "2. 对每个打折项用 steam_price_history 查历史低价\n"
        "3. 分成两组：\n"
        "   - 现在就是史低：low.shop == 'Steam' 且 low.amount == 当前价 → 建议现在买\n"
        "   - 不是史低：给出史低价、差价百分比 → 建议等\n"
        "4. low.shop 不是 Steam 的记录要单独标注「全渠道最低，Steam 端史低未知」，"
        "不能当成 Steam 史低\n"
        "输出：两张表 + 一句话总结（这波能省多少 / 建议买哪几个）。"
    )


@mcp.prompt(
    name="purchase_advisor",
    title="购买建议（预算约束）",
    description="给定预算和候选游戏，算出能买哪些、需要充值多少。",
)
def prompt_purchase_advisor(appids: str = "", budget: str = "") -> str:
    return (
        "帮我决定这次买什么。\n"
        f"候选游戏 appid：{appids or '（未指定，用 steam_deals 从愿望单里挑打折的）'}\n"
        f"预算：{budget or '（未指定）'}\n"
        "步骤：\n"
        "1. steam_checkout(action='preview') 先读真实钱包余额——不要假设余额\n"
        "2. 对每个候选用 steam_price_history 判断是否史低，非史低的标出差价\n"
        "3. 按余额算能买几个，给出组合方案\n"
        "4. 余额不够就说明差多少、需要充哪个档\n"
        "约束：只推荐史低的；用户点名要的必须算进去并说明它占了多少预算。"
    )


@mcp.prompt(
    name="account_health",
    title="账号与登录态体检",
    description="一条命令跑完网络、登录、在线状态的检查。",
)
def prompt_account_health() -> str:
    return (
        "检查 Steam MCP 的健康状态。\n"
        "顺序（别跳步）：\n"
        "1. steam_net_check — 5 个域名是否全 ok\n"
        "2. steam_auth_status — logged_in / has_refresh_token / renew.ok\n"
        "3. steam_me — 只有这个能证明会话真的活着，前两步都是历史快照\n"
        "4. 有异常先 steam_auth_refresh，再考虑重登\n"
        "注意：verification.valid=false + reason=expired 是自检的已知噪音，"
        "不代表会话已死，以 steam_me 能否返回档案为准。"
    )


@mcp.resource(
    "steam://docs/tools",
    name="tools",
    title="工具清单",
    description="TOOLS.md：全部工具的参数与说明。",
    mime_type="text/markdown",
)
def resource_tools_doc() -> str:
    path = Path(__file__).resolve().parent.parent / "TOOLS.md"
    return path.read_text(encoding="utf-8") if path.exists() else "# TOOLS.md 不存在"


@mcp.resource(
    "steam://docs/roadmap",
    name="roadmap",
    title="开发路线与踩坑记录",
    description="ROADMAP.md：实现细节、被实测推翻的判断、决定不做的方向。",
    mime_type="text/markdown",
)
def resource_roadmap_doc() -> str:
    path = Path(__file__).resolve().parent.parent / "ROADMAP.md"
    return path.read_text(encoding="utf-8") if path.exists() else "# ROADMAP.md 不存在"


def main() -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    print("[steam-mcp] starting stdio server", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
