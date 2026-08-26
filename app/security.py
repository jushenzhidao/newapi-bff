"""加密 Cookie 会话（无状态 BFF）。

会话载荷：{"uid": int, "username": str, "pat": str, "role": int}
pat = new-api 的 Personal Access Token（长期有效），
这是修订版方案二的核心：不代持 15 分钟 access_token，改持 PAT。

role 来自上游 /api/user/login 的 data.user.role（1 普通 / 10 管理员 / 100 root），
仅用于 BFF 自身的管理页鉴权。**它不放大权限**：调用上游依旧只用该用户的 PAT，
role 被伪造也拿不到上游管理员能力。之所以仍要放进加密载荷而非明文，
是避免普通用户改 Cookie 就能打开管理页（哪怕只是读到运营策略）。

## 为什么是加密而不只是签名

早期版本用 itsdangerous 的 URLSafeTimedSerializer，它只做 **签名**，不做
**加密** —— 载荷是 base64 编码的明文 JSON。实测：不需要 SECRET_KEY，
把 Cookie 值按 `.` 切开取第一段做 urlsafe_b64decode，PAT 直接可读。

这意味着任何能读到 Cookie 字符串的路径都等于泄露 PAT，而不只是「中间人抓包」：
浏览器 devtools、崩溃转储、误开 httponly 后的 XSS、把请求头贴进工单或日志、
CDN/WAF 的请求留存、用户自己截图求助。签名只保证「载荷没被篡改」，
完全不保证「载荷不可读」。

现在改为 AES-256-GCM 加密：密文 + 认证标签，兼具机密性与完整性，
GCM 的 tag 校验同时替代了原来签名提供的防篡改能力。

## 密钥派生

复用 config.SECRET_KEY（部署方已被要求用 openssl rand -hex 32 生成），
经 HKDF-SHA256 派生出 32 字节 AES 密钥，info 参数做域分离，避免同一个
SECRET_KEY 在未来别处复用时产生密钥重合。

## 兼容性

不兼容旧的签名 Cookie —— 旧 Cookie 解析失败即返回 None，表现为 401，
前端走既有的重新登录流程。这是刻意的：明文 PAT 已存在泄露风险，
上线时强制刷新一轮会话本身就是正确的安全动作。
"""
import base64
import json
import os
import time
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException, Request, Response

from . import config

# 版本前缀：将来若需换算法/换密钥派生方式，靠它区分格式而不是靠解析失败去猜。
_SCHEME = b"v2"

# GCM 推荐 96-bit nonce。每次加密必须重新随机生成 —— 同密钥下 nonce 重用会
# 直接破坏 GCM 的安全性（可恢复明文异或、可伪造 tag），这是 GCM 最致命的误用。
_NONCE_LEN = 12


def _aes_key() -> bytes:
    """从 SECRET_KEY 派生 AES-256 密钥。

    每次调用都重新派生而不缓存：SECRET_KEY 在测试里会被 monkeypatch 改写，
    缓存会让改写不生效。HKDF 本身开销极小（单次 HMAC），不值得为它引入
    需要手动失效的缓存。
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"bff-session-aead-v2",
    ).derive(config.SECRET_KEY.encode("utf-8"))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def _encrypt(payload: dict) -> str:
    # 签发时间随载荷一起加密，用于服务端判过期。不用 Cookie 的 max_age 代替：
    # max_age 由浏览器执行，攻击者拿到 Cookie 值后可以无限期重放。
    body = dict(payload)
    body["iat"] = int(time.time())
    plaintext = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    nonce = os.urandom(_NONCE_LEN)
    # nonce 作为 AAD 一并认证，防止密文与 nonce 被拆开重组
    ciphertext = AESGCM(_aes_key()).encrypt(nonce, plaintext, _SCHEME)
    return f"{_SCHEME.decode()}.{_b64e(nonce)}.{_b64e(ciphertext)}"


def _decrypt(token: str) -> Optional[dict]:
    try:
        scheme, nonce_b64, ct_b64 = token.split(".", 2)
    except ValueError:
        return None
    if scheme.encode() != _SCHEME:
        return None

    try:
        plaintext = AESGCM(_aes_key()).decrypt(
            _b64d(nonce_b64), _b64d(ct_b64), _SCHEME
        )
        body = json.loads(plaintext)
    except (InvalidTag, ValueError, TypeError, json.JSONDecodeError):
        # InvalidTag 覆盖了篡改、错密钥、错 nonce 三种情况；
        # 其余异常是畸形 base64 / 非 JSON。一律当作无效会话，不区分原因对外报错，
        # 避免把「密钥错」和「被篡改」的差异透露给攻击者。
        return None

    if not isinstance(body, dict):
        return None

    iat = body.pop("iat", None)
    if not isinstance(iat, int) or time.time() - iat > config.COOKIE_MAX_AGE:
        return None

    # 载荷结构校验：缺字段的会话继续往下走会在业务层炸出 KeyError（500），
    # 不如在这里判定为未登录（401），语义也更准确。
    if not all(k in body for k in ("uid", "username", "pat")):
        return None
    return body


def set_session(response: Response, payload: dict) -> None:
    response.set_cookie(
        key=config.COOKIE_NAME,
        value=_encrypt(payload),
        max_age=config.COOKIE_MAX_AGE,
        httponly=True,
        samesite=config.COOKIE_SAMESITE,
        secure=config.COOKIE_SECURE,
        path="/",
    )


def clear_session(response: Response) -> None:
    # 属性需与 set_session 一致：浏览器按 name+path+domain 匹配删除，属性不一致时
    # 部分浏览器会当成另一条 Cookie 写入，导致旧会话残留、登出失效。
    response.delete_cookie(
        config.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite=config.COOKIE_SAMESITE,
        secure=config.COOKIE_SECURE,
    )


def read_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(config.COOKIE_NAME)
    if not token:
        return None
    return _decrypt(token)


def require_session(request: Request) -> dict:
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return session


# new-api 的角色常量（common/constants.go）：普通 1 / 管理员 10 / root 100。
ROLE_ADMIN = 10


def is_admin(session: dict) -> bool:
    """判定会话是否具备管理员身份：上游 role >= 10，或在静态名单内。

    role 缺失按**非管理员**处理：本次改动之前签发的会话载荷里没有 role 字段，
    默认放行会让所有存量会话瞬间获得管理权限，这是不可接受的失效方向。
    这些用户重新登录一次即可拿到带 role 的新会话。

    名单是兜底通道（mock 模式、上游不返回 role 的实例），见 config.ADMIN_USERNAMES。
    """
    role = session.get("role")
    if isinstance(role, int) and not isinstance(role, bool) and role >= ROLE_ADMIN:
        return True
    username = session.get("username")
    return bool(username) and username in config.ADMIN_USERNAMES


def require_admin(request: Request) -> dict:
    """管理员依赖。读写管理接口都必须挂它 —— 配置项含运营策略，不该对普通用户可见。

    未登录返回 401（前端据此跳登录），已登录但非管理员返回 403
    （语义准确：重新登录也没用，不该引导用户去登录页打转）。
    """
    session = require_session(request)
    if not is_admin(session):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return session
