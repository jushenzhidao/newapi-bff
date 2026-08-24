"""兑换码登录 E2E（真实环境 https://api.aihuobao.cn）。

覆盖：
  1. 首次输码 → 自动建号 + 核销到账
  2. 同码再登录 → 进同一账号，不重复到账
  3. 带横杠/大写的写法 → 归一化后仍是同一账号
  4. 伪造码 → **建号前**即被拦截，不残留垃圾账号
  5. 已被别人用过的码 → 拒绝
  6. 绑定正式账号 → 余额保留、新账密可登录、原码失效
  7. 兑换码账号标识 is_redeem_account 正确下发

关键语义（本版重点）：
  兑换码必须是 new-api 管理员真实创建的。BFF 在建号**之前**先调
  `GET /api/redemption/` 遍历比对 key，确认存在且 status=1（未使用）才放行。
  伪造码走不到建号那一步，因此不存在"先建号再回滚"的时间窗。

运行前需启动 BFF：uvicorn app.main:app --port 8300

凭证从环境变量读取（脚本不含任何明文凭证）：
    export NEWAPI_ADMIN_USERNAME=<管理员用户名>
    export NEWAPI_ADMIN_PASSWORD=<管理员密码>
    python scripts/e2e_redeem_login.py

可选覆盖：BFF_BASE_URL、NEWAPI_BASE_URL。
注意：本脚本会在真实环境建号删号，不要对生产库跑。
"""
import asyncio
import os
import sys
from pathlib import Path

# 按文件位置推导项目根，不写死绝对路径 —— 否则换机器、进 CI runner 全跑不了
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import redeem_code

BFF = os.getenv("BFF_BASE_URL", "http://127.0.0.1:8300")
NEWAPI = os.getenv("NEWAPI_BASE_URL", "https://api.aihuobao.cn")

# 管理员凭证只从环境变量读，绝不留默认值。
# 这个脚本要连真实环境建号删号，凭证等同后台管理员密码；写进源码
# 就等于随仓库一起分发出去，而本仓库有公开 remote。
ADMIN_U = os.getenv("NEWAPI_ADMIN_USERNAME", "")
ADMIN_P = os.getenv("NEWAPI_ADMIN_PASSWORD", "")
if not (ADMIN_U and ADMIN_P):
    sys.exit(
        "缺少管理员凭证。本脚本会在真实环境建号/删号，需显式提供：\n"
        "  export NEWAPI_ADMIN_USERNAME=<管理员用户名>\n"
        "  export NEWAPI_ADMIN_PASSWORD=<管理员密码>\n"
        f"可选：BFF_BASE_URL（当前 {BFF}）、NEWAPI_BASE_URL（当前 {NEWAPI}）\n"
        "离线回归请改用 e2e_redeem_mock.py，它不需要任何凭证。"
    )

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


async def admin_headers(c):
    # 管理员登录也受 CriticalRateLimit 约束，测试里反复重登容易撞限流，做退避重试
    for attempt in range(5):
        r = await c.post("/api/user/login",
                         json={"username": ADMIN_U, "password": ADMIN_P})
        body = r.json()
        if body.get("success"):
            break
        wait = 3 * (attempt + 1)
        print(f"  admin login 受限，{wait}s 后重试：{body.get('message', '')[:60]}")
        await asyncio.sleep(wait)
    else:
        raise RuntimeError("管理员登录持续失败（上游限流）")
    d = body["data"]
    uid = d["user"]["id"]
    h0 = {"Authorization": f"Bearer {d['access_token']}", "New-Api-User": str(uid)}
    pat = (await c.get("/api/user/token", headers=h0)).json()["data"]
    return {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}


_H_CACHE: dict = {}


async def _H(c):
    """缓存管理员头，只在失效时重取——避免频繁登录撞上游限流。"""
    if "h" not in _H_CACHE:
        _H_CACHE["h"] = await admin_headers(c)
    return _H_CACHE["h"]


async def _H_refresh(c):
    _H_CACHE.pop("h", None)
    return await _H(c)


async def make_codes(c, H, count: int, quota: int = 500000):
    for refresh in (False, True):
        h = await (_H_refresh(c) if refresh else _H(c))
        r = await c.post("/api/redemption/", headers=h,
                         json={"name": "bff_e2e_rc", "quota": quota, "count": count})
        body = r.json()
        if body.get("success"):
            return body["data"]
    raise RuntimeError(f"创建兑换码失败: {body.get('message')}")


async def find_user(c, H, username):
    """按用户名精确查找。管理员 PAT 可能已被 BFF 侧调用轮换失效，失效则重取一次。"""
    for refresh in (False, True):
        h = await (_H_refresh(c) if refresh else _H(c))
        r = await c.get("/api/user/search", headers=h,
                        params={"keyword": username, "p": 1, "page_size": 10})
        body = r.json()
        if body.get("success"):
            for i in body["data"]["items"]:
                if i["username"] == username:
                    return i
            return None
    return None


async def cleanup(c, H, usernames):
    for u in usernames:
        it = await find_user(c, H, u)
        if it:
            h = await _H(c)
            r = await c.delete(f"/api/user/{it['id']}", headers=h)
            if not r.json().get("success"):
                h = await _H_refresh(c)
                r = await c.delete(f"/api/user/{it['id']}", headers=h)
            print(f"  cleanup: deleted {u} (uid={it['id']})")


async def main():
    async with httpx.AsyncClient(base_url=NEWAPI, timeout=30.0, trust_env=False) as up:
        H = await admin_headers(up)
        codes = await make_codes(up, H, 4)
        print("生成测试兑换码:", [c[:10] + "..." for c in codes])
        CODE_MAIN, CODE_B, CODE_C, CODE_D = codes

        derived_names = [redeem_code.derive_account(x)[0] for x in codes]
        bound_name = "bff_e2e_bound1"
        await cleanup(up, H, derived_names + [bound_name])

        async with httpx.AsyncClient(base_url=BFF, timeout=30.0, trust_env=False) as bff:
            # ---------- 1. 首次输码 ----------
            r = await bff.post("/api/user/login/code", json={"code": CODE_MAIN})
            j = r.json()
            check("1. 首次输码成功", j.get("success") is True, j.get("message", ""))
            check("1a. 标记为新账号", j.get("data", {}).get("is_new") is True)
            check("1b. 到账 10000 积分（¥1）",
                  j.get("data", {}).get("points") == 10000,
                  f"points={j.get('data',{}).get('points')}")

            r = await bff.get("/api/user/self")
            self1 = r.json()["data"]
            uid1 = self1["id"]
            check("1c. 余额 = 10000 积分", self1["points"] == 10000, f"points={self1['points']}")
            check("1d. is_redeem_account=True", self1["is_redeem_account"] is True)
            check("1e. display_name 友好化",
                  self1["display_name"].startswith("卡号用户"), self1["display_name"])
            check("1f. 用户名为派生名",
                  self1["username"] == redeem_code.derive_account(CODE_MAIN)[0])

            # ---------- 2. 同码再登录 ----------
            bff.cookies.clear()
            r = await bff.post("/api/user/login/code", json={"code": CODE_MAIN})
            j = r.json()
            check("2. 同码再登录成功", j.get("success") is True, j.get("message", ""))
            check("2a. 标记为老账号（不重复到账）", j.get("data", {}).get("is_new") is False)
            r = await bff.get("/api/user/self")
            self2 = r.json()["data"]
            check("2b. 进入同一账号 uid 不变", self2["id"] == uid1, f"{uid1} vs {self2['id']}")
            check("2c. 余额未翻倍", self2["points"] == 10000, f"points={self2['points']}")

            # ---------- 3. 归一化：带横杠 + 大写 ----------
            bff.cookies.clear()
            weird = "-".join([CODE_MAIN[i:i + 8] for i in range(0, len(CODE_MAIN), 8)]).upper()
            r = await bff.post("/api/user/login/code", json={"code": weird})
            j = r.json()
            check("3. 带横杠大写写法可登录", j.get("success") is True, f"input={weird[:20]}...")
            r = await bff.get("/api/user/self")
            check("3a. 归一化后进同一账号", r.json()["data"]["id"] == uid1)

            # ---------- 4. 伪造码：管理员没发过的码 ----------
            # 这是本功能最关键的安全边界。旧实现允许任意字符串建号、
            # 靠核销失败再删号回滚兜底；现在改为建号前置校验，伪造码
            # 连 new-api 的用户表都碰不到。
            bff.cookies.clear()
            bogus = "zzzz1111zzzz2222zzzz3333zzzz4444"
            r = await bff.post("/api/user/login/code", json={"code": bogus})
            j = r.json()
            check("4. 伪造码被拒绝", j.get("success") is False, j.get("message", ""))
            check("4a. 提示语点明「不存在」", "不存在" in j.get("message", ""),
                  j.get("message", ""))
            leaked = await find_user(up, H, redeem_code.derive_account(bogus)[0])
            check("4b. 伪造码未残留垃圾账号", leaked is None,
                  f"leaked uid={leaked['id']}" if leaked else "")
            check("4c. 未下发会话（拒绝即无 cookie）",
                  not bff.cookies.get("session"),
                  f"cookies={dict(bff.cookies)}")

            # 多试几种"看起来很像"的伪造码，确认没有任何一种能开出账号
            fakes = [
                "abcdefghijklmnopqrstuvwxyz012345",   # 纯字母数字，长度合法
                CODE_MAIN[:-1] + ("0" if CODE_MAIN[-1] != "0" else "1"),  # 改真码最后一位
                "0" * 32,
            ]
            all_rejected, no_leak = True, True
            for fk in fakes:
                bff.cookies.clear()
                rj = (await bff.post("/api/user/login/code", json={"code": fk})).json()
                if rj.get("success"):
                    all_rejected = False
                if await find_user(up, H, redeem_code.derive_account(fk)[0]):
                    no_leak = False
            check("4d. 多种伪造码全部被拒", all_rejected)
            check("4e. 多种伪造码均未建号", no_leak)

            # ---------- 5. 格式非法 ----------
            r = await bff.post("/api/user/login/code", json={"code": "abc"})
            check("5. 过短兑换码被格式校验拦截",
                  r.json().get("success") is False, r.json().get("message", ""))

            # ---------- 6. 已被他人用过的码 ----------
            # CODE_B 先由另一个"用户"核销掉，再拿去当登录码
            bff.cookies.clear()
            r = await bff.post("/api/user/login/code", json={"code": CODE_C})
            uid_c = (await bff.get("/api/user/self")).json()["data"]["id"]
            # 用 C 账号把 CODE_B 当普通兑换码充值掉
            r = await bff.post("/api/user/topup", json={"key": CODE_B})
            check("6. C 账号成功消耗掉 CODE_B", r.json().get("success") is True,
                  r.json().get("message", ""))
            bff.cookies.clear()
            r = await bff.post("/api/user/login/code", json={"code": CODE_B})
            j = r.json()
            check("6a. 已被用掉的码不能登录", j.get("success") is False, j.get("message", ""))
            check("6b. 提示语点明「已被使用」", "已被使用" in j.get("message", ""),
                  j.get("message", ""))
            leaked = await find_user(up, H, redeem_code.derive_account(CODE_B)[0])
            check("6c. 已用码未残留垃圾账号", leaked is None)

            # ---------- 7. 绑定正式账号 ----------
            bff.cookies.clear()
            await bff.post("/api/user/login/code", json={"code": CODE_MAIN})
            newpwd = "BoundPass123456"
            r = await bff.post("/api/user/bind",
                               json={"username": bound_name, "password": newpwd})
            j = r.json()
            check("7. 绑定正式账号成功", j.get("success") is True, j.get("message", ""))
            r = await bff.get("/api/user/self")
            self3 = r.json()["data"]
            check("7a. uid 不变（资产保留）", self3["id"] == uid1, f"{uid1} vs {self3['id']}")
            check("7b. 余额保留 10000", self3["points"] == 10000, f"points={self3['points']}")
            check("7c. 不再是兑换码账号", self3["is_redeem_account"] is False)
            check("7d. 用户名已更新", self3["username"] == bound_name, self3["username"])

            # ---------- 8. 绑定后新账密可登录，原码失效 ----------
            bff.cookies.clear()
            r = await bff.post("/api/user/login",
                               json={"username": bound_name, "password": newpwd})
            check("8. 绑定后用新账密登录成功", r.json().get("success") is True,
                  r.json().get("message", ""))
            r = await bff.get("/api/user/self")
            check("8a. 新账密进的是同一账号", r.json()["data"]["id"] == uid1)

            bff.cookies.clear()
            r = await bff.post("/api/user/login/code", json={"code": CODE_MAIN})
            j = r.json()
            check("8b. 原兑换码已无法登录", j.get("success") is False, j.get("message", ""))
            leaked = await find_user(up, H, redeem_code.derive_account(CODE_MAIN)[0])
            check("8c. 原码尝试登录未建垃圾账号", leaked is None)

            # ---------- 9. 已是正式账号不能重复绑定 ----------
            bff.cookies.clear()
            await bff.post("/api/user/login", json={"username": bound_name, "password": newpwd})
            r = await bff.post("/api/user/bind",
                               json={"username": "bff_e2e_other", "password": "Other12345678"})
            check("9. 正式账号拒绝重复绑定", r.json().get("success") is False,
                  r.json().get("message", ""))

            # ---------- 10. 绑定时用户名占用 ----------
            bff.cookies.clear()
            await bff.post("/api/user/login/code", json={"code": CODE_D})
            r = await bff.post("/api/user/bind",
                               json={"username": bound_name, "password": "Dup123456789"})
            check("10. 绑定重名被拒绝", r.json().get("success") is False,
                  r.json().get("message", ""))
            r = await bff.post("/api/user/bind",
                               json={"username": "rc_abcdef1234567890", "password": "Dup123456789"})
            check("10a. 禁止占用 rc_ 前缀（抢注防护）",
                  r.json().get("success") is False, r.json().get("message", ""))

            # ---------- 11. /api/config 动态配置下发 ----------
            r = await bff.get("/api/config")
            cfg = r.json().get("data") or {}
            check("11. /api/config 可访问", r.json().get("success") is True)
            check("11a. 下发品牌信息", bool(cfg.get("brand", {}).get("name")),
                  str(cfg.get("brand", {}).get("name")))
            check("11b. 下发接口地址", bool(cfg.get("api", {}).get("base_url")),
                  str(cfg.get("api", {}).get("base_url")))
            check("11c. 下发功能开关",
                  isinstance(cfg.get("features", {}).get("redeem_login"), bool))
            check("11d. 真实环境不下发演示码", not cfg.get("demo_codes"),
                  str(cfg.get("demo_codes")))

            # ---------- 12. find_redemption 直连校验 ----------
            # 上面所有拒绝用例都依赖这个函数，单独验一次它自身的正确性，
            # 避免"因为查不到所以全拒"这种假通过。
            from app import newapi_client as na
            rec_ok = await na.find_redemption(CODE_D)
            check("12. 真码能被 find_redemption 查到", rec_ok is not None)
            if rec_ok:
                check("12a. 查到的 key 完全一致", rec_ok.get("key") == CODE_D)
            rec_bad = await na.find_redemption("zzzz1111zzzz2222zzzz3333zzzz4444")
            check("12b. 伪造码查不到", rec_bad is None)

        # 清理
        print("\n--- cleanup ---")
        await cleanup(up, H, derived_names + [bound_name])

    print(f"\n{'=' * 50}\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
