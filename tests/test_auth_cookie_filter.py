"""登录态 cookie 过滤的离线验证：设备令牌（steamMachineAuth<steamid>）必须被保留。

跑法：./.venv/bin/python tests/test_auth_cookie_filter.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["STEAM_MCP_HOME"] = tempfile.mkdtemp(prefix="steam-mcp-cookiefilter-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import auth, secrets  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def test_keep_cookie() -> None:
    for name in ("steamLoginSecure", "sessionid", "steamRefresh_steam", "steamCountry", "browserid"):
        check(f"保留 {name}", auth._keep_cookie(name))
    check("保留设备令牌", auth._keep_cookie("steamMachineAuth76561190000000001"))
    check("丢弃无关 cookie", not auth._keep_cookie("steamRecentApps"))
    check("丢弃前缀相近的伪造名", not auth._keep_cookie("steamMachineAut"))


def test_cookies_for_passes_machine_auth() -> None:
    data = auth.state()
    data["cookies"] = {
        "steamLoginSecure": "s",
        "sessionid": "x",
        "steamMachineAuth76561190000000001": "machine-token",
    }
    secrets.save_credentials(data)
    jar = auth.cookies_for("store.steampowered.com")
    check("cookies_for 带出设备令牌", jar.get("steamMachineAuth76561190000000001") == "machine-token", str(sorted(jar)))


def main() -> None:
    print("登录态 cookie 过滤测试")
    test_keep_cookie()
    test_cookies_for_passes_machine_auth()
    print(f"\n结果：{len(FAILS)} 个失败" + ("" if not FAILS else f" -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
