"""端到端验证：登录态一键生成导入链接 → fetch 文档 → AI 自取 /v1/models 配置。

链路：
  1. 伪造加密 session cookie（uid/username/pat）。
  2. POST /api/setup/import-doc-prep-session → 用 session 的 PAT 调 /api/token 取「默认 API Key」
     （/api/token 列出、/api/token/{id}/key 取明文），加密进一次性 tok，返回链接。
     注意：进 tok 的是**默认 API Key**（用于 /v1/models），不是 session 的 PAT——这是之前 401 的根因。
  3. GET /api/setup/import-doc?tok=... → 直接渲染 markdown 文档（含 API Key + /v1/models 地址 +
     写 models.json 步骤），**BFF 后端不再代调 new-api**，零 401 风险。

本测试把「取 Key」两个 new-api 调用 monkeypatch 掉；import-doc 完全本地渲染，不触网。
"""
import time

from app import config, security
from app.newapi_client import NewApiError

_PAT = "sk-test-pat-for-import-doc-session-endpoint"
# 故意用无 sk- 前缀的 raw key：模拟 new-api /api/token/{id}/key 直返的明文，
# 验证 BFF 在 import-doc 渲染前会统一补 sk- 前缀（与教程页展示一致）。
_LIVE_KEY = "DB6ROAFB8B3liDVE3HdCesqKBrSv3UEK87tCwoEXICdvy8DM"
_LIVE_KEY_PREFIXED = f"sk-{_LIVE_KEY}"


def _session_cookie():
    payload = {"uid": 7, "username": "tester", "pat": _PAT, "iat": int(time.time())}
    return security._encrypt(payload)


def _patch_token_lookup(monkeypatch, *, key_fails=False):
    """mock 取默认 API Key 的两个 new-api 调用。"""
    from app import newapi_client as na_mod

    async def fake_list(pat, uid, page=1, size=100):
        return {"items": [{"id": 1, "name": "兑换码默认key"}]}

    async def fake_key(pat, uid, token_id):
        if key_fails:
            raise NewApiError("凭证已失效，请重新登录", 401)
        return _LIVE_KEY

    monkeypatch.setattr(na_mod, "list_tokens", fake_list)
    monkeypatch.setattr(na_mod, "get_token_key", fake_key)


def test_prep_session_requires_login(client):
    r = client.post("/api/setup/import-doc-prep-session")
    assert r.status_code == 401, r.text


def test_prep_session_round_trip(client, monkeypatch):
    """登录态一键生成 → 链接不含明文 Key → fetch 文档含 Key + /v1/models + 写文件步骤。"""
    _patch_token_lookup(monkeypatch)

    cookie = _session_cookie()
    r = client.post(
        "/api/setup/import-doc-prep-session",
        cookies={"bff_session": cookie},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success") is True, body
    link = body["data"]["link"]
    assert link.startswith("http")
    assert "/api/setup/import-doc?tok=" in link
    assert "v2t." in link, "应由 issue_setup_ticket 签发 v2t. 票"
    # 链接本身绝不能含明文 PAT 或明文 Key（均加密在 tok 内）
    assert _PAT not in link
    assert _LIVE_KEY not in link
    assert _LIVE_KEY_PREFIXED not in link

    tok = link.split("tok=", 1)[1]
    r2 = client.get("/api/setup/import-doc", params={"tok": tok})
    assert r2.status_code == 200, r2.text
    assert "text/markdown" in r2.headers.get("content-type", "")
    md = r2.text
    # 文档必须把活 Key（带 sk- 前缀）和 /v1/models 地址给到 AI
    assert _LIVE_KEY_PREFIXED in md, "文档内必须含带 sk- 前缀的 API Key"
    assert _LIVE_KEY not in md.replace(_LIVE_KEY_PREFIXED, ""), \
        "文档里 raw key（无 sk-）不应裸存在，必须统一前缀"
    assert "/models" in md, "文档应引导 AI 调 /v1/models 拿模型"
    assert "models.json" in md, "文档应引导 AI 写本机 models.json"
    # 文档里的模型基地址应与 WorkBuddy 实际配置一致
    assert config.API_BASE_URL.rstrip("/") in md
    # vendor 映射必须来自站点配置接口 /api/config，与 windows 脚本 lookup 逻辑一致
    assert "/api/config" in md, "文档应引导 AI 调 /api/config 拿 model_vendors"
    assert "model_vendors" in md, "文档应明确说明 vendor 取 model_vendors[id] || 'Custom'"
    # 单模型格式严格按飞哥示例：不要 maxInputTokens / maxOutputTokens 等多余字段
    assert "maxInputTokens" not in md
    assert "maxOutputTokens" not in md
    # 文件格式识别步骤必须写出（兼容顶层数组 / {models:[]} / 自定义对象三种形态）
    assert "顶层数组" in md, "文档应列出「顶层数组」形态（飞哥本机用的就是这种）"
    assert "{models:[]}" in md or '"models":[]' in md, "文档应列出「对象里含 models」形态"


def test_prep_session_tok_tampering_rejected(client, monkeypatch):
    _patch_token_lookup(monkeypatch)
    cookie = _session_cookie()
    r = client.post("/api/setup/import-doc-prep-session", cookies={"bff_session": cookie})
    tok = r.json()["data"]["link"].split("tok=", 1)[1]
    bad = tok[:-2] + "AA"
    r2 = client.get("/api/setup/import-doc", params={"tok": bad})
    assert r2.status_code == 404, r2.text


def test_prep_session_dead_key_rejected(client, monkeypatch):
    """取默认 Key 时 new-api 返回 401（PAT 失效）→ 端点直接 401 提示重登。"""
    _patch_token_lookup(monkeypatch, key_fails=True)
    cookie = _session_cookie()
    r = client.post(
        "/api/setup/import-doc-prep-session",
        cookies={"bff_session": cookie},
    )
    assert r.status_code == 401, r.text
    detail = r.json().get("detail", "")
    assert "重新登录" in detail


def test_key_already_prefixed_not_double_prefixed(client, monkeypatch):
    """new-api 已返回带 sk- 的 key → BFF 不能再补一遍前缀，避免 sk-sk- 双前缀。"""
    from app import newapi_client as na_mod

    async def fake_list(pat, uid, page=1, size=100):
        return {"items": [{"id": 1, "name": "兑换码默认key"}]}

    async def fake_key(pat, uid, token_id):
        return "sk-already-has-prefix-xyz"

    monkeypatch.setattr(na_mod, "list_tokens", fake_list)
    monkeypatch.setattr(na_mod, "get_token_key", fake_key)

    cookie = _session_cookie()
    r = client.post(
        "/api/setup/import-doc-prep-session",
        cookies={"bff_session": cookie},
    )
    tok = r.json()["data"]["link"].split("tok=", 1)[1]
    r2 = client.get("/api/setup/import-doc", params={"tok": tok})
    md = r2.text
    assert "sk-already-has-prefix-xyz" in md
    assert "sk-sk-already-has-prefix-xyz" not in md, "不应重复加 sk- 前缀"
