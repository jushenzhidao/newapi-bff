"""支付回跳落地页 + 按订单号判定到账的契约测试。

守的是两类事故：
1. 支付完成后浏览器落到 404（回跳地址与 BFF 路由不匹配）；
2. 到账判定被其他来源的余额变动污染，导致错发首充赠送或提示用户重复支付。
"""
import pytest

from app import newapi_client as na


# ==================== 回跳落地页 ====================
@pytest.mark.parametrize("path", ["/usage-logs", "/pay/return", "/console/log"])
def test_pay_return_paths_redirect_to_spa(client, path):
    """易支付 return_url 落到的路径必须 302 回 SPA，不能是 404。

    new-api 用 ServerAddress + "/usage-logs" 拼 return_url，BFF 无法在下单时改写；
    而 BFF 是 hash 路由，该真实路径原本不存在 —— 用户支付完就看到 404。
    """
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/#/topup")


def test_pay_return_carries_trade_no(client):
    """回跳需把商户订单号带给前端，前端才能按单查到账。"""
    r = client.get("/usage-logs?out_trade_no=USR46NOqpLhxs1787545523",
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/#/topup?trade_no=USR46NOqpLhxs1787545523"


def test_pay_return_accepts_trade_no_alias(client):
    """部分网关回跳用 trade_no 而非 out_trade_no，两者都要认。"""
    r = client.get("/pay/return?trade_no=USR1NOabc123", follow_redirects=False)
    assert r.headers["location"] == "/#/topup?trade_no=USR1NOabc123"


@pytest.mark.parametrize("evil", [
    "https://evil.com/x",
    "//evil.com",
    "a b",
    '"><script>alert(1)</script>',
    "../../etc/passwd",
])
def test_pay_return_rejects_hostile_trade_no(client, evil):
    """订单号只放行 [A-Za-z0-9_-]，否则会被拼进 Location 造成开放重定向/XSS。"""
    r = client.get("/usage-logs", params={"out_trade_no": evil}, follow_redirects=False)
    assert r.status_code == 302
    # 非法订单号一律丢弃，只跳回充值页本身
    assert r.headers["location"] == "/#/topup"


def test_pay_return_does_not_grant_quota(client, monkeypatch):
    """落地页绝不能据 URL 参数发钱。

    回跳参数完全由用户可控，若这里发钱，手拼一个 trade_status=TRADE_SUCCESS
    就能白拿额度。真正的到账由 new-api 的 notify 端（带签名校验）完成。
    """
    from app import promo

    async def boom(*a, **kw):
        raise AssertionError("回跳落地页不得触发任何赠送/加额度")

    monkeypatch.setattr(promo, "grant_first_topup", boom)
    monkeypatch.setattr(promo, "grant_signup", boom)
    r = client.get("/usage-logs?out_trade_no=USR1NOx&trade_status=TRADE_SUCCESS&money=500",
                   follow_redirects=False)
    assert r.status_code == 302


def test_pay_return_needs_no_login(client):
    """回跳时用户可能在新窗口且无 Cookie，落地页必须不要求登录，否则又是一个死路。"""
    r = client.get("/usage-logs", follow_redirects=False)
    assert r.status_code == 302


# ==================== 按订单号判定到账 ====================
def _login(client):
    r = client.post("/api/user/login", json={"username": "payer", "password": "pw12345678"})
    assert r.status_code == 200


def test_pay_status_pending_order_not_paid(client):
    """订单还是 pending 时不得判定为已支付。"""
    _login(client)
    r = client.post("/api/user/pay", json={"amount": 10, "payment_method": "wxpay"})
    order = r.json()["data"]
    s = client.post("/api/user/pay/status", json={
        "amount": 10, "baseline_points": order["baseline_points"],
        "order_no": order["order_no"],
    })
    assert s.status_code == 200
    body = s.json()["data"]
    assert body["paid"] is False
    assert body["order_status"] == "pending"


def test_pay_status_success_after_confirm(client):
    """订单被置为已支付后，按单查询应判定到账。"""
    _login(client)
    order = client.post("/api/user/pay",
                        json={"amount": 10, "payment_method": "wxpay"}).json()["data"]
    client.post("/api/user/pay/confirm", json={"order_no": order["order_no"]})
    s = client.post("/api/user/pay/status", json={
        "amount": 10, "baseline_points": order["baseline_points"],
        "order_no": order["order_no"],
    })
    body = s.json()["data"]
    assert body["paid"] is True
    assert body["order_status"] == "success"


def test_pay_status_ignores_other_users_order(client):
    """别人的订单号不能用来把自己的单判成已支付（越权 + 资损）。"""
    _login(client)
    order = client.post("/api/user/pay",
                        json={"amount": 10, "payment_method": "wxpay"}).json()["data"]
    client.post("/api/user/pay/confirm", json={"order_no": order["order_no"]})
    # 换一个用户，拿上一个用户已支付的订单号来查
    client.get("/api/user/logout")
    client.post("/api/user/login", json={"username": "other", "password": "pw12345678"})
    s = client.post("/api/user/pay/status", json={
        "amount": 10, "baseline_points": 0, "order_no": order["order_no"],
    })
    assert s.json()["data"]["paid"] is False


def test_pay_status_requires_session(client):
    """查单接口必须要登录，否则可枚举订单号探测他人支付状态。"""
    r = client.post("/api/user/pay/status",
                    json={"amount": 10, "baseline_points": 0, "order_no": "USR1NOx"})
    assert r.status_code == 401


def test_pay_status_order_no_optional(client):
    """order_no 缺失时不能 422，要回落到余额比对。

    发版瞬间用户浏览器里可能还是缓存的旧 app.js，它不会传这个字段。
    """
    _login(client)
    # baseline 取当前真实余额（注册礼包已入账），此时未支付应判未到账
    baseline = client.get("/api/user/self").json()["data"]["points"]
    r = client.post("/api/user/pay/status",
                    json={"amount": 10, "baseline_points": baseline})
    assert r.status_code == 200
    assert r.json()["data"]["paid"] is False


def test_balance_fallback_is_fooled_by_unrelated_topup(client):
    """记录余额比对的固有缺陷，用以说明为何要按订单号判定。

    用户下单 ¥10 一分钱没付，却用兑换码充了值 —— 余额差达标，
    兜底路径就会把本单判成已支付。按订单号判定不受此干扰
    （见 test_pay_status_pending_order_not_paid：同样场景下 pending 仍判未付）。
    """
    _login(client)
    baseline = client.get("/api/user/self").json()["data"]["points"]
    # 发一张 ¥10 的卡并兑换：钱来自兑换码，与任何在线订单无关
    key = client.post("/api/mock/redemption", json={"cny": 10}).json()["data"]["keys"][0]
    client.post("/api/user/topup", json={"key": key})

    # 不传 order_no → 走余额比对 → 被误判为已支付
    fooled = client.post("/api/user/pay/status",
                         json={"amount": 10, "baseline_points": baseline})
    assert fooled.json()["data"]["paid"] is True

    # 传真实 pending 订单号 → 按单查询 → 正确判定未支付
    order = client.post("/api/user/pay",
                        json={"amount": 10, "payment_method": "wxpay"}).json()["data"]
    precise = client.post("/api/user/pay/status", json={
        "amount": 10, "baseline_points": baseline, "order_no": order["order_no"],
    })
    assert precise.json()["data"]["paid"] is False
    assert precise.json()["data"]["order_status"] == "pending"


# ==================== 上游查单契约 ====================
def test_find_topup_order_matches_exact_trade_no(monkeypatch):
    """查单必须显式比对 trade_no，不能盲信上游返回的第一条。

    上游 keyword 目前是精确全匹配，但若某版改成前缀匹配，
    盲取 items[0] 就会把别的订单误判成本单。
    """
    import asyncio

    async def fake_request(method, path, **kw):
        return {"data": {"items": [
            {"trade_no": "USR1NOaaa", "status": "success", "money": 10},
            {"trade_no": "USR1NObbb", "status": "pending", "money": 10},
        ]}}

    monkeypatch.setattr(na, "request", fake_request)
    got = asyncio.run(na.find_topup_order("pat", 1, "USR1NObbb"))
    assert got["trade_no"] == "USR1NObbb"
    assert got["status"] == "pending"

    missing = asyncio.run(na.find_topup_order("pat", 1, "USR1NOzzz"))
    assert missing is None


def test_find_topup_order_handles_empty_payload(monkeypatch):
    """上游返回空/异形结构时应返回 None，而不是抛异常打断轮询。"""
    import asyncio

    async def empty(method, path, **kw):
        return {"data": None}

    monkeypatch.setattr(na, "request", empty)
    assert asyncio.run(na.find_topup_order("pat", 1, "USR1NOx")) is None
