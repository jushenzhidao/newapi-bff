"""会话 Cookie 加密的回归测试。

核心断言：PAT 绝不以任何可读形式出现在 Cookie 里。

历史背景：早期用 itsdangerous 只做签名，载荷是 base64 明文 JSON，
不需要 SECRET_KEY 就能解出 PAT。这组用例锁住「加密」这一语义，
防止将来有人为了「少一个依赖」把实现退回签名方案。
"""
import base64
import json
import time
from unittest import mock

import pytest

from app import config, security

_PAT = "sk-CANARY_PAT_MUST_NOT_LEAK"
_PAYLOAD = {"uid": 42, "username": "victim", "pat": _PAT}


@pytest.fixture
def fixed_key(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "k" * 64)
    return config.SECRET_KEY


def test_roundtrip_preserves_payload(fixed_key):
    assert security._decrypt(security._encrypt(_PAYLOAD)) == _PAYLOAD


def test_pat_absent_from_cookie_in_any_encoding(fixed_key):
    """逐段 base64 解码都不得还原出 PAT —— 这是本次修复的核心断言。"""
    token = security._encrypt(_PAYLOAD)

    assert _PAT not in token
    assert "CANARY" not in token

    for seg in token.split("."):
        # 用 validate=False 语义的宽松解码：解不出就是二进制垃圾，本身即符合预期，
        # 不吞异常也不跳过，直接断言解码结果里没有 canary。
        raw = base64.urlsafe_b64decode(
            seg.encode() + b"=" * (-len(seg) % 4)
        )
        assert b"CANARY" not in raw
        with pytest.raises((UnicodeDecodeError, json.JSONDecodeError, ValueError)):
            json.loads(raw.decode("utf-8"))


def test_tampering_is_rejected(fixed_key):
    """GCM 认证标签必须拒绝任何改动，替代原签名方案的防篡改能力。"""
    token = security._encrypt(_PAYLOAD)
    scheme, nonce, ct = token.split(".", 2)

    assert security._decrypt(f"{scheme}.{nonce}.{ct[:-4]}AAAA") is None
    assert security._decrypt(f"{scheme}.AAAAAAAAAAAAAAAA.{ct}") is None
    assert security._decrypt(f"v3.{nonce}.{ct}") is None


def test_wrong_key_cannot_decrypt(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "k" * 64)
    token = security._encrypt(_PAYLOAD)
    monkeypatch.setattr(config, "SECRET_KEY", "different" * 8)
    assert security._decrypt(token) is None


def test_nonce_never_reused(fixed_key):
    """GCM 在同密钥下重用 nonce 会直接丧失安全性，必须每次随机。"""
    nonces = {security._encrypt(_PAYLOAD).split(".")[1] for _ in range(300)}
    assert len(nonces) == 300


def test_server_side_expiry_ignores_browser_max_age(fixed_key):
    """过期必须由服务端依据加密的 iat 判定，否则重放 Cookie 值可绕过。"""
    stale = time.time() - config.COOKIE_MAX_AGE - 10
    with mock.patch("time.time", return_value=stale):
        token = security._encrypt(_PAYLOAD)
    assert security._decrypt(token) is None


@pytest.mark.parametrize(
    "bad",
    ["", "x", "v2", "v2.a", "v2.a.b", "....", "v2..", "not.a.token", "v2.$$$.###"],
)
def test_malformed_tokens_return_none(fixed_key, bad):
    """畸形输入一律判定为未登录（401），不能抛异常变成 500。"""
    assert security._decrypt(bad) is None


def test_incomplete_payload_rejected(fixed_key):
    """缺字段的会话在业务层会炸 KeyError（500），此处应提前判为未登录。"""
    assert security._decrypt(security._encrypt({"uid": 1})) is None
    assert security._decrypt(security._encrypt({"uid": 1, "username": "u"})) is None


def test_legacy_signed_cookie_is_rejected(fixed_key):
    """旧的 itsdangerous 明文签名 Cookie 必须失效，不留兼容后门。"""
    legacy_payload = base64.urlsafe_b64encode(json.dumps(_PAYLOAD).encode()).rstrip(b"=")
    legacy = f"{legacy_payload.decode()}.TIMESTAMP.SIGNATURE"
    assert security._decrypt(legacy) is None
