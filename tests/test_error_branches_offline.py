"""错误分支的离线验证：限流/接口下线/服务端拒绝必须给出可区分的 error 码。

跑法：./.venv/bin/python tests/test_error_branches_offline.py
这三个分支以前都退化成同一种「查不到」的含糊输出，
最容易误导的地方是把 429 限流当成「你没注册」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import auth, http  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)


class FakeResp:
    def __init__(self, status: int, text: str = "{}"):
        self.status = status
        self.text = text

    def looks_like_login_page(self) -> bool:
        return False

    def json(self):
        import json as _j

        return _j.loads(self.text)


class FakeError(http.SteamError):
    def __init__(self, msg: str, code: str = "bad_payload"):
        super().__init__(msg, code=code)


def test_apikey_429() -> None:
    """429 必须报 rate_limited，不能报 has_key:false。"""
    orig_req, orig_login = auth.http.request, auth.require_login
    auth.require_login = lambda: None
    auth.http.request = lambda *a, **k: FakeResp(429, "<html>Too Many Requests</html>")
    try:
        r = auth.fetch_webapi_key()
        check("apikey 429 → error=rate_limited", r.get("error") == "rate_limited")
        check("apikey 429 → ok=False", r.get("ok") is False)
        check("apikey 429 → 提示里说明不是没注册", "不是没注册" in r.get("hint", ""))
    finally:
        auth.http.request, auth.require_login = orig_req, orig_login


def test_apikey_no_key() -> None:
    """页面正常但没有 key 明文 → has_key:false，且带上 status 便于区分。"""
    orig_req, orig_login = auth.http.request, auth.require_login
    auth.require_login = lambda: None
    auth.http.request = lambda *a, **k: FakeResp(200, "<html>没有 key 的页面</html>")
    try:
        r = auth.fetch_webapi_key()
        check("apikey 200 无 key → has_key=False", r.get("has_key") is False)
        check("apikey 200 无 key → 无 error 字段", "error" not in r)
        check("apikey 200 无 key → 带 status", r.get("status") == 200)
    finally:
        auth.http.request, auth.require_login = orig_req, orig_login


def test_market_search_parse_failed() -> None:
    """页面结构变了要报 market_search_parse_failed，不能静默返回空。"""
    from steam_mcp import api

    orig = api._market_search_pages
    api._market_search_pages = lambda _html: None
    orig_req, orig_ck, orig_login = api.http.request, api.auth.cookies_for, api.auth.require_login
    api.auth.require_login = lambda: None
    api.auth.cookies_for = lambda *a: {}
    api.http.request = lambda *a, **k: FakeResp(200, "<html>no data</html>")
    try:
        try:
            api.market_search(730, "AK-47", 3)
            check("market_search 解析失败 → 抛 SteamError", False)
        except http.SteamError as exc:
            check("market_search 解析失败 → 抛 SteamError", True)
            check("market_search 解析失败 → code=market_search_parse_failed", exc.code == "market_search_parse_failed")
            check("market_search 解析失败 → 带替代方案", "steam_market_price" in (exc.hint or ""))
    finally:
        api._market_search_pages = orig
        api.http.request, api.auth.cookies_for, api.auth.require_login = orig_req, orig_ck, orig_login


def test_wishlist_rejected() -> None:
    """success:false 必须给出 error + hint，且不能把它说成 Steam 故障。"""
    from steam_mcp import api

    orig_login, orig_warm, orig_sid = api.auth.require_login, api.auth.warm_session, api.auth.sessionid
    orig_req, orig_ck = api.http.request, api.auth.cookies_for
    api.auth.require_login = lambda: None
    api.auth.warm_session = lambda: None
    api.auth.sessionid = lambda: "fake"
    api.auth.cookies_for = lambda *a: {}

    # 按 appid 区分：2947440 模拟「已在愿望单」的拒绝，552620 模拟成功
    def fake_request(_m, _url, data=None, **_k):
        if data and int(data.get("appid", 0)) == 552620:
            return FakeResp(200, '{"success":true,"wishlistCount":344}')
        return FakeResp(200, '{"success":false,"wishlistCount":343}')

    api.http.request = fake_request
    try:
        r = api.wishlist_edit("add", 2947440)
        check("wishlist false → ok=False", r.get("ok") is False)
        check("wishlist false → error=wishlist_noop", r.get("error") == "wishlist_noop")
        h = r.get("hint", "")
        check("wishlist false → hint 指向「已在愿望单/已拥有」", "已在愿望单" in h and "已拥有" in h)
        check("wishlist false → hint 不再说成 Steam 故障", "临时故障" not in h)

        r2 = api.wishlist_edit("add", 552620)
        check("wishlist true → ok=True", r2.get("ok") is True)
        check("wishlist true → 无 error 字段", "error" not in r2)
    finally:
        api.auth.require_login, api.auth.warm_session, api.auth.sessionid = orig_login, orig_warm, orig_sid
        api.http.request, api.auth.cookies_for = orig_req, orig_ck


if __name__ == "__main__":
    for fn in (test_apikey_429, test_apikey_no_key, test_market_search_parse_failed, test_wishlist_rejected):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n结果：{len(FAIL)} 个失败 / {len(PASS)} 个通过")
    sys.exit(1 if FAIL else 0)
