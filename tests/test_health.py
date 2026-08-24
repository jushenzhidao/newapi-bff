"""健康检查与部署契约测试。

这些用例守的是「部署配错但服务照样上线」这类事故，而不是业务逻辑。
"""
import importlib

import pytest


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "mock"
    assert body["uptime_seconds"] >= 0


def test_healthz_does_not_touch_upstream(client, monkeypatch):
    """存活探针必须是纯本地判断。

    一旦探针依赖上游，上游抖动会让编排系统反复重启容器，把一次可恢复的上游
    故障放大成本服务雪崩。这里把 http 客户端换成会爆炸的替身来锁住该行为。
    """
    from app import newapi_client as na

    def boom():
        raise AssertionError("healthz 不应发起任何上游请求")

    monkeypatch.setattr(na, "get_client", boom)
    assert client.get("/healthz").status_code == 200


def test_readyz_ready_when_configured(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert all(body["checks"].values())


def test_readyz_503_when_static_missing(client, monkeypatch):
    """静态资源没进镜像时必须判定不就绪 —— 否则首页会 500。"""
    from pathlib import Path

    from app import main

    monkeypatch.setattr(main, "STATIC_DIR", Path("/nonexistent-static-dir"))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "unready"
    assert "static_assets" in r.json()["failed"]


def test_readyz_503_when_state_dir_unwritable(client, monkeypatch):
    """状态目录不可写会让赠送失去幂等（同一用户可反复领取），必须拦住。"""
    from app import main

    monkeypatch.setattr(main, "_state_dir_writable", lambda: False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "state_dir_writable" in r.json()["failed"]


@pytest.mark.parametrize(
    "secret",
    [
        "dev-only-secret-change-me",   # 仓库里的默认值
        "",                            # BFF_SECRET_KEY= 传空串（实测漏过）
        "   ",                         # 只有空白
        "changeme",                    # 常见弱值
        "short-key-123",               # 长度不足
    ],
)
def test_readyz_rejects_weak_secret_in_real_mode(monkeypatch, secret):
    """真实模式下弱 SECRET_KEY 必须不就绪。

    弱密钥 = 任何人都能伪造会话 Cookie 冒充任意用户，而 Cookie 里带着用户的
    new-api PAT。曾经只判「不等于默认值」，结果 `BFF_SECRET_KEY=` 传空串时
    检查通过 —— 这组用例就是为了钉住该缺口。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BFF_MOCK_MODE", "0")
    monkeypatch.setenv("BFF_SECRET_KEY", secret)

    from app import config, main
    importlib.reload(config)
    importlib.reload(main)
    try:
        with TestClient(main.app) as c:
            r = c.get("/readyz")
        assert r.status_code == 503
        assert "secret_key_configured" in r.json()["failed"]
    finally:
        # 复原模块状态，否则后续用例会拿到 real 模式的 app
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(main)


def test_readyz_accepts_strong_secret_in_real_mode(monkeypatch):
    """正常注入的强密钥（openssl rand -hex 32）必须放行，不能把好配置也拦掉。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BFF_MOCK_MODE", "0")
    monkeypatch.setenv("BFF_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("NEWAPI_ADMIN_PAT", "fake-pat-for-test")
    monkeypatch.setenv("NEWAPI_ADMIN_UID", "1")

    from app import config, main
    importlib.reload(config)
    importlib.reload(main)
    try:
        with TestClient(main.app) as c:
            r = c.get("/readyz")
        assert r.status_code == 200
        assert r.json()["checks"]["secret_key_configured"] is True
        assert r.json()["checks"]["admin_cred_configured"] is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(main)


def test_readyz_requires_admin_cred_in_real_mode(monkeypatch):
    """真实模式下完全没有管理员凭证必须不就绪。

    config.py 移除了默认账密（源码里的默认值会随公开仓库和镜像一起分发，
    等同公开后台管理员密码）。代价是未配置时建号/首充赠送/兑换码登录会全部
    失败 —— 那属于必须在上线前拦住的配置缺失，不能让它静默上线。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BFF_MOCK_MODE", "0")
    monkeypatch.setenv("BFF_SECRET_KEY", "b" * 64)
    monkeypatch.delenv("NEWAPI_ADMIN_PAT", raising=False)
    monkeypatch.delenv("NEWAPI_ADMIN_UID", raising=False)
    monkeypatch.delenv("NEWAPI_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("NEWAPI_ADMIN_PASSWORD", raising=False)

    from app import config, main
    importlib.reload(config)
    importlib.reload(main)
    try:
        with TestClient(main.app) as c:
            r = c.get("/readyz")
        assert r.status_code == 503
        assert "admin_cred_configured" in r.json()["failed"]
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(main)


@pytest.mark.parametrize(
    "env",
    [
        {"NEWAPI_ADMIN_PAT": "pat-value", "NEWAPI_ADMIN_UID": "1"},
        {"NEWAPI_ADMIN_USERNAME": "admin", "NEWAPI_ADMIN_PASSWORD": "pw"},
    ],
    ids=["pat", "userpass"],
)
def test_readyz_accepts_either_admin_cred_form(monkeypatch, env):
    """PAT 直供 或 账密，二者其一即可就绪。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BFF_MOCK_MODE", "0")
    monkeypatch.setenv("BFF_SECRET_KEY", "c" * 64)
    for k in ("NEWAPI_ADMIN_PAT", "NEWAPI_ADMIN_UID",
              "NEWAPI_ADMIN_USERNAME", "NEWAPI_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    from app import config, main
    importlib.reload(config)
    importlib.reload(main)
    try:
        with TestClient(main.app) as c:
            r = c.get("/readyz")
        assert r.status_code == 200
        assert r.json()["checks"]["admin_cred_configured"] is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(main)


def test_config_has_no_hardcoded_admin_cred():
    """config.py 不得再出现明文管理员账密默认值。

    这是回归防线：曾经 `os.getenv("NEWAPI_ADMIN_USERNAME", "chatfire")` 把真实
    凭证写进源码，而本仓库有公开 remote。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "app" / "config.py").read_text("utf-8")
    assert "chatfire" not in src
    assert 'os.getenv("NEWAPI_ADMIN_USERNAME", "")' in src
    assert 'os.getenv("NEWAPI_ADMIN_PASSWORD", "")' in src


def test_index_served_with_cache_busting(client):
    """首页必须不缓存，且静态资源带内容哈希版本号。

    没有版本号时用户发版后仍拿缓存里的旧 app.js，会出现「新后端 + 老前端」
    这种极难复现的组合故障。
    """
    r = client.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")
    assert "/static/app.js?v=" in r.text
    assert "/static/style.css?v=" in r.text


@pytest.mark.parametrize("path", ["/api/user/self", "/api/token", "/api/log/self"])
def test_protected_endpoints_require_session(client, path):
    """未登录访问受保护接口必须 401，不能因为部署改动被意外放开。"""
    assert client.get(path).status_code == 401
