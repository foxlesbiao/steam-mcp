"""登出不删 API key。

webapi_key / itad_key 是账号级的第三方凭证，和登录会话无关。
早先 logout 直接 clear_credentials() 全清，导致：登出 → refresh 失效 →
重新导 cookie 登录后，Web API Key 也没了，library 的时长、成就完美度
全部退化成"不可用"，而且报错只说"没有 key"，看不出是登出连带删的。

跑法：./.venv/bin/python tests/test_logout_keeps_keys.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = [], []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="steam-mcp-test-")
    import os

    os.environ["STEAM_MCP_HOME"] = tmp

    # 必须在设置 env 之后 import，模块级常量会读它
    for mod in list(sys.modules):
        if mod.startswith("steam_mcp"):
            del sys.modules[mod]
    from steam_mcp import auth, secrets  # noqa: E402

    # 造一份"已登录 + 配好两个 key"的凭证
    secrets.save_credentials(
        {
            "steamid": "76561190000000000",
            "cookies": {"steamLoginSecure": "fake||jwt", "sessionid": "abc"},
            "access_token": "fake-token",
            "refresh_token": "fake-refresh",
            "webapi_key": "A" * 32,
            "itad_key": "B" * 40,
        }
    )

    before = auth.state()
    check("准备阶段：有登录态", bool(before.get("cookies")))
    check("准备阶段：有 webapi_key", before.get("webapi_key") == "A" * 32)
    check("准备阶段：有 itad_key", before.get("itad_key") == "B" * 40)

    r = auth.logout()
    check("logout 返回 ok", r.get("ok") is True)
    check("logout 声明保留了两个 key", set(r.get("kept") or []) == {"webapi_key", "itad_key"})

    after = auth.state()
    check("登出后 cookies 已清", not after.get("cookies"))
    check("登出后 access_token 已清", not after.get("access_token"))
    check("登出后 refresh_token 已清", not after.get("refresh_token"))
    check("登出后 webapi_key 仍在", after.get("webapi_key") == "A" * 32)
    check("登出后 itad_key 仍在", after.get("itad_key") == "B" * 40)

    # 再登一次，key 不能丢
    secrets.save_credentials(
        {
            **auth.state(),
            "steamid": "76561190000000000",
            "cookies": {"steamLoginSecure": "fake2||jwt2", "sessionid": "xyz"},
        }
    )
    again = auth.state()
    check("重新登录后 webapi_key 仍在", again.get("webapi_key") == "A" * 32)
    check("重新登录后 itad_key 仍在", again.get("itad_key") == "B" * 40)


if __name__ == "__main__":
    main()
    print(f"\n结果：{len(FAIL)} 个失败 / {len(PASS)} 个通过")
    sys.exit(1 if FAIL else 0)
