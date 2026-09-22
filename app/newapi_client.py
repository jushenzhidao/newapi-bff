"""new-api 真实代理客户端（契约已对 https://api.aihuobao.cn 实测核实）。

已核实契约（2026-08-22 实测）：
1. 登录:  POST /api/user/login → data.access_token(15min JWT) + data.user.id + data.session.sid
   重要：每次登录都新建一条 UserSession，上限 UserSessionActiveLimit=50，
        判定为 activeCount >= limit **硬拒绝、不淘汰最旧会话**，TTL 30 天。
        打满后返回 409 {"code":"AUTH_SESSION_LIMIT"} 且 30 天内无法登录。
        → BFF 必须在换完 PAT 后立刻 DELETE /api/user/sessions/{sid} 归还会话。
2. PAT:   GET /api/user/token（登录态调用一次）→ data 为长期 PAT 字符串
   用户态请求头：Authorization: Bearer <PAT> + New-Api-User: <uid>（缺一不可）
   PAT 走 users.access_token 列，**不经过会话系统** —— 所以删掉会话后 PAT 仍有效，
   但反过来 PAT 也调不了会话管理接口（403 AUTH_SESSION_REQUIRED）。
2b. 会话: GET    /api/user/sessions            列出活跃会话（需 access_token）
          DELETE /api/user/sessions/{sid}      删除指定会话（需 access_token）
          POST   /api/user/sessions/revoke-others  撤销其他会话（需 access_token）
3. 自身:  GET /api/user/self
4. Key:   GET /api/token/?p=&size=（返回掩码 key: "tTPn****6JJ4"）
          POST /api/token/ 创建（只返回 success，无 key 明文）
          POST /api/token/:id/key → data.key 明文（CriticalRateLimit，别频繁调）
          DELETE /api/token/:id
5. 日志:  GET /api/log/self?p=&page_size=&type=0；GET /api/log/self/stat?type=0 → {quota,rpm,tpm}
6. 支付:  POST /api/user/amount {amount,payment_method,top_up_code} → data 为应付金额字符串
          POST /api/user/pay → data 为易支付表单字段 + url（前端需表单 POST 提交跳转）
          回调 /api/user/epay/notify 由 new-api 自行处理，BFF 不代理
6b. 查单: GET /api/user/topup/self?p=&page_size=&keyword=<trade_no>
          → data.items[{trade_no, status, money, amount, complete_time, ...}]
          status: pending / success（common.TopUpStatus*）
          keyword **精确全匹配**（无 % 时 LIKE 全等），传完整 trade_no 命中 1 条、
          传前缀命中 0 条 —— 与 /api/redemption/search 不匹配 key 的行为不同。
          **身份取自 PAT，不看 New-Api-User 头**：管理员 PAT 配 New-Api-User: 46
          返回的仍是 uid=1 的订单，故无法用管理员 PAT 代查他人订单。
          另有 30 天查询硬窗口（早于此的订单查不到）。
          管理员全平台查单为 GET /api/user/topup（注意不是 /api/topup）。
7. 兑换:  POST /api/user/topup {key} → 失败 message: "Redemption failed..."
8. 建号:  POST /api/user/ (管理员) {username,password,display_name} → 仅 success，
          需 GET /api/user/search?keyword= 反查 uid
          注意：密码有 max 长度校验：实测 20 位通过、24 位报
             "Invalid input Key: 'User.Password' ... failed on the 'max' tag"
9. 加额度: POST /api/user/manage (管理员) {id, action:"add_quota", mode:"add", value:<quota>}
          mode/value 必填，缺失报 "Invalid parameters"；无幂等键，调用方需自行去重
10. 删号:  DELETE /api/user/:id (管理员) → 立即生效，search 随即查不到
11. 改账密: PUT /api/user/ (管理员) {id, username, password, display_name}
          uid 不变，余额/Key/日志全保留；重名报 "Duplicate entry ... for key 'users.username'"
12. 兑换码管理（管理员）:
          POST /api/redemption/ {name, quota, count} → data 为 key 数组
          GET  /api/redemption/?p=&page_size= → items[{id,key,status,quota,used_user_id,...}]
               status: 1=未使用 3=已使用；核销后 used_user_id 回填、redeemed_time 有值
          注意：GET /api/redemption/search?keyword=<完整key> → total=0
             keyword **不匹配 key 字段**（只匹配 name/id），无法按兑换码反查记录。
             两个后果：
               a) 「兑换码 → 账号」的绑定不能靠查表，只能确定性派生（见 redeem_code.py）
               b) 校验一张码是否真实存在，只能翻列表逐条比对（见 find_redemption）
"""
import asyncio
import json as _json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from . import config, observability

logger = logging.getLogger("bff.newapi")


class NewApiError(Exception):
    """new-api 返回业务失败或网络错误。message 可直接展示给用户。

    detail：底层真实原因（异常类型+原文 / 上游响应预览 / 结构化鉴权码），只进日志
    不直接展示给用户（对齐 flovart-bff 2026-09-18 排障需求）。
    """

    def __init__(self, message: str, status_code: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            trust_env=False,  # 不走本机代理
        )
        # 出站调用埋点：上游 new-api 的耗时与失败是 BFF 最主要的故障来源，
        # 只看入站请求会把上游超时误判成 BFF 自身慢。未启用 Logfire 时为空操作。
        observability.instrument_httpx(_client)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def user_headers(pat: str, uid: int) -> dict:
    return {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}


# ---------- 管理员凭证 ----------
# 凭证优先级（越靠前越不消耗会话配额）：
#   1) 环境变量 NEWAPI_ADMIN_PAT —— 完全不碰会话系统，生产首选
#   2) data/admin_cred.json 落盘缓存 —— 进程重启后复用，冷启不消耗会话
#   3) 账密 login 换 PAT —— 兜底，每次消耗一个会话配额（用完立刻归还）
#
# 为什么要这么设计：new-api 的会话上限 50 是**硬拒绝、不淘汰最旧会话**、TTL 30 天。
# 管理员会话一旦打满，login 永久 409，BFF 建号/加额度/兑换码全线瘫痪。
# 而 PAT 走 users.access_token 列，不经过会话系统 —— 会话打满时 PAT 依然可用。
# 只要 PAT 拿得到，BFF 就能一直活着，这是抵御该故障的唯一手段。
#
# 另一个坑：GET /api/user/token 每次调用都重新生成 PAT 并作废旧值（官方前端也会触发）。
# 所以 PAT 可能被外部改掉，401 时必须能回落到 login 重新换取。
_admin_cache: dict = {"pat": None, "uid": None}


def _load_admin_cred() -> None:
    """按优先级装载管理员凭证到内存缓存。"""
    if config.NEWAPI_ADMIN_PAT and config.NEWAPI_ADMIN_UID:
        _admin_cache["pat"] = config.NEWAPI_ADMIN_PAT
        _admin_cache["uid"] = config.NEWAPI_ADMIN_UID
        logger.info("admin cred loaded from env (no session consumed)")
        return
    try:
        with open(config.ADMIN_CRED_FILE, "r", encoding="utf-8") as f:
            d = _json.load(f)
        if d.get("pat") and d.get("uid"):
            _admin_cache["pat"] = d["pat"]
            _admin_cache["uid"] = int(d["uid"])
            logger.info("admin cred loaded from disk cache")
    except (OSError, ValueError, KeyError):
        pass


def _save_admin_cred(*, force: bool = False) -> None:
    """PAT 落盘。失败不影响主流程（只是下次冷启要多消耗一个会话）。

    ⭐ 2026-09-20 多应用共存改造：force=True 时**无视 env 直供也必须落盘** ——
    兜底轮换/读回出的 PAT 是本机凭据缓存文件的最新值，不落盘的话同机其他实例
    （蓝绿/灰度）永远拿不到。跨服务器一致性靠读回恢复（见 _self_heal_admin_cred）。
    """
    if config.NEWAPI_ADMIN_PAT and not force:
        return  # env 直供且非强制时无需落盘
    try:
        os.makedirs(os.path.dirname(config.ADMIN_CRED_FILE), exist_ok=True)
        tmp = config.ADMIN_CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"pat": _admin_cache["pat"], "uid": _admin_cache["uid"]}, f)
        os.replace(tmp, config.ADMIN_CRED_FILE)
        os.chmod(config.ADMIN_CRED_FILE, 0o600)  # PAT 等同管理员密码，禁止其他用户读
    except OSError as e:
        logger.warning("save admin cred failed: %s", e)


def _reload_admin_cred() -> bool:
    """从本机凭据缓存文件重读 PAT；发现与内存不同的新值则采纳并返回 True。

    ⭐ 401 自愈第一步（2026-09-20 多应用共存）：同机其他实例（蓝绿/灰度）可能
    刚完成兜底轮换/读回并把新 PAT 落了盘 —— 此时本进程重读文件即可拿到新值，
    **绝不能再走 login 轮换**（否则会把对方刚换的 PAT 又作废，形成乒乓循环，
    每轮白白消耗一个会话 + 签发额度，直到 50 会话/100 签发双限打爆）。
    磁盘值优先于 env/内存旧值：env 只在冷启时作为初始猜测，401 即证明它已失效。
    """
    try:
        with open(config.ADMIN_CRED_FILE, "r", encoding="utf-8") as f:
            d = _json.load(f)
        pat, uid = d.get("pat"), d.get("uid")
        if pat and uid and pat != _admin_cache["pat"]:
            _admin_cache["pat"] = pat
            _admin_cache["uid"] = int(uid)
            logger.warning(
                "admin PAT updated from cred file (peer instance healed) uid=%s",
                _admin_cache["uid"])
            return True
    except (OSError, ValueError, KeyError):
        pass
    return False


# 兜底轮换单飞锁：并发 401 时只放一个协程去 login，其余等锁后先双检（双检）。
# asyncio.Lock 自 3.10 起不再绑定事件循环，模块级懒创建安全。
_LOGIN_LOCK: "asyncio.Lock | None" = None
# 最近一次兜底轮换的 monotonic 时间戳（2026-09-22 三应用共用 uid=1 互踢熔断）。
#    跨机 + 读回失效场景下，A/B/C 谁轮换谁踢别人 → 无限乒乓烧穿会话/签发额度。
#    冷静期内拒绝再次轮换（见 _self_heal_admin_cred），把乒乓压成有界抖动。
_LAST_ADMIN_ROTATE: list = [0.0]
# 「401 但不是令牌值错」的上游鉴权码（new-api dashboard 链路，2026-09-22 语义分流）。
#    这些 401 的根因是账号状态（被封禁/用户信息非法/会话被吊销），轮换 PAT 救不了，
#    只会白踢共用账号的其他应用（互踢点火源之一）。命中即拒绝自愈轮换、503 转人工。
#    AUTH_UNAUTHORIZED / AUTH_TOKEN_EXPIRED（令牌值问题）不在内 —— 正常轮换。
#    未知码/旧版上游无 code → 维持旧行为（尝试自愈），由冷静期兜底限频。
NO_ROTATE_AUTH_CODES = frozenset({
    "AUTH_USER_DISABLED",     # 账号被封禁
    "AUTH_USER_INVALID",      # 用户信息非法
    "AUTH_SESSION_REVOKED",   # 登录会话被吊销（非 PAT 值问题）
})


def _auth_code_from_error(e: "NewApiError") -> str:
    """从 401 错误 detail 里提取上游结构化鉴权码（upstream_auth_code=AUTH_*），无则空串。"""
    m = re.search(r"upstream_auth_code=([A-Z_]+)", getattr(e, "detail", "") or "")
    return m.group(1) if m else ""


def _login_lock() -> "asyncio.Lock":
    global _LOGIN_LOCK
    if _LOGIN_LOCK is None:
        _LOGIN_LOCK = asyncio.Lock()
    return _LOGIN_LOCK


async def _self_heal_admin_cred(reject_code: str = "") -> None:
    """PAT 401 后的自愈序列（多应用共存根治，2026-09-20；跨机版见 READBACK）：

    0. 语义分流（2026-09-22）：上游 401 带结构化 code 且明确指向**账号状态**问题
       （封禁/用户信息非法/会话吊销）时，轮换 PAT 救不了、只会白踢共用账号的其他
       应用 → 直接拒绝自愈、503 转人工（互踢点火源之一，见 NO_ROTATE_AUTH_CODES）。
    1. 重读凭据文件 —— 同机多实例（蓝绿/灰度）场景对端可能已轮换并落盘，直接采纳即可；
    2. 文件无新值 → 锁内再查（等锁期间同进程其他协程可能已治愈）；
    3. ⭐ 读回恢复（NEWAPI_ADMIN_PAT_READBACK=1，默认开，跨服务器部署的关键）：
       login 拿会话后 **GET /api/user/self 把账号当前 access_token 原样读回来**——
       读操作不轮换 token，另一台服务器上共用该账号的 BFF 的 PAT 依然有效，
       从根上消灭「轮换乒乓」（乒乓只发生在同账号跨机场景，共享文件帮不上忙）；
    4. 读回失败（账号压根没设过 access_token）才最后兜底轮换
      （NEWAPI_ADMIN_LOGIN_FALLBACK=0 可彻底禁用 3/4 步转人工）。
    3.5. 轮换冷静期（NEWAPI_ADMIN_ROTATE_COOLDOWN，默认 900s，2026-09-22 三应用
      共用 uid=1 血案）：读回失效的上游（/api/user/self 不回 access_token）+ 跨机
      无共享文件时，A/B/C 谁兜底轮换谁踢别人 → 无限乒乓。冷静期内已轮换过仍 401
      → 拒绝再轮换、503 转人工，把乒乓压成「每实例每窗口至多一次」。
    """
    # 第 0 步：账号状态类 401 —— 轮换救不了，绝不轮换（否则白踢共用账号的其他应用）
    if reject_code in NO_ROTATE_AUTH_CODES:
        logger.error(
            "admin 401 code=%s 指向账号状态问题而非 PAT 失效，拒绝自愈轮换"
            "（轮换不会修复且会作废共用该账号的其他应用 PAT）——"
            "请到 new-api 后台核对该管理员账号状态/会话", reject_code)
        raise NewApiError("管理员账号状态异常（非凭证失效），请稍后重试或联系管理员", 503)
    pat_before = _admin_cache["pat"]
    if _reload_admin_cred():
        return
    if not config.NEWAPI_ADMIN_LOGIN_FALLBACK:
        logger.error(
            "admin PAT 401 且凭据文件无新值，兜底登录已禁用"
            "(NEWAPI_ADMIN_LOGIN_FALLBACK=0) —— 需人工更新 PAT 或凭据文件。")
        raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503)
    async with _login_lock():
        # 双检①：等锁期间同进程其他协程可能已完成治愈（缓存 PAT 已变）
        if _admin_cache["pat"] != pat_before:
            return
        # 双检②：同机另一进程可能刚轮换并落盘
        if _reload_admin_cred():
            return
        # 第 3 步：读回恢复（不轮换，跨机安全）
        if config.NEWAPI_ADMIN_PAT_READBACK:
            try:
                if await _recover_admin_pat_by_readback():
                    return
            except NewApiError as e:
                logger.warning("admin PAT readback failed: %s", e.message)
        # 第 3.5 步：轮换冷静期熔断（2026-09-22 flovart/hewapi/明判共用 uid=1 互踢血案）。
        #   读回已失败 + 冷静期内本进程轮换过 → 此刻再轮换几乎必然踢掉共用同
        #   一账号的对端，触发乒乓。宁可 503 转人工也不烧互踢循环。
        if config.NEWAPI_ADMIN_ROTATE_COOLDOWN > 0:
            since = time.monotonic() - _LAST_ADMIN_ROTATE[0]
            if since < config.NEWAPI_ADMIN_ROTATE_COOLDOWN:
                logger.error(
                    "admin PAT 401 且读回/凭据文件均无法自愈，但 %.0fs 前刚兜底轮换过"
                    "（冷静期 %ds 内拒绝再轮换）。大概率是多应用共用管理员账号互踢："
                    "继续轮换只会作废对端 PAT 形成乒乓。请人工处理——① 给每个业务"
                    "配独立 new-api 管理员账号（根治）；或 ② 在 new-api 后台取当前"
                    "有效 access_token 更新各实例 NEWAPI_ADMIN_PAT 后重启。",
                    since, config.NEWAPI_ADMIN_ROTATE_COOLDOWN)
                raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503)
        # 第 4 步：最后兜底——真轮换（会作废其他机器/对端的 PAT）
        await _admin_login()


async def _admin_session_login() -> tuple[int, str, dict]:
    """账密登录 new-api，返回 (uid, session_access_token, session_info)。

    只创建会话、**不轮换 access_token**。调用方用完必须 _release_session 归还。
    登录失败一次即抛（不做任何重试——会话签发额度按账号计，绝不自动重试）。
    """
    if not (config.NEWAPI_ADMIN_USERNAME and config.NEWAPI_ADMIN_PASSWORD):
        # 源码里不再留默认账密（会随公开仓库和镜像分发）。没配就明确报配置缺失，
        # 而不是拿空串去撞上游换回一句含糊的「用户名或密码错误」。
        logger.error(
            "管理员凭证未配置，BFF 的建号/加额度/兑换码不可用。"
            "请设置 NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID（推荐，不消耗会话配额），"
            "或 NEWAPI_ADMIN_USERNAME + NEWAPI_ADMIN_PASSWORD。"
        )
        raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503)
    try:
        body = await request("POST", "/api/user/login", headers={},
                             json={"username": config.NEWAPI_ADMIN_USERNAME,
                                   "password": config.NEWAPI_ADMIN_PASSWORD})
    except NewApiError as e:
        if e.status_code == 409:
            # 会话已打满且手上没有可用 PAT —— 这是运维事故而非用户错误。
            # 详细修复路径只写日志，不回传给终端用户（暴露内部部署细节没有意义，
            # 用户也无从下手）。运维排查见 scripts/fix_session_limit.py。
            logger.error(
                "管理员会话数已达 new-api 上限（50 个，硬拒绝不淘汰，TTL 30 天），"
                "login 返回 409 AUTH_SESSION_LIMIT，BFF 的建号/加额度/兑换码已不可用。"
                "恢复：运行 scripts/fix_session_limit.py 查看方案；"
                "根治：配置 NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID 环境变量，"
                "PAT 不经过会话系统，可彻底摆脱对 login 的依赖。"
            )
            raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503) from e
        raise
    data = body["data"]
    return int(data["user"]["id"]), data["access_token"], data.get("session") or {}


async def _recover_admin_pat_by_readback() -> bool:
    """读回恢复：登录后用 GET /api/user/self **读取**账号当前 access_token。

    ⭐ 与 _admin_login 的本质区别：不调 GET /api/user/token（那个动作会轮换并作废
    旧值）。读回来的 token 是账号现行有效值——其他机器/实例上用同一账号的 BFF
    不受任何影响。这是跨服务器部署（无共享卷可用）下避免互踢的核心手段。
    返回 True 表示已采纳新凭据；False 表示账号未设置过 access_token（需走轮换）。
    """
    uid, access_token, session = await _admin_session_login()
    try:
        body = await request("GET", "/api/user/self",
                             headers=user_headers(access_token, uid))
    finally:
        # 无论读回成败都立刻归还登录会话（会话是稀缺资源）
        try:
            await _release_session(access_token, uid, session)
        except Exception as e:  # noqa: BLE001 归还失败不影响主流程
            logger.debug("release session after readback failed: %s", e)
    pat = (body.get("data") or {}).get("access_token") or ""
    if not pat:
        logger.warning("admin account has no access_token set; readback empty")
        return False
    _admin_cache["pat"] = pat
    _admin_cache["uid"] = uid
    _save_admin_cred(force=True)
    logger.warning("admin PAT recovered by readback (no rotation) uid=%s", uid)
    return True


async def _admin_login() -> None:
    """管理员登录轮换 PAT。换完立刻归还会话 —— 否则会话累积到 50 就永久锁死。

    注意：只作最后兜底：GET /api/user/token 会**轮换并作废旧 access_token**，
    会让其他共用该账号的 BFF/实例立刻 401。401 自愈序列见 _self_heal_admin_cred
    （先读回、后轮换）。
    """
    uid, access_token, session = await _admin_session_login()
    try:
        pat = await _mint_access_token(access_token, uid, config.NEWAPI_ADMIN_PASSWORD)
    finally:
        try:
            await _release_session(access_token, uid, session)
        except Exception as e:  # noqa: BLE001 归还失败不影响主流程
            logger.debug("release session after rotate failed: %s", e)
    _admin_cache["pat"] = pat
    _admin_cache["uid"] = uid
    # ⭐ 强制落盘：新 PAT 是凭据文件的最新事实，env 直供模式也必须写（同机其他
    #   实例 401 时重读文件即可自愈）。跨机同步靠读回恢复（见 _self_heal_admin_cred）。
    _save_admin_cred(force=True)
    # 盖轮换时间戳（冷静期熔断用）：冷启无凭证的首次轮换同样会踢对端，一并计入。
    _LAST_ADMIN_ROTATE[0] = time.monotonic()


async def admin_request(method: str, path: str, *, json: Any = None,
                        params: dict | None = None) -> Any:
    """管理员请求：PAT 失效时自动重新登录重试一次。"""
    if _admin_cache["pat"] is None:
        _load_admin_cred()
    if _admin_cache["pat"] is None:
        await _admin_login()
    try:
        return await request(method, path, json=json, params=params,
                             headers=user_headers(_admin_cache["pat"], _admin_cache["uid"]))
    except NewApiError as e:
        if e.status_code != 401:
            raise
        # PAT 被外部轮换掉了（官方前端点一次「系统访问令牌」就会作废旧值）。
        # ⭐ 2026-09-20 根治互踢：先重读凭据文件/读回自愈，实在不行才锁内兜底轮换 ——
        #   绝不盲目 login（盲目轮换会作废其他机器上同账号 BFF 的 PAT，乒乓到双限打爆）。
        # ⭐ 2026-09-22 语义分流：上游 401 code 指向账号状态问题（非令牌值错）时
        #   直接拒绝轮换转人工 —— 这是「PAT 明明好好的却被当作旧了」的点火源之一。
        auth_code = _auth_code_from_error(e)
        logger.warning("admin PAT rejected (401), self-healing cred (auth_code=%s)", auth_code)
        await _self_heal_admin_cred(auth_code)
        return await request(method, path, json=json, params=params,
                             headers=user_headers(_admin_cache["pat"], _admin_cache["uid"]))


async def request(method: str, path: str, *, headers: dict, json: Any = None,
                  params: dict | None = None, client_ip: str | None = None) -> Any:
    """统一请求：网络错误与业务失败都抛 NewApiError，data 原样返回。

    client_ip：转发真实客户端 IP。**这不是可选优化** —— new-api 的 CriticalRateLimit
    以 mark+ClientIP() 为 key 计数（middleware/rate-limit.go），BFF 出口只有一个 IP，
    不转发的话一个用户狂点登录就会把全站登录锁死十几分钟。
    需在 new-api 侧配置信任代理（gin TrustedProxies）方能生效。
    """
    if client_ip:
        headers = {**headers, "X-Forwarded-For": client_ip, "X-Real-IP": client_ip}
    try:
        resp = await get_client().request(method, path, headers=headers, json=json, params=params)
    except httpx.HTTPError:
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502)
    if resp.status_code == 401:
        # 上游 401 带结构化鉴权码（new-api dashboard 链路：AUTH_UNAUTHORIZED/
        # AUTH_TOKEN_EXPIRED/AUTH_USER_DISABLED/AUTH_SESSION_REVOKED/AUTH_USER_INVALID）。
        # 带出去给 admin 401 处理链判断「是不是 PAT 值真的错了」——只有令牌值问题才值得轮换。
        try:
            code = str(resp.json().get("code") or "")
        except ValueError:
            code = ""
        raise NewApiError("凭证已失效，请重新登录", 401, detail=f"upstream_auth_code={code}")
    if resp.status_code == 409:
        # AUTH_SESSION_LIMIT：该账号活跃会话已达 50 且不淘汰旧会话，30 天内无法登录。
        # 正常情况下 login() 会归还会话，走到这里说明历史遗留会话堆积，需人工清理。
        try:
            code = resp.json().get("code", "")
        except ValueError:
            code = ""
        if code == "AUTH_SESSION_LIMIT":
            logger.error("账号会话数已达上限，需在 new-api 前端「登录设备管理」中清理会话")
            raise NewApiError("该账号登录设备数已达上限，请在官方前端退出其他设备后重试", 409)
        raise NewApiError("操作冲突，请稍后重试", 409)
    if resp.status_code == 429:
        # new-api 对登录等敏感接口有 CriticalRateLimit，窗口可能长达十几分钟，
        # Retry-After 单位为秒。必须给用户明确的等待时长，否则会反复重试加剧限流。
        retry = resp.headers.get("retry-after", "")
        wait = ""
        if retry.isdigit():
            secs = int(retry)
            wait = f"，请 {secs // 60 + 1} 分钟后再试" if secs >= 60 else f"，请 {secs} 秒后再试"
        if not wait:
            wait = "，请稍后再试"
        raise NewApiError("操作过于频繁" + wait, 429)
    try:
        body = resp.json()
    except ValueError:
        raise NewApiError("上游返回异常", 502)
    if isinstance(body, dict) and body.get("success") is False:
        raise NewApiError(body.get("message") or "操作失败", 400)
    return body


# ---------- 认证 ----------
async def send_verification(email: str, client_ip: str | None = None) -> None:
    """请求上游给 email 发注册验证码。

    实测契约（v1.0.0-rc.24）：**GET** `/api/verification?email=<addr>&turnstile=`
    —— 不是 POST+JSON，参数走 query；turnstile 站点开关关闭时传空串即可。
    验证码由 new-api 侧生成并存活 10 分钟，BFF 不持有、也无法校验它，
    最终校验必须交给上游 `/api/user/register`（见 main.py 注册流程）。

    失败一律是 HTTP 200 + success:false（request() 已统一转成 NewApiError），
    常见 message：
    - `invalid SMTP account`  → 上游没配 SMTP
    - `550 The recipient may contain a non-existent account...`
                              → SMTP 正常，但收件地址不存在（用户填错邮箱）
    """
    await request("GET", "/api/verification", headers={},
                  params={"email": email, "turnstile": ""},
                  client_ip=client_ip)


async def register_user(username: str, password: str, email: str,
                        verification_code: str,
                        client_ip: str | None = None) -> None:
    """走上游原生注册端建号，由上游校验邮箱验证码并绑定邮箱。

    为什么不用 admin_create_user 影子建号：管理员建号接口不校验验证码、
    也不写 email 字段，等于把邮箱验证整条链路旁路掉 —— 邮箱既没验证也没绑定，
    用户后续无法用邮箱找回密码。站点 email_verification=True 时必须走这里。

    实测 message：
    - `Email verification is enabled, please enter email address and verification code`
    - `Verification code is incorrect or has expired`
    """
    await request("POST", "/api/user/register", headers={},
                  json={"username": username, "password": password,
                        "email": email, "verification_code": verification_code},
                  client_ip=client_ip)


async def _mint_access_token(session_token: str, uid: int, password: str, *,
                             client_ip: str | None = None) -> str:
    """换取账号的系统访问令牌（PAT），兼容新旧网关。

    new-api v1.0.0-rc.37 起引入「安全验证 proof」（middleware/secure_verification.go）：
    GET /api/user/token（scope=access_token.generate）无条件要求 X-Security-Proof 头，
    缺失即 403「需要安全验证」。无 2FA/Passkey 的账号唯一可用方式是 password：
    先 POST /api/verify（密码换一次性 proof），再带 proof 换 PAT。
    proof 与登录会话绑定（SessionID），必须在同一会话内先消费、后归还会话。
    注意：rc.37 起 login 响应里的 access_token 是 15 分钟短效会话 JWT（非持久 PAT），
    只能用作 proof/换 token 的临时凭证，不可当 PAT 存。

    旧版网关没有 /api/verify（未知路由兜底成 SPA HTML，解析后为 502），此时跳过
    proof 直接换——旧网关本来就不拦。除 404/502 外的验证失败（如密码校验不过）
    绝不重试，原样抛给调用方（该端点有失败计数，重试会锁号）。
    """
    headers = user_headers(session_token, uid)
    proof = ""
    try:
        body = await request("POST", "/api/verify", headers=headers,
                             json={"method": "password",
                                   "scope": "access_token.generate",
                                   "password": password},
                             client_ip=client_ip)
        proof = (body.get("data") or {}).get("proof_token") or ""
        if not proof:
            logger.warning("verify returned empty proof_token; minting without proof")
    except NewApiError as e:
        if e.status_code in (404, 502):
            logger.info("gateway has no /api/verify (pre-rc.37), minting without proof")
        else:
            raise
    if proof:
        headers = {**headers, "X-Security-Proof": proof}
    pat_body = await request("GET", "/api/user/token", headers=headers,
                             client_ip=client_ip)
    return pat_body["data"]


async def login(username: str, password: str, client_ip: str | None = None) -> dict:
    """密码登录 → 换 PAT → **立刻归还会话**。返回 {uid, username, pat, user}。

    注意：会话必须归还，这不是优化而是硬性要求：
    new-api 每次 login 都新建一条 UserSession，上限 UserSessionActiveLimit=50，
    判定是 `activeCount >= limit` 直接报错 —— **硬拒绝，不淘汰最旧会话**，
    且 LoginSessionTTL 长达 30 天（service/auth_session.go）。
    BFF 只需要 PAT、根本不用会话，若登录后放着不管，同一账号登满 50 次就会
    收到 409 AUTH_SESSION_LIMIT 且 30 天内无法再登录 —— 管理员账号首当其冲，
    一旦锁死整个 BFF 的建号/加额度能力全废。
    （已实测踩坑：反复登录 chatfire 后全站管理员登录返回 409。）

    归还方式：拿到 PAT 后用 access_token 调 DELETE /api/user/sessions/{sid}。
    实测删除会话后 PAT 依然有效 —— 因为 PAT 走 users.access_token 列，
    不经过会话系统。
    2026-09-20 适配 rc.37 安全验证：换 PAT 前先 POST /api/verify 用密码换
    proof（见 _mint_access_token）。
    """
    body = await request("POST", "/api/user/login", headers={},
                         json={"username": username, "password": password},
                         client_ip=client_ip)
    data = body["data"]
    access_token = data["access_token"]
    user = data["user"]
    uid = user["id"]
    # 用登录态（15min JWT）先换 proof 再换长期 PAT
    pat = await _mint_access_token(access_token, uid, password, client_ip=client_ip)
    await _release_session(access_token, uid, data.get("session") or {})
    return {"uid": uid, "username": user["username"], "pat": pat, "user": user}


async def _release_session(access_token: str, uid: int, session: dict) -> None:
    """归还刚建立的登录会话，避免占满 UserSessionActiveLimit。

    必须用 access_token 而非 PAT：会话管理接口要求真实 dashboard 会话上下文，
    PAT 调用会被拒（403 AUTH_SESSION_REQUIRED）。
    失败只记日志不抛错 —— 会话没还上顶多浪费一个配额，
    不该让用户的登录因此失败。
    """
    sid = session.get("sid")
    if not sid:
        return
    try:
        await request("DELETE", f"/api/user/sessions/{sid}",
                      headers=user_headers(access_token, uid))
    except Exception:
        logger.warning("释放登录会话失败 uid=%s sid=%s（会话配额将被占用）", uid, sid)


async def admin_create_user(username: str, password: str, display_name: str = "") -> int:
    """管理员建号，返回 uid（建号接口不返回 id，需反查）。"""
    await admin_request("POST", "/api/user/",
                        json={"username": username, "password": password,
                              "display_name": display_name or username})
    found = await admin_request("GET", "/api/user/search",
                                params={"keyword": username, "p": 1, "page_size": 10})
    for item in found["data"]["items"]:
        if item["username"] == username:
            return item["id"]
    raise NewApiError("建号成功但未找到用户", 500)


async def admin_delete_user(uid: int) -> None:
    """管理员删除用户（实测 DELETE /api/user/{id}，删完 search 立即查不到）。

    用途：兑换码登录时若「建号成功但核销失败」，必须删号回滚，
    否则无效码会在系统里留下一堆空账号。
    """
    await admin_request("DELETE", f"/api/user/{int(uid)}")


async def admin_update_user(uid: int, username: str, password: str,
                            group: str = "default") -> None:
    """管理员修改用户账密（兑换码账号绑定密码）。

    实测契约（new-api 后台用户编辑抓包）：PUT /api/user/
      {id, username, display_name, password, group}
    - display_name 固定「兑换码用户」；group 默认 default（不传会被清空）
    - 改完旧账密立即失效、新账密可登录，uid 不变（余额/Key/日志全保留）
    - 用户名重复报 "Error 1062 (23000): Duplicate entry 'xxx' for key 'users.username'"
    """
    await admin_request("PUT", "/api/user/",
                        json={"id": int(uid), "username": username,
                              "display_name": "兑换码用户",
                              "password": password, "group": group})


async def admin_add_quota(uid: int, quota: int) -> None:
    """管理员为用户加额度（活动赠送）。

    实测契约：POST /api/user/manage {id, action:"add_quota", mode:"add", value:<quota>}
    mode 与 value 必填，缺失返回 "Invalid parameters"。
    注意：new-api 无幂等键，重复调用会重复加钱 —— 幂等由 promo.py 的状态文件保证。
    """
    await admin_request("POST", "/api/user/manage",
                        json={"id": int(uid), "action": "add_quota",
                              "mode": "add", "value": int(quota)})


# ---------- 用户态 ----------
async def get_self(pat: str, uid: int) -> dict:
    body = await request("GET", "/api/user/self", headers=user_headers(pat, uid))
    return body["data"]


# 注：用户态改密码（PUT /api/user/self）要求 original_password —— 兑换码账号
# 的旧密码是 HMAC 派生值、BFF 无状态不存码，拿不到，故「绑定密码」不走此路，
# 统一由 redeem_code.bind_account 用管理员接口代改（见 main.py bind 说明）。


async def list_tokens(pat: str, uid: int, page: int = 1, size: int = 100) -> dict:
    body = await request("GET", "/api/token/", headers=user_headers(pat, uid),
                         params={"p": page, "size": size})
    return body["data"]


async def create_token(pat: str, uid: int, name: str) -> None:
    await request("POST", "/api/token/", headers=user_headers(pat, uid),
                  json={"name": name, "remain_quota": 0, "expired_time": -1,
                        "unlimited_quota": True, "model_limits_enabled": False,
                        "model_limits": "", "allow_ips": "", "group": ""})


async def get_token_key(pat: str, uid: int, token_id: int) -> str:
    body = await request("POST", f"/api/token/{token_id}/key", headers=user_headers(pat, uid))
    return body["data"]["key"]


async def delete_token(pat: str, uid: int, token_id: int) -> None:
    await request("DELETE", f"/api/token/{token_id}", headers=user_headers(pat, uid))


async def get_logs(pat: str, uid: int, page: int, page_size: int) -> dict:
    body = await request("GET", "/api/log/self", headers=user_headers(pat, uid),
                         params={"p": page, "page_size": page_size, "type": 0})
    return body["data"]


async def get_log_stat(pat: str, uid: int) -> dict:
    body = await request("GET", "/api/log/self/stat", headers=user_headers(pat, uid),
                         params={"type": 0})
    return body["data"]


async def pay_amount(pat: str, uid: int, amount: int, method: str) -> str:
    body = await request("POST", "/api/user/amount", headers=user_headers(pat, uid),
                         json={"amount": amount, "payment_method": method, "top_up_code": ""})
    return str(body["data"])


def _trade_no_from_url(url: str) -> str:
    """从网关跳转地址里抽商户订单号。

    只认 out_trade_no / trade_no 两个键（易支付两种命名都在野生环境出现过），
    抽不到就返回空串 —— 空串会让上层退回余额比对兜底，比编一个订单号安全。
    """
    if not url:
        return ""
    try:
        q = parse_qs(urlparse(url).query)
    except ValueError:
        return ""
    for key in ("out_trade_no", "trade_no"):
        vals = q.get(key) or []
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return ""


async def pay_create(pat: str, uid: int, amount: int, method: str) -> dict:
    """创建支付订单，返回归一化结构 {mode, url, params, order_no}。

    mode 有两种，取决于上游把 data 返回成什么类型：
      - "form"     data 是对象 → 易支付表单字段，前端构表单 POST 跳收银台
      - "redirect" data 是字符串 → 网关直给一个跳转地址，前端直接打开

    **必须同时支持两种**：new-api 的 epay 适配在不同网关/不同版本下返回类型不同，
    实测微信支付（wxpay）走的就是「data 为字符串 URL」这一支。原实现写死按对象
    处理（body["data"].get(...)），微信充值必然抛
    AttributeError: 'str' object has no attribute 'get'，用户点一次充值就 500。

    order_no 在此处解析而不是留给上层：redirect 分支的订单号只能从 URL query 里抽，
    上层不该关心「订单号藏在哪」这种网关细节。抽不到就是空串，上层据此退回
    余额比对兜底（见 main.py 的 _pay_status_by_balance）。
    """
    body = await request("POST", "/api/user/pay", headers=user_headers(pat, uid),
                         json={"amount": amount, "payment_method": method, "top_up_code": ""})
    if not isinstance(body, dict):
        raise NewApiError("上游支付接口返回格式异常", status_code=502)
    data = body.get("data")
    url = str(body.get("url") or "")

    if isinstance(data, dict):
        order_no = str(data.get("out_trade_no") or data.get("trade_no") or "")
        return {"mode": "form", "url": url, "params": data, "order_no": order_no}

    if isinstance(data, str) and data.strip():
        target = data.strip()
        # data 是字符串时它本身就是收银台地址；body.url 此时通常为空，
        # 极少数网关两者都给，以 data 为准（url 可能只是网关根地址）。
        return {"mode": "redirect", "url": target, "params": {},
                "order_no": _trade_no_from_url(target)}

    # data 既不是对象也不是可用字符串：上游大概率报错但 HTTP 200。
    # 明确抛错好过把空表单交给前端 —— 后者表现为「点了充值弹出空白页」，无从排查。
    raise NewApiError(f"上游未返回可用的支付参数（data={type(data).__name__}）",
                      status_code=502)


# 充值订单状态（实测 GET /api/user/topup/self 返回值）
TOPUP_PENDING = "pending"
TOPUP_SUCCESS = "success"

# new-api 对充值记录查询有 30 天硬窗口（model/topup.go topUpQueryWindowSeconds），
# 早于此的订单一律查不到 —— 但支付轮询只关心刚下的单，不受影响。
TOPUP_QUERY_WINDOW_DAYS = 30


async def find_topup_order(pat: str, uid: int, trade_no: str) -> Optional[dict]:
    """按商户订单号查充值订单，返回 {trade_no, status, money, amount, ...}；查不到返回 None。

    这是判定「本单是否支付成功」的**权威依据**，取代原先的「余额变多就算到账」：
    余额差值会被其他来源的到账（兑换码、活动赠送、并发的另一笔充值）污染，
    既可能把别人的钱算成本单成功（错发首充赠送），也可能因容差判定把成功判成失败。

    契约要点（均已实测）：
    - keyword 是**精确全匹配**：传完整 trade_no 返回 1 条，传前缀返回 0 条
      （model/token.go sanitizeLikePattern 不补 %，无 % 时精确匹配）。
      与 /api/redemption/search 那个「keyword 不匹配 key 字段」的坑不同，这里可用。
    - **身份取自 PAT 而非 New-Api-User 头**：实测拿管理员 PAT 配 New-Api-User: 46
      返回的仍是 uid=1 的订单。所以必须传用户自己的 PAT，越权查单在此天然不成立；
      反过来也意味着**不能用管理员 PAT 代查用户订单**。
    """
    body = await request("GET", "/api/user/topup/self", headers=user_headers(pat, uid),
                         params={"p": 1, "page_size": 10, "keyword": trade_no})
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    # 精确匹配理论上只会有 1 条，仍显式比对 trade_no：keyword 语义若在上游版本间
    # 变化（例如某版改成前缀匹配），这里也不会把别的订单误判成本单。
    for it in data.get("items") or []:
        if isinstance(it, dict) and it.get("trade_no") == trade_no:
            return it
    return None


async def topup(pat: str, uid: int, key: str) -> Any:
    body = await request("POST", "/api/user/topup", headers=user_headers(pat, uid),
                         json={"key": key})
    return body.get("data")


# ---------- 兑换码校验 ----------
# 兑换码状态（实测）：1=未使用  3=已使用
REDEMPTION_UNUSED = 1
REDEMPTION_USED = 3

# 分页遍历上限。search 接口不匹配 key 字段（见头部契约 12），只能翻列表逐条比对。
# 100 条/页 × 50 页 = 5000 张码；超出该量级应改为 BFF 侧建索引缓存。
_REDEMPTION_PAGE_SIZE = 100


async def find_redemption(key: str) -> Optional[dict]:
    """按兑换码原文查 new-api 里的兑换码记录（管理员权限）。

    返回 {id, key, status, quota, name, used_user_id, ...}；查不到返回 None。

    为什么要遍历而不用 search：`GET /api/redemption/search?keyword=<完整key>`
    实测 total=0 —— keyword 只匹配 name/id，不匹配 key 字段。

    用途：**建号之前**先确认该码确实由管理员创建且未被使用。
    没有这一步，任意乱码都会先建出账号再删掉，白白污染用户表、
    也让攻击者能用乱码刷建号请求。
    """
    target = (key or "").strip()
    if not target:
        return None
    for page in range(1, config.REDEMPTION_MAX_PAGES + 1):
        body = await admin_request("GET", "/api/redemption/",
                                   params={"p": page, "page_size": _REDEMPTION_PAGE_SIZE})
        # admin_request 返回的是完整响应体 {data, message, success}，分页内容在
        # data 里。曾经漏了这一层解包 —— items 恒为空，于是**任何真实兑换码都被
        # 判为「不存在」**，前置校验从"拦伪造码"退化成"拦所有码"。
        # 兼容两种形状：外层带 data 的取 data，已是内层的直接用。
        outer = body if isinstance(body, dict) else {}
        inner = outer.get("data")
        data = inner if isinstance(inner, dict) else outer
        items = data.get("items") or []
        if not items:
            return None
        for it in items:
            if (it.get("key") or "").strip() == target:
                return it
        if len(items) < _REDEMPTION_PAGE_SIZE:
            return None          # 已是最后一页，确认不存在
    logger.warning("兑换码遍历达页数上限 %d，未找到目标码（可能需要调大 "
                   "BFF_REDEMPTION_MAX_PAGES 或改用索引缓存）", config.REDEMPTION_MAX_PAGES)
    return None
