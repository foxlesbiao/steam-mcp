"""结账/余额逻辑的不联网验证：金额解析、finalize 各道护栏、getfinalprice 一次性缓存。

跑法：隔离临时 STEAM_MCP_HOME，不碰真实凭证库，也不发任何网络请求。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

os.environ["STEAM_MCP_HOME"] = tempfile.mkdtemp(prefix="steam-mcp-checkout-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam_mcp import checkout, config, http  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


print("== 1) 金额解析（余额/抵扣金额都是展示串）==")
cases = {
    "Rp 1 857.17": 185717,
    "Rp 19 649": 1964900,
    "¥ 8.70": 870,
    "CN¥ 1,234.56": 123456,
    "Rp 0": 0,
    "": None,
}
for text, want in cases.items():
    got = checkout._parse_money_cents(text)
    check(f"{text!r} -> {want}", got == want, f"得到 {got}")

print("== 2) finalize 三道硬门槛 ==")
cfg = config.load()
cfg["behavior"]["allow_sensitive"] = False
config.save(cfg)

checkout._require_checkout = lambda: {"sessionid": "x"}  # 避免依赖凭证库
_real_final_price = checkout.final_price  # 第 5 节要还原成真实现
_real_wallet_balance = checkout._wallet_balance
checkout.final_price = lambda transid, refresh=True: {  # 固定价格
    "ok": True, "total": 100000, "wallet_charge": "Rp 1 000", "external_charge": "Rp 0",
    "wallet_balance": "Rp 5 000.00", "wallet_sufficient": True,
}

def expect_error(fn, want_code: str) -> tuple[bool, str]:
    try:
        res = fn()
        return False, f"没有抛错，返回 {str(res)[:120]}"
    except http.SteamError as exc:
        return exc.code == want_code, exc.code
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


ok, detail = expect_error(lambda: checkout.finalize("t1", expected_total=100000, confirm=False), "confirmation_required")
check("confirm=false 被拦", ok, detail)

ok, detail = expect_error(lambda: checkout.finalize("t1", expected_total=100000, confirm=True), "confirmation_required")
check("allow_sensitive=false 被拦", ok, detail)

cfg["behavior"]["allow_sensitive"] = True
config.save(cfg)

ok, detail = expect_error(lambda: checkout.finalize("t1", expected_total=None, confirm=True), "bad_input")
check("缺 expected_total 被拦", ok, detail)

print("== 3) 价格变动 / 余额不足 / 真外部支付 ==")
r = checkout.finalize("t1", expected_total=99999, confirm=True)
check("价格不一致 -> price_changed", r.get("blocked") == "price_changed", str(r))

checkout.final_price = lambda transid, refresh=True: {
    "ok": True, "total": 100000, "wallet_charge": "Rp 100 000", "external_charge": "Rp 0",
    "wallet_balance": "Rp 1 857.17", "wallet_sufficient": False, "shortfall_cents": 98143,
}
r = checkout.finalize("t2", expected_total=100000, confirm=True)
check("余额不足 -> insufficient_wallet", r.get("blocked") == "insufficient_wallet", str(r))
check("拦住时没有 charged", "charged" not in r or r.get("charged") is None, str(r))

checkout.final_price = lambda transid, refresh=True: {
    "ok": True, "total": 100000, "wallet_charge": "Rp 50 000", "external_charge": "Rp 500",
    "wallet_balance": "Rp 1 000.00", "wallet_sufficient": False,
}
r = checkout.finalize("t3", expected_total=100000, confirm=True)
check("余额不足时优先判 insufficient_wallet", r.get("blocked") == "insufficient_wallet", str(r))

checkout.final_price = lambda transid, refresh=True: {
    "ok": True, "total": 100000, "wallet_charge": "Rp 50 000", "external_charge": "Rp 500",
    "wallet_balance": "Rp 500 000.00", "wallet_sufficient": True,
}
r = checkout.finalize("t4", expected_total=100000, confirm=True)
check("真外部支付 -> external_payment_required", r.get("blocked") == "external_payment_required", str(r))

print("== 4) 「Rp 0」不能当成需要外部支付 ==")
sent: list[str] = []


class FakeResp:
    status = 200
    text = '{"success": 22, "purchaseresultdetail": 0}'

    def json(self):
        return {"success": 22, "purchaseresultdetail": 0}


def fake_request(method, url, **kwargs):
    sent.append(url)
    return FakeResp()


http.request = fake_request
checkout.final_price = lambda transid, refresh=True: {
    "ok": True, "total": 100000, "wallet_charge": "Rp 1 000", "external_charge": "Rp 0",
    "wallet_balance": "Rp 500 000.00", "wallet_sufficient": True,
}
r = checkout.finalize("t5", expected_total=100000, confirm=True)
check("Rp 0 不再误判，真的发了扣款请求", any("finalizetransaction" in u for u in sent), str(sent))
check("charged=true", r.get("charged") is True, str(r))

print("== 5) getfinalprice 一次性：缓存命中不重复请求 ==")
checkout.final_price = _real_final_price  # 还原真实现，前后几节的 mock 都撤掉
checkout._wallet_balance = lambda: {"found": True, "formatted": "Rp 1 857.17", "cents": 185717}
calls: list[str] = []


def counting_request(method, url, **kwargs):
    calls.append(url)
    return type("R", (), {
        "status": 200,
        "text": '{"success": 1, "total": 4200, "formattedSteamAccountTotal": "Rp 42", "formattedProviderTotal": "Rp 0"}',
        "json": lambda self=None: {"success": 1, "total": 4200, "formattedSteamAccountTotal": "Rp 42",
                                   "formattedProviderTotal": "Rp 0"},
    })()


http.request = counting_request
checkout._wallet_balance = lambda: {"found": True, "formatted": "Rp 1 857.17", "cents": 185717}
checkout._PRICE_CACHE.clear()
first = checkout.final_price("t6")
check("首次请求成功并缓存", first.get("ok") and "t6" in checkout._PRICE_CACHE, str(first))
n_before = len(calls)
second = checkout.final_price("t6", refresh=False)
check("refresh=False 命中缓存不再请求", len(calls) == n_before and second.get("cached"), str(second))
check("缓存里余额判定正确", second.get("wallet_sufficient") is True)

print("== 6) 新版购物车服务（AccountCart）==")
# varint 编码：5302727559 应解回原值
raw = checkout._pb_varint(5302727559)
val, shift, idx = 0, 0, 0
while True:
    b = raw[idx]
    idx += 1
    val |= (b & 0x7F) << shift
    shift += 7
    if not b & 0x80:
        break
check("protobuf varint 往返正确", val == 5302727559 and idx == len(raw), f"{val} / {raw.hex()}")

# token 提取：兼容 HTML 里被转义的 \\"strWebAPIToken\\":\\"xxx\\"
sample = '...{\\"strWebAPIToken\\":\\"eyJhbGciOiJI.abc.def\\",\\"x\\":1}...'
m = re.search(r'strWebAPIToken\\+"\s*:\s*\\+"([^"\\]+)', sample)
check("能从转义 SSR 里提取 token", bool(m) and m.group(1) == "eyJhbGciOiJI.abc.def", str(m and m.group(1)))

# cart('remove'/'clear') 走新服务
checkout.auth.require_login = lambda: {"steamid": "7656119"}  # 隔离环境没有凭证，跳过登录校验
checkout.remove_line_item = lambda lid: {"ok": True, "action": "remove", "line_item_id": lid, "service": {"ok": True}}
checkout.empty_cart = lambda: {"ok": True, "action": "clear", "remaining": []}
r = checkout.cart("remove", line_item_id="123")
check("cart(remove) 委托给 remove_line_item", r.get("ok") and r.get("action") == "remove", str(r))
r = checkout.cart("clear")
check("cart(clear) 委托给 empty_cart", r.get("ok") and r.get("action") == "clear", str(r))

cfg["behavior"]["allow_sensitive"] = False
config.save(cfg)
print("allow_sensitive 已复位:", config.load()["behavior"]["allow_sensitive"])

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASS")
