"""探测 new-api 兑换码（redemption）相关接口契约。

目的：确认 BFF 能否在「兑换码登录」流程中，于建号之前先校验兑换码是否有效，
避免无效码也创建影子账号（垃圾账号）。
"""
import asyncio
import json
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
        r = await c.post("/api/user/login", json={"username": ADMIN_U, "password": ADMIN_P})
        d = r.json()["data"]
        uid = d["user"]["id"]
        h0 = {"Authorization": f"Bearer {d['access_token']}", "New-Api-User": str(uid)}
        pat = (await c.get("/api/user/token", headers=h0)).json()["data"]
        H = {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}
        print("admin uid:", uid)

        # 创建兑换码：quota 单位是内部 quota（500000 = 1 元）
        payload = {"name": "bff_probe", "quota": 500000, "count": 2}
        r = await c.post("/api/redemption/", headers=H, json=payload)
        print(f"\n=== POST /api/redemption/ {payload} -> {r.status_code}")
        print(r.text[:1500])

        # 列表反查
        r = await c.get("/api/redemption/", headers=H, params={"p": 1, "page_size": 10})
        print(f"\n=== GET /api/redemption/ -> {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2500])

        # 搜索指定码
        r = await c.get("/api/redemption/search", headers=H,
                        params={"keyword": "bff_probe", "p": 1, "page_size": 10})
        print(f"\n=== GET /api/redemption/search?keyword=bff_probe -> {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2500])


asyncio.run(main())
