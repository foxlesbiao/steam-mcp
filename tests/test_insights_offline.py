"""重组分析层的离线验证（不联网、不碰真实凭证）。

跑法：./.venv/bin/python tests/test_insights_offline.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["STEAM_MCP_HOME"] = tempfile.mkdtemp(prefix="steam-mcp-insights-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import insights  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


NOW = 1_700_000_000
LIB = {
    "ok": True,
    "steamid": "1",
    "source": "api",
    "games": [
        {"appid": 1, "name": "AAA", "minutes": 6000, "minutes_2weeks": 120, "last_played": NOW - 3600},
        {"appid": 2, "name": "BBB", "minutes": 600, "minutes_2weeks": 0, "last_played": NOW - 400 * 86400},
        {"appid": 3, "name": "堆积如山", "minutes": 0, "minutes_2weeks": 0, "last_played": 0},
        {"appid": 4, "name": "CCC", "minutes": 300, "minutes_2weeks": 60, "last_played": NOW - 86400},
    ],
}


def test_playtime() -> None:
    insights.api.library = lambda *a, **k: LIB
    insights._NOW = lambda: NOW          # 冻结时钟，否则真实时间会把样例全判成久未启动
    r = insights.playtime_stats(top=3, idle_days=180)
    s = r["summary"]
    check("总时长正确", s["total_hours"] == 115.0, str(s))
    check("已玩/吃灰计数", (s["played"], s["never_played"]) == (3, 1), str(s))
    check("Top 按分钟降序", [g["name"] for g in r["top"]][:2] == ["AAA", "BBB"], str(r["top"]))
    check("近两周按分钟降序", [g["name"] for g in r["recent_2weeks"]] == ["AAA", "CCC"], str(r["recent_2weeks"]))
    check("吃灰榜含零时长", [g["name"] for g in r["idle"]["sample"]] == ["堆积如山"], str(r["idle"]))
    check("久未启动只算玩过的", [g["name"] for g in r["stale"]["sample"]] == ["BBB"], str(r["stale"]))


def test_deck() -> None:
    class FakeResp:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    for cat, want in ((3, "verified"), (2, "playable"), (1, "unsupported")):
        insights.http.request = lambda *a, **k: FakeResp({"success": 1, "results": {"resolved_category": cat}})
        # config.load 需要可用，用桩替掉语言读取
        insights.config.load = lambda: {"behavior": {"language": "schinese"}}
        got = insights.deck_compat(730)
        check(f"Deck {cat} -> {want}", got["deck"] == want, str(got))

    insights.http.request = lambda *a, **k: FakeResp({"success": 1, "results": {}})
    check("无报告 -> no_report", insights.deck_compat(999999)["deck"] == "no_report")


def test_deals() -> None:
    insights.api.resolve_steamid = lambda v: "1"
    insights._wishlist_appids = lambda sid: [11, 12, 13, 14, 15]
    prices = {
        11: {"appid": 11, "name": "九折", "discount": 90, "final": 10.0, "initial": 100.0, "currency": "IDR"},
        12: {"appid": 12, "name": "半价", "discount": 50, "final": 50.0, "initial": 100.0, "currency": "IDR"},
        13: {"appid": 13, "name": "限免", "discount": 100, "final": 0.0, "initial": 50.0, "currency": "IDR"},
        14: {"appid": 14, "name": "原价", "discount": 0, "final": 100.0, "initial": 100.0, "currency": "IDR"},
    }
    insights._price_one = lambda aid: (aid, prices.get(aid, {"appid": aid, "name": "x", "price": None}))

    r = insights.deals(max_fetch=3, limit=10, min_discount=0)
    check("首轮只抓 max_fetch 个", r["scanned"] == 3, str(r["scanned"]))
    check("首轮 has_more 为真", r["has_more"] is True, str(r))
    check("剩余数正确", r["remaining"] == 2, str(r["remaining"]))

    r2 = insights.deals(max_fetch=3, limit=10, min_discount=0)
    check("第二轮扫完", r2["scanned"] == 5 and r2["has_more"] is False, str(r2))

    r3 = insights.deals(limit=10, min_discount=60)
    check("min_discount 过滤生效", sorted(i["discount"] for i in r3["items"]) == [90, 100], str(r3["items"]))
    check("按折扣降序", [i["discount"] for i in r3["items"]] == [100, 90], str(r3["items"]))
    check("0 元限免单独列出", [f["name"] for f in r3["free"]] == ["限免"], str(r3["free"]))


def test_market_listings() -> None:
    class FakeResp:
        text = "{}"

        def json(self):
            return {
                "success": 1, "num_active_listings": 2, "total_count": 2,
                "listings_to_confirm": [{"a": 1}], "listings_on_hold": [], "buy_orders": [],
                "listings": [
                    {"listingid": "1", "price": 17900, "fee": 3660, "currencyid": "2010",
                     "active": 1, "status": 1, "time_created_str": "2026-01-01",
                     "asset": {"appid": 730, "name": "SSG 08 | 绿陶", "market_hash_name": "SSG 08 | Green",
                               "type": "步枪", "amount": "1"}},
                ],
            }

    insights.http.request = lambda *a, **k: FakeResp()
    r = insights.market_listings()
    check("挂单数正确", r["active_count"] == 2, str(r))
    check("待手机确认数透出", r["needs_mobile_confirmation"] == 1, str(r))
    it = r["items"][0]
    check("价格换算成分->元", it["price"] == 179.0 and it["fee"] == 36.6, str(it))
    check("原始分值为保留", it["price_raw_cents"] == 17900, str(it))
    check("市场名优先", it["name"] == "SSG 08 | 绿陶", str(it))


def test_library_diff() -> None:
    def fake_lib(value=None, **k):
        if value == "B":
            return {"steamid": "B", "games": [
                {"appid": 1, "name": "共享", "minutes": 600},
                {"appid": 3, "name": "B独有", "minutes": 60},
            ]}
        return {"steamid": "A", "games": [
            {"appid": 1, "name": "共享", "minutes": 1200},
            {"appid": 2, "name": "A独有", "minutes": 300},
        ]}

    insights.api.library = fake_lib
    r = insights.library_diff("A", "B", limit=5)
    check("共同数", r["common_count"] == 1, str(r))
    check("各自独有数", (r["only_a_count"], r["only_b_count"]) == (1, 1), str(r))
    check("共同游戏两边时长都在", r["common"][0]["hours"] == 20.0 and r["common"][0]["hours_other"] == 10.0, str(r["common"]))


def test_price_watch() -> None:
    store: dict[str, dict] = {}

    def fake_load(name, ttl):
        return dict(store.get(name) or {})

    def fake_save(name, rows):
        store[name] = dict(rows)

    insights._load_cache = fake_load
    insights._save_cache = fake_save
    insights._cache_ts = lambda name: 1_700_000_000 if name in store else 0
    insights.api.resolve_steamid = lambda v: "1"

    store["deals.json"] = {
        "11": {"appid": 11, "name": "降价", "final": 50.0, "discount": 50, "currency": "IDR"},
        "12": {"appid": 12, "name": "不变", "final": 100.0, "discount": 0, "currency": "IDR"},
    }
    store["price_snapshots.json"] = {
        "11": {"appid": 11, "name": "降价", "final": 100.0, "discount": 0, "currency": "IDR"},
        "12": {"appid": 12, "name": "不变", "final": 100.0, "discount": 0, "currency": "IDR"},
    }
    r = insights.price_watch()
    check("只报变化项", r["changed"] == 1, str(r))
    check("降价方向正确", r["price_drops"][0]["delta"] == -50.0, str(r["price_drops"]))
    check("默认不写基线", r["committed"] is False, str(r))

    r2 = insights.price_watch(commit=True)
    check("commit 落库", r2["committed"] is True, str(r2))
    r3 = insights.price_watch()
    check("落库后无变化", r3["changed"] == 0 and r3["previous_snapshot"], str(r3))


def test_money_parse() -> None:
    cases = {"¥ 0.21": 0.21, "Rp 19 649": 19649.0, "¥ 1,234.56": 1234.56,
             "CNY 99": 99.0, "": None, None: None}
    for raw, want in cases.items():
        got = insights._yuan(raw)
        check(f"金额解析 {raw!r}", got == want, f"got={got}")


def test_purchase_history() -> None:
    html = """<table class="account_table"><tr><th>日期</th><th>项目</th><th>类型</th></tr>
    <tr><td>2026 年 11 月 25 日</td><td><a href="https://store.steampowered.com/app/274170/">火线迈阿密2：空号</a></td><td>Steam 商店</td></tr>
    <tr><td>2026 年 7 月 16 日</td><td>Wuthering Waves
        </td><td>免费赠送</td></tr>
    </table>"""

    class FakeResp:
        text = html

    insights.http.request = lambda *a, **k: FakeResp()
    r = insights.purchase_history(limit=10)
    check("许可行数", r["total"] == 2, str(r["total"]))
    check("名称已压空白", r["items"][1]["name"] == "Wuthering Waves", str(r["items"][1]))
    check("从链接抠出 appid", r["items"][0]["id"] == 274170, str(r["items"][0]))
    check("来源字段保留", r["items"][0]["source"] == "Steam 商店", str(r["items"][0]))

    class EmptyResp:
        text = "<html>no table</html>"

    insights.http.request = lambda *a, **k: EmptyResp()
    r2 = insights.purchase_history()
    check("结构变化时给明确错误码", r2.get("error") == "licenses_unavailable", str(r2))


def test_market_compare() -> None:
    insights.api.market_price = lambda appid, name: {
        "lowest_price": "¥ 0.21", "median_price": "¥ 0.20", "volume": "49"}
    insights.buff.search_goods = lambda kw, game="csgo", limit=20: {"items": [
        {"goods_id": 1, "market_hash_name": "SSG 08 | Green Ceramic (Field-Tested)",
         "sell_min_price": "0.02", "sell_num": 593},
    ]}
    r = insights.market_compare("SSG 08 | Green Ceramic (Field-Tested)")
    check("Steam 价解析", r["steam"]["lowest"] == 0.21, str(r["steam"]))
    check("BUFF 价解析", r["buff"]["min"] == 0.02, str(r["buff"]))
    check("判定 BUFF 更便宜", r["cheaper"] == "buff", str(r))
    check("价差计算", r["diff"] == -0.19, str(r.get("diff")))

    insights.buff.search_goods = lambda kw, game="csgo", limit=20: {"items": [
        {"goods_id": 2, "market_hash_name": "别的磨损", "sell_min_price": "9.9", "sell_num": 1}]}
    r2 = insights.market_compare("SSG 08 | Green Ceramic (Field-Tested)")
    check("无同名项时如实报错并给候选", r2["buff"].get("error") == "no_exact_match"
          and len(r2["buff"]["candidates"]) == 1, str(r2["buff"]))
    check("缺一侧时不硬算差价", "cheaper" not in r2, str(r2))


def test_itad_price_history() -> None:
    insights.api.auth.state = lambda: {}          # 没配 key
    r = insights.price_history(730)
    check("无 key 时明确报 itad_key_missing", r.get("error") == "itad_key_missing", str(r))

    insights.api.auth.state = lambda: {"itad_key": "k"}

    class FakeResp:
        def json(self):
            return [{"id": "uuid-1",
                     "low": {"shop": {"name": "Fanatical"},
                             "price": {"amount": 3.99, "currency": "USD"},
                             "regular": {"amount": 21.99},
                             "cut": 82, "timestamp": "2022-11-27T21:33:36+01:00"}}]

    calls: list[tuple] = []

    def fake_get_json(url, params=None, **k):
        calls.append(("GET", url, params))
        return {"found": True, "game": {"id": "uuid-1", "slug": "ff7", "title": "FF7"}}

    def fake_request(method, url, params=None, json=None, **k):
        calls.append((method, url, params, json))
        return FakeResp()

    insights.http.get_json = fake_get_json
    insights.http.request = fake_request
    r2 = insights.price_history(1462040, country="ID")
    check("历史低价金额", r2["low"]["amount"] == 3.99, str(r2.get("low")))
    check("折扣与店铺", (r2["low"]["cut"], r2["low"]["shop"]) == (82, "Fanatical"), str(r2.get("low")))
    check("原价一并返回", r2["regular"] == 21.99, str(r2))
    check("带上 ITAD 链接", "isthereanydeal.com/game/ff7" in (r2.get("itad_url") or ""), str(r2))
    check("historylow 走 POST 且带 id",
          calls[-1][0] == "POST" and calls[-1][3] == ["uuid-1"], str(calls[-1]))


def main() -> None:
    print("重组分析离线测试")
    test_playtime()
    test_deck()
    test_deals()
    test_market_listings()
    test_library_diff()
    test_price_watch()
    test_money_parse()
    test_purchase_history()
    test_market_compare()
    test_itad_price_history()
    print(f"\n结果：{len(FAILS)} 个失败" + ("" if not FAILS else f" -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
