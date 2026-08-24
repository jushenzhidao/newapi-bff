"""验证兑换码 search 是否支持按完整 key 精确检索，以及核销后 used_user_id 是否回填。

这两点决定「兑换码登录」能否做成真正的身份绑定：
  首次登录 -> 建影子号 -> 核销码 -> used_user_id 记录该号
  再次登录 -> 按 key 搜到记录 -> 取 used_user_id -> 找回同一账号
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

CODE_A = "4ee5ee690c5b4edead59d4e6394f2bb7"
CODE_B = "7b464902fe40400f9b0641a2757b7018"


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

        # 1. 按完整 key 精确搜索
        for kw in [CODE_A, CODE_A[:8], "不存在的码xyz"]:
            r = await c.get("/api/redemption/search", headers=H,
                            params={"keyword": kw, "p": 1, "page_size": 10})
            items = r.json().get("data", {}).get("items", [])
            print(f"search keyword={kw!r} -> total={r.json()['data']['total']} "
                  f"keys={[i['key'][:12] for i in items]}")

        # 2. 建一个测试用户，用 CODE_A 核销，看 used_user_id 是否回填
        uname = "bff_rc_probe1"
        pwd = "RcProbe123456"
        r = await c.post("/api/user/", headers=H,
                         json={"username": uname, "password": pwd, "display_name": uname})
        print("\ncreate user:", r.status_code, r.text[:200])
        r = await c.get("/api/user/search", headers=H,
                        params={"keyword": uname, "p": 1, "page_size": 10})
        tuid = None
        for it in r.json()["data"]["items"]:
            if it["username"] == uname:
                tuid = it["id"]
        print("test uid:", tuid)

        # 用户登录换 PAT
        r = await c.post("/api/user/login", json={"username": uname, "password": pwd})
        d = r.json()["data"]
        h0 = {"Authorization": f"Bearer {d['access_token']}", "New-Api-User": str(tuid)}
        upat = (await c.get("/api/user/token", headers=h0)).json()["data"]
        UH = {"Authorization": f"Bearer {upat}", "New-Api-User": str(tuid)}

        # 核销
        r = await c.post("/api/user/topup", headers=UH, json={"key": CODE_A})
        print("topup:", r.status_code, r.text[:300])

        # 3. 再查该码
        r = await c.get("/api/redemption/search", headers=H,
                        params={"keyword": CODE_A, "p": 1, "page_size": 10})
        print("\nafter redeem:")
        print(json.dumps(r.json()["data"]["items"], ensure_ascii=False, indent=2))

        # 4. 已核销的码再次核销
        r = await c.post("/api/user/topup", headers=UH, json={"key": CODE_A})
        print("\nre-topup used code:", r.status_code, r.text[:300])

        # 5. 无效码核销
        r = await c.post("/api/user/topup", headers=UH, json={"key": "invalidcode123456"})
        print("topup invalid:", r.status_code, r.text[:300])


asyncio.run(main())
