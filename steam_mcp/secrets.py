"""凭证存储：Windows 上用 DPAPI 加密（绑定当前用户），其他平台用 AES-GCM + 本地密钥。

落盘内容只有密文；解密失败时按"未登录"处理，不抛异常给调用方。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import config

_IS_WIN = sys.platform == "win32"


def _vault_path() -> Path:
    return config.home_dir() / "credentials.bin"


def _key_path() -> Path:
    return config.home_dir() / ".vault.key"


# ---------------------------------------------------------------- Windows DPAPI


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN

    worker = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = [ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)]
    if not worker(*args):
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------- 非 Windows 回落


def _local_key() -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

    path = _key_path()
    if not path.exists():
        key = os.urandom(32)
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key
    return path.read_bytes()


def _aes(data: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(_local_key())
    if encrypt:
        nonce = os.urandom(12)
        return nonce + aes.encrypt(nonce, data, b"steam-mcp")
    return aes.decrypt(data[:12], data[12:], b"steam-mcp")


# ---------------------------------------------------------------- 对外接口


def load_credentials() -> dict[str, Any]:
    path = _vault_path()
    if not path.exists():
        return {}
    try:
        blob = path.read_bytes()
        plain = _dpapi(blob, protect=False) if _IS_WIN else _aes(blob, encrypt=False)
        data = json.loads(plain.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # 密文被拷到别的机器 / 用户下，或文件损坏 —— 视为未登录
        return {}


def save_credentials(data: dict[str, Any]) -> Path:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    blob = _dpapi(raw, protect=True) if _IS_WIN else _aes(raw, encrypt=True)
    path = _vault_path()
    path.write_bytes(blob)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_credentials() -> bool:
    path = _vault_path()
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def fingerprint(value: str | None) -> str:
    """用于日志/状态展示的不可逆指纹，绝不输出明文。"""
    import hashlib

    if not value:
        return "-"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"len={len(value)} sha256:{digest}"
