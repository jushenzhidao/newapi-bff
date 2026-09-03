"""newapi-bff 主应用（真实代理 + mock 双模式）。

真实模式（默认）：所有接口透传 https://api.aihuobao.cn（PAT 鉴权，契约见 newapi_client.py）
mock 模式（BFF_MOCK_MODE=1）：内存数据演示。

计价口径：对外只暴露「积分」，new-api 内部 quota 一律在 BFF 层换算后再返回。
换算参数在 config.py（BFF_POINTS_PER_CNY 等环境变量可配）。

路由契约：
  GET  /api/promo             活动与积分换算配置（无需登录）
  POST /api/user/login        登录（真实：密码登录→换 PAT）
  POST /api/user/login/code   兑换码登录（免注册：码派生影子账号，首次自动建号+核销）
  POST /api/user/bind         兑换码账号升级为正式账号（设置用户名密码）
  POST /api/user/register     注册（真实：管理员影子建号→登录→注册礼包）
  POST /api/verification      邮箱验证码（模拟）
  GET  /api/user/logout
  GET  /api/user/self         含 points / used_points / 首充资格
  GET  /api/token             Key 列表（真实环境为掩码）
  POST /api/token             创建 Key（返回明文，便于立即复制）
  POST /api/token/{id}/key    获取 Key 明文
  DELETE /api/token/{id}
  GET  /api/log/self          日志 + 统计（points 口径）
  POST /api/user/pay          创建支付订单（真实：返回易支付表单参数）
  POST /api/user/pay/status   支付到账查询（按订单号查上游订单状态，到账后发首充赠送）
  GET  /usage-logs            易支付回跳落地页（302 到 #/topup，不发钱）
  GET  /pay/return            同上（BFF 自有回跳地址）
  POST /api/user/pay/confirm  模拟到账（仅 mock 模式）
  POST /api/user/topup        兑换码充值（真实透传）
"""
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, docs_catalog, observability, pricing, promo, redeem_code, store
from . import newapi_client as na
from . import settings as dyn_settings
from .newapi_client import NewApiError
from .security import (
    _SETUP_TTL,  # 导入配置链接 ticket 的 TTL（10 分钟）
    clear_session,
    is_admin,
    issue_setup_ticket,
    open_setup_ticket,
    require_admin,
    require_session,
    set_session,
)

logger = logging.getLogger("bff")
logging.basicConfig(level=logging.INFO)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动/收尾。

    用 lifespan 而非 `@app.on_event("shutdown")`：starlette 1.6.0 已移除
    `Starlette.on_event`，当前能跑仅依赖 FastAPI 的兼容层，该层移除后
    `na.close()` 不再被调用会导致 httpx 连接池泄漏。且测试套件不覆盖
    shutdown 路径，这类问题拦不住，故提前迁移。
    """
    # try 必须包住启动逻辑本身，不只是 yield：若预置演示卡等启动步骤抛异常，
    # finally 才会执行、连接池才会归还。否则崩溃重启循环会持续累积泄漏的连接。
    try:
        logger.info(
            "BFF 启动，模式: %s | 1元=%s%s",
            "MOCK" if MOCK else f"REAL -> {config.NEWAPI_BASE_URL}",
            config.POINTS_PER_CNY,
            config.POINTS_UNIT_NAME,
        )
        # 真实模式没注入 NEWAPI_BASE_URL 就会连到内置默认（测试环境）上游：
        # 不报错、不 500，只是用户数据全写进另一个库。这类事故排查代价极高，
        # 启动时必须喊出来。
        if not MOCK and config.NEWAPI_BASE_URL_IS_DEFAULT:
            logger.warning(
                "NEWAPI_BASE_URL 未注入，正在使用内置默认值 %s —— 生产部署请显式"
                "设置为你的 new-api 域名，否则用户数据会写到错误的环境",
                config.NEWAPI_BASE_URL,
            )
        if MOCK:
            # mock 模式预置演示卡。兑换码语义与真实环境一致：只有预先发放的卡才有效，
            # 不能随便编一串就登录。
            cards = store.seed_demo_redemptions()
            logger.info("已预置 %d 张演示兑换码: %s", len(cards),
                        ", ".join(c["key"] for c in cards))
        yield
    finally:
        await na.close()


app = FastAPI(title="newapi-bff", docs_url=None, redoc_url=None, lifespan=_lifespan)

# 可观测性接线：未配 LOGFIRE_TOKEN 时为空操作，且初始化失败不影响服务启动。
observability.setup(app)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

MOCK = config.MOCK_MODE
P = config.quota_to_points  # quota → 积分（整数，用于余额/聚合）
PX = config.quota_to_points_exact  # quota → 积分（带小数，用于单条日志明细）
# new-api 对 User.Password 有 max 校验（实测 20 位通过、24 位失败）。
# 在 BFF 层先拦，避免用户填了 24 位密码后拿到一句英文 validation 报错。
MAX_PASSWORD_LEN = 20


# ---------- 异常处理：业务错误透传 message，内部错误不泄露 ----------
@app.exception_handler(NewApiError)
async def newapi_error_handler(request: Request, exc: NewApiError):
    return JSONResponse(status_code=exc.status_code,
                        content={"success": False, "message": exc.message})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"success": False, "message": "服务器内部错误"})


def client_ip(request: Request) -> str:
    """取真实客户端 IP，转发给 new-api 用于按 IP 限流计数。

    BFF 若部署在 Nginx 之后，X-Forwarded-For 第一段才是真实用户 IP。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def ok(data=None, message: str = ""):
    return {"success": True, "message": message, "data": data}


def fail(message: str, status_code: int = 400):
    return JSONResponse(status_code=status_code, content={"success": False, "message": message})


# ==================== 活动配置 ====================
@app.get("/api/promo")
async def get_promo():
    return ok(promo.public_config())


@app.get("/api/config")
async def get_site_config():
    """站点配置：品牌、文案、接入参数、功能开关。

    前端启动时拉一次，全站文案/示例代码都从这里取 —— 换品牌、换域名、
    换默认模型都只改环境变量，不用动前端代码。
    """
    return ok({
        "brand": {
            "name": config.BRAND_NAME,
            "logo_text": config.brand_logo_text(),
            "tagline": config.BRAND_TAGLINE,
            "hero_title": config.BRAND_HERO_TITLE,
            "hero_h1": config.BRAND_HERO_H1,
            "hero_h1_prefix": config.BRAND_HERO_H1_PREFIX,
            "hero_h1_accent": config.BRAND_HERO_H1_ACCENT,
            "hero_sub": config.BRAND_HERO_SUB,
            "hero_badge": config.BRAND_HERO_BADGE,
            "icp": config.BRAND_ICP,
            "contact": config.BRAND_CONTACT,
        },
        "api": {
            "base_url": config.API_BASE_URL,
            "default_model": config.DOC_DEFAULT_MODEL,
            "models": list(config.DOC_MODELS),
            # 模型 → 供应商映射（后台「模型供应商映射」配置），未配置的模型不在其中。
            # 单独成字段而非把 models 改成对象数组：脚本与前端都按字符串数组解析
            # models，改动会波及所有已下发的客户端。
            "model_vendors": dyn_settings.model_vendor_map(),
        },
        "features": {
            # 关掉后登录页不显示兑换码 Tab
            "redeem_login": config.REDEEM_LOGIN_ENABLED,
            "mock_mode": MOCK,
        },
        # mock 模式把「未使用的预置演示卡」下发给前端，方便体验；真实模式恒为空
        "demo_codes": store.preset_redemption_keys() if MOCK else [],
    })


# ==================== 认证 ====================
class LoginBody(BaseModel):
    username: str
    password: str


class CodeLoginBody(BaseModel):
    code: str


class BindBody(BaseModel):
    # 可选：留空 = 保持当前 rc_ 卡号名，仅设置密码（前端置灰展示时的路径）
    username: str = ""
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    email: str
    verification_code: str


class VerificationBody(BaseModel):
    email: str


@app.post("/api/user/login")
async def login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip()
    if not username or not body.password:
        return fail("用户名和密码不能为空")
    if MOCK:
        user = store.get_or_create_user(username, body.password)
        set_session(response, {"uid": user["uid"], "username": user["username"], "pat": user["pat"],
                               # mock 无真实角色，管理页入口靠 BFF_ADMIN_USERNAMES 名单
                               "role": 0})
        await promo.grant_signup(user["uid"])
        return ok({"username": user["username"]}, "登录成功")
    try:
        info = await na.login(username, body.password, client_ip=client_ip(request))
    except NewApiError as e:
        if e.status_code == 429:      # 上游限流，原样透传含等待时长的提示
            return fail(e.message, 429)
        msg = e.message
        if "password" in msg.lower() or "用户名或密码" in msg or e.status_code == 400:
            msg = "用户名或密码错误"
        return fail(msg, 401 if e.status_code in (400, 401) else e.status_code)
    set_session(response, {"uid": info["uid"], "username": info["username"], "pat": info["pat"],
                           # 上游若不返回 role 就存 0（非管理员），管理权限另有静态名单兜底
                           "role": int((info.get("user") or {}).get("role") or 0)})
    return ok({"username": info["username"]}, "登录成功")


@app.post("/api/user/login/code")
async def login_by_code(body: CodeLoginBody, request: Request, response: Response):
    """兑换码登录：输码即用，无需注册。

    码即身份 —— 同一个码永远进同一个账号（HMAC 确定性派生，详见 redeem_code.py）。
    首次使用会自动建号并核销到账；之后再输同一个码就是登录，不重复到账。

    注意：码必须是 new-api 管理员真实创建的：首次使用前会先查兑换码列表核验，
    不存在或已被使用一律拒绝，不会建号。
    """
    if not config.REDEEM_LOGIN_ENABLED:
        return fail("兑换码登录未开放", 404)
    code = body.code.strip()
    if not redeem_code.is_valid_format(code):
        return fail("兑换码格式不正确，请检查后重试")
    info = await redeem_code.login_or_create(code, client_ip=client_ip(request))
    set_session(response, {"uid": info["uid"], "username": info["username"],
                           # 兑换码账号是自动建号的普通用户，恒非管理员
                           "pat": info["pat"], "role": 0})
    pts = config.quota_to_points(info.get("redeemed_quota", 0))
    if info["is_new"]:
        msg = (f"兑换成功，到账 {pts:,} {config.POINTS_UNIT_NAME}"
               if pts else "兑换成功，已为你开通账号")
    else:
        msg = "登录成功"
    return ok({"username": info["username"], "is_new": info["is_new"],
               "points": pts, "need_bind": True}, msg)


@app.post("/api/user/bind")
async def bind_account(body: BindBody, response: Response,
                       session: dict = Depends(require_session)):
    """兑换码账号设置用户名密码，便于以后不带码登录。

    两种路径：
      - username 留空 / 等于当前卡号名（码前 18 位）→ 保持用户名不变，仅设置密码
        （派生密码被覆盖，原兑换码同样不再能登录；uid 记入「已设密码」登记）
      - username 为新名 → 改名 + 改密（经典路径）
    绑定后 uid 不变，余额/Key/日志全部保留。
    """
    username = body.username.strip() or session["username"]
    if not re.match(r"^[a-zA-Z0-9_]{2,20}$", username):
        return fail("用户名需为 2-20 位字母、数字或下划线")
    if username != session["username"] and username.startswith(redeem_code.RC_PREFIX):
        # 改名路径禁止换成 rc_ 开头的名字：那是「码前 18 位」的形态，
        # 换成未来的码前缀名会让该码登录时建号撞名、永久作废
        return fail(f"用户名不能以 {redeem_code.RC_PREFIX} 开头")
    if not 8 <= len(body.password) <= MAX_PASSWORD_LEN:
        return fail(f"密码需为 8-{MAX_PASSWORD_LEN} 位")
    if not redeem_code.is_redeem_uid(session["uid"]):
        return fail("当前账号已是正式账号，无需绑定")
    # 统一走管理员接口 PUT /api/user/（BFF 管理员 token 代改）：
    # 用户态 PUT /api/user/self 要求 original_password（旧密码），而兑换码
    # 用户的旧密码是派生密码、BFF 无状态设计不存码，拿不到 —— 管理员代改
    # 是唯一不需要 original_password 的路径。安全性由三层保证：
    #   ① require_session：只有本人会话能触发
    #   ② is_redeem_uid：只对兑换码账号生效
    #   ③ username 锁定为当前码前缀名，用户无法借此动别人账号
    # 管理员 token 仅在服务端使用，不下发前端。
    await redeem_code.bind_account(session["uid"], username, body.password)
    # 绑定完成（无论保持码前缀名还是改名）一律登记「已设密码」：
    # /user/self 的 is_redeem_account = 登记中 且 未设密码 —— 漏掉改名路径
    # 的话，改名绑定后兑换码 uid 登记还在、密码标记缺失，绑定横幅永远弹
    redeem_code.mark_password_set(session["uid"])
    # 改账密后旧 PAT 是否仍有效不做假设，直接用新账密重登换一份新的，
    # 避免用户绑定完立刻遇到 401。
    info = await _relogin(username, body.password, request_ip=None)
    # 绑定的前提是兑换码账号（上方已校验），故恒为普通用户
    set_session(response, {"uid": info["uid"], "username": info["username"],
                           "pat": info["pat"], "role": 0})
    return ok({"username": username}, "绑定成功，以后可用该账号密码登录")


async def _relogin(username: str, password: str, request_ip: str | None):
    if MOCK:
        u = store.users[username]
        return {"uid": u["uid"], "username": username, "pat": u["pat"]}
    return await na.login(username, password, client_ip=request_ip)


def _mail_error(msg: str) -> str:
    """把上游英文邮件报错翻成用户能照着处理的中文。

    上游 message 直接来自 SMTP 服务器响应，原文对终端用户没有指导意义。
    """
    low = msg.lower()
    if "invalid smtp" in low or "smtp account" in low:
        return "邮件服务未配置，请联系管理员"
    if "550" in msg or "non-existent account" in low or "recipient" in low:
        return "该邮箱地址不存在，请检查后重新输入"
    if "incorrect or has expired" in low:
        return "验证码错误或已过期，请重新获取"
    if "verification is enabled" in low:
        return "请填写邮箱并获取验证码"
    return msg


@app.post("/api/verification")
async def send_verification(body: VerificationBody, request: Request):
    email = body.email.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return fail("邮箱格式不正确")
    if MOCK:
        return ok(None, f"验证码已发送至 {email}（演示：任意 6 位数字均可）")
    try:
        await na.send_verification(email, client_ip=client_ip(request))
    except NewApiError as e:
        # 直接透传客户端的状态码：上游业务失败已在 newapi_client 归一成 400
        # （newapi_client.py:258），网络类故障保持 502/503/429 原样。
        return fail(_mail_error(e.message), e.status_code)
    return ok(None, f"验证码已发送至 {email}，10 分钟内有效")


@app.post("/api/user/register")
async def register(body: RegisterBody, request: Request, response: Response):
    username = body.username.strip()
    if not re.match(r"^[a-zA-Z0-9_]{2,20}$", username):
        return fail("用户名需为 2-20 位字母、数字或下划线")
    if not 8 <= len(body.password) <= MAX_PASSWORD_LEN:
        return fail(f"密码需为 8-{MAX_PASSWORD_LEN} 位")
    if re.fullmatch(r"[0-9a-f]{18}", username):
        # 18 位纯 hex 是兑换码用户名的形态（码前 18 位）。放行的话正常注册
        # 会抢占某张真实兑换码的用户名，导致该码登录时建号撞名、永久无法使用
        return fail("该用户名不可用，请换一个")
    # 验证码格式只做宽松兜底（非空、无空白、长度合理）：真正的裁决在上游 ——
    # new-api 发的码可能是字母数字混合，BFF 写死 6 位纯数字会把有效码误拦在门外
    _verify_code = body.verification_code.strip()
    if not re.match(r"^[A-Za-z0-9]{4,10}$", _verify_code):
        return fail("验证码格式不正确")
    if MOCK:
        if username in store.users:
            return fail("用户名已存在，请直接登录")
        user = store.get_or_create_user(username, body.password, body.email)
        set_session(response, {"uid": user["uid"], "username": user["username"],
                               "pat": user["pat"], "role": 0})
        gift = await promo.grant_signup(user["uid"])
        return ok({"username": user["username"], "gift_points": gift}, "注册成功")
    # 真实模式：走上游原生注册端 → 用户密码登录换 PAT
    #
    # 必须用 na.register_user 而非 admin_create_user：管理员建号接口既不校验
    # 邮箱验证码、也不写 email 字段，会把整条邮箱验证链路旁路掉 —— 用户拿到的是
    # 一个邮箱未绑定的账号，日后无法用邮箱找回密码。验证码状态只存在上游，
    # BFF 无从校验，因此把 email + verification_code 原样交给上游裁决。
    email = body.email.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return fail("邮箱格式不正确")
    try:
        await na.register_user(username, body.password, email,
                               _verify_code,
                               client_ip=client_ip(request))
    except NewApiError as e:
        msg = e.message
        low = msg.lower()
        # 先判验证码类错误："has expired" 含 "exist" 子串，若先做重名匹配，
        # 验证码过期会被误报成「用户名已存在」—— 用户会去改用户名，越改越错。
        if "verification" in low or "expired" in low or "验证码" in msg:
            msg = _mail_error(msg)
        elif "已存在" in msg or "exist" in low or "duplicate" in low:
            msg = "用户名已存在，请直接登录"
        else:
            msg = _mail_error(msg)
        return fail(msg, e.status_code)
    info = await na.login(username, body.password, client_ip=client_ip(request))
    # 新注册账号必然是普通用户，无需读取上游 role
    set_session(response, {"uid": info["uid"], "username": info["username"],
                           "pat": info["pat"], "role": 0})
    gift = await promo.grant_signup(info["uid"])
    msg = f"注册成功，已赠送 {gift:,} {config.POINTS_UNIT_NAME}" if gift else "注册成功"
    return ok({"username": info["username"], "gift_points": gift}, msg)


@app.get("/api/user/logout")
async def logout(response: Response):
    clear_session(response)
    return ok(None, "已退出登录")


def _self_payload(uid: int, username: str, email: str, quota: int,
                  used_quota: int, request_count: int,
                  session: dict | None = None) -> dict:
    is_rc = redeem_code.is_redeem_uid(uid)
    # 影子建号（注册/兑换码）没有真实邮箱，上游会补一个 <username>@example.com 占位。
    # 把它显示给用户既无意义，还会泄露内部派生的用户名 —— 一律隐去。
    email = (email or "").strip()
    if email.endswith("@example.com") or email.startswith("rc_"):
        email = ""
    return {
        "id": uid, "username": username,
        # 前端已改为直接显示 username，display_name 仅作兼容保留
        "display_name": f"卡号用户 {username[-6:]}" if is_rc else username,
        "email": email or "-",
        "points": P(quota), "used_points": P(used_quota),
        "request_count": request_count,
        "points_per_cny": config.POINTS_PER_CNY,
        "unit": config.POINTS_UNIT_NAME,
        "first_topup_available": (not promo.first_topup_used(uid)) and
                                 config.PROMO_FIRST_TOPUP_ENABLED,
        # 兑换码账号（按 uid 登记）：前端据此提示「绑定账号」，避免丢码即丢余额
        "is_redeem_account": (is_rc
                              and not redeem_code.password_set(uid)),
        # 仅用于前端决定是否显示管理入口。真正的鉴权在 require_admin，
        # 前端把这个字段改成 true 也调不通任何 /api/admin/* 接口。
        "is_admin": is_admin(session) if session else False,
    }


@app.get("/api/user/self")
async def user_self(session: dict = Depends(require_session)):
    if MOCK:
        user = store.get_user_by_uid(session["uid"])
        if user is None:
            raise HTTPException(status_code=401, detail="用户不存在")
        return ok(_self_payload(user["uid"], user["username"], user["email"],
                                user["quota"], user["used_quota"],
                                user["request_count"], session))
    d = await na.get_self(session["pat"], session["uid"])
    return ok(_self_payload(d["id"], d["username"], d.get("email"),
                            d.get("quota", 0), d.get("used_quota", 0),
                            d.get("request_count", 0), session))


# ==================== API Key 管理 ====================
class TokenCreateBody(BaseModel):
    name: str


def _token_payload(t: dict, key: str | None = None) -> dict:
    return {
        "id": t["id"], "name": t["name"], "key": key if key is not None else t["key"],
        "status": t["status"], "created_time": t["created_time"],
        "unlimited_quota": t.get("unlimited_quota", False),
        "used_points": P(t.get("used_quota", 0)),
    }


@app.get("/api/token")
async def list_tokens(session: dict = Depends(require_session)):
    if MOCK:
        return ok([_token_payload(t) for t in store.tokens.get(session["uid"], [])])
    d = await na.list_tokens(session["pat"], session["uid"])
    return ok([_token_payload(t) for t in d.get("items", [])])


@app.post("/api/token")
async def create_token(body: TokenCreateBody, session: dict = Depends(require_session)):
    name = body.name.strip() or "未命名 Key"
    if MOCK:
        t = store.create_token(session["uid"], name)
        return ok(_token_payload(t), "创建成功")
    pat, uid = session["pat"], session["uid"]
    await na.create_token(pat, uid, name)
    # 创建接口不返回 key，取列表第一条（按创建时间倒序）拿 id，再取明文
    d = await na.list_tokens(pat, uid, page=1, size=1)
    items = d.get("items", [])
    if not items:
        return ok(None, "创建成功")
    t = items[0]
    plain = await na.get_token_key(pat, uid, t["id"])
    return ok(_token_payload(t, plain), "创建成功")


@app.post("/api/token/{token_id}/key")
async def token_plain_key(token_id: int, session: dict = Depends(require_session)):
    if MOCK:
        for t in store.tokens.get(session["uid"], []):
            if t["id"] == token_id:
                return ok({"key": t["key"]})
        return fail("Key 不存在", 404)
    plain = await na.get_token_key(session["pat"], session["uid"], token_id)
    return ok({"key": plain})


@app.delete("/api/token/{token_id}")
async def delete_token(token_id: int, session: dict = Depends(require_session)):
    if MOCK:
        if not store.delete_token(session["uid"], token_id):
            return fail("Key 不存在", 404)
        return ok(None, "已删除")
    await na.delete_token(session["pat"], session["uid"], token_id)
    return ok(None, "已删除")


# ==================== 调用日志 ====================
def _log_payload(l: dict) -> dict:
    return {
        "id": l["id"], "type": l["type"], "model_name": l.get("model_name", ""),
        "token_name": l.get("token_name", ""),
        "prompt_tokens": l.get("prompt_tokens", 0),
        "completion_tokens": l.get("completion_tokens", 0),
        # 单条明细用带小数的换算：小请求 quota < 50 时整数换算会恒为 0
        "points": PX(l.get("quota", 0)), "content": l.get("content", ""),
        "created_at": l["created_at"],
    }


@app.get("/api/log/self")
async def log_self(p: int = 1, page_size: int = 10, session: dict = Depends(require_session)):
    p = max(1, p)
    page_size = min(max(1, page_size), 100)
    if MOCK:
        uid = session["uid"]
        all_logs = sorted(store.logs.get(uid, []), key=lambda x: x["created_at"], reverse=True)
        consume = [l for l in all_logs if l["type"] == 2]
        stat = {
            "request_count": len(consume),
            "points": P(sum(l["quota"] for l in consume)),
            "prompt_tokens": sum(l["prompt_tokens"] for l in consume),
            "completion_tokens": sum(l["completion_tokens"] for l in consume),
        }
        start = (p - 1) * page_size
        items = [_log_payload(l) for l in all_logs[start:start + page_size]]
        return ok({"items": items, "total": len(all_logs),
                   "page": p, "page_size": page_size, "stat": stat})
    pat, uid = session["pat"], session["uid"]
    d = await na.get_logs(pat, uid, p, page_size)
    st = await na.get_log_stat(pat, uid)
    items = [_log_payload(l) for l in d.get("items", [])]
    stat = {
        "request_count": d.get("total", 0),
        "points": P(st.get("quota", 0)),
        "rpm": st.get("rpm", 0),
        "tpm": st.get("tpm", 0),
        "prompt_tokens": sum(i["prompt_tokens"] for i in items),
        "completion_tokens": sum(i["completion_tokens"] for i in items),
    }
    return ok({"items": items, "total": d.get("total", 0), "page": p,
               "page_size": page_size, "stat": stat})


# ==================== 充值 ====================
class PayBody(BaseModel):
    amount: int
    payment_method: str


class PayConfirmBody(BaseModel):
    order_no: str


class PayStatusBody(BaseModel):
    amount: int
    baseline_points: int
    # 订单号：有它就走「按单查询」精确判定。留默认空值是为了兼容用户浏览器里
    # 缓存的旧版 app.js（发版瞬间正在轮询的请求不会因 422 中断）。
    order_no: str = ""


class TopupBody(BaseModel):
    key: str


class MockRedemptionBody(BaseModel):
    """仅 mock 模式的发卡请求（演示/测试用）。"""
    cny: float = 10
    count: int = 1
    name: str = ""


async def _current_points(session: dict) -> int:
    if MOCK:
        u = store.get_user_by_uid(session["uid"])
        return P(u["quota"]) if u else 0
    d = await na.get_self(session["pat"], session["uid"])
    return P(d.get("quota", 0))


@app.post("/api/user/pay")
async def create_pay_order(body: PayBody, session: dict = Depends(require_session)):
    if body.amount not in config.PAY_AMOUNTS:
        return fail("充值金额不在允许范围内")
    if body.payment_method not in ("alipay", "wxpay"):
        return fail("不支持的支付方式")
    uid = session["uid"]
    base_points = config.cny_to_points(body.amount)
    bonus = 0 if promo.first_topup_used(uid) else promo.bonus_points_for(body.amount)
    baseline = await _current_points(session)
    common = {"amount": body.amount, "points": base_points, "bonus_points": bonus,
              "total_points": base_points + bonus, "baseline_points": baseline}
    if MOCK:
        order_no = time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(4)
        store.orders[order_no] = {"uid": uid, "amount_cents": body.amount * 100,
                                  "method": body.payment_method, "status": "pending",
                                  "created_at": int(time.time())}
        return ok({"mode": "mock", "order_no": order_no,
                   "method": body.payment_method, **common}, "订单已创建")
    pat = session["pat"]
    payable = await na.pay_amount(pat, uid, body.amount, body.payment_method)
    gw = await na.pay_create(pat, uid, body.amount, body.payment_method)
    # gateway_mode 区分「表单 POST 提交」与「直接跳转 URL」两种收银台形态。
    # 微信支付走后者（上游 data 返回字符串地址），必须让前端知道该走哪条路 ——
    # 否则会把一个 URL 字符串当对象去遍历字段，构出空表单。
    return ok({"mode": "epay", "gateway_mode": gw["mode"], "payable": payable,
               "gateway": gw["url"], "params": gw["params"],
               "order_no": gw["order_no"], **common}, "订单已创建")


@app.post("/api/user/pay/status")
async def pay_status(body: PayStatusBody, session: dict = Depends(require_session)):
    """查询本单是否已到账；确认到账后发放首充赠送（幂等）。

    判定依据是**上游订单状态**（GET /api/user/topup/self?keyword=<trade_no>），
    而不是「余额变多了」。这一点是刻意的：

    余额差值判定有两类错误，且都直接造成资损或客诉：
      1) 误判成功：用户下单 ¥500 不付，同时用一张 ¥500 兑换码到账，余额差达标
         → 本单被判成功并发放首充赠送，等于白送一笔赠送额度；
      2) 误判失败：并发/连续充值时 baseline 已过期，差值算不准，钱到了却提示未到账
         → 用户重复支付。
    订单号是支付网关与 new-api 之间的唯一凭据，用它判定不受任何其他到账干扰。

    与浏览器回跳彻底解耦：到账由 new-api 的服务端 notify 完成（epay notify_url
    直连上游，不经过 BFF），本接口只是「读状态」。所以用户关掉支付页、回跳地址
    配错、甚至根本没回跳，都不影响到账与本接口的判定结果。
    """
    uid = session["uid"]
    points = await _current_points(session)
    trade_no = (body.order_no or "").strip()

    if MOCK:
        # mock 模式：订单状态由 /api/user/pay/confirm 置为 paid，语义与真实环境一致
        order = store.orders.get(trade_no) if trade_no else None
        if order is not None and order.get("uid") == uid:
            if order.get("status") != "paid":
                return ok({"paid": False, "points": points, "order_status": "pending"})
            bonus = await promo.grant_first_topup(uid, body.amount)
            if bonus:
                points = await _current_points(session)
            return ok({"paid": True, "points": points, "order_status": "success",
                       "recharged_points": max(0, points - body.baseline_points),
                       "bonus_points": bonus})
        return await _pay_status_by_balance(uid, body, points)

    if not trade_no:
        # 老前端缓存兜底：没有订单号时只能退回余额比对
        return await _pay_status_by_balance(uid, body, points)

    try:
        order = await na.find_topup_order(session["pat"], uid, trade_no)
    except NewApiError as e:
        # 查单失败不能报「支付失败」——钱可能已经到账，只是这一次查询没成功。
        # 回报 paid=False 让前端继续轮询，比抛错更贴近事实。
        logger.warning("查单失败 uid=%s trade_no=%s: %s", uid, trade_no, e.message)
        return ok({"paid": False, "points": points, "order_status": "unknown"})

    if order is None:
        # 订单不属于当前用户，或已超出上游 30 天查询窗口
        logger.warning("查单未命中 uid=%s trade_no=%s", uid, trade_no)
        return ok({"paid": False, "points": points, "order_status": "not_found"})

    status = str(order.get("status") or "")
    if status != na.TOPUP_SUCCESS:
        return ok({"paid": False, "points": points, "order_status": status or "pending"})

    # 订单已 success：以订单实付金额为准发放赠送，而不是前端传的 amount ——
    # amount 来自客户端，可被篡改成 ¥500 骗取更高档位的赠送。
    paid_cny = _order_paid_cny(order, body.amount)
    bonus = await promo.grant_first_topup(uid, paid_cny)
    if bonus:
        points = await _current_points(session)
    return ok({"paid": True, "points": points, "order_status": na.TOPUP_SUCCESS,
               "recharged_points": max(0, points - body.baseline_points),
               "bonus_points": bonus})


def _order_paid_cny(order: dict, fallback_amount: int) -> float:
    """从订单记录取实付金额（元）。取不到时回落到下单金额。

    只信上游订单里的 money 字段：它由 new-api 在建单时写入、回调时校验，
    客户端改不动。前端传来的 amount 仅作为极端异常下的兜底。
    """
    try:
        money = float(order.get("money") or 0)
    except (TypeError, ValueError):
        money = 0.0
    return money if money > 0 else float(fallback_amount or 0)


async def _pay_status_by_balance(uid: int, body: PayStatusBody, points: int):
    """兜底判定：余额比对（仅在拿不到订单号时使用）。

    保留 90% 容差是为了给上游折扣/手续费留余量；这条路径精度不如按单查询
    （会被其他来源的到账干扰），所以只在没有订单号时才走。
    """
    gained = points - body.baseline_points
    if gained <= 0:
        return ok({"paid": False, "points": points, "order_status": "pending"})
    expected = config.cny_to_points(body.amount)
    if gained < expected * 0.9:
        logger.warning("pay_status 到账不匹配 uid=%s expected=%s gained=%s", uid, expected, gained)
        return ok({"paid": False, "points": points, "order_status": "pending"})
    bonus = await promo.grant_first_topup(uid, body.amount)
    return ok({"paid": True, "points": points, "order_status": "success",
               "recharged_points": gained, "bonus_points": bonus})


@app.post("/api/user/pay/confirm")
async def confirm_pay_order(body: PayConfirmBody, session: dict = Depends(require_session)):
    if not MOCK:
        return fail("真实环境到账由支付网关回调完成，无需确认", 400)
    order = store.orders.get(body.order_no)
    if order is None or order["uid"] != session["uid"]:
        return fail("订单不存在", 404)
    if order["status"] == "paid":
        return ok(None, "订单已支付，请勿重复操作")
    order["status"] = "paid"
    cny = order["amount_cents"] / 100
    store.add_quota(session["uid"], config.points_to_quota(config.cny_to_points(cny)),
                    f"在线充值 ¥{cny:g}（{order['method']}）")
    bonus = await promo.grant_first_topup(session["uid"], cny)
    user = store.get_user_by_uid(session["uid"])
    msg = "支付成功，积分已到账"
    if bonus:
        msg += f"，首充额外赠送 {bonus:,} {config.POINTS_UNIT_NAME}"
    return ok({"points": P(user["quota"]), "bonus_points": bonus}, msg)


@app.post("/api/user/topup")
async def topup(body: TopupBody, session: dict = Depends(require_session)):
    code = body.key.strip()
    if len(code) < 6:
        return fail("兑换码格式不正确")
    if MOCK:
        # 与真实环境同语义：只有管理员发放过的码才能充值，不能随便编。
        try:
            quota = store.use_redemption(code, session["uid"])
        except ValueError as e:
            return fail("该兑换码已被使用" if str(e) == "used"
                        else "兑换码不存在，请检查后重试")
        pts = P(quota)
        return ok({"points": pts}, f"兑换成功，到账 {pts:,} {config.POINTS_UNIT_NAME}")
    before = await _current_points(session)
    try:
        await na.topup(session["pat"], session["uid"], code)
    except NewApiError as e:
        msg = e.message
        if "Redemption failed" in msg:
            msg = "兑换失败：兑换码无效或已被使用"
        return fail(msg, e.status_code if e.status_code != 502 else 502)
    after = await _current_points(session)
    gained = max(0, after - before)
    return ok({"points": gained, "balance": after},
              f"兑换成功，到账 {gained:,} {config.POINTS_UNIT_NAME}")


@app.post("/api/mock/redemption")
async def mock_create_redemption(body: MockRedemptionBody):
    """仅 mock 模式：发放兑换码，对应管理员 POST /api/redemption/。

    真实环境的码必须由管理员在 new-api 后台创建，BFF 不提供发卡能力 ——
    这里只是为了让演示和自动化测试能拿到有效的码。
    """
    if not MOCK:
        return fail("真实环境请在 new-api 后台创建兑换码", 404)
    cny = body.cny if body.cny and body.cny > 0 else 10
    quota = config.points_to_quota(config.cny_to_points(cny))
    recs = [store.create_redemption(quota, name=body.name or f"测试卡 ¥{cny:g}")
            for _ in range(max(1, min(body.count, 20)))]
    return ok({"keys": [r["key"] for r in recs],
               "points": config.cny_to_points(cny)}, "已发放")


# ==================== 管理员：动态配置 ====================
class SettingsPatchBody(BaseModel):
    values: dict


class SettingsResetBody(BaseModel):
    keys: list[str]


def _settings_view() -> dict:
    """管理页所需的完整视图：字段元数据 + 当前值 + 默认值 + 是否被覆盖。

    元数据由后端下发而非前端硬编码 —— 否则每加一个配置项都要改两处，
    且前端漏改的表现是「字段存在却不可见」，比报错更难发现。
    """
    defaults = config.defaults()
    items = []
    for key, (group, label, _fn, hint) in dyn_settings.SPECS.items():
        default = defaults[key]
        items.append({
            "key": key, "group": group, "label": label, "hint": hint,
            "type": ("bool" if isinstance(default, bool)
                     else "int" if isinstance(default, int)
                     else "float" if isinstance(default, float)
                     else "list" if isinstance(default, tuple | list)
                     else "str"),
            "value": getattr(config, key),
            "default": list(default) if isinstance(default, tuple) else default,
            "overridden": dyn_settings.has_override(key),
        })
    return {
        "items": items,
        "groups": [{"key": k, "label": v}
                   for k, v in dyn_settings.GROUP_LABELS.items()],
    }


@app.get("/api/admin/settings")
async def admin_settings_get(_s: dict = Depends(require_admin)):
    return ok(_settings_view())


# 改这些键会影响档案的插值结果或可见范围，保存后必须丢弃档案缓存，
# 否则页面提示「立即生效」而文档页仍是旧文案 —— 这种「提示成功但没变化」
# 最难排查。宁可多失效一次（重新读六份 YAML 的成本可忽略）。
_DOCS_AFFECTING_KEYS = frozenset({
    "BRAND_NAME", "BRAND_CONTACT", "API_BASE_URL", "POINTS_UNIT_NAME",
    "POINTS_PER_CNY", "DOC_DEFAULT_MODEL", "DOC_PRODUCTS",
    "DOC_VIDEO_WIN_URL", "DOC_VIDEO_MAC_URL", "DOC_VIDEO_MANUAL_URL",
    # DOC_MODELS 会出现在教程「手动配置」的支持模型清单里，也要失效缓存
    "DOC_MODELS",
})
_PRICING_AFFECTING_KEYS = frozenset({
    "PRICING_TTL", "PRICING_GROUPS", "DOC_MODELS", "POINTS_PER_CNY",
})


def _invalidate_caches(keys: Iterable[str]) -> None:
    touched = set(keys)
    if touched & _DOCS_AFFECTING_KEYS:
        docs_catalog.invalidate()
    if touched & _PRICING_AFFECTING_KEYS:
        pricing.invalidate()


@app.put("/api/admin/settings")
async def admin_settings_put(body: SettingsPatchBody,
                             _s: dict = Depends(require_admin)):
    try:
        await dyn_settings.update(body.values)
    except dyn_settings.ValidationError as e:
        return fail(str(e), 400)
    except dyn_settings.StorageError as e:
        # 部署问题（卷没挂 / 路径不可写），不是请求内容的问题，用 500 而非 400。
        return fail(str(e), 500)
    _invalidate_caches(body.values.keys())
    return ok(_settings_view(), "已保存，立即生效")


@app.post("/api/admin/settings/reset")
async def admin_settings_reset(body: SettingsResetBody,
                               _s: dict = Depends(require_admin)):
    try:
        await dyn_settings.reset(body.keys)
    except dyn_settings.ValidationError as e:
        return fail(str(e), 400)
    except dyn_settings.StorageError as e:
        return fail(str(e), 500)
    _invalidate_caches(body.keys)
    return ok(_settings_view(), "已重置为环境变量默认值")


# ==================== 产品文档 ====================
# 教程内容不再硬编码在前端，而是由 docs/products/*.yml 驱动，
# 新增在售产品只需加一个档案文件，前后端都不用改。

@app.get("/api/docs")
async def docs_index():
    """产品索引。不含正文，避免首屏把六份档案全拉下来。"""
    return ok({"products": docs_catalog.index()})


@app.get("/api/docs/{product_id}")
async def docs_detail(product_id: str):
    """单产品正文。含 pricing_table 段时注入实时系数表。

    定价拉取失败不影响本接口返回 —— 文档是售前页面，打不开等于卖不出去，
    一张标注了日期的旧价格表远好过一个白屏。stale 标记交给前端显式提示。
    """
    product = docs_catalog.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="产品不存在或未上架")

    if any(s["type"] == "pricing_table" for s in product["sections"]):
        pricing_data = await pricing.table()
        # 不改档案缓存里的原对象，否则第二次请求会读到被注入过的副本
        product = dict(product)
        product["sections"] = [
            {**s, "data": pricing_data} if s["type"] == "pricing_table" else s
            for s in product["sections"]
        ]
    return ok(product)


# ==================== 健康检查 ====================
APP_VERSION = config.APP_VERSION  # 单一事实源在 config，健康检查与 Logfire 共用
_STARTED_AT = time.time()


@app.get("/healthz")
async def healthz():
    """存活探针：纯本地判断，不触碰上游，可高频调用。

    刻意不调 new-api —— 探针一旦依赖上游，上游抖动就会让容器被编排系统反复重启，
    把一次可恢复的上游故障放大成本服务的雪崩。上游可用性由业务接口自身的错误
    处理和监控覆盖。
    """
    return {"status": "ok", "mode": "mock" if MOCK else "real",
            "version": APP_VERSION, "uptime_seconds": int(time.time() - _STARTED_AT)}


@app.get("/readyz")
async def readyz():
    """就绪探针：校验「上线前必须配对的部署条件」，任一不满足返回 503。

    检查项都是真实踩过的坑：静态资源没进镜像会让首页 500；data 目录不可写会让
    首充赠送失去幂等（同一用户可反复领取）；SECRET_KEY 用默认值等于任何人都能
    伪造会话 Cookie 冒充任意用户、并解出别人 Cookie 里的 PAT（该值经 HKDF 派生
    出会话加密密钥）；管理员凭证缺失会让建号/赠送/兑换码静默瘫痪。
    """
    # 每项返回 (通过?, 失败原因)。原因必须是**可行动的**：
    # 早期版本只返回一个 bool，运维看到 `secret_key_configured: false` 时，
    # 「变量没注入」「注入了空串」「值太短」这三种完全不同的处置动作
    # （补 .env / 查 compose 插值 / 重新生成密钥）无法区分 —— 实测就是
    # 因为这个日志读不出信息，才多花了时间在错的方向上排查。
    checks: dict[str, bool] = {}
    reasons: dict[str, str] = {}

    for name, (passed, reason) in {
        "static_assets": _check_static_assets(),
        "state_dir_writable": _check_state_dir(),
        # mock 模式是本地演示，允许用默认密钥；真实模式必须注入
        "secret_key_configured": (True, "") if MOCK else _check_secret_key(),
        # 真实模式必须有管理员凭证，否则注册、首充赠送、兑换码登录全都不可用。
        # config.py 已移除默认账密（会随公开仓库/镜像分发），所以这里要显式兜住。
        "admin_cred_configured": (True, "") if MOCK else _check_admin_cred(),
        # 档案文件没进镜像时，产品页会静默变成空列表而不是报错 ——
        # 没有任何告警，只是「文档入口点进去什么都没有」，很容易上线后才被用户发现。
        "doc_products_loadable": _check_doc_products(),
    }.items():
        checks[name] = passed
        if not passed:
            reasons[name] = reason

    if reasons:
        # 带原因一起打，日志本身就够定位问题，不用再去翻代码猜哪个分支挂了
        logger.error("readiness 检查未通过: %s",
                     "; ".join(f"{k}: {v}" for k, v in reasons.items()))
        return JSONResponse(status_code=503,
                            content={"status": "unready", "checks": checks,
                                     "failed": list(reasons), "reasons": reasons})
    return {
        "status": "ready", "checks": checks, "version": APP_VERSION,
        # 上游地址对排障至关重要：连错库（测试/生产）不会报错，只会表现为
        # 「用户在 A 环境注册的账号，用 B 环境的 Key 调不通」。这里回显 host
        # （不含路径与任何凭证，readyz 是无认证探针），并标记是否走了默认值。
        "newapi_host": urlparse(config.NEWAPI_BASE_URL).hostname or "",
        "newapi_is_default": bool(getattr(config, "NEWAPI_BASE_URL_IS_DEFAULT", False)),
    }


def _check_doc_products() -> tuple[bool, str]:
    """档案能加载、且白名单里的 id 都真实存在。

    白名单拼错一个字母的后果是该产品从页面上消失，而不是报错 —— 运营不会知道，
    所以必须在就绪阶段就拦住。
    """
    try:
        loaded = docs_catalog.all_products()
    except Exception as e:  # 档案语法错误 / 目录缺失
        return False, f"产品档案加载失败：{e}（检查 docs/products 是否进了镜像）"
    if not loaded:
        return False, f"{docs_catalog.DOCS_DIR} 下没有任何有效档案"
    unknown = [p for p in config.DOC_PRODUCTS if p not in loaded]
    if unknown:
        return False, (f"DOC_PRODUCTS 含不存在的产品 id {unknown}，"
                       f"这些产品不会显示；可选值：{sorted(loaded)}")

    # 图标键名写错只会静默回落成 book，页面看着「正常」但图标是错的。
    # 键集从 static/app.js 现场解析，避免这里硬编码一份副本跟前端漂移。
    known_icons = _icon_keys()
    if known_icons:
        bad = sorted({
            f"{pid}:{p['icon']}" for pid, p in loaded.items()
            if p.get("icon") and p["icon"] not in known_icons
        })
        if bad:
            return False, (f"档案图标键名不存在于 static/app.js ICONS：{bad}；"
                           f"可选值：{sorted(known_icons)}")
    return True, ""


def _icon_keys() -> set[str]:
    """解析 static/app.js 的 ICONS 注册表键名。

    解析失败返回空集，让调用方跳过这项检查 —— 图标键名校验是锦上添花，
    不该因为正则没匹配上就把整个服务判成 unready。
    """
    try:
        src = (STATIC_DIR / "app.js").read_text("utf-8")
    except OSError:
        return set()
    m = re.search(r"const\s+ICONS\s*=\s*\{", src)
    if not m:
        return set()
    # 从 `{` 起做括号配平，取到注册表字面量结束为止，避免误吞后面的代码
    depth, start = 0, m.end() - 1
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[start:i]
                return set(re.findall(r"[\{,]\s*([a-zA-Z_][\w]*)\s*:", body))
    return set()


def _check_static_assets() -> tuple[bool, str]:
    if (STATIC_DIR / "index.html").is_file():
        return True, ""
    return False, f"{STATIC_DIR / 'index.html'} 不存在（静态资源未进镜像，首页会 500）"


def _check_state_dir() -> tuple[bool, str]:
    if _state_dir_writable():
        return True, ""
    d = Path(config.PROMO_STATE_FILE).parent
    return False, f"{d} 不可写（赠送幂等无法落盘，同一用户可反复领取）"


def _state_dir_writable() -> bool:
    """活动状态目录是否可写（决定赠送幂等能否落盘）。"""
    d = Path(config.PROMO_STATE_FILE).parent
    try:
        d.mkdir(parents=True, exist_ok=True)
        return os.access(d, os.W_OK)
    except OSError:
        return False


# 已知弱值黑名单。只比对「不等于默认值」是不够的：`BFF_SECRET_KEY=` 传空串时
# 它确实不等于默认值，但空密钥派生出的 AES 密钥是公开可计算的，等于没加密 ——
# 实测踩到过。
_DEFAULT_SECRET = config.SECRET_KEY_DEFAULT  # 单一事实源，避免两处字面量漂移
_WEAK_SECRETS = {_DEFAULT_SECRET, "changeme", "secret", "test"}
_MIN_SECRET_LEN = 32  # openssl rand -hex 32 给 64 字符，正常配置远超此值


# 失败时回给运维的修复指引文案，不是密钥本身（变量名含 SECRET 触发 S105 误报）。
_FIX_SECRET = "用 `openssl rand -hex 32` 生成后写入 .env 的 BFF_SECRET_KEY"  # noqa: S105


def _check_secret_key() -> tuple[bool, str]:
    """会话加密密钥是否足够强，附可行动的失败原因。

    自 Cookie 改为 AES-256-GCM 加密后（app/security.py），这个值经 HKDF 派生出
    AES 密钥，它同时承担两件事：机密性（PAT 不可读）与完整性（会话不可伪造）。
    密钥弱 = 攻击者既能伪造会话冒充任意用户，也能解出别人 Cookie 里的 PAT。
    这是宁可不启动也不能放过的配置错误，判定从严：空值、过短、已知弱值一律不通过。

    原因里只带长度和判定结论，**不带密钥内容**：readyz 是无认证探针，
    响应会进日志和监控系统，泄露密钥前缀等于泄露密钥。
    """
    raw = config.SECRET_KEY or ""
    key = raw.strip()
    if not key:
        # 区分「压根没设」和「设了空串」：前者补变量，后者查 compose 插值是否被吞
        how = "BFF_SECRET_KEY 未注入" if not raw else "BFF_SECRET_KEY 注入了空值"
        return False, f"{how}，{_FIX_SECRET}"
    if key.lower() in _WEAK_SECRETS:
        # config.SECRET_KEY 有默认占位值兜底，所以「变量没注入」会走到这里。
        # 两者处置动作不同（补变量 vs 换掉手填的弱值），措辞要分开。
        if key == _DEFAULT_SECRET:
            return False, f"BFF_SECRET_KEY 未注入，仍是默认占位值，{_FIX_SECRET}"
        return False, f"BFF_SECRET_KEY 是已知弱值，{_FIX_SECRET}"
    if len(key) < _MIN_SECRET_LEN:
        return False, (f"BFF_SECRET_KEY 只有 {len(key)} 字符，"
                       f"至少需要 {_MIN_SECRET_LEN}，{_FIX_SECRET}")
    return True, ""


def _check_admin_cred() -> tuple[bool, str]:
    """是否具备管理员凭证（PAT 直供 或 账密，二者其一即可），附失败原因。

    PAT 优先：它不经过 new-api 的会话系统，可绕开「50 会话硬上限打满后
    login 永久 409」这个最高危单点故障。
    """
    has_pat = bool(config.NEWAPI_ADMIN_PAT and config.NEWAPI_ADMIN_UID)
    has_userpass = bool(config.NEWAPI_ADMIN_USERNAME and config.NEWAPI_ADMIN_PASSWORD)
    if has_pat or has_userpass:
        return True, ""
    # 指明缺哪一半，避免运维两边都去试
    if config.NEWAPI_ADMIN_PAT and not config.NEWAPI_ADMIN_UID:
        return False, "已配 NEWAPI_ADMIN_PAT 但缺 NEWAPI_ADMIN_UID，两者必须成对"
    if config.NEWAPI_ADMIN_USERNAME and not config.NEWAPI_ADMIN_PASSWORD:
        return False, "已配 NEWAPI_ADMIN_USERNAME 但缺 NEWAPI_ADMIN_PASSWORD，两者必须成对"
    return False, ("缺管理员凭证，注册/首充赠送/兑换码登录都会失效；"
                   "配 NEWAPI_ADMIN_PAT+NEWAPI_ADMIN_UID（推荐，可绕开会话上限）"
                   "或 NEWAPI_ADMIN_USERNAME+NEWAPI_ADMIN_PASSWORD")


# ==================== 静态资源 ====================
def _asset_hash(name: str) -> str:
    """按文件内容算短哈希，用作 URL 版本号。

    没有版本号时浏览器会一直用缓存里的旧 app.js —— 发版后用户看到的还是老界面，
    且新后端 + 老前端的组合很容易出现难以复现的诡异问题。
    内容哈希保证「改了才失效、没改就命中缓存」。
    """
    try:
        # usedforsecurity=False：这里只是缓存版本号，不做完整性校验，
        # 也不参与任何安全判断，选 md5 纯粹图快
        return hashlib.md5((STATIC_DIR / name).read_bytes(),
                           usedforsecurity=False).hexdigest()[:8]
    except OSError:
        return "0"


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("/static/style.css", f"/static/style.css?v={_asset_hash('style.css')}")
    html = html.replace("/static/app.js", f"/static/app.js?v={_asset_hash('app.js')}")
    # index.html 本身必须不缓存，否则版本号更新推不到客户端
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


# ==================== 客户端配置脚本直读（/setup/<file>） ====================
# 教程页一键命令（curl https://<域名>/setup/workbuddy-mac.sh | bash）与
# 「下载配置工具」链接都指向 /setup/<file>。static 目录挂在 /static 前缀下，
# 这里补一条不带前缀的路由，部署后无需 nginx 额外映射 /setup -> static/setup：
#   - 白名单取 setup 目录实际文件名（新增脚本零改动）
#   - Content-Disposition: attachment 保证浏览器点击 .cmd/.sh 是下载而非打开
#     （curl | bash 不受该头影响，两种用法共用同一路径）
_SETUP_DIR = STATIC_DIR / "setup"


def _site_origin(request: Request) -> str:
    """当前用户访问 BFF 的实际 origin（含协议）。

    容器前通常有反向代理（Ingress / nginx），此时 request.url 是容器内的
    http://app:8000，而用户看到的是 https://workbuddy.oneapis.cn —— 所以优先
    取 X-Forwarded-*（Host 才是用户实际输入的域名，Proto 决定 http/https）。
    两个头都缺时回退 starlette 的 base_url（裸跑场景）。
    """
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "").strip()
    proto = (request.headers.get("x-forwarded-proto", "")
             .split(",")[0].strip() or request.url.scheme)
    if host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


@app.get("/setup/{filename}")
async def setup_file(filename: str, request: Request):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="文件不存在")
    path = _SETUP_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    # 脚本里的域名占位符在下发时按当前访问域名/后台配置替换：同一份代码部署到
    # 测试环境（aihuobao）与生产（oneapis）都自动适配，不必为换环境改代码。
    #   __BFF_ORIGIN__    → 用户访问 BFF 的 origin（脚本下载源，也从这里拉
    #                       /api/config 与能力清单）
    #   __API_BASE_URL__  → 后台「对外 API 地址」（new-api 网关，环境相关）
    # 占位符未替换（旧镜像/直接打开模板文件）时脚本会走内置兜底清单，不会崩。
    text = (path.read_text(encoding="utf-8")
            .replace("__BFF_ORIGIN__", _site_origin(request))
            .replace("__API_BASE_URL__", str(config.API_BASE_URL)))
    body = text.encode("utf-8")
    # Windows 批处理（.cmd/.bat）必须 CRLF 换行：LF-only 会被 cmd.exe 误解析，
    # 表现为双击闪退。git 仓库按 LF 存储、Linux 镜像里也是 LF，这里在下发时
    # 统一规范化为 CRLF（不依赖 git 配置，任何构建/运行环境都正确）。
    if filename.lower().endswith((".cmd", ".bat")):
        body = body.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


# ==================== 支付回跳落地页 ====================
# 易支付的 return_url 由 new-api 用 ServerAddress + "/usage-logs" 拼出
# （controller/topup.go: paymentReturnPath("/usage-logs")），BFF 无法在下单时改写它。
# 而 BFF 是 hash 路由（#/topup），/usage-logs 这个真实路径原本不存在 → 用户支付完
# 落到 404。这里把它接住，302 回 SPA 的充值页并带上订单号。
#
# 关键认知：**回跳只影响体验，不影响到账**。到账走 notify_url（支付网关服务端
# 直连 new-api），与浏览器跳哪儿完全无关。所以本落地页不做任何发钱动作，
# 也不校验易支付签名 —— 它只是个跳转，签名校验由 new-api 的 notify 端负责。
# 若这里据 URL 参数发钱，任何人手拼一个 trade_status=TRADE_SUCCESS 就能白拿额度。
def _pay_return_redirect(request: Request) -> RedirectResponse:
    trade_no = (request.query_params.get("out_trade_no")
                or request.query_params.get("trade_no") or "").strip()
    # 只放行订单号字符集，避免把任意内容拼进 Location 造成开放重定向/XSS
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", trade_no):
        trade_no = ""
    target = f"/#/topup?trade_no={trade_no}" if trade_no else "/#/topup"
    return RedirectResponse(target, status_code=302)


@app.get("/usage-logs")
async def pay_return_usage_logs(request: Request):
    """接住 new-api 默认 return_url（ServerAddress + /usage-logs）的回跳。"""
    return _pay_return_redirect(request)


@app.get("/pay/return")
async def pay_return(request: Request):
    """BFF 自有回跳地址。把上游 ServerAddress 配成 BFF 域名后，
    这个路径语义比 /usage-logs 更贴切，两者行为一致。"""
    return _pay_return_redirect(request)


@app.get("/console/log")
async def pay_return_console_log(request: Request):
    """兼容 new-api 旧版主题的回跳路径。"""
    return _pay_return_redirect(request)


# ==================== WorkBuddy 一键配置（链接触发 + 确定性配置器）====================
def _load_capabilities() -> dict:
    """读 WorkBuddy 模型能力清单（图片输入），默认全部开启。

    文件：static/setup/workbuddy-model-capabilities.txt，格式 `模型ID|true/false`，
    `#` 开头为注释，查找键统一小写。未登记视为 true。
    """
    p = Path(__file__).resolve().parent.parent / "static" / "setup" / "workbuddy-model-capabilities.txt"
    caps: dict[str, bool] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return caps
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        mid, val = line.split("|", 1)
        caps[mid.strip().lower()] = val.strip().lower() not in ("false", "0", "no")
    return caps


@app.post("/api/setup/ticket")
async def issue_setup_ticket_endpoint(request: Request, session: dict = Depends(require_session)):
    """签发 WorkBuddy 一键配置链接所需的 ticket。

    仅登录用户可调用（防别人生成你的配置链接）。ticket 自包含 uid+pat、
    短时效（10 分钟），返回的 url/deeplink 交给用户复制到 WorkBuddy 即可，
    链接本身不含任何长期密钥。
    """
    bff_origin = str(request.base_url).rstrip("/")
    ticket = issue_setup_ticket(session["uid"], session["pat"])
    # bff 参数必须是 BFF 自身地址：WorkBuddy 用它调 GET {bff}/api/setup/export。
    # 不能填上游网关 config.API_BASE_URL —— 否则 WorkBuddy 会去网关域调 export 而 404。
    # export 内部调 new-api 用的是服务端配置的 NEWAPI_BASE_URL，不依赖这个参数。
    #
    # 强制 https：避免 WorkBuddy 从 http 调 export 时触发 nginx 301，
    # 不同 HTTP 客户端对 30x 重定向的 query/method 保留行为有差异
    # （部分客户端跟 301 后会丢 query 或改 method），直连 https 最稳。
    bff = f"https://{urlparse(bff_origin).netloc}".rstrip("/")
    url = f"{bff}/setup?ticket={ticket}&bff={quote(bff)}"
    deeplink = f"workbuddy://import-models?bff={quote(bff)}&ticket={ticket}"
    return ok({"ticket": ticket, "url": url, "deeplink": deeplink, "expires_in": 600})


async def _build_setup_models(pat: str) -> dict:
    """用给定 PAT 调 new-api 的 /v1/models，拼装 WorkBuddy 可用的模型配置片段。

    白名单过滤 / vendor 映射 / 能力拼装都在这里完成，WorkBuddy 只负责写文件。

    /v1/models 是 OpenAI 兼容的用户态端点：请求头带 ``Authorization: Bearer <PAT>``
    即可，new-api 会自动从 token 解析出当前用户，**不需要**像 /api/token/、/api/user/amount
    这类管理类接口那样额外带 ``New-Api-User`` 头，因此也不必先 GET /api/user/self 反查 uid。
    """
    whitelist = list(config.DOC_MODELS)
    vendor_map = dyn_settings.model_vendor_map()
    base_url = config.API_BASE_URL
    caps = _load_capabilities()

    body = await na.request("GET", "/v1/models",
                            headers={"Authorization": f"Bearer {pat}"})
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        items = []
    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]

    def allowed(mid: str) -> bool:
        if mid in whitelist:
            return True
        return any(mid.startswith(w) for w in whitelist)

    filtered = [mid for mid in ids if allowed(mid)]
    out = []
    for mid in filtered:
        out.append({
            "id": mid,
            "name": mid,
            "vendor": vendor_map.get(mid, "Custom"),
            "url": base_url,
            "apiKey": pat,
            "supportsToolCall": True,
            "supportsImages": caps.get(mid.lower(), True),
            "supportsReasoning": True,
            "useCustomProtocol": False,
            "reasoning": {"supportedEfforts": ["low", "medium", "high", "xhigh", "max"]},
            "maxInputTokens": 200000,
            "maxOutputTokens": 65536,
        })
    return {"models": out, "count": len(out), "base_url": base_url}


class SetupPatBody(BaseModel):
    pat: str


@app.post("/api/setup/from-pat")
async def setup_from_pat(body: SetupPatBody):
    """用用户当场粘贴的 PAT 拼装 WorkBuddy 模型配置（方式 2）。

    与 /api/setup/export（ticket 方案）相比：不依赖 BFF 登录态、也不依赖 cookie
    里的 PAT。PAT 是 new-api 的瞬时钥匙，长期持有会被强刷覆盖导致 401；让用户从
    new-api 官方前端复制最新 PAT 当场粘贴，钥匙永远是新鲜的。BFF 收到后只做一次性
    查询与拼装，不持久化。返回结构等同 /api/setup/export，供 WorkBuddy skill 直接写盘。
    """
    pat = body.pat.strip()
    if not pat:
        raise HTTPException(status_code=400, detail="缺少 PAT（api key）")
    try:
        result = await _build_setup_models(pat)
    except NewApiError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return ok(result)


# ==================== WorkBuddy 一键配置：链接触发（Y 档，无明文密钥）====================
# 用户粘贴 PAT → 平台调 prep 端点把 PAT 加密进一次性 tok（无状态、10 分钟有效）→
# 返回链接 https://<origin>/api/setup/import-doc?tok=...（链接本身不含明文 PAT）。
# 用户把链接用自然语言包裹（如「读取我给你的链接对应的文档，按照文档执行 <链接>」）
# 发给本机 WorkBuddy；WorkBuddy 的 AI fetch 该链接 → BFF 解开 tok 拿 PAT → 调 new-api
# 拼装模型清单 → 内联进一篇 markdown「执行说明书」返回。AI 按文档直接写本机
# %USERPROFILE%\.workbuddy\models.json，全程零登录态、零 skill 预装、链接不含密钥。
# 复用 issue_setup_ticket / open_setup_ticket（AES-GCM 无状态票据，TTL=_SETUP_TTL=10min）。
class SetupImportLinkBody(BaseModel):
    pat: str


@app.post("/api/setup/import-doc-prep")
async def import_doc_prepare(body: SetupImportLinkBody, request: Request):
    """预生成 WorkBuddy 导入链接：把 PAT 加密进一次性 tok，返回不含明文密钥的链接。

    不依赖登录态（用户给的是 new-api PAT，不是 BFF 会话）。返回的链接 10 分钟内有效、
    且泄露危害等同短期票据（不含明文 PAT）。
    """
    pat = body.pat.strip()
    if not pat:
        raise HTTPException(status_code=400, detail="缺少 PAT（api key）")
    # uid 固定 0：import-doc 端点只取 pat，不需要 uid 语义
    tok = issue_setup_ticket(0, pat)
    origin = str(request.base_url).rstrip("/")
    link = f"{origin}/api/setup/import-doc?tok={tok}"
    return ok({"link": link, "expires_in": _SETUP_TTL})


@app.post("/api/setup/import-doc-prep-session")
async def import_doc_prepare_session(request: Request, session: dict = Depends(require_session)):
    """登录态一键预生成 WorkBuddy 导入链接：从当前会话的 cookie 拿 PAT，签发一次性 tok。

    入口为浏览器里已经登录本平台的用户。零摩擦 — 不需要用户再去 new-api 复制 PAT、也不需要
    在这里再粘贴一遍，直接点按钮就把链接生成并复制到剪贴板（自然语言包裹版）。然后粘到本机
    WorkBuddy 即可。依旧走 issue_setup_ticket / open_setup_ticket（AES-GCM 无状态票据），
    链接本身不含明文 PAT，TTL 同 _SETUP_TTL（10 分钟）。

    未登录直接 401（require_session 兜底）。
    """
    pat = (session.get("pat") or "").strip()
    if not pat:
        raise HTTPException(status_code=401, detail="当前会话未携带 PAT，请重新登录")
    # 生成链接前先探活：new-api 的 PAT 会在「再次生成/复制令牌」时被覆盖作废，
    # 而 BFF 登录 cookie 不校验 PAT 是否存活，避免用户拿到一条必失败的链接。
    # 直接拿 PAT 调 /v1/models（OpenAI 兼容端点，Bearer 即可）验证是否还活着。
    try:
        await na.request("GET", "/v1/models",
                        headers={"Authorization": f"Bearer {pat}"})
    except NewApiError as e:
        if e.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="登录会话里的访问令牌已失效（可能你在别处重新生成过令牌）。"
                       "请在 workbuddy.oneapis.cn 退出重新登录，再生成导入链接。",
            )
        raise
    uid = int(session.get("uid") or 0)
    tok = issue_setup_ticket(uid, pat)
    origin = str(request.base_url).rstrip("/")
    link = f"{origin}/api/setup/import-doc?tok={tok}"
    return ok({"link": link, "expires_in": _SETUP_TTL})


@app.get("/api/setup/import-doc")
async def import_doc(tok: str = Query(...)):
    """动态生成 WorkBuddy 导入文档（markdown）。

    tok 由 import-doc-prep 签发（加密含 pat + 短时效）。解开拿到 pat 后用它调 new-api
    拼装模型清单，内联进一篇 markdown「执行说明书」返回。WorkBuddy 的 AI fetch 此链接
    即得完整导入指令 + 数据，无需再发 HTTP、无需 skill 预装。

    失败（tok 篡改/过期/畸形）→ 404，不透露原因。
    """
    sess = open_setup_ticket(tok)
    if not sess:
        raise HTTPException(status_code=404, detail="链接无效或已过期，请重新生成")
    pat = sess["pat"]
    try:
        result = await _build_setup_models(pat)
    except NewApiError as e:
        if e.status_code in (401, 403):
            # PAT 在 new-api 端已死（被再次生成令牌覆盖 / 过期）。给清晰指引，
            # 不要原样透传 new-api 那句含糊的「凭证已失效，请重新登录」。
            raise HTTPException(
                status_code=410,
                detail="链接内的访问令牌已失效（可能在别处重新生成过令牌）。"
                       "请在 workbuddy.oneapis.cn 重新登录，再重新生成导入链接。",
            )
        raise HTTPException(status_code=e.status_code, detail=str(e))
    models = result["models"]
    data_json = json.dumps({"models": models}, ensure_ascii=False, indent=2)
    md = (
        "# WorkBuddy 自定义模型导入\n\n"
        "你是 WorkBuddy 助手。请按以下步骤把模型配置写入本机，不要向我索要额外信息。\n\n"
        "1. 读取下方「模型配置数据」中的 JSON。\n"
        "2. 读取本机配置文件（Windows: `%USERPROFILE%\\.workbuddy\\models.json`；"
        "macOS/Linux: `~/.workbuddy/models.json`）。\n"
        "3. 将 JSON 的 `models` 数组合并进该文件的 `models` 数组：按 `id` 去重，已存在的覆盖。\n"
        "4. 原子写回原文件（先写临时文件再替换，避免半截写入）。\n"
        "5. 提示用户：重启 WorkBuddy 后配置生效。\n\n"
        "## 模型配置数据\n"
        "```json\n"
        f"{data_json}\n"
        "```\n\n"
        "注意：以上数据已包含访问密钥，请勿透露到对话外，写完即可丢弃。\n"
    )
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/setup/export")
async def export_models(
    ticket: Optional[str] = Query(None),
    t: Optional[str] = Query(None),
):
    """导出当前用户可用的模型清单，供 WorkBuddy 确定性写入 models.json。

    免登录：ticket 即凭证（内含 uid+pat）。所有白名单过滤 / vendor 映射 /
    能力拼装都在服务端完成，WorkBuddy 只负责把返回的 models 写入本机文件，
    不解析自然语言、不长期持有密钥。

    参数：`ticket`（推荐，已生成链接默认走这个）或 `t`（alias，兼容早期链接）。
    """
    token = ticket or t
    if not token:
        raise HTTPException(status_code=422, detail="缺少 ticket 参数")
    sess = open_setup_ticket(token)
    if sess is None:
        raise HTTPException(status_code=401, detail="配置链接无效或已过期，请重新生成")
    try:
        result = await _build_setup_models(sess["pat"], sess["uid"])
    except NewApiError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return ok(result)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
