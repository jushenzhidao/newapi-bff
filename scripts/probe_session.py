"""探测 new-api 会话上限（AUTH_SESSION_LIMIT）与会话清理接口。

背景：反复 POST /api/user/login 会耗尽会话配额，之后返回
  409 {"code":"AUTH_SESSION_LIMIT","message":"Conflict"}
且**再也登不进去**。BFF 每次兑换码登录都要 login 一次，必须解决。
"""
import asyncio
import os
import sys
from pathlib import Path

# 按文件位置推导项目根，不写死绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BASE = os.getenv("NEWAPI_BASE_URL", "https://api.aihuobao.cn")
# 管理员凭证只从环境变量读，不留默认值 —— 本仓库有公开 remote，
# 写进源码等于随仓库分发管理员密码。
ADMIN_U = os.getenv("NEWAPI_ADMIN_USERNAME", "")
ADMIN_P = os.getenv("NEWAPI_ADMIN_PASSWORD", "")
if not (ADMIN_U and ADMIN_P):
    sys.exit("需设置 NEWAPI_ADMIN_USERNAME / NEWAPI_ADMIN_PASSWORD 环境变量后再运行")


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0, trust_env=False) as c:
        # 探测会话相关端点是否存在（未鉴权也能看出 404 vs 401）
        for method, path in [
            ("GET", "/api/user/auth/sessions"),
            ("GET", "/api/user/sessions"),
            ("DELETE", "/api/user/auth/sessions"),
            ("POST", "/api/user/auth/logout-all"),
            ("GET", "/api/user/logout"),
        ]:
            r = await c.request(method, path)
            print(f"{method:6} {path:34} -> {r.status_code}  {r.text[:120]}")

        # 带 cookie 的登录：观察是否复用会话
        print("\n--- 复用 cookie 登录 ---")
        async with httpx.AsyncClient(base_url=BASE, timeout=20.0, trust_env=False) as c2:
            for i in range(3):
                r = await c2.post("/api/user/login",
                                  json={"username": ADMIN_U, "password": ADMIN_P})
                print(f"  第{i+1}次 -> {r.status_code} {r.text[:120]}")
                print(f"       cookies: {list(c2.cookies.keys())}")


asyncio.run(main())
