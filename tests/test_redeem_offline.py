"""钱包充值码兑换的离线验证：三道门槛 + 码脱敏（不联网）。

跑法：./.venv/bin/python tests/test_redeem_offline.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["STEAM_MCP_HOME"] = tempfile.mkdtemp(prefix="steam-mcp-redeem-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import freelicense  # noqa: E402

FAILS: list[str] = []
CALLS: list[tuple] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status
        self.text = str(body)

    def json(self):
        return self._body


def install(sensitive: bool, body):
    freelicense.config.load = lambda: {"behavior": {"allow_sensitive": sensitive}}
    freelicense.auth.require_login = lambda: {}
    freelicense.auth.warm_session = lambda: {}
    freelicense.auth.sessionid = lambda: "sess"
    freelicense.auth.cookies_for = lambda host: {"sessionid": "sess"}
    CALLS.clear()

    def fake_request(method, url, data=None, **k):
        CALLS.append((method, url, data))
        return FakeResp(body)

    freelicense.http.request = fake_request


def test_input_guards() -> None:
    install(True, {"success": 1})
    r = freelicense.redeem_wallet_code("AB")
    check("短码直接拒", r.get("error") == "bad_input", str(r))

    r2 = freelicense.redeem_wallet_code("ABCD-1234-EFGH-5678")
    check("未 confirm 拒", r2.get("error") == "confirmation_required", str(r2))
    check("未 confirm 时不发请求", CALLS == [], str(CALLS))

    install(False, {"success": 1})
    r3 = freelicense.redeem_wallet_code("ABCD-1234-EFGH-5678", confirm=True)
    check("allow_sensitive 关闭时拒", r3.get("error") == "confirmation_required", str(r3))
    check("关闭时也不发请求", CALLS == [], str(CALLS))


def test_success() -> None:
    install(True, {"success": 1, "detail": 0})
    r = freelicense.redeem_wallet_code("WXYZ-9876-ABCD-5432", confirm=True)
    check("成功标记", r.get("ok") is True and r.get("redeemed") is True, str(r))
    check("确实发了 POST", CALLS and CALLS[0][0] == "POST" and "ajaxredeemwalletcode" in CALLS[0][1], str(CALLS))
    check("提交的是原始码", CALLS[0][2].get("wallet_code") == "WXYZ-9876-ABCD-5432", str(CALLS[0][2]))
    check("返回只给掩码", r.get("code_hint") == "WXYZ****", str(r))
    check("返回值不含完整码", "WXYZ-9876" not in str(r), str(r))


def test_rejected() -> None:
    install(True, {"success": 2, "detail": 14})
    r = freelicense.redeem_wallet_code("ABCD-0000-0000-0000", confirm=True)
    check("失败不冒充成功", r.get("ok") is False and r.get("redeemed") is False, str(r))
    check("保留 detail 码", r.get("detail") == 14, str(r))
    check("给出可读 hint", "地区" in (r.get("hint") or ""), str(r.get("hint")))


def test_captcha() -> None:
    install(True, {"success": 2, "detail": 0, "captcha_needed": True, "captcha_gid": "g123"})
    r = freelicense.redeem_wallet_code("ABCD-0000-0000-0001", confirm=True)
    check("要求验证码时明确透出", r.get("captcha_required") is True, str(r))
    check("带上 captcha_gid", r.get("captcha_gid") == "g123", str(r))


def main() -> None:
    print("充值码兑换离线测试")
    test_input_guards()
    test_success()
    test_rejected()
    test_captcha()
    print(f"\n结果：{len(FAILS)} 个失败" + ("" if not FAILS else f" -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
