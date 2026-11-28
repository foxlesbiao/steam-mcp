"""评测接口的离线验证。

/appreviews/ 的 json=1 现在 302 回首页（真实浏览器里也一样），
新端点是 /ajaxappreviews/，需要一组固定参数才返回数据。
这里锁住两件事：请求发对了地方、返回字段映射没丢。

跑法：./.venv/bin/python tests/test_reviews_endpoint.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import api, http  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)


class FakeResp:
    def __init__(self, status: int, text: str):
        self.status = status
        self.text = text

    def json(self):
        return json.loads(self.text)


SAMPLE = {
    "success": 1,
    "query_summary": {
        "num_reviews": 2,
        "review_score": 8,
        "review_score_desc": "特别好评",
        "total_positive": 100,
        "total_negative": 20,
        "total_reviews": 120,
    },
    "cursor": "abc=",
    "reviews": [
        {
            "voted_up": True,
            "review": "好玩",
            "author": {"playtime_forever": 468},
            "votes_up": 7,
            "weighted_vote_score": "0.6",
            "timestamp_created": 1700000000,
        },
        {
            "voted_up": False,
            "review": "the map pool is bad",
            "author": {"playtime_forever": 6000},
            "votes_up": 3,
            "weighted_vote_score": "0.4",
            "timestamp_created": 1700000100,
        },
    ],
}


def with_patched(fn):
    """把 http.request / cookies_for 换掉，跑完复原。"""
    orig_req = api.http.request
    orig_ck = api.auth.cookies_for
    api.auth.cookies_for = lambda *a: {}
    api.http.request = fn
    try:
        return api.reviews(730, limit=2)
    finally:
        api.http.request = orig_req
        api.auth.cookies_for = orig_ck


def test_hits_ajax_endpoint() -> None:
    """必须打到 /ajaxappreviews/，不能再用 /appreviews/。"""
    seen = {}

    def fake(method, url, **kwargs):
        seen["url"] = url
        seen["params"] = kwargs.get("params") or {}
        return FakeResp(200, json.dumps(SAMPLE))

    r = with_patched(fake)
    check("请求的是 ajaxappreviews", "/ajaxappreviews/730" in seen["url"])
    check("没再用旧的 appreviews", "/appreviews/" not in seen["url"].replace("/ajaxappreviews/", ""))

    p = seen["params"]
    for need in ("cursor", "filter", "language", "num_per_page", "purchase_type", "playtime_type"):
        check(f"带上参数 {need}", need in p)
    check("cursor 初始值为 *", p.get("cursor") == "*")
    check("num_per_page 跟随 limit", p.get("num_per_page") == 2)
    check("ok=True", r.get("ok") is True)


def test_field_mapping() -> None:
    r = with_patched(lambda *a, **k: FakeResp(200, json.dumps(SAMPLE)))
    check("source 标成 ajaxappreviews_api", r.get("source") == "ajaxappreviews_api")
    check("cursor 透出", r.get("cursor") == "abc=")

    s = r.get("summary") or {}
    check("summary.total_reviews", s.get("total_reviews") == 120)
    check("summary.total_positive", s.get("total_positive") == 100)
    check("summary.score_desc", s.get("score_desc") == "特别好评")

    items = r.get("items") or []
    check("items 条数 = limit", len(items) == 2)
    first = items[0]
    check("voted_up 映射", first.get("voted_up") is True)
    check("text 映射", first.get("text") == "好玩")
    check("hours 从分钟换算", first.get("hours") == 7.8)
    check("votes_up 映射", first.get("votes_up") == 7)


def test_falls_back_on_html() -> None:
    """接口回 HTML（302 跟随后是首页）时不能抛，要回落到页面统计块。"""
    orig_page = api._review_summary_from_page
    api._review_summary_from_page = lambda appid, lang: {"ok": True, "source": "page_fallback", "appid": appid}
    try:
        r = with_patched(lambda *a, **k: FakeResp(200, "<!DOCTYPE html><html>首页</html>"))
        check("回 HTML 时走 fallback", r.get("source") == "page_fallback")
    finally:
        api._review_summary_from_page = orig_page


if __name__ == "__main__":
    for fn in (test_hits_ajax_endpoint, test_field_mapping, test_falls_back_on_html):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n结果：{len(FAIL)} 个失败 / {len(PASS)} 个通过")
    sys.exit(1 if FAIL else 0)
