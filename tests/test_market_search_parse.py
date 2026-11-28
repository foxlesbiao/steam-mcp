"""市场搜索的页面解析测试。

Steam 把 /market/search/render/ 下线后，结果是服务端直出的 React Router 页面，
数据埋在 <script> 里 renderContext.queryData 的 dehydrated TanStack Query state 中。
这段解析纯字符串处理，不联网也能测。

跑法：./.venv/bin/python tests/test_market_search_parse.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp.api import _market_search_pages  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)


def build_page(queries: list[dict]) -> str:
    """造一个和 Steam 同形的页面：script 里是外层 JSON，queryData 是再套一层的 JSON 字符串。"""
    query_data = json.dumps({"mutations": [], "queries": queries})
    root = {"route": "/market/search", "renderContext": {"queryData": query_data}}
    return (
        "<html><head>"
        f"<script>{json.dumps(root, ensure_ascii=False)}</script>"
        "<script>(()=>{const e=document.getElementById('valve-ssr-data');})()</script>"
        "</head><body></body></html>"
    )


def result_page(total: int, names: list[str]) -> dict:
    return {
        "start": 0,
        "total_count": total,
        "results": [
            {
                "strHash": n,
                "cSellOrders": 10 + i,
                "cBuyOrders": i,
                "strMinSellSubtotal": f"IDR {1000 * (i + 1):,}",
                "eCurrency": 10,
                "asset_description": {"market_name": n.replace(" | ", "（★） | "), "icon_url": f"icon{i}"},
            }
            for i, n in enumerate(names)
        ],
    }


def test_finds_market_search_query() -> None:
    """正常页面：能从一堆 query 里认出 market_search，且不靠索引。"""
    queries = [
        {"queryKey": ["AOWarningCookie"], "state": {"data": "x"}},
        {"queryKey": ["CurrentUserWalletDetails", 388685856], "state": {"data": {}}},
        {"queryKey": ["market_search", {"appid": 730, "strQuery": "AK-47"}], "state": {"data": {"pages": [result_page(68, ["A", "B"])]}}},
        {"queryKey": ["market", "app_facets", 730], "state": {"data": {}}},
    ]
    pages = _market_search_pages(build_page(queries))
    check("找到 market_search 的 pages", pages is not None)
    check("total_count 正确", pages and pages[0]["total_count"] == 68)
    check("results 条数正确", pages and len(pages[0]["results"]) == 2)


def test_query_key_variants() -> None:
    """queryKey 可能是 list 也可能是转义过的字符串，两种都要认。"""
    as_list = [{"queryKey": ["market_search", {"appid": 730}], "state": {"data": {"pages": [result_page(5, ["X"])]}}}]
    check("queryKey 为 list 时能识别", _market_search_pages(build_page(as_list)) is not None)

    # 字符串形态（经过一次 JSON 序列化后的样子）
    raw = json.dumps({"mutations": [], "queries": as_list})
    # 把 queryData 再包一层字符串，模拟多重编码
    root = {"renderContext": {"queryData": json.dumps(raw)}}
    html = f"<html><script>{json.dumps(root)}</script></html>"
    check("多层 JSON 字符串也能挖到", _market_search_pages(html) is not None)


def test_without_results() -> None:
    """没有 market_search 条目时返回 None，不能抛异常。"""
    queries = [{"queryKey": ["marketfilters", 753], "state": {"data": {}}}]
    check("无 market_search 时返回 None", _market_search_pages(build_page(queries)) is None)


def test_broken_html() -> None:
    """坏页面不能炸。"""
    check("空字符串不抛异常", _market_search_pages("") is None)
    check("纯文本不抛异常", _market_search_pages("<html>no scripts</html>") is None)
    check("script 里是坏 JSON 不抛异常", _market_search_pages("<script>{not json}</script>") is None)


def test_field_mapping() -> None:
    """结果字段映射：strHash → hash_name，strMinSellSubtotal → sell_price。"""
    queries = [{"queryKey": ["market_search", {}], "state": {"data": {"pages": [result_page(1, ["AK-47 | Redline (Field-Tested)"])]}}}]
    pages = _market_search_pages(build_page(queries))
    item = pages[0]["results"][0]
    check("strHash 在", item.get("strHash") == "AK-47 | Redline (Field-Tested)")
    check("cSellOrders 在", item.get("cSellOrders") == 10)
    check("strMinSellSubtotal 在", item.get("strMinSellSubtotal") == "IDR 1,000")


if __name__ == "__main__":
    for fn in (
        test_finds_market_search_query,
        test_query_key_variants,
        test_without_results,
        test_broken_html,
        test_field_mapping,
    ):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n结果：{len(FAIL)} 个失败 / {len(PASS)} 个通过")
    sys.exit(1 if FAIL else 0)
