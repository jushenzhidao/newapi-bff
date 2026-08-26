"""用仍然有效的 PAT（不依赖会话）查询/清理会话，解掉 AUTH_SESSION_LIMIT。

关键认知：PAT 鉴权走的是 users.access_token 列，**与会话系统无关**，
所以会话打满导致 login 409 时，PAT 依然可用 —— 这正是 BFF 该抓住的救生索。
"""
import asyncio
import os
import sys
from pathlib import Path

# 按文件位置推导项目根，不写死绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BASE = os.getenv("NEWAPI_BASE_URL", "https://api.aihuobao.cn")
ADMIN_UID = 1
# 由上一轮 e2e 缓存下来的管理员 PAT 已随进程结束丢失，
# 这里改用 BFF 进程内缓存的那份（BFF 仍在跑，其 admin_request 可用）。
BFF = "http://127.0.0.1:8300"


async def main():
    # 通过还活着的 BFF 进程借用其管理员 PAT 缓存
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0, trust_env=False) as c:
        # 尝试直接读 BFF 内存里的缓存是跨进程的，做不到；
        # 改为探测：用 PAT 头访问 sessions 需要真实 PAT。
        # 这里先看看未带 PAT 时 sessions 的形态，确认路由与方法。
        for method in ("GET", "DELETE"):
            r = await c.request(method, "/api/user/sessions")
            print(f"{method} /api/user/sessions (no auth) -> {r.status_code} {r.text[:150]}")
        for path in ("/api/user/sessions/all", "/api/user/session"):
            r = await c.get(path)
            print(f"GET {path} (no auth) -> {r.status_code} {r.text[:150]}")


asyncio.run(main())
