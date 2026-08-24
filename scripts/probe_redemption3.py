"""验证：1) 核销后兑换码记录状态  2) 管理员删号接口（用于回滚无效码建的垃圾账号）"""
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


async def admin_headers(c):
    r = await c.post("/api/user/login", json={"username": ADMIN_U, "password": ADMIN_P})
    d = r.json()["data"]
    uid = d["user"]["id"]
    h0 = {"Authorization": f"Bearer {d['access_token']}", "New-Api-User": str(uid)}
    pat = (await c.get("/api/user/token", headers=h0)).json()["data"]
    return {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0, trust_env=False) as c:
        H = await admin_headers(c)

        # 1. 全量列表看核销后状态
        r = await c.get("/api/redemption/", headers=H, params={"p": 1, "page_size": 20})
        print("=== redemption list after redeem ===")
        print(json.dumps(r.json()["data"], ensure_ascii=False, indent=2)[:2000])

        # 2. 不存在的用户名登录 —— 看返回什么（用于「派生账号是否已存在」判定）
        r = await c.post("/api/user/login",
                         json={"username": "rc_nonexistent_zzz", "password": "whatever12345"})
        print("\n=== login nonexistent user ===")
        print(r.status_code, r.text[:300])

        # 3. 删除测试用户（回滚能力验证）
        r = await c.get("/api/user/search", headers=H,
                        params={"keyword": "bff_rc_probe1", "p": 1, "page_size": 10})
        items = r.json()["data"]["items"]
        print("\nfound:", [(i["id"], i["username"]) for i in items])
        if items:
            tuid = items[0]["id"]
            r = await c.delete(f"/api/user/{tuid}", headers=H)
            print(f"DELETE /api/user/{tuid} ->", r.status_code, r.text[:300])
            # 也试试 manage delete
            r2 = await c.get("/api/user/search", headers=H,
                             params={"keyword": "bff_rc_probe1", "p": 1, "page_size": 10})
            print("after delete search:", r2.json()["data"]["total"])


asyncio.run(main())
