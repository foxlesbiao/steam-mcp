"""stdio 启动入口。

MCP 客户端配置示例（路径换成你本机的绝对路径）：
{
  "steam": {
    "type": "stdio",
    "command": "/path/to/.venv/bin/python",
    "args": ["/path/to/mcp_server.py"]
  }
}

Windows 下 command 用 .venv\\\\Scripts\\\\python.exe。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from steam_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
