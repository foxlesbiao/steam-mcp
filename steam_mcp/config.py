"""配置与路径管理。

配置文件默认位于 <项目>/data/config.json，可用环境变量 STEAM_MCP_HOME 覆盖目录。
配置文件里只放非敏感设置；凭证单独由 secrets.py 加密保存。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "steam-mcp"

DEFAULT_CONFIG: dict[str, Any] = {
    "network": {
        # direct = 忽略系统代理环境变量，直连（配合 Steamcommunity 302 之类本地反代必须用这个）
        # env    = 跟随 HTTP_PROXY/HTTPS_PROXY
        # custom = 使用 network.proxy 指定的地址
        "mode": "direct",
        "proxy": None,
        # system = 用 Windows 证书库（能信任本地反代的自签根证书）
        # certifi = 仅公共 CA；insecure = 不校验证书（不推荐）
        "tls_trust": "system",
        "timeout": 25,
        "retries": 2,
        # 每个域名的请求最小间隔（秒），Steam 对高频访问很敏感。
        # steamcommunity.com 实测 2s 仍会撞 429（/dev/apikey、/market/history 都踩过），放到 5s。
        "min_interval": {
            "store.steampowered.com": 0.4,
            "steamcommunity.com": 5.0,
            "api.steampowered.com": 0.3,
            "login.steampowered.com": 0.5,
            "buff.163.com": 1.5,
            "checkout.steampowered.com": 1.0,
        },
    },
    "webapi": {
        # 可选：Steam Web API Key，用于公开数据接口（https://steamcommunity.com/dev/apikey）
        "key": None,
    },
    "behavior": {
        # 是否允许"花钱 / 改账号安全设置 / 接受交易"这类不可逆操作
        "allow_sensitive": False,
        # 登录态自动续期：过期时用 refresh token 静默换一套新 cookie（含各域名完整 cookie）。
        # 关掉就只能手动 steam_auth_refresh 或重新登录。
        "auto_renew": True,
        # 两次会话校验的最小间隔（秒），避免每个工具调用都多打一次网络请求。
        "renew_check_seconds": 600,
        "max_items": 60,
        "language": "schinese",
        "country": "CN",
    },
}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def home_dir() -> Path:
    raw = os.environ.get("STEAM_MCP_HOME")
    path = Path(raw).expanduser() if raw else project_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home_dir() / "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        save(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_CONFIG)
    return _deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})


def save(config: dict[str, Any]) -> Path:
    path = config_path()
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update(patch: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(load(), patch)
    save(merged)
    return merged
