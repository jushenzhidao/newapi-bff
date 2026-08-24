"""探测 new-api 密码字段的最大长度限制（派生密码长度必须落在合法区间）。"""
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
        r = await c.post("/api/user/login", json={"username": ADMIN_U, "password": ADMIN_P})
        d = r.json()["data"]
        uid = d["user"]["id"]
        h0 = {"Authorization": f"Bearer {d['access_token']}", "New-Api-User": str(uid)}
        pat = (await c.get("/api/user/token", headers=h0)).json()["data"]
        H = {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}

        created = []
        for n in (16, 20, 24, 28, 32):
            uname = f"bff_pwdlen_{n}"
            r = await c.post("/api/user/", headers=H,
                             json={"username": uname, "password": "a" * n,
                                   "display_name": uname})
            j = r.json()
            print(f"len={n:>3} create -> success={j.get('success')} msg={j.get('message','')[:90]}")
            if j.get("success"):
                created.append(uname)
                # 顺便验证能否用该密码登录
                lr = await c.post("/api/user/login",
                                  json={"username": uname, "password": "a" * n})
                print(f"        login -> success={lr.json().get('success')}")

        # 清理
        for uname in created:
            r = await c.get("/api/user/search", headers=H,
                            params={"keyword": uname, "p": 1, "page_size": 10})
            for i in r.json()["data"]["items"]:
                if i["username"] == uname:
                    await c.delete(f"/api/user/{i['id']}", headers=H)
                    print("cleanup", uname)


asyncio.run(main())
