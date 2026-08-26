"""注册与邮箱验证码链路的回归测试。

这条链路此前零覆盖，导致 /api/verification 长期是「只校验正则就返回成功」的
占位实现而没人发现 —— 接口提示发送成功，信却从未发出。所以这里的测试重点不是
happy path，而是锁住两个事实：

1. 真实模式下必须真的调用上游（占位实现会让这些用例失败）。
2. 注册必须交由上游校验验证码，BFF 不得旁路（旧实现走管理员影子建号，
   既不校验验证码也不绑定邮箱，等于验证码形同虚设）。

验证码状态只存在于 new-api 侧，BFF 无从校验，因此「正确验证码能注册成功」
无法在离线测试中构造 —— 该场景已通过真实上游手工验证，此处只锁契约与错误映射。
"""
import pytest

from app import main
from app.newapi_client import NewApiError


@pytest.fixture
def real_mode(monkeypatch):
    """关掉 mock，让端点走真实分支，但把出站调用换成替身。

    必须 patch `main.MOCK`：它是 import 期从 config.MOCK_MODE 求值出的模块级常量
    （main.py:91），改 config.MOCK_MODE 对已求值的它无效。
    """
    monkeypatch.setattr(main, "MOCK", False)


def test_verification_calls_upstream_in_real_mode(client, real_mode, monkeypatch):
    """真实模式必须把请求打到上游。占位实现不调上游，会在此失败。"""
    calls = []

    async def fake_send(email, client_ip=None):
        calls.append(email)

    monkeypatch.setattr(main.na, "send_verification", fake_send)

    r = client.post("/api/verification", json={"email": "user@example.com"})

    assert r.status_code == 200
    assert calls == ["user@example.com"], "真实模式下未调用上游发信接口"


def test_verification_rejects_bad_email_without_calling_upstream(
    client, real_mode, monkeypatch
):
    """格式非法应本地拦截。打到上游只会白耗一次限流配额。"""

    async def fail(email, client_ip=None):
        raise AssertionError("格式非法时不应调用上游")

    monkeypatch.setattr(main.na, "send_verification", fail)

    r = client.post("/api/verification", json={"email": "not-an-email"})

    assert r.status_code == 400
    assert "邮箱格式" in r.json()["message"]


@pytest.mark.parametrize(
    "upstream_message, expected_fragment",
    [
        ("invalid SMTP account", "邮件服务"),
        (
            "550 The recipient may contain a non-existent account, "
            "please check the recipient address.",
            "不存在",
        ),
    ],
)
def test_smtp_errors_are_translated(
    client, real_mode, monkeypatch, upstream_message, expected_fragment
):
    """上游英文 SMTP 报错对终端用户没有指导意义，必须翻成可操作中文。"""

    # 400 而非默认 502：上游业务失败（HTTP 200 + success:false）已在
    # newapi_client.py:258 归一成 400，替身必须与真实行为一致。
    async def fake_send(email, client_ip=None):
        raise NewApiError(upstream_message, 400)

    monkeypatch.setattr(main.na, "send_verification", fake_send)

    r = client.post("/api/verification", json={"email": "user@example.com"})

    assert r.status_code == 400
    body = r.json()["message"]
    assert expected_fragment in body
    assert upstream_message not in body, "原始英文报错不应直接抛给用户"


def test_register_delegates_verification_to_upstream(client, real_mode, monkeypatch):
    """注册必须走上游注册端，由它校验验证码并绑定邮箱。

    旧实现走 admin_create_user 影子建号，绕过验证码校验且不写 email。
    若有人改回那条路径，本用例会失败。
    """
    calls = []

    async def fake_register(username, password, email, code, client_ip=None):
        calls.append((username, password, email, code))

    monkeypatch.setattr(main.na, "register_user", fake_register)

    async def unexpected(*args, **kwargs):
        raise AssertionError("注册不应走管理员影子建号，那会旁路验证码校验")

    monkeypatch.setattr(main.na, "admin_create_user", unexpected)

    # 建号成功后端点会用用户密码登录换 PAT，这步同样不能打真实上游
    async def fake_login(username, password, client_ip=None):
        return {"uid": 1, "username": username, "pat": "test-pat", "role": 0}

    monkeypatch.setattr(main.na, "login", fake_login)

    client.post(
        "/api/user/register",
        json={
            "username": "alice",
            "password": "Passw0rd123",
            "email": "alice@example.com",
            "verification_code": "123456",
        },
    )

    assert len(calls) == 1, "未调用上游注册接口"
    assert calls[0][2] == "alice@example.com", "email 未透传，邮箱不会被绑定"
    assert calls[0][3] == "123456", "验证码未透传，上游无法校验"


def test_register_surfaces_expired_code_in_chinese(client, real_mode, monkeypatch):
    """验证码过期是最高频的失败场景，提示必须让用户知道该重新获取。"""

    async def fake_register(username, password, email, code, client_ip=None):
        raise NewApiError("Verification code is incorrect or has expired", 400)

    monkeypatch.setattr(main.na, "register_user", fake_register)

    r = client.post(
        "/api/user/register",
        json={
            "username": "alice",
            "password": "Passw0rd123",
            "email": "alice@example.com",
            "verification_code": "000000",
        },
    )

    assert r.status_code == 400
    assert "重新获取" in r.json()["message"]


def test_register_requires_valid_email(client, real_mode, monkeypatch):
    """邮箱非法时不该走到上游，否则会建出邮箱不可用的账号。"""

    async def fail(*args, **kwargs):
        raise AssertionError("邮箱非法时不应调用上游注册")

    monkeypatch.setattr(main.na, "register_user", fail)

    r = client.post(
        "/api/user/register",
        json={
            "username": "alice",
            "password": "Passw0rd123",
            "email": "bad-email",
            "verification_code": "123456",
        },
    )

    assert r.status_code == 400
