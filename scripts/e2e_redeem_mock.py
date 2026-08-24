"""兑换码登录 mock 模式 E2E（不依赖上游，可离线跑，用于回归）。

核心语义：**兑换码必须是管理员真实发放的**，用户不能随便编一串就开号。
所以每个用例都先通过 /api/mock/redemption 发卡，再拿卡去登录 ——
这与真实环境「管理员在 new-api 后台发卡」的流程一一对应。

真实环境 E2E 见 e2e_redeem_login.py。
启动：BFF_MOCK_MODE=1 uvicorn app.main:app --port 8301
换端口时用 BFF_BASE_URL 覆盖，如 BFF_BASE_URL=http://127.0.0.1:8000
"""
import asyncio
import os
import secrets
import sys
from pathlib import Path

# 按文件位置推导项目根，不写死绝对路径 —— 否则换机器、进 CI runner 全跑不了
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BFF = os.getenv("BFF_BASE_URL", "http://127.0.0.1:8301")
PASS, FAIL = [], []

# mock store 是进程内内存，服务不重启就会保留上一轮的账号。
# 每轮用随机后缀，保证脚本可以对同一个服务反复执行。
RUN = secrets.token_hex(4)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


async def issue(c, cny=50, count=1):
    """发卡（对应真实环境管理员在 new-api 后台创建兑换码）。"""
    r = await c.post("/api/mock/redemption",
                     json={"cny": cny, "count": count, "name": f"e2e-{RUN}"})
    return r.json()["data"]["keys"]


async def main():
    BOUND_U = f"mockbound{RUN}"
    async with httpx.AsyncClient(base_url=BFF, timeout=15.0, trust_env=False) as c:
        # ---------- 先发卡 ----------
        code, code_b, code_c = await issue(c, cny=50, count=3)

        # ---------- 核心语义：伪造的码必须被拒绝 ----------
        r = await c.post("/api/user/login/code",
                         json={"code": f"iamafakecode{RUN}00000000"})
        j = r.json()
        check("0. 伪造兑换码被拒绝", j.get("success") is False, j.get("message", ""))
        c.cookies.clear()
        r = await c.get("/api/user/self")
        check("0a. 伪造码不会开出账号", r.status_code == 401)

        # ---------- 首登 ----------
        r = await c.post("/api/user/login/code", json={"code": code})
        j = r.json()
        check("1. 首次兑换码登录", j.get("success") is True, j.get("message", ""))
        check("1a. 标记新账号", j["data"]["is_new"] is True)
        pts = j["data"]["points"]
        check("1b. 有到账积分", pts > 0, f"points={pts}")

        s = (await c.get("/api/user/self")).json()["data"]
        uid = s["id"]
        check("1c. is_redeem_account", s["is_redeem_account"] is True)
        check("1d. display_name 友好", s["display_name"].startswith("卡号用户"), s["display_name"])
        check("1e. 余额到账", s["points"] == pts, f"{s['points']} vs {pts}")

        # ---------- 复登 ----------
        c.cookies.clear()
        r = await c.post("/api/user/login/code", json={"code": code})
        j = r.json()
        check("2. 复登成功", j.get("success") is True)
        check("2a. 非新账号", j["data"]["is_new"] is False)
        s2 = (await c.get("/api/user/self")).json()["data"]
        check("2b. 同一 uid", s2["id"] == uid)
        check("2c. 余额未翻倍", s2["points"] == pts, f"{s2['points']} vs {pts}")

        # ---------- 归一化：大小写/横杠等价 ----------
        # 注意：归一化只作用于「派生账号」，核销时用原文。
        # 该码已核销，此处走的是「已有账号直接登录」分支。
        c.cookies.clear()
        weird = "-".join([code[i:i + 6] for i in range(0, len(code), 6)]).upper()
        await c.post("/api/user/login/code", json={"code": weird})
        s3 = (await c.get("/api/user/self")).json()["data"]
        check("3. 横杠大写归一化到同一账号", s3["id"] == uid)

        # ---------- 不同码 → 不同账号 ----------
        c.cookies.clear()
        await c.post("/api/user/login/code", json={"code": code_b})
        s4 = (await c.get("/api/user/self")).json()["data"]
        check("4. 不同码进不同账号", s4["id"] != uid, f"{s4['id']} vs {uid}")

        # ---------- 格式校验 ----------
        r = await c.post("/api/user/login/code", json={"code": "ab"})
        check("5. 短码被拦截", r.json().get("success") is False, r.json().get("message", ""))

        # ---------- 绑定升级 ----------
        c.cookies.clear()
        await c.post("/api/user/login/code", json={"code": code})
        r = await c.post("/api/user/bind", json={"username": BOUND_U, "password": "MockPass1234"})
        check("6. 绑定成功", r.json().get("success") is True, r.json().get("message", ""))
        s5 = (await c.get("/api/user/self")).json()["data"]
        check("6a. uid 不变", s5["id"] == uid)
        check("6b. 余额保留", s5["points"] == pts)
        check("6c. 不再是兑换码账号", s5["is_redeem_account"] is False)

        c.cookies.clear()
        r = await c.post("/api/user/login", json={"username": BOUND_U, "password": "MockPass1234"})
        check("7. 新账密可登录", r.json().get("success") is True)

        # ---------- 前缀抢注防护 + 密码边界 ----------
        c.cookies.clear()
        await c.post("/api/user/login/code", json={"code": code_c})
        r = await c.post("/api/user/bind",
                         json={"username": "rc_1234567890abcdef", "password": "MockPass1234"})
        check("8. 禁止占用 rc_ 前缀", r.json().get("success") is False, r.json().get("message", ""))
        r = await c.post("/api/user/bind", json={"username": f"ok{RUN}", "password": "short"})
        check("9. 短密码被拦截", r.json().get("success") is False, r.json().get("message", ""))
        r = await c.post("/api/user/bind", json={"username": f"ok{RUN}", "password": "x" * 21})
        check("9a. 超长密码被拦截（上游 max=20）",
              r.json().get("success") is False, r.json().get("message", ""))

        # ---------- 充值页兑换码同样不能伪造 ----------
        c.cookies.clear()
        fresh, = await issue(c, cny=10, count=1)
        await c.post("/api/user/login/code", json={"code": code})   # 用已绑定的老账号登录
        c.cookies.clear()
        await c.post("/api/user/login", json={"username": BOUND_U, "password": "MockPass1234"})
        before = (await c.get("/api/user/self")).json()["data"]["points"]
        r = await c.post("/api/user/topup", json={"key": f"faketopup{RUN}0000"})
        check("10. 充值页伪造码被拒绝", r.json().get("success") is False, r.json().get("message", ""))
        r = await c.post("/api/user/topup", json={"key": fresh})
        check("10a. 真实卡充值成功", r.json().get("success") is True, r.json().get("message", ""))
        after = (await c.get("/api/user/self")).json()["data"]["points"]
        check("10b. 余额增加", after > before, f"{before} -> {after}")
        r = await c.post("/api/user/topup", json={"key": fresh})
        check("10c. 同卡不能重复充值", r.json().get("success") is False, r.json().get("message", ""))

        # ---------- 站点配置端点 ----------
        cfg = (await c.get("/api/config")).json()["data"]
        check("11. /api/config 返回品牌", bool(cfg["brand"]["name"]), cfg["brand"]["name"])
        check("11a. 下发接入地址", bool(cfg["api"]["base_url"]), cfg["api"]["base_url"])
        check("11b. mock 模式标识", cfg["features"]["mock_mode"] is True)

    print(f"\n{'=' * 46}\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        sys.exit(1)
    print("mock 模式全部通过")


asyncio.run(main())
