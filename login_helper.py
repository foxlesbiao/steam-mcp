"""本地登录助手：在终端里完成登录，凭证直接进加密库，不经过对话记录。

用法：
    .venv\\Scripts\\python.exe login_helper.py

选 1 = 粘贴浏览器 cookie（推荐）
选 2 = 账号密码登录（需要 api.steampowered.com 可达）
选 3 = 查看当前状态
选 4 = 网络自检

密码登录顺序（1.2.1 起）：先等待手机 Steam App / 邮箱确认（无需输码），
等不到才弹出验证码输入框；验证码直接回车会跳过，不会报「验证码为空」。
等待时长可用环境变量 STEAM_MCP_CONFIRM_WAIT_SECONDS 调整（默认 60 秒）。
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from steam_mcp import api, auth, config, http  # noqa: E402


def _confirm_wait() -> float:
    raw = os.environ.get("STEAM_MCP_CONFIRM_WAIT_SECONDS", "60")
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


def show(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def do_cookie() -> None:
    print(
        "\n浏览器登录 Steam 后：F12 → Application（应用）→ Cookies → https://store.steampowered.com\n"
        "找到 steamLoginSecure，右键 Copy value，粘贴到下面（输入不回显，粘贴后回车）。\n"
        "想自动续期的话，再单独粘一次 steamRefresh_steam。留空跳过。\n"
    )
    login_secure = getpass.getpass("steamLoginSecure: ").strip()
    if not login_secure:
        print("没有输入，取消。")
        return
    refresh = getpass.getpass("steamRefresh_steam（可留空）: ").strip()
    blob = f"steamLoginSecure={login_secure}"
    if refresh:
        blob += f"; steamRefresh_steam={refresh}"
    try:
        result = auth.import_cookie(blob, verify=True)
    except http.SteamError as exc:
        print("登录失败：", json.dumps(exc.as_dict(), ensure_ascii=False))
        return
    show(result)


def do_password() -> None:
    print("\n账号密码只在本进程内存里做 RSA 加密后发给 Steam，不写磁盘。\n")
    username = input("Steam 账号名: ").strip()
    password = getpass.getpass("密码: ")
    if not username or not password:
        print("账号或密码为空。")
        return
    try:
        result = auth.login_with_password(username, password)
    except http.SteamError as exc:
        print("登录失败：", json.dumps(exc.as_dict(), ensure_ascii=False))
        return
    show(result)

    stage = result.get("stage")
    if stage == "logged_in":
        return
    if stage == "need_cookie":
        print("\n身份验证通过了，但本地反代没把 steamLoginSecure 下发给我们。")
        print("补一步就好：从浏览器复制这个 cookie（详见下面提示）。")
        do_cookie()
        return

    kinds = result.get("allowed_confirmations") or []
    confirm_types = result.get("confirmation_types") or []
    code_types = result.get("code_types") or []
    labels = result.get("guard_labels") or {}
    suggested = result.get("suggested_code_type", 3)
    wait = _confirm_wait()

    # ---- 第一步：先等待手机 Steam App / 邮箱确认（无需输验证码） ----
    if confirm_types:
        desc = "、".join(f"{t}={labels.get(str(t), '确认')}" for t in confirm_types)
        print(f"\n[1/2] 检测到可在手机/邮箱直接确认：{desc}")
        print("      请在手机 Steam App 上点「确认登录」（邮箱确认则去邮箱点链接）。")
        print(f"      正在等待确认，最多 {wait:.0f} 秒；这段时间不用输入任何验证码。")
        print("      （等待时长可用环境变量 STEAM_MCP_CONFIRM_WAIT_SECONDS 调整）")
        try:
            polled = auth.poll_login(timeout=wait)
        except KeyboardInterrupt:
            print("\n已中断等待，直接转到验证码方式。")
            polled = {}
        except http.SteamError as exc:
            print("轮询失败：", json.dumps(exc.as_dict(), ensure_ascii=False))
            polled = {}
        show(polled)
        if polled.get("stage") == "logged_in" or (polled.get("verification") or {}).get("valid"):
            print("\n确认成功，登录完成。")
            return
        if polled.get("stage") == "need_cookie":
            print("\n身份验证通过了，但本地反代没把 steamLoginSecure 下发给我们，补一次 cookie：")
            do_cookie()
            return
        print("\n等待时间内没有完成确认，下面才轮到验证码方式。\n")

    # ---- 第二步：确认没等到，才弹出验证码输入 ----
    if code_types:
        print(f"[2/2] 允许的验证方式（数字代表类型）: {kinds}")
        for k in sorted(labels, key=lambda x: int(x) if str(x).isdigit() else 999):
            print(f"      {k} = {labels[k]}")
        print("      2=邮箱验证码  3=手机令牌动态码  4=手机App确认  5=邮箱确认")
        code = getpass.getpass(
            "Steam Guard 验证码（手机令牌填动态码，邮箱填邮件码；直接回车=跳过，不会报错）: "
        ).strip()
        if not code:
            # 关键：回车不再触发 submit_guard_code("") → bad_input「验证码为空」
            print("\n已跳过验证码，没有向 Steam 提交任何东西，登录会话仍保留。")
            print("稍后在手机 App 完成确认后，重新运行本助手选 2，或调用 steam_auth_poll 继续。")
            return
        try:
            show(auth.submit_guard_code(code, suggested))
        except http.SteamError as exc:
            print("验证码提交失败：", json.dumps(exc.as_dict(), ensure_ascii=False))
        return

    # Steam 没给任何 confirmation_type：直接轮询（老行为）
    if stage == "awaiting_poll":
        print("请在手机 Steam App 上确认登录，然后按回车继续…")
        input()
        try:
            show(auth.poll_login())
        except http.SteamError as exc:
            print("轮询失败：", json.dumps(exc.as_dict(), ensure_ascii=False))
        return

    # 走到这里说明：只有确认类(4/5)、没有输码类，且等待已超时
    if confirm_types and not code_types:
        print("\n本次登录只支持手机/邮箱确认，等待已超时；登录会话仍保留。")
        print("在手机 App 完成确认后，重新运行本助手选 2，或调用 steam_auth_poll 继续。")
        return

    print("\n本次登录没有提供可用的验证方式；会话仍保留，可稍后重新运行本助手重试。")


def main() -> None:
    print("Steam MCP 本地助手")
    print(f"  数据目录: {config.home_dir()}")
    print("  1) 粘贴浏览器 cookie 登录（推荐）")
    print("  2) 账号密码登录（先等手机确认，等不到再输验证码）")
    print("  3) 查看登录状态")
    print("  4) 网络自检")
    choice = input("选择 [1-4]: ").strip()
    if choice == "1":
        do_cookie()
    elif choice == "2":
        do_password()
    elif choice == "3":
        show(auth.status())
    elif choice == "4":
        show(api.net_check())
    else:
        print("没选，退出。")


if __name__ == "__main__":
    main()
