"""充值金额口径的回归测试：档位金额 vs 上游应付金额。

守的是一个真实事故：上游 new-api 的「充值价格 Price」默认 7.3（美元兑人民币），
而 BFF 把 amount 原样当人民币档位传过去 —— 用户点「¥10 档」扫出 ¥73.00 的二维码，
页面标价与实收差 7.3 倍，且全程不报错。

两道防线：下单前预览（前端如实展示应付金额 / 直接拦下不可用的档位）、
下单时的权威比对（用上游返回的应付金额，不信任何本地推算）。
"""
import asyncio

import pytest

from app import newapi_client as na


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """把动态配置读写挪出真实 data/：否则开关取值取决于开发者本机改过什么。"""
    from app import settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {})


@pytest.fixture
def paid_user(client, monkeypatch):
    """已登录 + 切到「真实上游」分支的 TestClient。

    真实分支会调上游，所以 get_self（取基线余额）必须打桩 —— 否则用例会
    真的发出网请求，测试结论就取决于本机能不能连通上游了。
    """
    from app import main, settings

    client.post("/api/user/login", json={"username": "payer", "password": "pw12345678"})

    async def fake_self(pat, uid):
        return {"quota": 0}

    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(na, "get_self", fake_self)
    # 隔离本机 data/settings.json：否则这个开关的取值取决于开发者本机改过什么。
    monkeypatch.setattr(settings, "_cache", {})
    return client


def _follow_rate(monkeypatch, on: bool):
    """开启/关闭「叠加上游汇率」。

    改 `_DEF_*` 而不是给 config 挂同名属性：动态取值走的正是 `_DEF_*`，
    挂一个真属性反而会把 __getattr__ 挡在外面（保存后不生效，最难查的一类）。
    """
    from app import config

    monkeypatch.setattr(config, "_DEF_TOPUP_FOLLOW_UPSTREAM_RATE", on)


def _login(client):
    r = client.post("/api/user/login", json={"username": "payer", "password": "pw12345678"})
    assert r.status_code == 200


# ==================== 预览：不叠加上游汇率（默认）====================
def test_preview_blocks_tier_when_upstream_price_is_not_one(client, monkeypatch):
    """上游 Price=7.3 时，预览必须给出「不可下单」而不是悄悄显示 ¥10。"""
    from app import main

    _login(client)
    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(na, "get_upstream_topup_price", lambda: _async(7.3))

    d = client.get("/api/user/pay/preview?amount=10").json()["data"]
    assert d["blocked"] is True
    assert "7.3" in d["blocked_reason"]
    assert "73.00" in d["blocked_reason"]
    assert d["upstream_price"] == 7.3
    assert d["follow_upstream_rate"] is False
    assert d["points"] == 100000      # 代码默认 1 元 = 10000 积分


def test_preview_allows_tier_when_upstream_price_is_one(client, monkeypatch):
    """上游 Price=1（档位金额即应付金额）时不应误拦。"""
    from app import main

    _login(client)
    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(na, "get_upstream_topup_price", lambda: _async(1))

    d = client.get("/api/user/pay/preview?amount=10").json()["data"]
    assert d["blocked"] is False
    assert d["payable"] == 10.0


def test_preview_does_not_block_when_price_unknown(client, monkeypatch):
    """读不到上游汇率时不预拦：上游抖一下不该变成「用户充不了值」。

    真正的把关在下单那一刻（用上游本人的应付金额），比这里的估算可靠。
    """
    from app import main

    _login(client)
    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(na, "get_upstream_topup_price", lambda: _async(None))

    d = client.get("/api/user/pay/preview?amount=10").json()["data"]
    assert d["blocked"] is False
    assert d["upstream_price"] is None


def test_preview_follows_upstream_rate_when_enabled(client, monkeypatch):
    """开启叠加汇率后，页面看到的就是真实应付金额，而不是档位金额。"""
    from app import main

    _login(client)
    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(na, "get_upstream_topup_price", lambda: _async(7.3))
    _follow_rate(monkeypatch, True)

    d = client.get("/api/user/pay/preview?amount=10").json()["data"]
    assert d["follow_upstream_rate"] is True
    assert d["blocked"] is False
    assert d["payable"] == 73.0


def test_preview_rejects_unknown_tier(client):
    """档位白名单之外的金额不能进入下单链路。"""
    _login(client)
    assert client.get("/api/user/pay/preview?amount=999").status_code == 400


def test_preview_in_mock_mode_shows_tier_amount(client):
    """mock 模式（本地演示）没有上游汇率这回事，应付金额即档位金额。"""
    _login(client)
    d = client.get("/api/user/pay/preview?amount=30").json()["data"]
    assert d["blocked"] is False
    assert d["payable"] == 30.0
    assert d["upstream_price"] is None


def test_preview_requires_session(client):
    assert client.get("/api/user/pay/preview?amount=10").status_code == 401


# ==================== 下单：权威金额闸门 ====================
def test_create_order_rejects_when_upstream_payable_differs(paid_user, monkeypatch):
    """上游要收 ¥73 而档位是 ¥10 → 拒绝下单，且**不得**在上游建单。

    建单与算金额是两次调用，闸门卡在中间：拒绝时上游不留孤儿订单，
    用户也不会在收银台看到第二个价格。
    """
    called = []

    async def fake_amount(pat, uid, amount, method):
        return "73.00"

    async def fake_create(pat, uid, amount, method):
        called.append(amount)
        return {"mode": "redirect", "url": "https://gw/x", "params": {}, "order_no": "O1"}

    monkeypatch.setattr(na, "pay_amount", fake_amount)
    monkeypatch.setattr(na, "pay_create", fake_create)

    r = paid_user.post("/api/user/pay", json={"amount": 10, "payment_method": "wxpay"})
    assert r.status_code == 409
    assert called == [], "金额不一致时绝不能在上游建单"
    msg = r.json()["message"]
    assert "73.00" in msg and "充值价格" in msg


def test_create_order_passes_when_payable_matches(paid_user, monkeypatch):
    """上游金额与档位一致时正常下单，并把应付金额一并下发。"""
    async def fake_amount(pat, uid, amount, method):
        return "10.00"

    async def fake_create(pat, uid, amount, method):
        return {"mode": "redirect", "url": "https://gw/x", "params": {}, "order_no": "O2"}

    monkeypatch.setattr(na, "pay_amount", fake_amount)
    monkeypatch.setattr(na, "pay_create", fake_create)

    r = paid_user.post("/api/user/pay", json={"amount": 10, "payment_method": "wxpay"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["order_no"] == "O2"
    assert d["payable"] == "10.00"
    assert d["follow_upstream_rate"] is False


def test_create_order_skips_gate_when_following_upstream_rate(paid_user, monkeypatch):
    """开启叠加汇率后闸门放行：此时 ¥73 正是我们认可的应付金额。"""
    async def fake_amount(pat, uid, amount, method):
        return "73.00"

    async def fake_create(pat, uid, amount, method):
        return {"mode": "redirect", "url": "https://gw/x", "params": {}, "order_no": "O3"}

    monkeypatch.setattr(na, "pay_amount", fake_amount)
    monkeypatch.setattr(na, "pay_create", fake_create)
    _follow_rate(monkeypatch, True)

    r = paid_user.post("/api/user/pay", json={"amount": 10, "payment_method": "wxpay"})
    assert r.status_code == 200
    assert r.json()["data"]["follow_upstream_rate"] is True


def test_create_order_rejects_unparsable_payable(paid_user, monkeypatch):
    """上游返回了不能解析的应付金额时宁可报错，也不能默认按档位金额下单。"""
    async def fake_amount(pat, uid, amount, method):
        return ""

    async def fake_create(pat, uid, amount, method):
        raise AssertionError("应付金额未知时不得建单")

    monkeypatch.setattr(na, "pay_amount", fake_amount)
    monkeypatch.setattr(na, "pay_create", fake_create)

    r = paid_user.post("/api/user/pay", json={"amount": 10, "payment_method": "wxpay"})
    assert r.status_code == 502


# ==================== 上游汇率读取 ====================
def test_upstream_price_read_from_status(monkeypatch):
    monkeypatch.setattr(na, "_status_price_cache", {"price": None, "at": 0.0})

    async def fake_request(method, path, **kw):
        return {"data": {"price": 7.3}}

    monkeypatch.setattr(na, "request", fake_request)
    assert asyncio.run(na.get_upstream_topup_price()) == 7.3


def test_upstream_price_failure_is_unknown_not_one(monkeypatch):
    """读不到时必须返回 None。回落成 1.0 等于把「不知道」伪装成「没问题」。"""
    monkeypatch.setattr(na, "_status_price_cache", {"price": None, "at": 0.0})

    async def boom(method, path, **kw):
        raise na.NewApiError("上游服务暂时不可用", 502)

    monkeypatch.setattr(na, "request", boom)
    assert asyncio.run(na.get_upstream_topup_price()) is None


def _async(value):
    async def run():
        return value
    return run()
