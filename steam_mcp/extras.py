"""补充层：徽章 / 市场成交历史 / 库存估值 / 成就完美度。

对应 ROADMAP §3-§4 剩余的四项。全部只读。

实测记录（2026-11-26）：
- badges：`/my/badges` 会 302 到 `/id/<vanity>/badges`，跟随后是标准 HTML（badge_row 结构），
  可以正则解析出 appid / 名称 / 卡牌进度 / 经验值。216 个 badge_row → 72 个真徽章。
- market history：React 页面内嵌 loaderData 只有配置，数据靠 `GET /market/actions?q=QueryMyHistory`
  懒加载；**必须带 Referer: .../market/history**（缺了返回 400 eResult=8），
  `qp=[start,count]` 且 count 只接受 25 或 50。
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import api, auth, http

_NOW = time.time
COMMUNITY = "https://steamcommunity.com"
ACTIONS = f"{COMMUNITY}/market/actions"


# ---------------------------------------------------------------- 公共小工具


def _evidence(url: str, status: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一的可追溯字段：调用方不用重跑就能知道数据从哪来、什么时候抓的。"""
    ev: dict[str, Any] = {"source_url": url, "status": status, "fetched_at": int(_NOW())}
    if extra:
        ev.update(extra)
    return {"evidence": ev}


def _text(seg: str) -> str:
    t = re.sub(r"<script.*?</script>", "", seg, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- 徽章


def badges(limit: int | None = None) -> dict[str, Any]:
    """我的徽章进度：每款游戏的卡牌收集情况 + 经验值 + 可合成状态。"""
    ck = auth.cookies_for("steamcommunity.com")
    if not ck:
        raise http.NotLoggedIn("查徽章需要登录。")

    # 实测坑：`/my/badges` 跟随 302 拿到的页面只有 34 个徽章（裁剪版），
    # 而直接请求 /id/<vanity>/badges 有 72 个。所以要分两步：先拿 Location，再单独请求。
    r0 = http.request("GET", f"{COMMUNITY}/my/badges", cookies=ck)
    target = r0.headers.get("location") or ""
    if not target:
        target = f"{COMMUNITY}/my/badges"
    elif target.startswith("/"):
        target = COMMUNITY + target
    resp = http.request("GET", target, params={"l": api._lang()}, cookies=ck)
    if resp.looks_like_login_page() or resp.status != 200:
        raise http.NotLoggedIn("徽章页要求登录。", hint="先 steam_auth_login 或 steam_auth_import_cookie。")

    page = html_lib.unescape(resp.text)
    parts = re.split(r'(?=<div[^>]*class="badge_row(?:\s[^"]*)?"[^>]*>)', page)

    rows: list[dict[str, Any]] = []
    for seg in parts[1:]:
        appid_m = re.search(r"/gamecards/(\d+)/", seg)
        name_m = re.search(r'class="badge_title">\s*(.*?)\s*<', seg, re.S)
        name = re.sub(r"\s+", " ", name_m.group(1)).strip() if name_m else None
        if name:
            name = html_lib.unescape(name).replace("&nbsp;", "").strip()
        text = _text(seg)
        prog = re.search(r"(\d+)\s*/\s*(\d+)\s*张卡牌", text)
        xp = re.search(r"(\d+)\s*点经验值", text)
        lvl = re.search(r"等级\s*(\d+)", text)
        rows.append({
            "appid": int(appid_m.group(1)) if appid_m else None,
            "name": name,
            "cards_owned": int(prog.group(1)) if prog else None,
            "cards_total": int(prog.group(2)) if prog else None,
            "xp": int(xp.group(1)) if xp else None,
            "level": int(lvl.group(1)) if lvl else None,
        })

    # 同一 appid 可能有多条（普通徽章 + 闪亮徽章），保留但在 summary 里去重
    complete = [r for r in rows if r["cards_owned"] and r["cards_total"] and r["cards_owned"] >= r["cards_total"]]
    partial = [r for r in rows if r["cards_total"] and (r["cards_owned"] or 0) < r["cards_total"]]
    games = [r for r in rows if r["appid"]]
    shown = rows[:limit] if limit else rows

    out: dict[str, Any] = {
        "ok": True,
        "total_badges": len(rows),
        "game_badges": len(games),
        "completed": len(complete),
        "in_progress": len(partial),
        "badges": shown,
    }
    out.update(_evidence(resp.url, resp.status))
    return out


# ---------------------------------------------------------------- 市场成交历史


def market_history(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """我的市场成交历史（买入/卖出的完成记录）。

    count 只接受 25 或 50（实测 5 返回空），offset 需按 25 的倍数递增。
    """
    ck = auth.cookies_for("steamcommunity.com")
    if not ck:
        raise http.NotLoggedIn("查成交历史需要登录。")

    # 实测：count=25 时 offset 必须 >=25（offset=0 会返回 400 eResult=8），
    # 所以首页一律用 count=50，再按 limit 截断。
    count = 50 if offset == 0 else (50 if limit >= 50 else 25)
    qp = json.dumps([int(offset), count], separators=(",", ":"))
    resp = http.request(
        "GET", ACTIONS, params={"q": "QueryMyHistory", "qp": qp},
        cookies=ck,
        headers={
            "Referer": f"{COMMUNITY}/market/history",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    if resp.status != 200:
        raise http.SteamError(
            f"成交历史拿不到（HTTP {resp.status}）",
            code="market_history_unavailable",
            hint="确认已登录且市场资格正常；这条接口要求带 Referer，缺了会返回 400。",
        )
    body = resp.json()
    data = body.get("data") or {}
    if data.get("eResult") not in (1, None):
        raise http.SteamError(
            f"成交历史返回 eResult={data.get('eResult')}",
            code="market_history_unavailable",
            hint="eResult=8 多为参数或权限问题；确认已登录、市场资格正常。",
        )

    events = data.get("rgEvents") or []
    rows = []
    total_received = 0
    for e in events[: max(1, int(limit))]:
        desc = e.get("description") or {}
        purchase = e.get("purchase") or {}
        received = purchase.get("received_amount")
        try:
            received_i = int(received) if received not in (None, "") else None
        except (TypeError, ValueError):
            received_i = None
        if received_i:
            total_received += received_i
        rows.append({
            "event_type": e.get("event_type"),
            "time_event": e.get("time_event"),
            "listing_id": (e.get("listing") or {}).get("listingid"),
            "name": desc.get("name"),
            "market_hash_name": desc.get("market_hash_name"),
            "type": desc.get("type"),
            "appid": desc.get("appid"),
            "received_raw_cents": received_i,
            "received_currency": purchase.get("received_currencyid"),
        })

    out: dict[str, Any] = {
        "ok": True,
        "offset": offset,
        "returned": len(rows),
        "has_more": bool(data.get("bMore")),
        "sum_received_raw_cents": total_received,
        "currency_note": "received_raw_cents 是最小货币单位（分/Rp 等），币种见 received_currency，不替你换算。",
        "events": rows,
    }
    out.update(_evidence(resp.url, resp.status, {"query": "QueryMyHistory"}))
    return out


# ---------------------------------------------------------------- 库存估值


def _parse_price(s: Any) -> int | None:
    """从 "Rp 1 234,56" / "¥1.23" 这类字符串里取数值（该币种的原始单位：CNY 是分，IDR 是卢比）。

    ponytail: 只做「去掉非数字字符」的粗解析，遇到 "1.234,56" 这种欧陆千分位会错一位。
    真要精确金额走 market_price 的 raw 字段（待 Steam 提供），别在这里加汇率库。
    """
    if s is None:
        return None
    t = str(s)
    # 先判断小数分隔符：最后一段 1-2 位数字且前面有点或逗号 → 视为小数
    m = re.search(r"(\d[\d\s.,]*)\s*$", t)
    if not m:
        return None
    num = m.group(1).strip()
    dec_sep = None
    if re.search(r"[.,]\d{1,2}$", num):
        dec_sep = num[-3] if len(num) >= 3 else None
    if dec_sep:
        head, _, tail = num.rpartition(dec_sep)
        digits = re.sub(r"\D", "", head) or "0"
        return int(digits) * 100 + int(re.sub(r"\D", "", tail).ljust(2, "0")[:2])
    digits = re.sub(r"\D", "", num)
    return int(digits) if digits else None


def inventory_value(
    top_n: int = 30,
    appid: int = 753,
    contextid: str = "6",
    currency: str = "CNY",
    max_price_checks: int | None = None,
) -> dict[str, Any]:
    """库存估值：按当前市场最低价汇总。

    逐件查价受市场 2s/次限速，所以 top_n 是硬上限，且允许单独限制查价次数。
    """
    inv = api.inventory(appid=appid, contextid=contextid, limit=2000)
    items = inv.get("items") or []
    marketable = [i for i in items if i.get("marketable") and i.get("market_hash_name")]
    checks = max_price_checks if max_price_checks is not None else min(top_n, len(marketable))
    targets = marketable[:checks]

    # ponytail: 串行查价，市场域 2s/次是硬限制；并行只会被限速器排队，反而更难诊断
    priced: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for it in targets:
        try:
            p = api.market_price(appid, it["market_hash_name"], currency=currency)
            cents = _parse_price(p.get("lowest_price"))
            priced.append({
                "name": it.get("name"),
                "market_hash_name": it.get("market_hash_name"),
                "assetid": it.get("assetid"),
                "type": it.get("type"),
                "lowest_price": p.get("lowest_price"),
                "lowest_raw": cents,
                "median_price": p.get("median_price"),
                "volume": p.get("volume"),
            })
        except Exception as exc:  # 单件失败不能拖垮整批
            failed.append({"name": it.get("name"), "error": type(exc).__name__, "message": str(exc)[:160]})

    def _raw(x: dict[str, Any]) -> int:
        v = x.get("lowest_raw")
        return int(v) if isinstance(v, (int, float)) else 0

    total = sum(_raw(x) for x in priced)
    priced.sort(key=_raw, reverse=True)

    out: dict[str, Any] = {
        "ok": True,
        "inventory_total": inv.get("total"),
        "marketable": len(marketable),
        "checked": len(targets),
        "priced": len(priced),
        "failed": len(failed),
        "est_total_raw": total,
        "currency": currency,
        "currency_note": "est_total_raw 按各件最低挂单价累加，不等于能立刻卖出的金额（有手续费和流动性风险）。",
        "top": priced[:top_n],
        "has_more": len(marketable) > len(targets),
    }
    if failed:
        out["sample_failures"] = failed[:5]
    out.update(_evidence(f"{COMMUNITY}/inventory/{inv.get('steamid')}/{appid}/{contextid}", 200))
    return out


# ---------------------------------------------------------------- 成就完美度


def achievement_hunter(
    limit: int = 10,
    min_percent: float = 0.0,
    max_apps: int = 60,
    value: str | None = None,
) -> dict[str, Any]:
    """全库成就完美度：找出「再拿 N 个就全成就」的游戏。

    无 Web API Key 时拿不到 achievements 总数，只能给出已解锁数量并标注 warnings。
    """
    lib = api.library(value, limit=100000)
    games = lib.get("games") or []
    if not games:
        return {"ok": False, "error": "library_unavailable", "hint": "游戏库拿不到，先确认档案公开。"}

    # 只扫玩过的（没玩过的基本 0 成就，扫了也没信息量）
    played = [g for g in games if (g.get("minutes") or 0) > 0]
    scan = played[:max_apps]

    rows: list[dict[str, Any]] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        futs = {
            pool.submit(_safe_achievements, g["appid"], value): g
            for g in scan
        }
        for fut in as_completed(futs):
            g = futs[fut]
            try:
                r = fut.result()
            except Exception:
                errors += 1
                continue
            if not r:
                errors += 1
                continue
            total, unlocked = r.get("total"), r.get("unlocked")
            pct = round(unlocked / total * 100, 1) if total else None
            rows.append({
                "appid": g["appid"],
                "name": g.get("name"),
                "hours": round((g.get("minutes") or 0) / 60, 1),
                "unlocked": unlocked,
                "total": total,
                "percent": pct,
                "remaining": (total - unlocked) if total else None,
                "source": r.get("source"),
            })

    known = [r for r in rows if r["total"]]
    known.sort(key=lambda x: (x["remaining"] if x["remaining"] is not None else 9999, -(x["percent"] or 0)))
    if min_percent:
        known = [r for r in known if (r["percent"] or 0) >= min_percent]

    out: dict[str, Any] = {
        "ok": True,
        "scanned": len(scan),
        "library_played": len(played),
        "resolved": len(rows),
        "errors": errors,
        "has_more": len(played) > len(scan),
        "ranked": known[:limit],
    }
    if not known:
        out["warnings"] = [
            "没有拿到 total（成就总数）。无 Web API Key 时只能读已解锁列表，"
            "先 steam_config_set(webapi_key=...) 或 steam_webapi_key(action=\"set\", ...) 才能算完美度。",
        ]
    out.update(_evidence("https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/", 200))
    return out


def _safe_achievements(appid: int, value: str | None) -> dict[str, Any] | None:
    """单游戏成就摘要；失败返回 None 让上层计数。"""
    try:
        r = api.achievements(appid, value=value, limit=0)
        return r
    except Exception:
        return None
