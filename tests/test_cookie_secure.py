"""会话 Cookie 安全属性的回归测试。

背景：Cookie 载荷含用户的 new-api PAT，Secure 属性丢失等于让 PAT 在明文 HTTP
请求里裸奔。此处锁住「默认开启」这一语义，防止后续改动把它悄悄改回 False。

注意 config 是 import 时求值的模块级常量，所以这里用 importlib.reload 配合
monkeypatch.setenv，不能只 setenv。
"""
import importlib

import pytest

from app import config, security


def _reload_config(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BFF_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("BFF_COOKIE_SECURE", value)
    return importlib.reload(config)


def test_cookie_secure_defaults_to_true(monkeypatch):
    """未配置时必须为 True —— 安全开关不能依赖部署方记得打开。"""
    try:
        assert _reload_config(monkeypatch, None).COOKIE_SECURE is True
    finally:
        importlib.reload(config)


def test_blank_value_falls_back_to_secure(monkeypatch):
    """`.env` 里留空行或 compose 展开出空串时，必须回落到 True 而非静默关闭。"""
    try:
        for blank in ("", "   "):
            assert _reload_config(monkeypatch, blank).COOKIE_SECURE is True
    finally:
        importlib.reload(config)


def test_explicit_zero_disables(monkeypatch):
    """本地开发用 http://127.0.0.1 时需要能显式关掉。"""
    try:
        assert _reload_config(monkeypatch, "0").COOKIE_SECURE is False
        assert _reload_config(monkeypatch, "false").COOKIE_SECURE is False
    finally:
        importlib.reload(config)


def test_set_session_honors_config(monkeypatch):
    """set_session 必须读配置，不能是硬编码字面量。"""
    from fastapi import Response

    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    resp = Response()
    security.set_session(resp, {"uid": 1, "username": "u", "pat": "p"})
    header = resp.headers["set-cookie"]
    assert "Secure" in header
    assert "HttpOnly" in header

    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    resp = Response()
    security.set_session(resp, {"uid": 1, "username": "u", "pat": "p"})
    assert "Secure" not in resp.headers["set-cookie"]


def test_clear_session_attributes_match_set(monkeypatch):
    """删除时属性须与写入一致，否则部分浏览器会当成另一条 Cookie，登出失效。"""
    from fastapi import Response

    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    resp = Response()
    security.clear_session(resp)
    header = resp.headers["set-cookie"]
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "Path=/" in header


# ==================== SameSite ====================

def test_samesite_defaults_to_lax(monkeypatch):
    """默认必须是 lax：strict 会让支付回跳后显示未登录，而钱已到账。"""
    try:
        monkeypatch.delenv("BFF_COOKIE_SAMESITE", raising=False)
        assert importlib.reload(config).COOKIE_SAMESITE == "lax"
    finally:
        importlib.reload(config)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("strict", "strict"),
        ("STRICT", "strict"),
        (" none ", "none"),
        ("lax", "lax"),
        ("", "lax"),          # 留空回落
        ("garbage", "lax"),   # 非法值回落，不能让浏览器静默忽略非法属性
    ],
)
def test_samesite_parsing(monkeypatch, raw, expected):
    try:
        monkeypatch.setenv("BFF_COOKIE_SAMESITE", raw)
        assert importlib.reload(config).COOKIE_SAMESITE == expected
    finally:
        importlib.reload(config)


def test_samesite_reaches_cookie_header(monkeypatch):
    """set_session / clear_session 都必须读配置，不能残留硬编码 lax。"""
    from fastapi import Response

    for mode in ("lax", "strict"):
        monkeypatch.setattr(config, "COOKIE_SAMESITE", mode)

        resp = Response()
        security.set_session(resp, {"uid": 1, "username": "u", "pat": "p"})
        assert f"SameSite={mode}" in resp.headers["set-cookie"]

        resp = Response()
        security.clear_session(resp)
        assert f"SameSite={mode}" in resp.headers["set-cookie"]
