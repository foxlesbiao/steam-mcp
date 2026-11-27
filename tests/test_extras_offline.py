"""补充层的离线验证：徽章解析 / 成交历史参数 / 价格解析 / 估值累加。

跑法：./.venv/bin/python tests/test_extras_offline.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["STEAM_MCP_HOME"] = tempfile.mkdtemp(prefix="steam-mcp-extras-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import extras  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeResp:
    def __init__(self, body="", status=200, url="https://steamcommunity.com/x", headers=None):
        self.text = body
        self.status = status
        self.url = url
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

    def looks_like_login_page(self) -> bool:
        return False


# ---------------------------------------------------------------- 价格解析


def test_parse_price() -> None:
    cases = [
        ("¥ 0.55", 55),
        ("¥1.23", 123),
        ("Rp 18,100", 18100),
        ("Rp 1 234,56", 123456),
        ("$12.00", 1200),
        ("", None),
        (None, None),
    ]
    for raw, want in cases:
        got = extras._parse_price(raw)
        check(f"parse {raw!r} -> {want}", got == want, f"got {got}")


# ---------------------------------------------------------------- 徽章解析


BADGE_HTML = """
<div class="badge_row is_link">
  <a class="badge_row_overlay" href="https://steamcommunity.com/id/x/gamecards/730/"></a>
  <div class="badge_row_inner">
    <div class="badge_title_row">
      <div class="badge_title">Counter-Strike 2 &nbsp;</div>
    </div>
    <span>总时数 100 小时</span>
    <div class="badge_progress_info">已收集 4 / 9 张卡牌</div>
    <span>100 点经验值</span>
  </div>
</div>
<div class="badge_row is_link">
  <div class="badge_row_inner">
    <div class="badge_title">帐户服务年资 &nbsp;</div>
    <span>450 点经验值</span>
  </div>
</div>
"""


def test_badges() -> None:
    extras.auth.cookies_for = lambda host: {"sessionid": "x"}
    extras.http.request = lambda method, url, **kw: (
        FakeResp("", 302, headers={"location": "/id/someone/badges"})
        if url.endswith("/my/badges")
        else FakeResp(BADGE_HTML, 200, url="https://steamcommunity.com/id/someone/badges?l=schinese")
    )

    r = extras.badges()
    check("解析出 2 条徽章", r.get("total_badges") == 2, str(r.get("total_badges")))
    check("游戏徽章 1 条", r.get("game_badges") == 1, str(r.get("game_badges")))
    first = r["badges"][0]
    check("appid 提取", first["appid"] == 730, str(first))
    check("名称去 &nbsp;", first["name"] == "Counter-Strike 2", repr(first["name"]))
    check("卡牌进度", (first["cards_owned"], first["cards_total"]) == (4, 9), str(first))
    check("经验值", first["xp"] == 100, str(first))
    check("无 appid 的徽章仍保留", r["badges"][1]["appid"] is None, str(r["badges"][1]))
    check("带 evidence", r["evidence"]["status"] == 200, str(r.get("evidence")))


# ---------------------------------------------------------------- 成交历史


def test_market_history_params() -> None:
    seen: dict[str, str] = {}

    def fake_request(method, url, **kw):
        seen["qp"] = kw["params"]["qp"]
        seen["referer"] = kw["headers"].get("Referer", "")
        body = {"data": {"eResult": 1, "bMore": True, "rgEvents": [
            {"event_type": 3, "time_event": 1, "listing": {"listingid": "1"},
             "description": {"name": "刀", "market_hash_name": "Knife", "appid": 730},
             "purchase": {"received_amount": "1810", "received_currencyid": "2010"}},
        ]}}
        return FakeResp(json.dumps(body))

    extras.auth.cookies_for = lambda host: {"sessionid": "x"}
    extras.http.request = fake_request

    r = extras.market_history(limit=25, offset=0)
    check("首页强制 count=50", seen["qp"] == "[0,50]", seen["qp"])
    check("Referer 必带", seen["referer"].endswith("/market/history"), seen["referer"])
    check("返回一条事件", r["returned"] == 1, str(r["returned"]))
    check("金额累加", r["sum_received_raw_cents"] == 1810, str(r["sum_received_raw_cents"]))
    check("has_more 透出", r["has_more"] is True, str(r["has_more"]))

    extras.market_history(limit=25, offset=25)
    check("非首页可用 count=25", seen["qp"] == "[25,25]", seen["qp"])

    extras.market_history(limit=50, offset=50)
    check("limit>=50 用 count=50", seen["qp"] == "[50,50]", seen["qp"])


# ---------------------------------------------------------------- 库存估值


def test_inventory_value() -> None:
    extras.api.inventory = lambda **kw: {
        "ok": True, "steamid": "1", "total": 3,
        "items": [
            {"assetid": "a", "name": "A", "market_hash_name": "A", "marketable": True, "type": "card"},
            {"assetid": "b", "name": "B", "market_hash_name": "B", "marketable": True, "type": "card"},
            {"assetid": "c", "name": "C", "market_hash_name": None, "marketable": False, "type": "card"},
        ],
    }
    prices = {"A": {"ok": True, "lowest_price": "¥ 1.00", "median_price": "¥ 2.00", "volume": "3"},
              "B": {"ok": True, "lowest_price": "¥ 0.50", "median_price": None, "volume": "1"}}
    extras.api.market_price = lambda appid, hn, currency="CNY": prices[hn]

    r = extras.inventory_value(top_n=5)
    check("只算可交易且有 hash_name 的", r["marketable"] == 2, str(r["marketable"]))
    check("总估值 = 1.00 + 0.50", r["est_total_raw"] == 150, str(r["est_total_raw"]))
    check("按价格降序", [x["name"] for x in r["top"]] == ["A", "B"], str(r["top"]))
    check("无失败", r["failed"] == 0, str(r["failed"]))


def test_inventory_value_survives_one_failure() -> None:
    extras.api.inventory = lambda **kw: {
        "ok": True, "steamid": "1", "total": 2,
        "items": [
            {"assetid": "a", "name": "A", "market_hash_name": "A", "marketable": True, "type": "card"},
            {"assetid": "b", "name": "B", "market_hash_name": "B", "marketable": True, "type": "card"},
        ],
    }

    def flaky(appid, hn, currency="CNY"):
        if hn == "B":
            raise RuntimeError("市场限速")
        return {"ok": True, "lowest_price": "¥ 3.00", "median_price": None, "volume": "1"}

    extras.api.market_price = flaky
    r = extras.inventory_value(top_n=5)
    check("单件失败不拖垮整批", r["priced"] == 1 and r["failed"] == 1, f"{r['priced']}/{r['failed']}")
    check("失败样本透出", r.get("sample_failures") and r["sample_failures"][0]["name"] == "B", str(r.get("sample_failures")))


def main() -> None:
    print("补充层离线测试")
    test_parse_price()
    test_badges()
    test_market_history_params()
    test_inventory_value()
    test_inventory_value_survives_one_failure()
    print(f"\n结果：{len(FAILS)} 个失败" + ("" if not FAILS else f" -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
