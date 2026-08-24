"""签名 Cookie 会话（无状态 BFF）。

会话载荷：{"uid": int, "username": str, "pat": str}
pat = new-api 的 Personal Access Token（长期有效），
这是修订版方案二的核心：不代持 15 分钟 access_token，改持 PAT。
"""
from typing import Optional

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="bff-session")


def set_session(response: Response, payload: dict) -> None:
    token = _serializer.dumps(payload)
    response.set_cookie(
        key=config.COOKIE_NAME,
        value=token,
        max_age=config.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # 生产环境走 HTTPS 时改为 True
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(config.COOKIE_NAME, path="/")


def read_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(config.COOKIE_NAME)
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=config.COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def require_session(request: Request) -> dict:
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return session
