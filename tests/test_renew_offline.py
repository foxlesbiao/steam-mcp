"""自动续期逻辑的不联网验证：mock 掉 HTTP 层，只看行为与写回结果。

跑法：STEAM_MCP_HOME 指向临时目录，避免碰真实凭证库。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="steam-mcp-renew-")
os.environ["STEAM_MCP_HOME"] = TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import auth, http, secrets  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeResp:
    def __init__(self, status=200, text="{}", cookies=None):
        self.status = status
        self.text = text
        self.headers = {"location": ""}
        self.raw = type("R", (), {"cookies": cookies or {}})()

    def json(self):
        return json.loads(self.text)

    def looks_like_login_page(self):
        return False


def seed(**extra):
    data = {
        "steamid": "76561190000000001",
        "steamid_ok": True,
        "cookies": {"steamLoginSecure": "76561190000000001||old.access", "sessionid": "sess-old"},
        "domain_cookies": {"store.steampowered.com": {"sessionid": "sess-old"}},
        "access_token": "old.access",
        "refresh_token": "refresh-abc",
        "method": "password",
        "obtained_at": 1,
    }
    data.update(extra)
    secrets.save_credentials(data)
    auth._ensure_cache.update({"checked_at": 0.0, "valid": False})


TRANSFER_HOSTS = {
    "https://store.steampowered.com/login/settoken": "store.steampowered.com",
    "https://steamcommunity.com/login/settoken": "steamcommunity.com",
    "https://help.steampowered.com/login/settoken": "help.steampowered.com",
    "https://checkout.steampowered.com/login/settoken": "checkout.steampowered.com",
    "https://steam.tv/login/settoken": "steam.tv",
}

calls: list[tuple[str, str]] = []


def install_happy_http():
    """模拟一条完全成功的续期链路。"""
    calls.clear()

    def fake_request(method, url, *, params=None, data=None, cookies=None, headers=None,
                     allow_redirects=False, retries=0, multipart=False):
        calls.append((method, url))
        if url.endswith("/jwt/ajaxrefresh"):
            return FakeResp(text=json.dumps({"success": True, "refresh_token": "refresh-new"}))
        if url.endswith("/jwt/finalizelogin"):
            return FakeResp(text=json.dumps({
                "success": True,
                "steamID": "76561190000000001",
                "transfer_info": [{"url": u, "params": {"nonce": "n", "auth": "a"}} for u in TRANSFER_HOSTS],
            }))
        host = url.split("/")[2]
        # 每个域都下发自己的 sessionid；checkout 域还会下发自己的 steamLoginSecure
        cookies_out = {"sessionid": f"sess-{host}", "steamCountry": "CN%7Cxx"}
        if host.startswith("checkout"):
            cookies_out["steamLoginSecure"] = "76561190000000001||checkout.access"
        return FakeResp(text="{}", cookies=cookies_out)

    def fake_post_json(url, **kwargs):
        calls.append(("POST", url))
        if url.endswith("GenerateAccessTokenForApp/v1/"):
            return {"response": {"access_token": "new.access"}}
        return {"response": {}}

    http.request = fake_request
    http.post_json = fake_post_json


def install_verify(valid, status=200, reason=None):
    def fake_verify():
        if valid is None:
            return {"valid": None, "reason": "network"}
        if valid:
            return {"valid": True, "status": 200, "account_name": "tester"}
        return {"valid": False, "reason": reason or "expired", "status": status}

    auth.verify_session = fake_verify


def install_verify_seq(seq):
    """按顺序返回校验结果（最后一个结果重复使用）。续期前过期、续期后有效的真实场景。"""
    items = list(seq)
    state = {"i": 0}

    def fake_verify():
        idx = min(state["i"], len(items) - 1)
        state["i"] += 1
        return items[idx]

    auth.verify_session = fake_verify


print("== 1) 白名单：steam.tv 已放行 ==")
check("check_url(steam.tv) 不报 Blocked", http.check_url("https://steam.tv/login/settoken") == "steam.tv")

print("== 2) 会话有效：不触发续期 ==")
seed()
install_verify(True)
install_happy_http()
out = auth.ensure_session()
check("checked=True 且未续期", out.get("checked") is True and not out.get("renewed"), str(out))
check("没有打续期接口", not any("ajaxrefresh" in u for _, u in calls), str(calls))

print("== 3) 节流：窗口内第二次直接命中缓存 ==")
calls.clear()
out2 = auth.ensure_session()
check("cached=True", out2.get("cached") is True, str(out2))
check("零网络请求", calls == [], str(calls))

print("== 4) 过期 + 续期成功：写回完整各域 cookie ==")
seed()
install_verify_seq([
    {"valid": False, "reason": "expired", "status": 302},   # ensure_session 首次校验
    {"valid": True, "status": 200, "account_name": "tester"},  # 续期后复核
])
install_happy_http()
auth._ensure_cache.update({"checked_at": 0.0, "valid": False})
out3 = auth.ensure_session()
check("renewed=True", out3.get("renewed") is True, str(out3))
saved = secrets.load_credentials()
check("新的 refresh token 已落库", saved.get("refresh_token") == "refresh-new", str(saved.get("refresh_token")))
check("access token 已更新", saved.get("access_token") == "new.access")
check("steamLoginSecure 自拼成功", saved["cookies"]["steamLoginSecure"] == "76561190000000001||new.access")
dom = saved.get("domain_cookies", {})
check("checkout 域 cookie 收齐", bool(dom.get("checkout.steampowered.com")), str(dom))
check("5 个域名全部落库", len(dom) == 5, str(sorted(dom)))
check("steam.tv 也在其中", "steam.tv" in dom)
check("旧 store cookie 未被抹掉（合并语义）", isinstance(dom.get("store.steampowered.com"), dict), str(dom.get("store.steampowered.com")))

print("== 5) 返回体不泄漏 token 明文 ==")
blob = json.dumps(out3, ensure_ascii=False)
check("无 refresh-new 明文", "refresh-new" not in blob)
check("无 new.access 明文", "new.access" not in blob)
renew_view = auth._renew(force=True)
view_blob = json.dumps(renew_view, ensure_ascii=False)
check("续期返回体无 token 明文", "refresh-new" not in view_blob and "new.access" not in view_blob)
check("只给指纹", isinstance(renew_view.get("session_cookie"), str)
      and renew_view["session_cookie"].startswith("len="), str(renew_view.get("session_cookie")))
check("checkout_ready 标记正确", renew_view.get("checkout_ready") is True, str(renew_view.get("checkout_ready")))

print("== 6) 过期 + 续期失败：如实抛 NotLoggedIn ==")
seed()


def fail_all(method, url, **kwargs):
    calls.append((method, url))
    return FakeResp(status=200, text=json.dumps({"success": False, "error": 21}))


http.request = fail_all
http.post_json = lambda url, **kw: {"response": {}}
auth.verify_session = lambda: {"valid": False, "reason": "expired", "status": 302}
auth._ensure_cache.update({"checked_at": 0.0, "valid": False})
try:
    auth.ensure_session()
    check("应抛 NotLoggedIn", False, "没有抛异常")
except http.NotLoggedIn as exc:
    check("抛 NotLoggedIn", True)
    check("错误信息提到续期失败", "续期" in exc.message, exc.message)
    check("hint 指向重新登录", "重新登录" in (exc.hint or ""), exc.hint or "")

print("== 7) 网络异常不误判为过期 ==")
seed()
install_verify(None)
calls.clear()
out7 = auth.ensure_session()
check("skipped=network", out7.get("skipped") == "network", str(out7))
check("没有尝试续期", not any("ajaxrefresh" in u for _, u in calls), str(calls))

print("== 8) 没有 refresh token 时给出明确原因 ==")
seed(refresh_token=None)
saved = secrets.load_credentials()
saved.get("cookies", {}).pop("steamRefresh_steam", None)
secrets.save_credentials(saved)
auth._ensure_cache.update({"checked_at": 0.0, "valid": False})
install_verify(False)
r = auth.refresh()
check("error=no_refresh_token", r.get("error") == "no_refresh_token", str(r))
check("hint 提示导入/登录", "登录" in (r.get("hint") or ""), r.get("hint") or "")

print("== 9) auto_renew 关闭：只校验不续期 ==")
seed()
from steam_mcp import config as cfgmod  # noqa: E402

cfg = cfgmod.load()
cfg["behavior"]["auto_renew"] = False
cfgmod.save(cfg)
install_verify(False)
auth._ensure_cache.update({"checked_at": 0.0, "valid": False})
calls.clear()
out9 = auth.ensure_session()
check("reason=auto_renew_disabled", out9.get("reason") == "auto_renew_disabled", str(out9))
check("未发续期请求", not any("ajaxrefresh" in u for _, u in calls), str(calls))
cfg["behavior"]["auto_renew"] = True
cfgmod.save(cfg)

print("== 10) logout 清空并重置缓存 ==")
auth.logout()
check("凭证已清", not secrets.load_credentials())
check("缓存复位", auth._ensure_cache["valid"] is False)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASS")
