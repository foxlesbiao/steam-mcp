"""购物车页解析的离线验证（不联网、不碰凭证）。

跑法：./.venv/bin/python tests/test_cart_parse.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["STEAM_MCP_HOME"] = tempfile.mkdtemp(prefix="steam-mcp-cartparse-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import checkout  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# 真实购物车页的形态：JSON 字符串里内嵌了一层转义的 line_item_id / packageid
ONE = r'{"loaderData":["{\"line_item_id\":\"5326109652\",\"item_id\":{\"packageid\":423337}}"]}'
DUP = (
    r'{"loaderData":["{\"line_item_id\":\"111\",\"item_id\":{\"packageid\":470673}},'
    r'{\"line_item_id\":\"222\",\"item_id\":{\"packageid\":423337}},'
    r'{\"line_item_id\":\"222\",\"item_id\":{\"packageid\":423337}}"]}'
)
# 空购物车页：只有空状态组件文案，没有任何行项目
EMPTY = '<div class="cart_empty">购物车是空的</div><p>Your cart is empty</p>'


def test_one_item() -> None:
    items, _ = checkout._parse_cart_html(ONE)
    check("单车载：抓到 1 项", len(items) == 1, str(items))
    check("单车载：line_item_id 正确", items and items[0].get("line_item_id") == "5326109652", str(items))
    check("单车载：packageid 已解析", items and items[0].get("packageid") == 423337, str(items))


def test_dedup() -> None:
    items, _ = checkout._parse_cart_html(DUP)
    check("重复行项目去重后剩 2 项", len(items) == 2, str(items))
    check("去重保留不同 packageid",
          sorted(str(i.get("packageid")) for i in items) == ["423337", "470673"], str(items))


def test_empty_page() -> None:
    items, _ = checkout._parse_cart_html(EMPTY)
    check("空车页 → 0 项（不被'购物车是空的'文案误判为有货）", items == [], str(items))


def main() -> None:
    print("购物车解析离线测试")
    test_one_item()
    test_dedup()
    test_empty_page()
    print(f"\n结果：{len(FAILS)} 个失败" + ("" if not FAILS else f" -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
