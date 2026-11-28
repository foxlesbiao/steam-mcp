"""prompt / resource 注册的离线验证。

这两个能力空着时协议层返回 []，客户端会以为服务端没实现；
而且它们不像 tool 那样有 @_safe 兜底，注册失败是静默的——
所以留一条断言，改 server.py 时能被拦住。

跑法：./.venv/bin/python tests/test_prompts_resources_offline.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import server  # noqa: E402

EXPECTED_PROMPTS = {"wishlist_sale_check", "purchase_advisor", "account_health"}
EXPECTED_RESOURCES = {"steam://docs/tools", "steam://docs/roadmap"}

PASS, FAIL = [], []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)


async def run() -> None:
    prompts = await server.mcp.list_prompts()
    names = {p.name for p in prompts}
    check(f"prompt 数量 = {len(EXPECTED_PROMPTS)}", names == EXPECTED_PROMPTS)
    for p in prompts:
        check(f"prompt {p.name} 有 description", bool(p.description))

    resources = await server.mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    check(f"resource 数量 = {len(EXPECTED_RESOURCES)}", uris == EXPECTED_RESOURCES)
    for r in resources:
        check(f"resource {r.uri} 有 mimeType", bool(r.mime_type))

    # get_prompt 实际能渲染
    got = await server.mcp.get_prompt("account_health", {})
    msgs = getattr(got, "messages", None) or []
    check("get_prompt(account_health) 返回消息", bool(msgs))
    text = getattr(msgs[0], "content", "") if msgs else ""
    check("account_health 正文提到 steam_me", "steam_me" in str(text))

    # 带参数的 prompt 参数替换生效
    got2 = await server.mcp.get_prompt("purchase_advisor", {"appids": "1761390", "budget": "150"})
    msgs2 = getattr(got2, "messages", None) or []
    body = str(getattr(msgs2[0], "content", "")) if msgs2 else ""
    check("purchase_advisor 参数替换生效", "1761390" in body and "150" in body)

    # read_resource 实际能读
    docs = Path(__file__).resolve().parent.parent
    res = await server.mcp.read_resource("steam://docs/tools")
    content = "".join(getattr(c, "content", "") for c in res) if not isinstance(res, str) else res
    check("read_resource(tools) 非空", len(content) > 100)
    check("read_resource(tools) 内容与 TOOLS.md 一致",
          content.strip()[:200] == (docs / "TOOLS.md").read_text(encoding="utf-8").strip()[:200])


if __name__ == "__main__":
    asyncio.run(run())
    print(f"\n结果：{len(FAIL)} 个失败 / {len(PASS)} 个通过")
    sys.exit(1 if FAIL else 0)
