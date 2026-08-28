"""兑换码登录（免注册开箱即用）。

场景：把 new-api 的兑换码当"充值卡"卖 —— 用户拿到码，输码即用，
不用注册、不用邮箱、不用记密码。码本身既是身份凭证也是充值凭证。

════════════ 为什么是"确定性派生"而不是"查表" ════════════
最直觉的做法是建一张「兑换码 → uid」映射表，但实测否掉了：
  GET /api/redemption/search?keyword=<完整兑换码>  →  total=0
  new-api 的 search 只匹配 name / id，**不匹配 key 字段**（实测 2026-08-22）。
核销记录里虽然有 used_user_id，但既然搜不到那条记录，就取不到它。
BFF 自己存映射表又会引入一个必须备份、必须防丢的有状态组件 —— 码一多就是事故源。

所以改用**确定性派生**：账号完全由兑换码算出来，不存任何映射。
    username = rc_<HMAC-SHA256(secret, code)[:16]>
    password = <HMAC-SHA256(secret, code + "|pwd")[:32]>
同一个码，任何时候算出来都是同一个账号；换个码就是另一个账号。
BFF 重装、换机器、删库都不影响 —— 只要 BFF_SECRET_KEY 不变。

注意：BFF_SECRET_KEY 是这套机制的命根子：
   密钥一换，所有兑换码用户都会被派生到全新的空账号，等于集体丢余额。
   生产环境必须固定注入并纳入备份，绝不能用默认值、绝不能随部署重新生成。

════════════ 登录流程 ════════════
                     ┌─ 已存在 → 直接登录（老用户凭码回来）
输入码 → 派生账密 → 查号 ┤
                     └─ 不存在 → ①校验码 → ②建号 → ③核销到账
                                    ↓ 码不存在/已被他人用掉
                                 直接拒绝，**不建号**

注意：①「校验码」这一步不能省：
   兑换码必须是 **new-api 管理员真实创建的**，不能让用户随便编一串就开号。
   校验走 na.find_redemption(code)，翻兑换码列表精确比对 key。
   没有这一步的话，任意乱码都会先建出一个账号、核销失败再删掉 ——
   既污染用户表，也让人能用乱码刷建号请求。

   为什么不能靠"核销失败再回滚"：那是事后补救，副作用已经发生（建号→删号），
   而且 new-api 对「无效码」和「已被使用」返回同一句话，无法给用户准确提示。
   前置校验能明确区分：码不存在 / 码已被使用 / 码可用。

════════════ 已核实的上游契约（2026-08-22 实测）════════════
  POST   /api/user/topup {key}  → 成功 data=<quota>；失败 "Redemption failed..."
                                  （无效码和已用码返回**同一句话**，无法区分）
  GET    /api/redemption/?p=&page_size=
                                → items[{id,key,status,quota,used_user_id}]
                                  status: 1=未使用 3=已使用
  GET    /api/redemption/search → keyword 不匹配 key，别指望用它查码
                                  （所以校验只能翻页比对，见 na.find_redemption）
  DELETE /api/user/{id}         → 管理员删号，用于回滚
  PUT    /api/user/ {id, username, password}
                                → 管理员改账密；重名报 "Duplicate entry ... for key 'users.username'"
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re

from . import config
from . import newapi_client as na
from .newapi_client import NewApiError

logger = logging.getLogger("bff.redeem_code")

# 兑换码字符集：new-api 生成的是 32 位 hex，这里放宽到常见的分隔符写法，
# 允许用户从卡片上带着横杠抄进来。可通过 BFF_REDEEM_CODE_PATTERN 覆盖。
_CODE_RE = re.compile(config.REDEEM_CODE_PATTERN)

USERNAME_PREFIX = "rc_"


def normalize(code: str) -> str:
    """统一兑换码写法：去空白、去分隔符、转小写。

    用户从卡面抄码时常带横杠或空格（4ee5-ee69-0c5b...），
    不归一化的话同一个码会派生出不同账号 —— 这是必须堵死的坑。
    注意：归一化只用于**派生账号**，真正核销时仍用用户原始输入，
    因为上游认的是原始 key。
    """
    return re.sub(r"[\s\-_]", "", code or "").strip().lower()


def is_valid_format(code: str) -> bool:
    return bool(_CODE_RE.match((code or "").strip()))


def _hmac(msg: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


# new-api 对 User.Password 有 max 长度校验，实测 20 位可用、24 位报
# "Field validation for 'Password' failed on the 'max' tag"。取 20 位。
# 20 位 hex = 80 bit 熵，且密码由 SECRET_KEY 派生、从不外传，强度足够。
_PWD_LEN = 20


def derive_account(code: str) -> tuple[str, str]:
    """兑换码 → (username, password)，纯函数、无状态、可重现。

    用 HMAC 而非明文散列，避免拿到用户名就能反推兑换码：
    没有 SECRET_KEY 就算不出 username 和 code 的对应关系。
    """
    norm = normalize(code)
    username = USERNAME_PREFIX + _hmac(norm)[:16]
    # 密码另加盐，确保即使 username 泄露（它会出现在管理后台用户列表里）
    # 也推不出密码。
    password = _hmac(norm + "|pwd")[:_PWD_LEN]
    return username, password


def is_redeem_account(username: str) -> bool:
    """判断是否为兑换码派生的影子账号（前端据此提示"绑定账号"）。"""
    return bool(username) and username.startswith(USERNAME_PREFIX) and len(username) == len(USERNAME_PREFIX) + 16


def mask(code: str) -> str:
    """日志脱敏：兑换码等同于钱，不能明文进日志。"""
    c = (code or "").strip()
    if len(c) <= 8:
        return "****"
    return f"{c[:4]}****{c[-4:]}"


async def login_or_create(code: str, client_ip: str | None = None) -> dict:
    """兑换码登录主流程。

    返回 {uid, username, pat, is_new, redeemed_quota}
      is_new=True 表示本次是首次使用该码（已建号并核销到账）
      is_new=False 表示老用户凭码回来登录（不重复到账）
    """
    raw = (code or "").strip()
    username, password = derive_account(raw)

    if config.MOCK_MODE:
        return _mock_login(raw, username, password)

    # ---- 情况一：该码已用过，派生账号存在，直接登录 ----
    try:
        info = await na.login(username, password, client_ip=client_ip)
        logger.info("兑换码登录（已有账号） code=%s uid=%s", mask(raw), info["uid"])
        return {**info, "is_new": False, "redeemed_quota": 0}
    except NewApiError as e:
        if e.status_code == 429:
            raise  # 限流原样抛出，让上层给出等待时长
        # 账号不存在 → 走首次流程。这里不细分错误类型：
        # new-api 对"用户不存在"和"密码错误"返回同一句话，而密码是我们自己派生的，
        # 不可能错，所以走到这里只可能是账号不存在。
        logger.debug("派生账号不存在，进入首次流程 code=%s", mask(raw))

    # ---- 情况二：首次使用 ----
    # ① 先校验这码是不是管理员真建过的、且没被用掉。
    #    必须在建号之前做，否则乱码会先建出账号再删掉。
    await _assert_redeemable(raw)

    # ② 建号
    try:
        uid = await na.admin_create_user(username, password, "兑换码用户")
    except NewApiError as e:
        # 并发下两个请求同时首登同一个码：一个建号成功，另一个撞重名。
        # 撞到的那个直接改走登录，不算失败。
        if _is_duplicate(e.message):
            logger.info("并发首登撞重名，改走登录 code=%s", mask(raw))
            info = await na.login(username, password, client_ip=client_ip)
            return {**info, "is_new": False, "redeemed_quota": 0}
        raise

    # ③ 核销到账。校验通过后核销仍可能失败（并发下被别人抢先核销，
    #    或上游异常），此时必须删号回滚，不留空账号。
    try:
        info = await na.login(username, password, client_ip=client_ip)
        quota = await na.topup(info["pat"], uid, raw)
    except Exception as exc:
        await _rollback(uid, raw, exc)
        raise NewApiError("兑换码核销失败，可能已被使用，请稍后重试", 400)
    # ④ 补默认 Key。上游只有 /api/user/register 会派发初始令牌（GENERATE_DEFAULT_TOKEN，
    #    上游默认关闭、本站已开），管理员建号接口 POST /api/user/ 不建任何令牌。
    #    必须用**用户自己的 PAT**：/api/token/ 的归属取自 PAT，New-Api-User 头不改变它（见 newapi_client.py:521）。
    #    失败不回滚——额度已到账，为一把可手动补建的 Key 撤销充值代价过大。
    for attempt in range(3):
        try:
            await na.create_token(info["pat"], uid, "兑换码默认key")
            break
        except NewApiError as e:
            if e.status_code == 429:
                # /api/token/ 走 GlobalWebRateLimit，窗口按分钟计，短退避重试没有意义
                logger.warning("默认 Key 创建被限流 uid=%s，用户需自行创建", uid)
                break
            if attempt == 2:
                logger.warning("默认 Key 创建失败 uid=%s err=%s，用户需自行创建", uid, e.message)
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception:
            logger.exception("默认 Key 创建异常 uid=%s，用户需自行创建", uid)
            break

    logger.info("兑换码首次登录成功 code=%s uid=%s quota=%s", mask(raw), uid, quota)
    return {**info, "is_new": True, "redeemed_quota": int(quota or 0)}


async def _assert_redeemable(raw: str) -> None:
    """确认兑换码是 new-api 管理员创建的且未被使用，否则抛出可读错误。

    这是「不能随便造码」的把关点。三种结果：
      查不到       → 码不存在（用户输错或伪造）
      status=3     → 码已被使用（这张卡的余额已经进了别人的账号）
      status=1     → 放行

    管理员凭证不可用时（如会话被打满）无法校验 —— 此时**拒绝**而不是放行，
    宁可让用户稍后重试，也不能在校验失效的情况下开闸建号。
    """
    try:
        rec = await na.find_redemption(raw)
    except NewApiError:
        raise
    except Exception:
        logger.exception("兑换码校验异常 code=%s", mask(raw))
        raise NewApiError("兑换码校验失败，请稍后重试", 503)

    if rec is None:
        logger.info("兑换码不存在，拒绝建号 code=%s", mask(raw))
        raise NewApiError("兑换码不存在，请检查后重试", 400)

    status = rec.get("status")
    if status == na.REDEMPTION_USED:
        logger.info("兑换码已被使用 code=%s used_user_id=%s",
                    mask(raw), rec.get("used_user_id"))
        raise NewApiError("该兑换码已被使用", 400)
    if status != na.REDEMPTION_UNUSED:
        # 未来 new-api 可能新增禁用等状态，一律按不可用处理
        logger.warning("兑换码状态异常 code=%s status=%s", mask(raw), status)
        raise NewApiError("该兑换码当前不可用", 400)


def _mock_login(raw: str, username: str, password: str) -> dict:
    """mock 模式：语义与真实环境完全一致 —— **只有预置的演示卡才能登录**。

    早期实现允许任意字符串登录，那是错误的演示：会让人误以为兑换码
    可以随便编。现在同样走「查卡 → 校验状态 → 建号 → 核销」。
    """
    from . import store

    if store.user_exists(username):
        u = store.users[username]
        return {"uid": u["uid"], "username": username, "pat": u["pat"],
                "user": u, "is_new": False, "redeemed_quota": 0}

    # 与真实模式相同：先校验码，再建号
    rec = store.find_redemption(raw)
    if rec is None:
        raise NewApiError("兑换码不存在，请检查后重试", 400)
    if rec["status"] != store.REDEMPTION_UNUSED:
        raise NewApiError("该兑换码已被使用", 400)

    u = store.create_user_exact(username, password)
    try:
        quota = store.use_redemption(raw, u["uid"])
    except ValueError:
        store.delete_user(u["uid"])  # 与真实模式一致的回滚
        raise NewApiError("兑换码核销失败，可能已被使用，请稍后重试", 400)
    return {"uid": u["uid"], "username": username, "pat": u["pat"],
            "user": u, "is_new": True, "redeemed_quota": quota}


def _is_duplicate(msg: str) -> bool:
    m = (msg or "").lower()
    return "duplicate" in m or "已存在" in msg or "exist" in m


async def _rollback(uid: int, code: str, exc: Exception) -> None:
    """核销失败 → 删除刚建的影子账号。

    删号本身失败也不能让主流程挂掉（用户看到的应该是"兑换码无效"，
    而不是"服务器错误"），只记日志等人工清理。
    """
    logger.warning("兑换码核销失败，回滚删号 code=%s uid=%s err=%s", mask(code), uid, exc)
    try:
        await na.admin_delete_user(uid)
    except Exception:
        logger.exception("回滚删号失败，残留空账号 uid=%s，需人工清理", uid)

# ==================== 「已设密码」登记 ====================
# 允许 rc_ 名仅绑密码（不改名）后，两个问题要分开回答：
#   username 前缀 → 「是不是兑换码派生账号」（结构判定，看 is_redeem_account）
#   本文件这份登记 → 「是否已设置过密码、无需再提示绑定」（运营状态）
# 文件丢失的后果仅仅是绑定横幅重新出现、用户再设一次密码 —— 无资损，可接受。

_BOUND_FILE = os.path.join(config.DATA_DIR, "redeem-bound.json")


def password_set(uid: int) -> bool:
    try:
        with open(_BOUND_FILE, "r", encoding="utf-8") as f:
            return str(uid) in json.load(f)
    except (OSError, ValueError):
        return False


def mark_password_set(uid: int) -> None:
    """登记该兑换码账号已设置密码（rc_ 名保持不变的绑定路径）。"""
    data: dict = {}
    try:
        with open(_BOUND_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    import time
    data[str(uid)] = int(time.time())
    d = os.path.dirname(_BOUND_FILE)
    os.makedirs(d, exist_ok=True)
    with open(_BOUND_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True)


async def bind_account(uid: int, new_username: str, new_password: str) -> None:
    """把兑换码影子账号升级为正式账号（改用户名 + 密码）。

    绑定后：
      - 用新账密正常登录
      - 原兑换码**不再能登录**（派生账号名已改，算出来的名字查无此人；
        保持 rc_ 名仅改密时，派生密码被覆盖，同样不再能登录）
    余额、Key、日志全部保留，因为 uid 没变。
    """
    if config.MOCK_MODE:
        from . import store
        try:
            store.rename_user(uid, new_username, new_password)
        except ValueError as e:
            if str(e) == "duplicate":
                raise NewApiError("该用户名已被占用，请换一个", 400)
            raise NewApiError("账号不存在", 404)
        return
    try:
        await na.admin_update_user(uid, new_username, new_password)
    except NewApiError as e:
        if _is_duplicate(e.message):
            raise NewApiError("该用户名已被占用，请换一个", 400)
        raise
    logger.info("兑换码账号已绑定为正式账号 uid=%s username=%s", uid, new_username)
