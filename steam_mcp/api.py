"""Steam 各类接口的封装。

一个原则：能走 store 域名就不走 api 域名（国内 api 常不可达），能走 JSON 就不解析 HTML，
解析 HTML 的地方一定带 source / warning 字段，让调用方知道数据是怎么来的。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import socket
import time
from typing import Any, Iterable

from . import auth, config, http

STORE = "https://store.steampowered.com"
COMMUNITY = "https://steamcommunity.com"
API = "https://api.steampowered.com"

CURRENCY_CODES = {
    "CNY": 23, "USD": 1, "EUR": 3, "GBP": 2, "JPY": 8, "RUB": 5, "HKD": 29, "TWD": 30,
}


# ---------------------------------------------------------------- 小工具


def _lang() -> str:
    return config.load()["behavior"].get("language", "schinese")


def _cc() -> str:
    return config.load()["behavior"].get("country", "CN")


def _limit() -> int:
    return int(config.load()["behavior"].get("max_items", 60))


def _token() -> str | None:
    data = auth.state()
    return data.get("access_token")


def _api_key() -> str | None:
    return config.load()["webapi"].get("key") or auth.state().get("webapi_key")


def _api_auth_params() -> dict[str, Any]:
    """IPlayerService 系接受 access_token；ISteamUser/ISteamUserStats/ISteamNews 只认 key。"""
    params: dict[str, Any] = {}
    token = _token()
    key = _api_key()
    if key:
        params["key"] = key
    elif token:
        params["access_token"] = token
    return params


def _api_key_params() -> dict[str, Any] | None:
    key = _api_key()
    return {"key": key} if key else None


def _api_available() -> bool:
    return bool(_api_auth_params())


def _price(cents: Any) -> str | None:
    if not isinstance(cents, int):
        return None
    return f"¥{cents / 100:.2f}" if cents >= 0 else None


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)  # RSS/JSON 里常见 &lt;p&gt; 这种转义，先还原再剥标签
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- 网络自检


POISON_HINTS = ("157.240.", "31.13.", "59.24.", "8.7.198.", "8.8.4.")


def net_check() -> dict[str, Any]:
    cfg = config.load()["network"]
    hosts = {
        "store.steampowered.com": "/api/appdetails?appids=730",
        "steamcommunity.com": "/",
        "api.steampowered.com": "/ISteamWebAPIUtil/GetServerInfo/v1/",
        "login.steampowered.com": "/jwt/finalizelogin",
        "help.steampowered.com": "/",
    }
    results = []
    for host, path in hosts.items():
        item: dict[str, Any] = {"host": host}
        try:
            item["ip"] = socket.gethostbyname(host)
        except OSError as exc:
            item["ip"] = None
            item["verdict"] = "dns_failed"
            item["detail"] = str(exc)
            results.append(item)
            continue

        if item["ip"].startswith("127.") or item["ip"] == "0.0.0.0":
            item["note"] = "本地反代已接管（hosts 指向本机），符合 Steamcommunity 302 的工作方式"
        elif host.endswith("steamcommunity.com") and item["ip"].startswith(POISON_HINTS):
            item["note"] = "解析到境外无关 IP，疑似 DNS 污染"

        started = time.time()
        try:
            resp = http.request("GET", f"https://{host}{path}", allow_redirects=False)
            item["status"] = resp.status
            item["ms"] = int((time.time() - started) * 1000)
            item["verdict"] = "ok" if resp.status < 400 or resp.status in (301, 302, 303, 405) else "http_error"
        except http.SteamError as exc:
            item["status"] = None
            item["ms"] = int((time.time() - started) * 1000)
            item["verdict"] = exc.code
            item["detail"] = exc.message
        results.append(item)

    ok = [r for r in results if r.get("verdict") == "ok"]
    return {
        "ok": True,
        "network_mode": cfg.get("mode"),
        "proxy": cfg.get("proxy"),
        "tls_trust": cfg.get("tls_trust"),
        "reachable": len(ok),
        "total": len(results),
        "hosts": results,
        "advice": _advice(results),
    }


def _advice(results: Iterable[dict[str, Any]]) -> list[str]:
    tips = []
    for item in results:
        verdict = item.get("verdict")
        if verdict == "ok":
            continue
        host = item["host"]
        if host == "steamcommunity.com":
            tips.append("steamcommunity.com 不通：启动 Steamcommunity 302 / Watt Toolkit 的本地反代，再重跑自检。")
        elif host == "api.steampowered.com":
            tips.append("api.steampowered.com 不通：成就、新闻等 Web API 接口受影响，可配置代理（network.mode=custom）。")
        elif verdict == "blocked" and "代理" in (item.get("detail") or ""):
            tips.append(f"{host} 走代理失败：若已启用本地反代，把 network.mode 改成 direct。")
    if not tips:
        tips.append("全部域名可达。")
    return tips


# ---------------------------------------------------------------- 用户与档案


def resolve_steamid(value: str | None) -> str:
    """接受 steamID64 / 个人链接 / 自定义 URL 名 / "me"。"""
    if not value or value.lower() in ("me", "self", "我"):
        sid = auth.steamid()
        if not sid:
            raise http.NotLoggedIn("当前未登录，拿不到自己的 SteamID。")
        return sid

    text = value.strip()
    if text.isdigit() and len(text) == 17:
        return text

    match = re.search(r"/profiles/(\d{17})", text)
    if match:
        return match.group(1)

    vanity = text
    match = re.search(r"steamcommunity\.com/id/([^/?#]+)", text)
    if match:
        vanity = match.group(1)

    key_params = _api_key_params()
    if key_params:
        try:
            resp = http.get_json(
                f"{API}/ISteamUser/ResolveVanityURL/v1/",
                params={**key_params, "vanityurl": vanity},
            )
            body = resp.get("response", {})
            if body.get("success") == 1 and body.get("steamid"):
                return body["steamid"]
        except http.SteamError:
            pass

    try:
        xml = http.get_text(f"{COMMUNITY}/id/{vanity}?xml=1")
        match = re.search(r"<steamID64>(\d{17})</steamID64>", xml)
        if match:
            return match.group(1)
    except http.SteamError as exc:
        raise http.Rejected(
            f"无法解析 '{value}' 对应的 SteamID。",
            code="resolve_failed",
        ) from exc
    raise http.Rejected(f"无法解析 '{value}' 对应的 SteamID。", code="resolve_failed")


def _summaries_from_api(steamids: list[str]) -> list[dict[str, Any]] | None:
    params = _api_key_params()
    if not params:
        return None
    try:
        resp = http.get_json(
            f"{API}/ISteamUser/GetPlayerSummaries/v2/",
            params={**params, "steamids": ",".join(steamids)},
        )
    except http.SteamError:
        return None
    players = (resp.get("response") or {}).get("players") or []
    return [_normalize_api_player(p) for p in players]


def _normalize_api_player(p: dict[str, Any]) -> dict[str, Any]:
    state_map = {0: "离线", 1: "在线", 2: "忙碌", 3: "离开", 4: "打盹", 5: "想交易", 6: "想玩游戏"}
    return {
        "steamid": p.get("steamid"),
        "name": p.get("personaname"),
        "profile_url": p.get("profileurl"),
        "avatar": p.get("avatarfull"),
        "status": state_map.get(p.get("personastate", 0), "未知"),
        "in_game": p.get("gameextrainfo"),
        "game_id": p.get("gameid"),
        "country": p.get("loccountrycode"),
        "created": p.get("timecreated"),
        "last_logoff": p.get("lastlogoff"),
        "real_name": p.get("realname"),
        "source": "api",
    }


def _summary_from_xml(steamid: str) -> dict[str, Any]:
    xml = http.get_text(f"{COMMUNITY}/profiles/{steamid}?xml=1")

    state_map = {"offline": "离线", "online": "在线", "in-game": "游戏中", "away": "离开", "snooze": "打盹"}
    raw_state = (re.search(r"<onlineState>(.*?)</onlineState>", xml, re.DOTALL) or [None, ""])[1]
    raw_state = _clean(raw_state).lower()

    def tag(name: str) -> str | None:
        m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", xml, re.DOTALL)
        return _clean(m.group(1)) if m and m.group(1) else None

    return {
        "steamid": tag("steamID64") or steamid,
        "name": tag("steamID"),
        "profile_url": tag("steamID64") and f"{COMMUNITY}/profiles/{steamid}/",
        "avatar": tag("avatarFull"),
        "status": state_map.get(raw_state, raw_state or None),
        "state_message": tag("stateMessage"),
        "real_name": tag("realname"),
        "summary": tag("summary"),
        "location": tag("location"),
        "member_since": tag("memberSince"),
        "privacy": tag("privacyState"),
        "vac_banned": tag("vacBanned"),
        "source": "community_xml",
    }


def profile(value: str | None = None) -> dict[str, Any]:
    steamid = resolve_steamid(value)
    players = _summaries_from_api([steamid])
    if players:
        out = players[0]
    else:
        out = _summary_from_xml(steamid)
    out["level"] = _level(steamid)
    return {"ok": True, "profile": out}


def _level(steamid: str) -> int | None:
    if _api_available():
        try:
            resp = http.get_json(
                f"{API}/IPlayerService/GetSteamLevel/v1/",
                params={**_api_auth_params(), "steamid": steamid},
            )
            level = (resp.get("response") or {}).get("player_level")
            if isinstance(level, int):
                return level
        except http.SteamError:
            pass
    try:
        html = http.get_text(f"{COMMUNITY}/profiles/{steamid}/badges/?l={_lang()}")
        match = re.search(r'class="profile_xp_block_level".*?(\d+)', html, re.DOTALL)
        if match:
            return int(match.group(1))
        match = re.search(r'profile_count_link_total[^>]*>\s*(\d+)', html)
        if match:
            return int(match.group(1))
    except http.SteamError:
        pass
    return None


def me() -> dict[str, Any]:
    auth.require_login()
    steamid = auth.steamid()
    data = profile(steamid)["profile"]
    data["account"] = auth.state().get("account")
    return {"ok": True, "me": data}


# ---------------------------------------------------------------- 游戏库


def _owned_appids(steamid: str) -> list[int] | None:
    try:
        resp = http.get_json(
            f"{STORE}/dynamicstore/userdata/",
            params={"id": steamid, "cc": _cc(), "l": _lang()},
            cookies=auth.cookies_for("store.steampowered.com"),
        )
    except http.SteamError:
        return None
    if isinstance(resp, dict):
        apps = resp.get("rgOwnedApps") or []
        if apps:
            return [int(a) for a in apps]
    return None


APP_CACHE_TTL = 600  # appdetails 缓存 10 分钟：批量任务（deal 扫描/库存估值）能砍掉大量重复请求


def _app_cache_path(appid: int | str) -> str:
    d = str(config.home_dir() / "cache" / "appdetails")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_lang()}_{_cc()}_{appid}.json")


def _appdetails_raw(appid: int | str) -> dict[str, Any] | None:
    """取单个 appdetails 的 data 节点，带 10 分钟本地缓存。

    缓存键带 language + cc：同一 appid 在不同区/语言下价格与描述不同，不能共用。
    """
    path = _app_cache_path(appid)
    try:
        if time.time() - os.path.getmtime(path) < APP_CACHE_TTL:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass

    try:
        resp = http.get_json(
            f"{STORE}/api/appdetails",
            params={"appids": str(appid), "l": _lang(), "cc": _cc()},
        )
    except http.SteamError:
        return None
    node = resp.get(str(appid)) if isinstance(resp, dict) else None
    if not isinstance(node, dict) or not node.get("success"):
        return None
    data = node.get("data") or {}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass  # 缓存写不进去不影响主流程
    return data


def _appdetails_batch(appids: list[int]) -> dict[int, dict[str, Any]]:
    """appdetails 只接受单个 appid（逗号批量会 400），只能逐个取。

    命中 10 分钟缓存时不发请求；未命中的仍受 store 0.4s/次限速。
    """
    out: dict[int, dict[str, Any]] = {}
    for appid in appids[:60]:  # 硬上限，防止大列表把限速打满
        data = _appdetails_raw(appid)
        if data:
            out[appid] = data
    return out


def library(
    value: str | None = None,
    *,
    search: str | None = None,
    limit: int | None = None,
    with_details: bool = True,
) -> dict[str, Any]:
    steamid = resolve_steamid(value)
    limit = limit or _limit()
    warnings: list[str] = []

    games: list[dict[str, Any]] = []
    source = "api"
    if _api_available():
        try:
            resp = http.get_json(
                f"{API}/IPlayerService/GetOwnedGames/v1/",
                params={
                    **_api_auth_params(),
                    "steamid": steamid,
                    "include_appinfo": 1,
                    "include_played_free_games": 1,
                    "skip_unvetted_apps": 0,
                },
            )
            for g in (resp.get("response") or {}).get("games", []) or []:
                games.append(
                    {
                        "appid": g.get("appid"),
                        "name": g.get("name"),
                        "minutes": g.get("playtime_forever"),
                        "minutes_2weeks": g.get("playtime_2weeks", 0),
                        "last_played": g.get("rtime_last_played"),
                    }
                )
        except http.SteamError:
            games = []

    if not games:
        source = "dynamicstore"
        appids = _owned_appids(steamid)
        if not appids:
            raise http.Rejected(
                "拿不到游戏库。可能游戏详情设为私密，或该账号没有公开游戏。",
                code="library_unavailable",
            )
        details = _appdetails_batch(appids[:limit]) if with_details else {}
        if with_details and not details:
            warnings.append("store 详情获取失败，只返回了 appid。")
        for appid in appids:
            d = details.get(appid, {})
            games.append(
                {
                    "appid": appid,
                    "name": d.get("name"),
                    "minutes": None,
                    "release_date": (d.get("release_date") or {}).get("date"),
                    "genres": [g.get("description") for g in d.get("genres", [])][:3],
                    "price": (d.get("price_overview") or {}).get("final_formatted"),
                }
            )
        warnings.append("未取到游玩时长：该字段需要 api.steampowered.com（IPlayerService）。")

    if search:
        needle = search.lower()
        games = [g for g in games if needle in (g.get("name") or "").lower()]

    games.sort(key=lambda g: (g.get("minutes") or 0), reverse=True)
    total = len(games)
    return {
        "ok": True,
        "steamid": steamid,
        "total": total,
        "returned": min(total, limit),
        "source": source,
        "games": games[:limit],
        "warnings": warnings,
    }


def recent_games(value: str | None = None) -> dict[str, Any]:
    steamid = resolve_steamid(value)
    if not _api_available():
        raise http.Rejected(
            "最近游玩需要 api.steampowered.com（IPlayerService/GetRecentlyPlayedGames）。",
            code="api_unavailable",
        )
    resp = http.get_json(
        f"{API}/IPlayerService/GetRecentlyPlayedGames/v1/",
        params={**_api_auth_params(), "steamid": steamid, "count": 20},
    )
    games = [
        {
            "appid": g.get("appid"),
            "name": g.get("name"),
            "minutes_2weeks": g.get("playtime_2weeks"),
            "minutes_total": g.get("playtime_forever"),
        }
        for g in (resp.get("response") or {}).get("games", []) or []
    ]
    return {"ok": True, "steamid": steamid, "count": len(games), "games": games}


# ---------------------------------------------------------------- 商店


def store_search(term: str, limit: int = 15) -> dict[str, Any]:
    if not term.strip():
        raise http.SteamError("搜索词为空", code="bad_input")
    resp = http.get_json(
        f"{STORE}/api/storesearch/",
        params={"term": term, "l": _lang(), "cc": _cc()},
    )
    items = []
    for item in (resp.get("items") or [])[:limit]:
        price = item.get("price") or {}
        items.append(
            {
                "appid": item.get("id"),
                "name": item.get("name"),
                "type": item.get("type"),
                "price": price.get("final_formatted"),
                "currency": price.get("currency"),
                "platforms": item.get("platforms"),
            }
        )
    return {"ok": True, "total": resp.get("total"), "returned": len(items), "items": items}


def app_details(appid: int | str, *, include_raw: bool = False) -> dict[str, Any]:
    d = _appdetails_raw(appid)  # 10 分钟缓存，见 _appdetails_raw
    if not d:
        raise http.Rejected(f"查不到 appid={appid} 的商店信息。", code="not_found")
    brief = {
        "appid": d.get("steam_appid"),
        "name": d.get("name"),
        "type": d.get("type"),
        "is_free": d.get("is_free"),
        "short_description": d.get("short_description"),
        "release_date": (d.get("release_date") or {}).get("date"),
        "coming_soon": (d.get("release_date") or {}).get("coming_soon"),
        "developers": d.get("developers"),
        "publishers": d.get("publishers"),
        "genres": [g.get("description") for g in d.get("genres", [])],
        "categories": [c.get("description") for c in d.get("categories", [])][:8],
        "platforms": d.get("platforms"),
        "metacritic": (d.get("metacritic") or {}).get("score"),
        "recommendations": (d.get("recommendations") or {}).get("total"),
        "price": (d.get("price_overview") or {}).get("final_formatted"),
        "discount_percent": (d.get("price_overview") or {}).get("discount_percent"),
        "header_image": d.get("header_image"),
        "dlc": d.get("dlc"),
    }
    if include_raw:
        brief["_raw"] = d
    return {"ok": True, "app": brief}


# ---------------------------------------------------------------- 愿望单


def wishlist(value: str | None = None, limit: int | None = None) -> dict[str, Any]:
    steamid = resolve_steamid(value)
    limit = limit or _limit()
    jar = auth.cookies_for("store.steampowered.com")

    # 首选：wishlistdata（公开愿望单/官方会话下直接出 JSON）
    try:
        resp = http.request(
            "GET",
            f"{STORE}/wishlist/profiles/{steamid}/wishlistdata/",
            params={"p": 0, "l": _lang()},
            cookies=jar,
        )
        if resp.status == 200 and resp.text.strip().startswith("{"):
            body = resp.json()
            items = []
            for appid, node in body.items():
                if not appid.isdigit() or not isinstance(node, dict):
                    continue
                subs = node.get("subs") or []
                price = subs[0].get("price") if subs and isinstance(subs[0], dict) else None
                items.append(
                    {
                        "appid": int(appid),
                        "name": node.get("name"),
                        "price_cents": price,
                        "review": node.get("review_desc"),
                        "release": node.get("release_string"),
                        "priority": node.get("priority"),
                    }
                )
            items.sort(key=lambda i: i.get("priority") or 9999)
            return {"ok": True, "steamid": steamid, "total": len(items), "source": "wishlistdata", "items": items[:limit]}
    except http.SteamError:
        pass

    # 回落：dynamicstore 的 rgWishlist（只给 appid，自己拼详情）
    user_data = http.get_json(
        f"{STORE}/dynamicstore/userdata/",
        params={"id": steamid, "cc": _cc(), "l": _lang()},
        cookies=jar,
    )
    ids = [int(a) for a in (user_data.get("rgWishlist") or []) if str(a).isdigit()] if isinstance(user_data, dict) else []
    if not ids:
        raise http.Rejected("愿望单为空，或该账号愿望单设为私密。", code="wishlist_unavailable")
    details = _appdetails_batch(ids[:limit])
    items = []
    for idx, appid in enumerate(ids):
        d = details.get(appid, {})
        items.append(
            {
                "appid": appid,
                "name": d.get("name"),
                "price_cents": (d.get("price_overview") or {}).get("final"),
                "review": None,
                "release": (d.get("release_date") or {}).get("date"),
                "priority": idx + 1,
            }
        )
    return {
        "ok": True,
        "steamid": steamid,
        "total": len(items),
        "source": "dynamicstore",
        "warnings": ["愿望单详情由 dynamicstore + 商店批量详情拼成，只含前若干条的名称/价格。"],
        "items": items[:limit],
    }


def wishlist_edit(action: str, appid: int) -> dict[str, Any]:
    auth.require_login()
    auth.warm_session()  # 写操作必须带 Steam 下发的 sessionid
    action = action.lower()
    endpoints = {
        "add": f"{STORE}/api/addtowishlist",
        "remove": f"{STORE}/api/removefromwishlist",
    }
    if action not in endpoints:
        raise http.SteamError("action 只能是 add 或 remove", code="bad_input")
    resp = http.request(
        "POST",
        endpoints[action],
        data={"appid": int(appid), "sessionid": auth.sessionid()},
        cookies=auth.cookies_for("store.steampowered.com"),
    )
    if resp.status != 200:
        raise http.Rejected(f"愿望单{action}失败：HTTP {resp.status} {resp.text[:160]}")
    try:
        body = resp.json()
    except http.SteamError:
        body = {"raw": resp.text[:160]}
    ok = bool(body.get("success"))
    out: dict[str, Any] = {"ok": ok, "action": action, "appid": appid, "response": body}
    if not ok:
        # Steam 返回 200 + success:false 且 wishlistCount 不变，几乎总是这两种「本来就不该改」的情况：
        #   1. add 一个已在愿望单里的游戏
        #   2. add 一个已拥有的游戏
        # 两者都在 Steam 侧静默拒绝，返回结构和「真的失败」一模一样。
        # 别把它当成 Steam 故障——先查状态，用 IWishlistService/GetWishlist + GetOwnedGames 对照。
        out["error"] = "wishlist_noop"
        out["hint"] = (
            "Steam 返回 200 但 success=false，wishlistCount 未变——通常不是故障，而是这次操作本来就没意义："
            "add 时该游戏已在愿望单里，或该游戏已拥有（已拥有不能加愿望单）；remove 时它本来就不在。"
            "先核对现状再重试：GetWishlist 看是否已在愿望单、GetOwnedGames 看是否已拥有。"
        )
    return out


# ---------------------------------------------------------------- 社区


def _resolve_users(steamids: list[str]) -> dict[str, dict[str, Any]]:
    """社区 JSON 批量解析用户名/头像，比抓 HTML 稳。"""
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(steamids), 100):
        chunk = steamids[start : start + 100]
        try:
            resp = http.get_json(
                f"{COMMUNITY}/actions/ajaxresolveusers",
                params={"steamids": ",".join(chunk)},
                cookies=auth.cookies_for("steamcommunity.com"),
            )
        except http.SteamError:
            continue
        if not isinstance(resp, list):
            continue
        for user in resp:
            sid = user.get("steamid")
            if sid:
                avatar = user.get("avatar_url")
                out[sid] = {
                    "name": user.get("persona_name"),
                    "avatar": f"https://avatars.steamstatic.com/{avatar}.jpg" if avatar and "http" not in avatar else avatar,
                }
    return out


def friends(value: str | None = None, limit: int | None = None) -> dict[str, Any]:
    steamid = resolve_steamid(value)
    limit = limit or _limit()
    resp = http.request(
        "GET",
        f"{COMMUNITY}/profiles/{steamid}/friends/?l={_lang()}",
        cookies=auth.cookies_for("steamcommunity.com"),
        allow_redirects=True,  # /profiles/<id>/friends 会跳到自定义 URL，属正常
    )
    if resp.looks_like_login_page():
        raise http.NotLoggedIn(
            "好友列表页要求登录，当前会话无效。",
            hint="先登录：steam_auth_import_cookie / steam_auth_login，登录后重试。",
        )
    html_text = resp.text

    # 真实结构：<div class="selectable friend_block_v2 persona in-game" data-steamid="..." data-search="名字 ; 游戏 ; ">
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(r'<div\b[^>]*class="[^"]*friend_block_v2[^"]*"[^>]*>', html_text):
        tag = m.group(0)
        m_sid = re.search(r'data-steamid="(\d{17})"', tag)
        if not m_sid or m_sid.group(1) in seen:
            continue
        seen.add(m_sid.group(1))
        m_name = re.search(r'data-search="([^";]*)', tag)
        m_state = re.search(r'class="[^"]*\bpersona\s+([a-zA-Z-]+)', tag)
        name = html_lib.unescape(m_name.group(1)).strip() if m_name else None
        entries.append({"steamid": m_sid.group(1), "name": name or None, "state": m_state.group(1) if m_state else None})

    if not entries:
        private = "friend" in html_text.lower() and "private" in html_text.lower()
        return {
            "ok": True,
            "steamid": steamid,
            "count": 0,
            "source": "community_html",
            "warnings": [
                "好友列表为空或不可见（可能是隐私设置），也可能是页面结构变了。"
                + (" 页面提示好友列表为私密。" if private else "")
            ],
            "friends": [],
        }

    items = entries[:limit]
    # data-search 已带名字；只有缺名字时才追加一次 ajaxresolveusers（该接口限流很凶）
    if any(not i.get("name") for i in items):
        resolved = _resolve_users([i["steamid"] for i in items])
        for item in items:
            extra = resolved.get(item["steamid"]) or {}
            item["name"] = item["name"] or extra.get("name")
            item["avatar"] = extra.get("avatar")
    return {"ok": True, "steamid": steamid, "count": len(items), "total_on_page": len(entries), "source": "community_html", "friends": items}


def achievements(appid: int, value: str | None = None, limit: int = 40) -> dict[str, Any]:
    steamid = resolve_steamid(value)

    # 首选：Web API（要 key，access_token 不认）
    key_params = _api_key_params()
    if key_params:
        resp = http.get_json(
            f"{API}/ISteamUserStats/GetPlayerAchievements/v1/",
            params={**key_params, "steamid": steamid, "appid": appid, "l": _lang()},
        )
        body = resp.get("playerstats") or {}
        if not body.get("success"):
            raise http.Rejected(f"成就不可用：{body.get('error') or '游戏无成就或档案私密'}")
        list_ = body.get("achievements") or []
        unlocked = [a for a in list_ if a.get("achieved")]
        return {
            "ok": True,
            "steamid": steamid,
            "appid": appid,
            "game": body.get("gameName"),
            "source": "api",
            "total": len(list_),
            "unlocked": len(unlocked),
            "percent": round(len(unlocked) / len(list_) * 100, 1) if list_ else 0,
            "achievements": [
                {
                    "api_name": a.get("apiname"),
                    "achieved": bool(a.get("achieved")),
                    "unlock_time": a.get("unlocktime") or None,
                }
                for a in list_[:limit]
            ],
        }

    # 回落：社区成就页（只能拿到已解锁 + 时间，没有名称和总数）
    resp = http.request(
        "GET",
        f"{COMMUNITY}/profiles/{steamid}/achievements/{appid}?l={_lang()}",
        cookies=auth.cookies_for("steamcommunity.com"),
        allow_redirects=True,
    )
    if resp.looks_like_login_page() or resp.status != 200:
        raise http.NotLoggedIn("成就页要求登录或该页面不可见。")
    page = html_lib.unescape(resp.text)
    found = re.findall(
        r'internal_key\\":(\d+),\\"unlocked\\":(true|false)(?:,\\"unlock_time\\":(\d+))?',
        page,
    )
    unlocked = [{"internal_key": int(k), "unlock_time": int(t) if t else None} for k, flag, t in found if flag == "true"]

    # 社区页其实带总数，只是散在两种文案里：
    #   中文「成就：已解锁 1 项，共 1 项」/「1 项成就已解锁，共 1 项（占 100%）」
    #   英文「1 of 1 achievements unlocked」
    total_n, unlocked_n = None, len(unlocked)
    for pat in (
        r"已解锁\s*(\d+)\s*项[，,]\s*共\s*(\d+)\s*项",
        r"(\d+)\s*项成就已解锁[，,]\s*共\s*(\d+)\s*项",
        r"(\d+)\s+of\s+(\d+)\s+achievements",
    ):
        m = re.search(pat, page)
        if m:
            unlocked_n, total_n = int(m.group(1)), int(m.group(2))
            break

    out: dict[str, Any] = {
        "ok": True,
        "steamid": steamid,
        "appid": appid,
        "source": "community_page",
        "total": total_n,
        "unlocked": unlocked_n,
        "achievements": unlocked[:limit],
    }
    if total_n:
        out["percent"] = round(unlocked_n / total_n * 100, 1)
    else:
        out["warnings"] = [
            "页面里没解析到成就总数（可能该游戏无成就或页面改版）；"
            "精确数据需 Web API Key：steam_config_set(webapi_key=\"...\")。",
        ]
    return out


def inventory(steamid: str | None = None, appid: int = 753, contextid: str = "6", limit: int | None = None) -> dict[str, Any]:
    target = steamid or auth.steamid()
    if not target:
        raise http.NotLoggedIn("需要 SteamID 才能查库存。")
    limit = limit or _limit()
    resp = http.get_json(
        f"{COMMUNITY}/inventory/{target}/{appid}/{contextid}",
        params={"l": _lang(), "count": 2000},
        cookies=auth.cookies_for("steamcommunity.com"),
    )
    if not isinstance(resp, dict) or resp.get("success") in (0, False):
        msg = (resp or {}).get("Error") if isinstance(resp, dict) else None
        raise http.Rejected(f"库存不可访问：{msg or '私密或不存在'}", code="inventory_unavailable")
    descs = {}
    for d in resp.get("descriptions", []) or []:
        descs[f"{d.get('classid')}_{d.get('instanceid')}"] = d
    items = []
    for asset in (resp.get("assets") or [])[:limit]:
        d = descs.get(f"{asset.get('classid')}_{asset.get('instanceid')}", {})
        items.append(
            {
                "assetid": asset.get("assetid"),
                "appid": asset.get("appid"),
                "name": d.get("name"),
                "market_hash_name": d.get("market_hash_name"),
                "type": d.get("type"),
                "marketable": d.get("marketable"),
                "tradable": d.get("tradable"),
                "icon": d.get("icon_url"),
            }
        )
    return {
        "ok": True,
        "steamid": target,
        "appid": appid,
        "contextid": contextid,
        "total": resp.get("total_inventory_count"),
        "returned": len(items),
        "items": items,
    }


def market_price(appid: int, hash_name: str, currency: str = "CNY") -> dict[str, Any]:
    code = CURRENCY_CODES.get(currency.upper(), 23)
    resp = http.get_json(
        f"{COMMUNITY}/market/priceoverview/",
        params={"appid": appid, "currency": code, "market_hash_name": hash_name, "l": _lang()},
        cookies=auth.cookies_for("steamcommunity.com"),
    )
    if not isinstance(resp, dict) or not resp.get("success"):
        raise http.Rejected(f"取不到 {hash_name} 的价格（名称需与市场完全一致）。", code="not_found")
    return {
        "ok": True,
        "appid": appid,
        "market_hash_name": hash_name,
        "currency": currency.upper(),
        "lowest_price": resp.get("lowest_price"),
        "median_price": resp.get("median_price"),
        "volume": resp.get("volume"),
    }


def market_search(appid: int, query: str = "", limit: int = 10) -> dict[str, Any]:
    try:
        resp = http.get_json(
            f"{COMMUNITY}/market/search/render/",
            params={
                "query": query,
                "start": 0,
                "count": limit,
                "search_descriptions": 0,
                "sort_column": "popular",
                "sort_dir": "desc",
                "appid": appid,
                "norender": 1,
            },
            cookies=auth.cookies_for("steamcommunity.com"),
        )
    except http.SteamError as exc:
        # 2026-11 起 /market/search/render/ 对所有参数组合都返回 404 HTML，
        # Steam 没给替代的公开接口。直接说清楚，别让调用方以为是自己参数写错了。
        if exc.code == "bad_payload" and "404" in str(exc):
            return {
                "ok": False,
                "error": "market_search_retired",
                "appid": appid,
                "hint": "搜索接口 /market/search/render/ 已被 Steam 下线（404）。改用 steam_market_price（需完整 market_hash_name）或 steam_buff_search（按关键词）。",
            }
        raise
    results = []
    for r in (resp.get("results") or [])[:limit]:
        sell = r.get("sell_price")
        results.append(
            {
                "hash_name": r.get("hash_name"),
                "name": r.get("name"),
                "sell_listings": r.get("sell_listings"),
                "sell_price": _price(sell) if isinstance(sell, int) else sell,
                "sell_price_cents": sell,
                "icon": r.get("asset_description", {}).get("icon_url") if isinstance(r.get("asset_description"), dict) else None,
            }
        )
    return {"ok": True, "appid": appid, "total": resp.get("total_count"), "items": results}


def news(appid: int, count: int = 5) -> dict[str, Any]:
    key_params = _api_key_params()
    if key_params:
        try:
            resp = http.get_json(
                f"{API}/ISteamNews/GetNewsForApp/v2/",
                params={**key_params, "appid": appid, "count": count, "maxlength": 600, "l": _lang()},
            )
            items = []
            for n in (resp.get("appnews") or {}).get("newsitems", []) or []:
                items.append(
                    {
                        "title": n.get("title"),
                        "date": n.get("date"),
                        "url": n.get("url"),
                        "author": n.get("author"),
                        "contents": _clean(n.get("contents"))[:400],
                    }
                )
            return {"ok": True, "appid": appid, "source": "api", "count": len(items), "items": items}
        except http.SteamError:
            pass

    xml = http.get_text(f"{STORE}/feeds/news/app/{appid}/?cc={_cc()}&l={_lang()}")
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        block = m.group(1)

        def pick(t: str) -> str | None:
            mm = re.search(rf"<{t}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>", block, re.DOTALL)
            return _clean(mm.group(1)) if mm else None

        items.append({"title": pick("title"), "url": pick("link"), "date": pick("pubDate"), "contents": (pick("description") or "")[:300]})
    return {"ok": True, "appid": appid, "source": "store_rss", "count": len(items[:count]), "items": items[:count]}


def reviews(appid: int, limit: int = 10, language: str = "all") -> dict[str, Any]:
    """先试官方 appreviews JSON；被重定向（匿名/网络原因）就回落到商店页里的统计块。"""
    try:
        resp = http.request(
            "GET",
            f"{STORE}/appreviews/{appid}",
            params={"json": 1, "language": language, "filter": "all", "num_per_page": limit, "purchase_type": "all"},
            headers={
                "Referer": f"{STORE}/app/{appid}/",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if resp.status == 200 and resp.text.strip().startswith("{"):
            body = resp.json()
            summary = body.get("query_summary") or {}
            items = []
            for r in (body.get("reviews") or [])[:limit]:
                items.append(
                    {
                        "voted_up": r.get("voted_up"),
                        "text": (r.get("review") or "")[:400],
                        "hours": round((r.get("author") or {}).get("playtime_forever", 0) / 60, 1),
                        "votes_up": r.get("votes_up"),
                        "weighted_vote_score": r.get("weighted_vote_score"),
                        "timestamp": r.get("timestamp_created"),
                    }
                )
            return {
                "ok": True,
                "appid": appid,
                "source": "appreviews_api",
                "summary": {
                    "score_desc": summary.get("review_score_desc"),
                    "total_reviews": summary.get("total_reviews"),
                    "total_positive": summary.get("total_positive"),
                    "total_negative": summary.get("total_negative"),
                },
                "items": items,
            }
    except http.SteamError:
        pass

    return _review_summary_from_page(appid, language)


def _review_summary_from_page(appid: int, language: str) -> dict[str, Any]:
    html = http.get_text(f"{STORE}/app/{appid}/?l={_lang() if language == 'all' else language}&cc={_cc()}")
    pos = re.search(r"summary_num_positive_reviews&quot;:(\d+)", html)
    total = re.search(r"summary_num_reviews&quot;:(\d+)", html)
    desc = re.search(r'class="game_review_summary[^"]*">([^<]+)<', html)
    name = re.search(r'<div id="appHubAppName"[^>]*>([^<]+)<', html)

    if not (total and total.group(1) != "0"):
        raise http.Rejected(
            "拿不到评测数据（可能是成人内容需登录/年龄验证，或该应用无评测）。",
            code="reviews_unavailable",
        )

    total_n = int(total.group(1))
    pos_n = int(pos.group(1)) if pos else None
    recent = re.search(r"过去 30 天内的 ([\d,]+) 篇用户评测中有 (\d+)% 为好评", html)
    return {
        "ok": True,
        "appid": appid,
        "game": _clean(name.group(1)) if name else None,
        "source": "store_page_html",
        "summary": {
            "score_desc": _clean(desc.group(1)) if desc else None,
            "total_reviews": total_n,
            "total_positive": pos_n,
            "positive_percent": round(pos_n / total_n * 100, 1) if pos_n else None,
            "recent_30d": {
                "reviews": int(recent.group(1).replace(",", "")) if recent else None,
                "positive_percent": int(recent.group(2)) if recent else None,
            },
        },
        "items": [],
        "warnings": ["逐条评测需要 appreviews 接口（登录后或代理可用时才有），本次只拿到汇总。"],
    }


# ---------------------------------------------------------------- 购物车


def cart(action: str = "view", appid: int | None = None) -> dict[str, Any]:
    auth.require_login()
    action = action.lower()
    if action == "add":
        auth.warm_session()
    jar = auth.cookies_for("store.steampowered.com")

    if action == "view":
        resp = http.request("GET", f"{STORE}/cart/", cookies=jar)
        if resp.status in (301, 302, 303):
            raise http.NotLoggedIn("购物车页面被重定向到登录页，cookie 可能已失效。")
        lines = []
        for m in re.finditer(r'data-cart-line-item-id="(\d+)"', resp.text):
            lines.append({"line_item_id": m.group(1)})
        return {
            "ok": True,
            "action": "view",
            "line_count": len(lines),
            "line_items": lines,
            "html_length": len(resp.text),
            "warnings": ["购物车结构与 Steam 前端强耦合，detail 字段可能不完整；需要精确数据时用 steam_request 拉整页自行解析。"],
        }

    if appid is None:
        raise http.SteamError("add/remove 需要 appid", code="bad_input")

    if action == "add":
        resp = http.request(
            "POST",
            f"{STORE}/api/addtocart",
            data={"appid": int(appid), "qty": 1, "sessionid": auth.sessionid()},
            cookies=jar,
        )
        body = resp.json() if resp.text.strip().startswith("{") else {"raw": resp.text[:200]}
        return {"ok": bool(body.get("success", 1)), "action": "add", "appid": appid, "response": body}

    raise http.SteamError(
        "购物车移除接口不稳定，未内置。用 steam_request 调用 store 的购物车接口，或直接在客户端操作。",
        code="unsupported",
    )
