"""探测管理员修改用户（改用户名/密码）接口，用于「兑换码用户升级为正式账号」。"""
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
        uname, pwd = "bff_upd_probe", "UpdProbe123456"
        await c.post("/api/user/", headers=H,
                     json={"username": uname, "password": pwd, "display_name": uname})
        r = await c.get("/api/user/search", headers=H,
                        params={"keyword": uname, "p": 1, "page_size": 10})
        items = [i for i in r.json()["data"]["items"] if i["username"] == uname]
        if not items:
            print("create failed")
            return
        u = items[0]
        tuid = u["id"]
        print("uid:", tuid, "| fields:", sorted(u.keys()))

        # 1. 管理员改用户名 + 密码：PUT /api/user/
        newname, newpwd = "bff_upd_renamed", "Renamed1234567"
        body = {"id": tuid, "username": newname, "password": newpwd,
                "display_name": u.get("display_name") or newname}
        r = await c.put("/api/user/", headers=H, json=body)
        print("\nPUT /api/user/ ->", r.status_code, r.text[:300])

        # 2. 验证新账密可登录
        r = await c.post("/api/user/login", json={"username": newname, "password": newpwd})
        print("login with new creds ->", r.status_code, r.text[:200])

        # 3. 验证旧账密失效
        r = await c.post("/api/user/login", json={"username": uname, "password": pwd})
        print("login with old creds ->", r.status_code, r.text[:200])

        # 4. 改成已存在的用户名，看报错（借用管理员自己的用户名，它必然存在）
        r = await c.put("/api/user/", headers=H,
                        json={"id": tuid, "username": ADMIN_U, "display_name": "x"})
        print("\nrename to existing ->", r.status_code, r.text[:300])

        # 清理
        r = await c.get("/api/user/search", headers=H,
                        params={"keyword": "bff_upd", "p": 1, "page_size": 10})
        for i in r.json()["data"]["items"]:
            d = await c.delete(f"/api/user/{i['id']}", headers=H)
            print("cleanup delete", i["id"], i["username"], d.status_code)


asyncio.run(main())
