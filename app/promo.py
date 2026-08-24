"""运营活动：注册礼包 + 首充赠送。

设计要点：
- BFF 无数据库，用本地 JSON 文件记录「哪些 uid 已领过注册礼 / 已用过首充」，保证幂等。
  文件写入用「临时文件 + 原子 rename」，避免进程被杀时写坏。
- 赠送动作调用 new-api 管理员接口：
    POST /api/user/manage  {id, action:"add_quota", mode:"add", value:<quota>}
  （已实测：mode/value 是必填，缺失会报 Invalid parameters）
- 赠送失败不阻塞主流程，仅记日志，避免注册/支付因活动挂掉。
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any

from . import config
from . import newapi_client as na

logger = logging.getLogger("bff.promo")

_lock = asyncio.Lock()
_state: dict[str, Any] | None = None


def _default_state() -> dict:
    return {"signup": {}, "first_topup": {}}


def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    path = config.PROMO_STATE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("signup", {})
        data.setdefault("first_topup", {})
        _state = data
    except (OSError, ValueError, json.JSONDecodeError):
        _state = _default_state()
    return _state


def _save() -> None:
    path = config.PROMO_STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_load(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        logger.exception("保存活动状态失败")
        if os.path.exists(tmp):
            os.unlink(tmp)


def public_config() -> dict:
    """前端展示用的活动配置（不含任何内部换算细节）。"""
    return {
        "unit": config.POINTS_UNIT_NAME,
        "points_per_cny": config.POINTS_PER_CNY,
        "pay_amounts": [
            {"cny": a, "points": config.cny_to_points(a)} for a in config.PAY_AMOUNTS
        ],
        "signup": {
            "enabled": config.PROMO_SIGNUP_ENABLED and config.PROMO_SIGNUP_POINTS > 0,
            "points": config.PROMO_SIGNUP_POINTS,
        },
        "first_topup": {
            "enabled": config.PROMO_FIRST_TOPUP_ENABLED and config.PROMO_FIRST_TOPUP_RATE > 0,
            "title": config.PROMO_TITLE,
            "rate": config.PROMO_FIRST_TOPUP_RATE,
            "min_cny": config.PROMO_FIRST_TOPUP_MIN_CNY,
            "max_points": config.PROMO_FIRST_TOPUP_MAX_POINTS,
        },
    }


def bonus_points_for(cny: float) -> int:
    """给定充值金额（元），返回首充可赠积分（未判定资格）。"""
    if not (config.PROMO_FIRST_TOPUP_ENABLED and config.PROMO_FIRST_TOPUP_RATE > 0):
        return 0
    if cny < config.PROMO_FIRST_TOPUP_MIN_CNY:
        return 0
    pts = int(config.cny_to_points(cny) * config.PROMO_FIRST_TOPUP_RATE)
    return min(pts, config.PROMO_FIRST_TOPUP_MAX_POINTS)


def first_topup_used(uid: int) -> bool:
    return str(uid) in _load()["first_topup"]


def signup_claimed(uid: int) -> bool:
    return str(uid) in _load()["signup"]


async def _grant(uid: int, points: int) -> bool:
    """调用 new-api 管理员接口加额度。失败返回 False（不抛异常）。"""
    quota = config.points_to_quota(points)
    if quota <= 0:
        return False
    if config.MOCK_MODE:
        from . import store
        store.add_quota(uid, quota, "活动赠送")
        return True
    try:
        await na.admin_add_quota(uid, quota)
        return True
    except Exception:
        logger.exception("赠送积分失败 uid=%s points=%s", uid, points)
        return False


async def grant_signup(uid: int) -> int:
    """注册礼包。返回实际到账积分，0 表示未发放。"""
    if not (config.PROMO_SIGNUP_ENABLED and config.PROMO_SIGNUP_POINTS > 0):
        return 0
    async with _lock:
        st = _load()
        if str(uid) in st["signup"]:
            return 0
        # 先占位再发放，避免并发重复；发放失败则回滚占位
        st["signup"][str(uid)] = {"points": config.PROMO_SIGNUP_POINTS, "at": int(time.time())}
        _save()
    okd = await _grant(uid, config.PROMO_SIGNUP_POINTS)
    if not okd:
        async with _lock:
            _load()["signup"].pop(str(uid), None)
            _save()
        return 0
    return config.PROMO_SIGNUP_POINTS


async def grant_first_topup(uid: int, cny: float) -> int:
    """首充赠送。返回实际到账积分，0 表示无资格或未发放。"""
    points = bonus_points_for(cny)
    if points <= 0:
        return 0
    async with _lock:
        st = _load()
        if str(uid) in st["first_topup"]:
            return 0
        st["first_topup"][str(uid)] = {"points": points, "cny": cny, "at": int(time.time())}
        _save()
    okd = await _grant(uid, points)
    if not okd:
        async with _lock:
            _load()["first_topup"].pop(str(uid), None)
            _save()
        return 0
    return points
