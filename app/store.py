"""MVP 内存数据层 —— 语义对齐 new-api，方便后续替换为真实代理。

对齐点：
- 余额用 quota 整数表示，QUOTA_PER_UNIT = 500000 对应 1 元（new-api 默认换算）
- 对外展示单位为「积分」：1 元 = 10000 积分，故 1 积分 = 50 quota
- API Key 形如 sk-xxxx（new-api token key 为 48 位随机串，展示时加 sk- 前缀）
- 日志 type: 1=充值 2=消费（同 new-api LogType）
后续切真实 new-api 时，本模块函数逐一替换为 newapi_client 调用。
"""
import random
import secrets
import string
import time
from typing import Optional

from . import config

# 换算口径统一由 config 提供（1 元 = QUOTA_PER_CNY quota = POINTS_PER_CNY 积分）
QUOTA_PER_UNIT = config.QUOTA_PER_CNY
quota_to_points = config.quota_to_points
points_to_quota = config.points_to_quota

_MODELS = ["gpt-4o", "gpt-4o-mini", "claude-sonnet-4-5", "deepseek-v3", "qwen-max"]

# users[username] = {uid, username, password, email, quota, used_quota, request_count, pat}
users: dict[str, dict] = {}
# tokens[uid] = [ {id, name, key, status, created_time, remain_quota, unlimited_quota, used_quota} ]
tokens: dict[int, list] = {}
# logs[uid] = [ {id, type, model_name, token_name, prompt_tokens, completion_tokens, quota, created_at} ]
logs: dict[int, list] = {}
# orders[order_no] = {uid, amount_cents, method, status, created_at}
orders: dict[str, dict] = {}

_uid_seq = 1000
_token_seq = 1
_log_seq = 1


def _gen_key() -> str:
    alphabet = string.ascii_letters + string.digits
    return "sk-" + "".join(secrets.choice(alphabet) for _ in range(48))


def get_or_create_user(username: str, password: str, email: str = "") -> dict:
    """MVP：任意用户名密码即登录成功（不存在则自动开通，模拟影子建号）。"""
    global _uid_seq
    user = users.get(username)
    if user is None:
        _uid_seq += 1
        user = {
            "uid": _uid_seq,
            "username": username,
            "password": password,
            "email": email or f"{username}@example.com",
            "quota": 0,  # 初始为 0，注册礼包由 promo 模块发放
            "used_quota": 0,
            "request_count": 0,
            "pat": secrets.token_hex(16),  # 模拟 new-api PAT
        }
        users[username] = user
        tokens[user["uid"]] = []
        logs[user["uid"]] = []
        _seed_demo_data(user)
    return user


def get_user_by_uid(uid: int) -> Optional[dict]:
    for u in users.values():
        if u["uid"] == uid:
            return u
    return None


def user_exists(username: str) -> bool:
    return username in users


def create_user_exact(username: str, password: str, email: str = "") -> dict:
    """严格建号：用户名已存在则抛 ValueError，对应 new-api 管理员建号语义。

    与 get_or_create_user 的区别：后者是 MVP 演示用的「任意账密即登录」，
    而兑换码首登流程依赖真实的「已存在就失败」语义来区分新老用户。
    """
    if username in users:
        raise ValueError("duplicate")
    return get_or_create_user(username, password, email)


def rename_user(uid: int, new_username: str, new_password: str) -> None:
    """对应 new-api 管理员 PUT /api/user/ 改账密（uid 不变，余额/Key/日志保留）。"""
    user = get_user_by_uid(uid)
    if user is None:
        raise ValueError("not found")
    if new_username in users and users[new_username]["uid"] != uid:
        raise ValueError("duplicate")
    users.pop(user["username"], None)
    user["username"] = new_username
    user["password"] = new_password
    users[new_username] = user


def delete_user(uid: int) -> None:
    """对应 new-api 管理员 DELETE /api/user/:id（核销失败时回滚用）。"""
    user = get_user_by_uid(uid)
    if user is None:
        return
    users.pop(user["username"], None)
    tokens.pop(uid, None)
    logs.pop(uid, None)


# ==================== 兑换码池（对齐 new-api redemptions 表）====================
# 语义必须和真实环境一致：**只有管理员预先创建的码才有效**。
# 早期 mock 允许任意字符串登录，那是错误的演示 —— 会让人误以为
# 兑换码可以随便编，与真实行为完全不符。
#
# redemptions[key] = {id, key, name, quota, status, used_user_id, created_time}
# status: 1=未使用 3=已使用（同 new-api RedemptionCodeStatus）
REDEMPTION_UNUSED = 1
REDEMPTION_USED = 3

redemptions: dict[str, dict] = {}
_redemption_seq = 0


def create_redemption(quota: int, name: str = "演示卡", key: Optional[str] = None) -> dict:
    """对应管理员 POST /api/redemption/ 发卡。"""
    global _redemption_seq
    _redemption_seq += 1
    k = key or secrets.token_hex(16)
    rec = {
        "id": _redemption_seq, "key": k, "name": name, "quota": int(quota),
        "status": REDEMPTION_UNUSED, "used_user_id": 0,
        "created_time": int(time.time()),
    }
    redemptions[k] = rec
    return rec


def find_redemption(key: str) -> Optional[dict]:
    """按码原文精确查找。对应 na.find_redemption（真实环境需翻页比对）。"""
    return redemptions.get((key or "").strip())


def use_redemption(key: str, uid: int) -> int:
    """核销并到账，返回到账 quota。对应 POST /api/user/topup。

    失败一律抛 ValueError，由调用方转成用户可读提示。
    """
    rec = find_redemption(key)
    if rec is None:
        raise ValueError("not found")
    if rec["status"] != REDEMPTION_UNUSED:
        raise ValueError("used")
    rec["status"] = REDEMPTION_USED
    rec["used_user_id"] = uid
    rec["redeemed_time"] = int(time.time())
    add_quota(uid, rec["quota"], f"兑换码充值（{rec['name']}）")
    return rec["quota"]


def seed_demo_redemptions() -> list[dict]:
    """预置几张固定面额的演示卡，让 mock 模式开箱可体验。

    固定 key 便于文档标注和自动化测试；面额覆盖不同档位。
    真实环境没有这一步 —— 卡必须由管理员在 new-api 后台发。
    """
    if redemptions:
        return list(redemptions.values())
    presets = [
        ("DEMO-CARD-0010-0001", 10, "演示卡 ¥10"),
        ("DEMO-CARD-0050-0002", 50, "演示卡 ¥50"),
        ("DEMO-CARD-0100-0003", 100, "演示卡 ¥100"),
    ]
    out = []
    for key, cny, name in presets:
        rec = create_redemption(config.points_to_quota(config.cny_to_points(cny)),
                                name=name, key=key)
        rec["is_preset"] = True      # 前端只展示预置卡，不暴露测试脚本发的卡
        out.append(rec)
    return out


def preset_redemption_keys() -> list[str]:
    """预置演示卡里仍未使用的码，供登录页提示。

    只取 is_preset 的，避免把 E2E 脚本发的临时卡也显示出来。
    """
    return [r["key"] for r in redemptions.values()
            if r.get("is_preset") and r["status"] == REDEMPTION_UNUSED]


def _seed_demo_data(user: dict) -> None:
    """给新用户造一把默认 Key 和近 7 天调用日志，让页面开箱有数据。"""
    uid = user["uid"]
    create_token(uid, "默认 Key")
    now = int(time.time())
    total_quota = 0
    for _ in range(36):
        pt = random.randint(200, 4000)
        ct = random.randint(50, 2500)
        q = int((pt * 0.4 + ct * 1.6) * random.uniform(8, 20))
        total_quota += q
        add_log(
            uid,
            log_type=2,
            model_name=random.choice(_MODELS),
            token_name="默认 Key",
            prompt_tokens=pt,
            completion_tokens=ct,
            quota=q,
            created_at=now - random.randint(0, 7 * 24 * 3600),
        )
    user["used_quota"] = total_quota
    user["request_count"] = 36


def create_token(uid: int, name: str) -> dict:
    global _token_seq
    _token_seq += 1
    t = {
        "id": _token_seq,
        "name": name,
        "key": _gen_key(),
        "status": 1,
        "created_time": int(time.time()),
        "unlimited_quota": True,
        "remain_quota": 0,
        "used_quota": 0,
    }
    tokens.setdefault(uid, []).insert(0, t)
    return t


def delete_token(uid: int, token_id: int) -> bool:
    lst = tokens.get(uid, [])
    for i, t in enumerate(lst):
        if t["id"] == token_id:
            lst.pop(i)
            return True
    return False


def add_log(uid: int, log_type: int, model_name: str = "", token_name: str = "",
            prompt_tokens: int = 0, completion_tokens: int = 0, quota: int = 0,
            created_at: Optional[int] = None, content: str = "") -> dict:
    global _log_seq
    _log_seq += 1
    item = {
        "id": _log_seq,
        "type": log_type,
        "model_name": model_name,
        "token_name": token_name,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "quota": quota,
        "content": content,
        "created_at": created_at or int(time.time()),
    }
    logs.setdefault(uid, []).insert(0, item)
    return item


def add_quota(uid: int, quota: int, remark: str) -> None:
    """对应 new-api /api/user/manage add_quota 语义。"""
    user = get_user_by_uid(uid)
    if user:
        user["quota"] += quota
        add_log(uid, log_type=1, quota=quota, content=remark)
