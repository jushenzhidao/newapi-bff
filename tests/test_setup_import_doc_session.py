"""端到端验证：从登录态 cookie 预生成导入链接 → fetch 拿动态 markdown → 端点不依赖 /api/setup/from-pat。

链路：
  1. 直接伪造一个加密的 session cookie（依赖 security.issue_session 等价物即可，
     这里直接 _encrypt 加密 json 数据塞进 cookie）。
  2. POST /api/setup/import-doc-prep-session → 期望 200 + 返回带 tok 的 link（不含明文 PAT）。
  3. GET  /api/setup/import-doc?tok=... → 期望 200 + text/markdown，且 JSON 内嵌 models。

mock new-api（避免打真实）：用 BFF_MOCK_MODE 即可（conftest 已设）。
"""
import time

from app import config, security

_PAT = "sk-test-pat-for-import-doc-session-endpoint"


def _session_cookie():
    """发一发'伪造'/模拟的 cookie：
    直接走 security._encrypt 把 {uid, username, pat} 加密成 BFF cookie 载荷，base64/json 顺序
    严格按 security 内部约定（issue_session 用同样的字段）。"""
    payload = {"uid": 7, "username": "tester", "pat": _PAT, "iat": int(time.time())}
    blob = security._encrypt(payload)
    # security 解析 cookie 用的是 base64-url 安全的紧凑形式，看一眼既有 main 怎么用
    return blob


def test_prep_session_requires_login(client):
    """无 cookie 必须 401（require_session 兜底）。"""
    r = client.post("/api/setup/import-doc-prep-session")
    assert r.status_code == 401, r.text


def test_prep_session_round_trip(client, monkeypatch):
    """登录态一键生成 → 返回链接不含明文 PAT → fetch 拿到含 JSON 的 markdown。

    mock _build_setup_models 内的 new-api 调用，让它返回最小可用的 models 列表。"""
    # mock 拼装函数 —— 真实 new-api 由 BFF_MOCK_MODE=1 在内部兜底（_build_setup_models 不一定走 mock，
    # 这里直接 monkeypatch 它，彻底不依赖网络）。
    from app import main as main_mod

    fake_models = [
        {
            "id": "test-model-a",
            "name": "Test Model A",
            "vendor": "Custom",
            "baseUrl": config.NEWAPI_BASE_URL,
            "apiKey": _PAT,
            "capabilities": ["chat"],
        }
    ]
    fake_resp = {"models": fake_models, "user": {"id": 7, "username": "tester"}, "vendors": {"test-model-a": "Custom"}}

    async def fake_build(pat, uid=None):
        assert pat == _PAT
        return fake_resp

    monkeypatch.setattr(main_mod, "_build_setup_models", fake_build)

    # 1) prep-session 必须看到 cookie 才放行
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
    assert "v2t." in link, "应当由 issue_setup_ticket 签发 v2t. 票"
    assert _PAT not in link, "链接本身绝不能含明文 PAT"

    # 2) 用 tok 再 fetch 文档
    tok = link.split("tok=", 1)[1]
    r2 = client.get("/api/setup/import-doc", params={"tok": tok})
    assert r2.status_code == 200, r2.text
    assert "text/markdown" in r2.headers.get("content-type", "")
    md = r2.text
    assert _PAT in md, "文档内必须含 apiKey（WorkBuddy 写 models.json 需 key）"
    # markdown 中需要含完整 models 数组的多行 JSON
    assert '"models"' in md
    assert '"id": "test-model-a"' in md
    assert f'"apiKey": "{_PAT}"' in md


def test_prep_session_tok_tampering_rejected(client, monkeypatch):
    """tok 被篡改后必须 404，不能泄露 PAT。"""
    from app import main as main_mod

    async def fake_build(pat, uid=None):
        return {"models": [], "user": {"id": 0, "username": ""}, "vendors": {}}

    monkeypatch.setattr(main_mod, "_build_setup_models", fake_build)

    cookie = _session_cookie()
    r = client.post("/api/setup/import-doc-prep-session", cookies={"bff_session": cookie})
    tok = r.json()["data"]["link"].split("tok=", 1)[1]
    bad = tok[:-2] + "AA"
    r2 = client.get("/api/setup/import-doc", params={"tok": bad})
    assert r2.status_code == 404, r2.text
