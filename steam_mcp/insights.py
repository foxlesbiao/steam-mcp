"""数据重组层：只用现有接口/本地缓存做二次加工，不新增不可靠端点。

对应 ROADMAP 第 3 节（G3）。所有函数只读。
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import api, buff, config, http

_NOW = time.time

DECK_URL = "https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport"
DECK_LEVEL = {3: "verified", 2: "playable", 1: "unsupported"}


def _cache_dir() -> str:
    path = str(config.home_dir() / "cache")
    os.makedirs(path, exist_ok=True)
    return path


def _load_cache(name: str, ttl: float) -> dict[str, Any]:
    p = os.path.join(_cache_dir(), name)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if _NOW() - float(data.get("_ts") or 0) < ttl:
            return data.get("rows") or {}
    except (OSError, ValueError):
        pass
    return {}


def _cache_ts(name: str) -> int:
    try:
        with open(os.path.join(_cache_dir(), name), encoding="utf-8") as f:
            return int(json.load(f).get("_ts") or 0)
    except (OSError, ValueError):
        return 0


def _save_cache(name: str, rows: dict[str, Any]) -> None:
    p = os.path.join(_cache_dir(), name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"_ts": _NOW(), "rows": rows}, f, ensure_ascii=False)
    os.replace(tmp, p)


# ---------------------------------------------------------------- 游玩时长重组

def playtime_stats(value: str | None = None, top: int = 20, idle_days: int = 180) -> dict[str, Any]:
    """总时长 / Top N / 近两周 / 吃灰榜 / 久未启动。纯重组 owned games，无额外请求。"""
    lib = api.library(value, limit=100000)
    games = lib.get("games") or []
    if not games:
        return {"ok": False, "error": "library_unavailable", "hint": "游戏库拿不到，先确认档案公开。",
                "steamid": lib.get("steamid")}

    for g in games:
        g["minutes"] = g.get("minutes") or 0
    total_min = sum(g["minutes"] for g in games)
    played = [g for g in games if g["minutes"] > 0]
    never = [g for g in games if g["minutes"] == 0]
    cutoff = _NOW() - idle_days * 86400
    stale = [g for g in played if (g.get("last_played") or 0) and g["last_played"] < cutoff]

    def brief(g: dict[str, Any]) -> dict[str, Any]:
        return {
            "appid": g.get("appid"),
            "name": g.get("name"),
            "hours": round(g["minutes"] / 60, 1),
            "minutes_2weeks": g.get("minutes_2weeks") or 0,
            "last_played": g.get("last_played"),
        }

    return {
        "ok": True,
        "steamid": lib.get("steamid"),
        "summary": {
            "total_games": len(games),
            "played": len(played),
            "never_played": len(never),
            "total_hours": round(total_min / 60, 1),
            "avg_hours_played": round(total_min / 60 / len(played), 1) if played else 0,
        },
        "top": [brief(g) for g in sorted(played, key=lambda g: -g["minutes"])[:top]],
        "recent_2weeks": [
            brief(g) for g in sorted(
                (g for g in games if g.get("minutes_2weeks")), key=lambda g: -(g.get("minutes_2weeks") or 0)
            )[:top]
        ],
        "idle": {"count": len(never), "sample": [brief(g) for g in never[:top]]},
        "stale": {"days": idle_days, "count": len(stale),
                  "sample": [brief(g) for g in sorted(stale, key=lambda g: g.get("last_played") or 0)[:top]]},
        "source": lib.get("source"),
    }


# ---------------------------------------------------------------- Deck 兼容

def deck_compat(appid: int) -> dict[str, Any]:
    """Steam Deck 兼容等级。匿名可访问，无需登录。"""
    resp = http.request("GET", DECK_URL, params={"nAppID": int(appid), "l": config.load()["behavior"].get("language", "schinese")})
    try:
        body = resp.json() or {}
    except http.SteamError:
        return {"appid": appid, "deck": "unknown", "error": "bad_payload"}
    res = body.get("results") or {}
    cat = res.get("resolved_category")
    if cat is None:
        return {"appid": appid, "deck": "no_report",
                "hint": "该 app 没有 Deck 兼容报告（多半不是可玩游戏）。"}
    return {
        "appid": appid,
        "deck": DECK_LEVEL.get(int(cat), f"category_{cat}"),
        "flags": [
            {"type": i.get("display_type"), "token": (i.get("loc_token") or "").lstrip("#")}
            for i in (res.get("resolved_items") or [])
        ],
        "source_url": f"{DECK_URL}?nAppID={int(appid)}",
        "fetched_at": int(_NOW()),
    }


def app_extras(appids: list[int], limit: int = 20) -> dict[str, Any]:
    """批量 Deck 兼容 + 商店资源（封面/截图/预告片）。"""
    ids = [int(a) for a in appids][:limit]
    out: list[dict[str, Any]] = []
    for aid in ids:
        item: dict[str, Any] = {"appid": aid}
        try:
            item.update(deck_compat(aid))
        except http.SteamError as exc:
            item["deck_error"] = exc.code or "request_failed"
        try:
            d = api.app_details(aid)
            app = d.get("app") or {}
            item.update({
                "name": app.get("name"),
                "header_image": app.get("header_image"),
                "platforms": app.get("platforms"),
                "metacritic": app.get("metacritic"),
                "recommendations": app.get("recommendations"),
                "genres": app.get("genres"),
            })
        except http.SteamError as exc:
            item["details_error"] = exc.code or "request_failed"
        out.append(item)
    return {"ok": True, "count": len(out), "items": out}


# ---------------------------------------------------------------- 市场只读

def market_listings(limit: int = 50) -> dict[str, Any]:
    """我当前的在售挂单。只读，不做任何买卖动作。"""
    resp = http.request(
        "GET",
        "https://steamcommunity.com/market/mylistings/",
        params={"norender": 1},
        cookies=api.auth.cookies_for("steamcommunity.com"),
    )
    body = resp.json() or {}
    if not body.get("success") and not body.get("listings"):
        return {"ok": False, "error": "mylistings_unavailable",
                "hint": "市场挂单读不到；确认已登录且 steamcommunity.com 可达。"}

    items: list[dict[str, Any]] = []
    for L in (body.get("listings") or [])[:limit]:
        a = L.get("asset") or {}
        price = L.get("converted_price") or L.get("price") or 0
        fee = L.get("converted_fee") if L.get("converted_fee") is not None else L.get("fee") or 0
        items.append({
            "listingid": L.get("listingid"),
            "appid": a.get("appid"),
            "name": a.get("market_name") or a.get("name"),
            "market_hash_name": a.get("market_hash_name"),
            "type": a.get("type"),
            "amount": int(a.get("amount") or 1),
            "price": round(price / 100, 2),
            "fee": round(fee / 100, 2),
            # 原值一并给出：这两个字段的确切语义（卖家实收 / 买家支付）随版本有出入，不替调用方下结论
            "price_raw_cents": price,
            "fee_raw_cents": fee,
            "currency_id": str(L.get("converted_currencyid") or L.get("currencyid") or ""),
            "active": bool(L.get("active")),
            "status": L.get("status"),
            "created": L.get("time_created_str"),
            "can_reprice_at": L.get("time_can_reprice"),
        })

    confirm = len(body.get("listings_to_confirm") or [])
    hold = len(body.get("listings_on_hold") or [])
    return {
        "ok": True,
        "active_count": body.get("num_active_listings") or len(items),
        "total_count": body.get("total_count"),
        "needs_mobile_confirmation": confirm,
        "on_hold": hold,
        "buy_orders": len(body.get("buy_orders") or []),
        "items": items,
        "note": "只读视图；上架/改价/撤单不在本工具范围内。",
    }


# ---------------------------------------------------------------- 库对比 / 价格快照

def library_diff(a: str | None = None, b: str | None = None, limit: int = 30) -> dict[str, Any]:
    """两人游戏库对比：共同 / 各自独有 / 共同游戏的时长差。"""
    la = api.library(a, limit=100000)
    lb = api.library(b, limit=100000)
    ga = {g["appid"]: g for g in la.get("games") or []}
    gb = {g["appid"]: g for g in lb.get("games") or []}
    if not ga or not gb:
        return {"ok": False, "error": "library_unavailable",
                "hint": "至少一方的游戏库拿不到（可能私密）。"}

    def brief(g: dict[str, Any]) -> dict[str, Any]:
        return {"appid": g.get("appid"), "name": g.get("name"),
                "hours": round((g.get("minutes") or 0) / 60, 1)}

    common = sorted(set(ga) & set(gb))
    common_rows = [brief(ga[i]) | {"hours_other": round((gb[i].get("minutes") or 0) / 60, 1)} for i in common]
    common_rows.sort(key=lambda r: -(r["hours"] + r["hours_other"]))
    return {
        "ok": True,
        "a": {"steamid": la.get("steamid"), "games": len(ga)},
        "b": {"steamid": lb.get("steamid"), "games": len(gb)},
        "common_count": len(common),
        "only_a_count": len(set(ga) - set(gb)),
        "only_b_count": len(set(gb) - set(ga)),
        "common": common_rows[:limit],
        "only_a": [brief(ga[i]) for i in sorted(set(ga) - set(gb), key=lambda i: -(ga[i].get("minutes") or 0))[:limit]],
        "only_b": [brief(gb[i]) for i in sorted(set(gb) - set(ga), key=lambda i: -(gb[i].get("minutes") or 0))[:limit]],
    }


def price_watch(value: str | None = None, *, commit: bool = False, limit: int = 30) -> dict[str, Any]:
    """愿望单价格变化。基于 steam_deals 的缓存做快照 diff，不额外发请求。

    commit=True 才把当前快照写成新的基线；默认纯只读。
    """
    steamid = api.resolve_steamid(value)
    current = _load_cache("deals.json", 10 ** 9)      # 不看 TTL，只取最近一次扫描结果
    if not current:
        return {"ok": False, "error": "no_snapshot",
                "hint": "先跑一次 steam_deals 生成价格缓存，再来比对。"}

    prev = _load_cache("price_snapshots.json", 10 ** 9)
    prev_ts = _cache_ts("price_snapshots.json")

    changes: list[dict[str, Any]] = []
    for aid, cur in current.items():
        if "final" not in cur:
            continue
        old = prev.get(aid)
        if not old or "final" not in old:
            continue
        if old.get("final") != cur.get("final") or old.get("discount") != cur.get("discount"):
            changes.append({
                "appid": cur["appid"],
                "name": cur.get("name"),
                "was": old.get("final"),
                "now": cur.get("final"),
                "delta": round((cur.get("final") or 0) - (old.get("final") or 0), 2),
                "discount_was": old.get("discount"),
                "discount_now": cur.get("discount"),
                "currency": cur.get("currency"),
            })
    changes.sort(key=lambda r: r["delta"])

    if commit:
        _save_cache("price_snapshots.json", current)

    return {
        "ok": True,
        "steamid": steamid,
        "tracked": sum(1 for v in current.values() if "final" in v),
        "changed": len(changes),
        "price_drops": [c for c in changes if c["delta"] < 0][:limit],
        "price_rises": [c for c in changes if c["delta"] > 0][:limit],
        "previous_snapshot": prev_ts or None,
        "committed": commit,
        "hint": None if prev_ts else "还没有基线快照，先调一次 commit=true 建立基线。",
    }


# ---------------------------------------------------------------- ITAD 历史低价

ITAD = "https://api.isthereanydeal.com"


def _itad_key() -> str:
    return (api.auth.state().get("itad_key") or "").strip()


def set_itad_key(key: str) -> dict[str, Any]:
    """保存 ITAD API key（加密进凭证库）。传空字符串等于清除。"""
    from . import secrets
    data = api.auth.state()
    key = (key or "").strip()
    if key:
        data["itad_key"] = key
    else:
        data.pop("itad_key", None)
    secrets.save_credentials(data)
    return {"ok": True, "configured": bool(key), "fingerprint": secrets.fingerprint(key) if key else None,
            "hint": None if key else "已清除 ITAD key。"}


def price_history(appid: int, country: str = "CN") -> dict[str, Any]:
    """某个游戏的 ITAD 历史最低价（SteamDB 的合规替代）。SteamDB 本身 403 不可用。"""
    key = _itad_key()
    if not key:
        return {
            "ok": False,
            "error": "itad_key_missing",
            "hint": "去 https://isthereanydeal.com/apps/my/ 注册应用拿免费 key，"
                    "再调 steam_itad_key(key=\"...\") 写入（限速 1000 次/5 分钟）。",
        }

    lookup = http.get_json(f"{ITAD}/games/lookup/v1", params={"appid": int(appid), "key": key})
    game = (lookup or {}).get("game") or {}
    if not game.get("id"):
        return {"ok": False, "error": "itad_game_not_found",
                "hint": "ITAD 没有这个 appid 的条目（可能不在其收录范围）。", "appid": int(appid)}

    lows = http.request(
        "POST",
        f"{ITAD}/games/historylow/v1",
        params={"key": key, "country": country},
        json=[game["id"]],
    ).json() or []
    low = (lows[0] if lows else {}).get("low") or {}
    price = low.get("price") or {}
    regular = low.get("regular") or {}
    return {
        "ok": True,
        "appid": int(appid),
        "title": game.get("title"),
        "itad_id": game.get("id"),
        "itad_url": f"https://isthereanydeal.com/game/{game.get('slug')}/info/",
        "country": country,
        "low": {
            "amount": price.get("amount"),
            "currency": price.get("currency"),
            "cut": low.get("cut"),
            "shop": (low.get("shop") or {}).get("name"),
            "at": low.get("timestamp"),
        } if price else None,
        "regular": regular.get("amount"),
        "note": "ITAD 按其区域口径返回当地货币；与 Steam 商店区价格可能不同源。数据来自 IsThereAnyDeal API。",
    }


# ---------------------------------------------------------------- 消费/许可历史

def purchase_history(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """账号的许可/购买记录（/account/licenses/ 表格）。只读。"""
    resp = http.request(
        "GET",
        "https://store.steampowered.com/account/licenses/",
        cookies=api.auth.cookies_for("store.steampowered.com"),
        allow_redirects=True,
    )
    text = resp.text
    table = re.search(r'<table class="account_table">(.*?)</table>', text, re.S)
    if not table:
        return {"ok": False, "error": "licenses_unavailable",
                "hint": "许可页面结构变了或未登录；先用 steam_auth_status 确认会话。"}

    rows: list[dict[str, Any]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 3 or not cells[0]:
            continue
        link = re.search(r'href="[^"]*?/(?:app|sub)/(\d+)', tr)
        rows.append({
            "date": cells[0],
            "name": cells[1],
            "source": cells[2],
            "extra": cells[3] if len(cells) > 3 else None,
            "id": int(link.group(1)) if link else None,
        })

    return {
        "ok": True,
        "total": len(rows),
        "returned": len(rows[offset:offset + limit]),
        "items": rows[offset:offset + limit],
        "note": "按页面顺序；金额不在该页，页面只给许可类型。",
    }


# ---------------------------------------------------------------- 跨市场比价

def _yuan(text: str | None) -> float | None:
    """从 '¥ 0.21' / 'Rp 19 649' 这类文案里抠数字。"""
    if not text:
        return None
    s = str(text).replace("\u00a0", " ")
    s = re.sub(r"(?<=\d) (?=\d{3}\b)", "", s)      # 空格千分位：19 649 -> 19649
    nums = re.findall(r"[\d][\d.,]*", s)
    if not nums:
        return None
    raw = nums[-1].replace(",", "")
    if raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def market_compare(hash_name: str, appid: int = 730, game: str = "csgo") -> dict[str, Any]:
    """同一件饰品在 Steam 市场与网易 BUFF 的最低价对比（都是人民币口径）。"""
    out: dict[str, Any] = {"ok": True, "market_hash_name": hash_name, "appid": int(appid)}

    try:
        sp = api.market_price(int(appid), hash_name)
        out["steam"] = {
            "lowest": _yuan(sp.get("lowest_price")),
            "median": _yuan(sp.get("median_price")),
            "volume": sp.get("volume"),
            "raw": sp.get("lowest_price"),
        }
    except http.SteamError as exc:
        out["steam"] = {"error": exc.code or "request_failed", "hint": exc.hint}

    try:
        res = buff.search_goods(hash_name, game=game, limit=20)
        match = next((i for i in res.get("items", [])
                      if (i.get("market_hash_name") or "").lower() == hash_name.lower()), None)
        if match:
            out["buff"] = {
                "goods_id": match.get("goods_id"),
                "min": _yuan(match.get("sell_min_price")),
                "sell_num": match.get("sell_num"),
                "raw": match.get("sell_min_price"),
            }
        else:
            out["buff"] = {"error": "no_exact_match", "candidates": (res.get("items") or [])[:5],
                           "hint": "BUFF 没有同名同磨损项；用候选里的 market_hash_name 再查。"}
    except http.SteamError as exc:
        out["buff"] = {"error": exc.code or "request_failed", "hint": exc.hint}

    s = (out.get("steam") or {}).get("lowest")
    b = (out.get("buff") or {}).get("min")
    if s and b:
        out["cheaper"] = "buff" if b < s else ("steam" if s < b else "same")
        out["diff"] = round(b - s, 2)
        out["diff_pct"] = round((b - s) / s * 100, 1)
    out["note"] = "Steam 侧为市场挂单价，BUFF 为最低在售；跨平台价差受交易冷却与手续费影响，不等于可套利。"
    return out


def _wishlist_appids(steamid: str) -> list[int]:
    """愿望单 appid 列表（IWishlistService 优先，回落 dynamicstore）。"""
    key = (api._api_key() or "").strip()
    if key:
        try:
            resp = http.get_json(
                "https://api.steampowered.com/IWishlistService/GetWishlist/v1/",
                params={"key": key, "steamid": steamid},
            )
            ids = [int(i["appid"]) for i in (resp.get("response") or {}).get("items", []) if i.get("appid")]
            if ids:
                return ids
        except http.SteamError:
            pass
    data = http.get_json(
        "https://store.steampowered.com/dynamicstore/userdata/",
        params={"id": steamid, "cc": api._cc(), "l": api._lang()},
        cookies=api.auth.cookies_for("store.steampowered.com"),
    )
    return [int(a) for a in ((data or {}).get("rgWishlist") or []) if str(a).isdigit()]


def _price_one(appid: int) -> tuple[int, dict[str, Any] | None]:
    try:
        resp = http.request(
            "GET",
            "https://store.steampowered.com/api/appdetails",
            params={"appids": int(appid), "cc": api._cc(), "l": api._lang()},
        )
        node = (resp.json() or {}).get(str(appid)) or {}
        g = node.get("data") or {}
        po = g.get("price_overview")
        if not po:
            return appid, {"appid": int(appid), "name": g.get("name"), "price": None,
                           "coming": (g.get("release_date") or {}).get("coming_soon", False)}
        return appid, {
            "appid": int(appid),
            "name": g.get("name"),
            "discount": po.get("discount_percent") or 0,
            "final": po.get("final", 0) / 100,
            "initial": po.get("initial", 0) / 100,
            "currency": po.get("currency"),
        }
    except http.SteamError:
        return appid, None


def deals(
    value: str | None = None,
    *,
    min_discount: int = 0,
    limit: int = 40,
    max_fetch: int = 120,
    refresh: bool = False,
) -> dict[str, Any]:
    """愿望单折扣雷达。

    Steam 的 appdetails 不支持批量、wishlistdata 要会话，所以只能逐个查；
    结果落 data/cache/deals.json（TTL 30 分钟），未抓完的会如实返回 has_more。
    ponytail: 单轮最多补抓 max_fetch 个，避免工具调用卡到分钟级；多次调用即可扫完。
    """
    steamid = api.resolve_steamid(value)
    ids = _wishlist_appids(steamid)
    if not ids:
        return {"ok": False, "error": "wishlist_unavailable", "hint": "愿望单为空或私密。", "steamid": steamid}

    rows = {} if refresh else _load_cache("deals.json", 1800)
    missing = [a for a in ids if str(a) not in rows]
    batch = missing[:max_fetch]

    if batch:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_price_one, a): a for a in batch}
            for fut in as_completed(futures):
                aid, rec = fut.result()
                if rec is not None:
                    rows[str(aid)] = rec
        _save_cache("deals.json", rows)

    # 只有拿到价格的行才参与折扣统计；无价（未发售/下架）的记录不含 final 字段
    known = [rows[str(a)] for a in ids if str(a) in rows and "final" in rows[str(a)]]
    scanned = sum(1 for a in ids if str(a) in rows)
    disc = [r for r in known if (r.get("discount") or 0) > 0 and (r.get("discount") or 0) >= min_discount]
    disc.sort(key=lambda r: (-(r.get("discount") or 0), r.get("final") or 0))
    dominated = [r for r in disc if (r.get("discount") or 0) >= 100 and (r.get("initial") or 0) > 0]

    return {
        "ok": True,
        "steamid": steamid,
        "wishlist_total": len(ids),
        "scanned": scanned,
        "has_more": bool(missing[len(batch):]),
        "remaining": max(0, len(missing) - len(batch)),
        "discounted": len(disc),
        "free": [{"appid": r["appid"], "name": r["name"]} for r in dominated],
        "items": disc[:limit],
        "cache_ttl_seconds": 1800,
        "hint": None if not missing[len(batch):] else "还有未扫描的条目，再调一次即可继续（结果会累积）。",
    }
