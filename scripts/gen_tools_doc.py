#!/usr/bin/env python3
"""从 MCP tools/list 生成完整工具文档 TOOLS.md（避免手写遗漏）。

用法: python3 gen_tools_doc.py > TOOLS.md
"""
import json
import os
import subprocess
import sys

ROOT = os.path.expanduser("~/tools/steam-mcp/steam_mcp")
PY = os.path.join(ROOT, ".venv/bin/python")

# 分组：key 是工具名前缀/名单，value 是小节标题与说明
GROUPS = [
    ("登录与续期", "账号接入、Steam Guard、会话保活", [
        "steam_auth_status", "steam_auth_import_cookie", "steam_auth_login", "steam_auth_guard",
        "steam_auth_poll", "steam_auth_refresh", "steam_auth_logout", "steam_session_warm", "steam_webapi_key",
    ]),
    ("诊断与配置", "网络、代理、行为开关", ["steam_net_check", "steam_config_get", "steam_config_set"]),
    ("档案与社交", "玩家信息、好友、成就", [
        "steam_me", "steam_user_profile", "steam_friends", "steam_achievements",
    ]),
    ("游戏库", "库、最近游玩、时长分析", [
        "steam_library", "steam_recent_games", "steam_playtime_stats", "steam_library_diff",
    ]),
    ("商店", "搜索、详情、愿望单、购物车、结账、兑换", [
        "steam_store_search", "steam_app_details", "steam_wishlist", "steam_wishlist_edit",
        "steam_cart", "steam_free_license", "steam_checkout", "steam_checkout_session",
        "steam_redeem_wallet_code",
    ]),
    ("市场（只读）", "行情、我的挂单、跨平台比价", [
        "steam_inventory", "steam_market_price", "steam_market_search", "steam_market_listings",
        "steam_market_compare",
    ]),
    ("分析与价格", "折扣雷达、Deck 兼容、价格快照、历史低价", [
        "steam_deals", "steam_app_extras", "steam_deck_compat", "steam_price_watch",
        "steam_price_history", "steam_itad_key",
    ]),
    ("账号历史", "购买与许可记录", ["steam_purchase_history"]),
    ("内容", "新闻、评测", ["steam_news", "steam_reviews"]),
    ("网易 BUFF", "第三方市场行情", [
        "steam_buff_login", "steam_buff_status", "steam_buff_logout", "steam_buff_search", "steam_buff_price",
    ]),
    ("通用", "已登录任意请求", ["steam_request"]),
]


def fetch_tools() -> list[dict]:
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "docgen", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    p = subprocess.run(
        [PY, "mcp_server.py", "--env", f"STEAM_MCP_HOME={ROOT}/data"],
        input="\n".join(json.dumps(m) for m in msgs) + "\n",
        cwd=ROOT, capture_output=True, text=True, timeout=90,
    )
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("id") == 2:
            return d["result"]["tools"]
    raise SystemExit("拿不到 tools/list\n" + p.stderr[-800:])


def fmt_params(schema: dict | None) -> str:
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    if not props:
        return "—"
    out = []
    for name, spec in props.items():
        t = spec.get("type") or "any"
        mark = "" if name in required else "?"
        default = spec.get("default")
        seg = f"`{name}{mark}`"
        if default not in (None, "", []):
            seg += f"={default}"
        out.append(seg)
    return "，".join(out)


def main() -> None:
    tools = fetch_tools()
    by_name = {t["name"]: t for t in tools}
    version = json.loads(subprocess.run(
        [PY, "-c", "import json,pathlib,re;p=pathlib.Path('steam_mcp/server.py').read_text();"
                   "print(json.dumps(re.search(r'version=\"([^\"]+)\"',p).group(1)))"],
        cwd=ROOT, capture_output=True, text=True).stdout or '""')

    print(f"""# 工具总览

> 自动生成，勿手改。重新生成：`python3 scripts/gen_tools_doc.py > TOOLS.md`
> 当前版本 **v{version}**，共 **{len(tools)}** 个工具。
>
> 参数列里带 `?` 的是可选参数。所有工具都返回 JSON 对象，失败时带 `error` 码与 `hint`，
> 不抛裸异常。写操作与花钱操作在说明开头标了 `[写入]` / `[花钱]`。

## 约定

| 约定 | 说明 |
|---|---|
| 返回值 | 始终是 dict；成功 `ok: true`，失败 `ok: false` + `error` + `hint` |
| 凭证 | cookie / token / 兑换码只出现在请求体里，返回值只给 `len=… sha256:…` 或 `ABCD****` 指纹 |
| 只读 | 标注 `[只读]` 的工具不会改动任何状态 |
| 写入门槛 | 标注 `[写入]` 需要 `confirm=true`；标注 `[花钱]` 还需要 `behavior.allow_sensitive=true` |
| 限速 | store 0.4s/次、community 2s/次、api 0.3s/次，内置退避重试 |
| 批量 | 长任务返回 `{{scanned, remaining, has_more}}` 之类进度字段，不阻塞到超时 |
""")

    covered: set[str] = set()
    for title, subtitle, names in GROUPS:
        rows = [by_name[n] for n in names if n in by_name]
        if not rows:
            continue
        print(f"\n## {title}\n\n{subtitle}\n")
        print("| 工具 | 参数 | 说明 |")
        print("|---|---|---|")
        for t in rows:
            covered.add(t["name"])
            desc = (t.get("description") or "").replace("\n", " ").replace("|", "\\|")
            print(f"| `{t['name']}` | {fmt_params(t.get('inputSchema'))} | {desc} |")

    missing = [t for t in tools if t["name"] not in covered]
    if missing:
        print("\n## 其他\n\n| 工具 | 参数 | 说明 |")
        print("|---|---|---|")
        for t in missing:
            desc = (t.get("description") or "").replace("\n", " ").replace("|", "\\|")
            print(f"| `{t['name']}` | {fmt_params(t.get('inputSchema'))} | {desc} |")

    print("""
## 错误码速查

| error | 含义 | 处理 |
|---|---|---|
| `confirmation_required` | 写/花钱操作缺 `confirm` 或 `allow_sensitive` | 补齐后重试 |
| `not_logged_in` | 没有可用会话 | `steam_auth_login` 或 `steam_auth_import_cookie` |
| `bad_input` | 参数不合格 | 按 hint 修正 |
| `library_unavailable` | 游戏库拿不到（可能私密） | 检查档案可见性 |
| `wishlist_unavailable` | 愿望单为空或私密 | 同上 |
| `mylistings_unavailable` | 市场挂单读不到 | 确认登录且 community 域可达 |
| `licenses_unavailable` | 许可页结构变化或未登录 | `steam_auth_status` 排查 |
| `itad_key_missing` | 没配 ITAD key | `steam_itad_key` 写入 |
| `refresh_rejected` | 续期凭证失效 | 重新登录（refresh token 无法自愈） |
| `Blocked` | 域名不在白名单 | 该域名不支持，换官方接口 |
""")


if __name__ == "__main__":
    main()
