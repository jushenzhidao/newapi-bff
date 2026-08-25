"""兑换码登录 mock 模式 E2E（不依赖上游，可离线跑，用于回归）。

核心语义：**兑换码必须是管理员真实发放的**，用户不能随便编一串就开号。
所以每个用例都先通过 /api/mock/redemption 发卡，再拿卡去登录 ——
这与真实环境「管理员在 new-api 后台发卡」的流程一一对应。

真实环境 E2E 见 e2e_redeem_login.py。
启动：BFF_MOCK_MODE=1 BFF_COOKIE_SECURE=0 uvicorn app.main:app --port 8301
换端口时用 BFF_BASE_URL 覆盖，如 BFF_BASE_URL=http://127.0.0.1:8000

## 为什么必须 BFF_COOKIE_SECURE=0

本脚本通过明文 http 访问 BFF。COOKIE_SECURE 默认是 1（生产安全默认值），
此时会话 Cookie 带 Secure 属性，httpx 按 RFC 6265 拒绝在 http 请求上回传它 ——
登录接口返回 success=true，但后续每个请求都是未登录的 401。

这与 tests/conftest.py 里显式关掉 Secure 的原因完全一致（TestClient 的
base_url 同样是明文 http）。脚本启动时会自检并给出可操作的报错，
不再让它退化成某行 `.json()["data"]` 的 KeyError。
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


async def self_of(c):
    """读当前会话的用户信息。

    直接 .json()["data"] 在会话失效时会炸 KeyError，堆栈指向脚本内部某一行，
    完全看不出真正原因（最典型的就是 Secure Cookie 没被回传）。
    这里把「会话没带上」翻译成可操作的报错。
    """
    r = await c.get("/api/user/self")
    if r.status_code == 401:
        sys.exit(
            "\n[ABORT] /api/user/self 返回 401：登录成功但会话 Cookie 没有回传。\n"
            f"  最常见原因：服务端 BFF_COOKIE_SECURE 未关闭，而本脚本走明文 http（{BFF}）。\n"
            "  Secure Cookie 不会在 http 请求上发送，登录态因此丢失。\n"
            "  修复：启动服务时加 BFF_COOKIE_SECURE=0，例如\n"
            "    BFF_MOCK_MODE=1 BFF_COOKIE_SECURE=0 BFF_SECRET_KEY=<随机值> \\\n"
            "      uvicorn app.main:app --host 127.0.0.1 --port 8301\n"
        )
    body = r.json()
    if "data" not in body:
        sys.exit(f"\n[ABORT] /api/user/self 响应缺少 data 字段："
                 f"status={r.status_code} body={body}\n")
    return body["data"]


async def preflight(c):
    """启动自检：确认服务在 mock 模式，且会话 Cookie 能在明文 http 上回传。

    放在所有用例之前，让配置问题在第一时间以明确信息暴露，
    而不是伪装成某条业务断言的失败。
    """
    r = await c.get("/api/config")
    if r.status_code != 200:
        sys.exit(f"\n[ABORT] 无法读取 /api/config（status={r.status_code}）。"
                 f"确认服务已在 {BFF} 启动。\n")
    features = r.json()["data"]["features"]
    if not features.get("mock_mode"):
        sys.exit("\n[ABORT] 服务未运行在 mock 模式。本脚本依赖 /api/mock/redemption 发卡，"
                 "请以 BFF_MOCK_MODE=1 启动。\n")

    probe, = await issue(c, cny=10, count=1)
    r = await c.post("/api/user/login/code", json={"code": probe})
    if r.json().get("success") is not True:
        sys.exit(f"\n[ABORT] 自检登录失败：{r.json().get('message', '')}\n")
    if not c.cookies.get("bff_session"):
        sys.exit(
            "\n[ABORT] 登录返回成功，但客户端没有存下 bff_session Cookie。\n"
            f"  本脚本以明文 http 访问（{BFF}），而带 Secure 属性的 Cookie "
            "会被 httpx 按 RFC 6265 丢弃。\n"
            "  修复：启动服务时加 BFF_COOKIE_SECURE=0。\n"
        )
    await self_of(c)          # 会话真的可用（401 时在 self_of 内报错退出）
    c.cookies.clear()


async def main():
    BOUND_U = f"mockbound{RUN}"
    async with httpx.AsyncClient(base_url=BFF, timeout=15.0, trust_env=False) as c:
        await preflight(c)

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

        s = await self_of(c)
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
        s2 = await self_of(c)
        check("2b. 同一 uid", s2["id"] == uid)
        check("2c. 余额未翻倍", s2["points"] == pts, f"{s2['points']} vs {pts}")

        # ---------- 归一化：大小写/横杠等价 ----------
        # 注意：归一化只作用于「派生账号」，核销时用原文。
        # 该码已核销，此处走的是「已有账号直接登录」分支。
        c.cookies.clear()
        weird = "-".join([code[i:i + 6] for i in range(0, len(code), 6)]).upper()
        await c.post("/api/user/login/code", json={"code": weird})
        s3 = await self_of(c)
        check("3. 横杠大写归一化到同一账号", s3["id"] == uid)

        # ---------- 不同码 → 不同账号 ----------
        c.cookies.clear()
        await c.post("/api/user/login/code", json={"code": code_b})
        s4 = await self_of(c)
        check("4. 不同码进不同账号", s4["id"] != uid, f"{s4['id']} vs {uid}")

        # ---------- 格式校验 ----------
        r = await c.post("/api/user/login/code", json={"code": "ab"})
        check("5. 短码被拦截", r.json().get("success") is False, r.json().get("message", ""))

        # ---------- 绑定升级 ----------
        c.cookies.clear()
        await c.post("/api/user/login/code", json={"code": code})
        r = await c.post("/api/user/bind", json={"username": BOUND_U, "password": "MockPass1234"})
        check("6. 绑定成功", r.json().get("success") is True, r.json().get("message", ""))
        s5 = await self_of(c)
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
        before = (await self_of(c))["points"]
        r = await c.post("/api/user/topup", json={"key": f"faketopup{RUN}0000"})
        check("10. 充值页伪造码被拒绝", r.json().get("success") is False, r.json().get("message", ""))
        r = await c.post("/api/user/topup", json={"key": fresh})
        check("10a. 真实卡充值成功", r.json().get("success") is True, r.json().get("message", ""))
        after = (await self_of(c))["points"]
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
