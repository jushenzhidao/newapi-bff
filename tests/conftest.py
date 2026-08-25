"""测试夹具。

关键点：app.config 在 import 时就读环境变量并求值成模块级常量，所以环境变量
必须在 import app.main 之前设好 —— 在测试函数里 monkeypatch.setenv 是没用的。
"""
import os

import pytest

# mock 模式：不连真实 new-api，CI 里没有上游也能跑
os.environ.setdefault("BFF_MOCK_MODE", "1")
os.environ.setdefault("BFF_SECRET_KEY", "test-only-secret-not-for-production")

# TestClient 的 base_url 是明文 http://testserver，Secure Cookie 会被 httpx 丢弃，
# 导致所有依赖登录态的用例拿到 401。生产默认值是 1，这里显式关掉。
os.environ.setdefault("BFF_COOKIE_SECURE", "0")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient。状态文件重定向到 tmp_path，避免测试污染真实 data/。"""
    from fastapi.testclient import TestClient

    from app import config, main, promo

    monkeypatch.setattr(config, "PROMO_STATE_FILE", str(tmp_path / "promo_state.json"))
    monkeypatch.setattr(promo, "_state", None)  # 清掉模块级缓存，防止跨用例串味
    with TestClient(main.app) as c:
        yield c
