"""端到端验证：从登录态 cookie 预生成导入链接 → fetch 拿动态 markdown → 端点不依赖 /api/setup/from-pat。

链路：
  1. 直接伪造一个加密的 session cookie（走 security._encrypt 加密 {uid, username, pat}）。
  2. POST /api/setup/import-doc-prep-session → 期望 200 + 返回带 tok 的 link（不含明文 PAT）。
     该端点生成链接前会先用 cookie 里的 PAT 调 /v1/models 探活（Bearer 即可），PAT 已死则直接 401。
  3. GET  /api/setup/import-doc?tok=... → 期望 200 + text/markdown，且 JSON 内嵌 models。
     _build_setup_models 抛 401 时返回清晰的 410 指引，而非透传 new-api 含糊文案。

本测试把「探活(/v1/models)」与「拼装(_build_setup_models)」两个 new-api 调用直接 monkeypatch，
彻底不依赖网络与 mock 路由实现。
"""
import time

from app import config, security
from app.newapi_client import NewApiError

_PAT = "sk-test-pat-for-import-doc-session-endpoint"


def _session_cookie():
    """伪造一个 BFF 加密 cookie（字段顺序严格按 security 内部约定）。"""
    payload = {"uid": 7, "username": "tester", "pat": _PAT, "iat": int(time.time())}
    return security._encrypt(payload)


def _patch_probe_and_build(monkeypatch, models, *, probe_fails=False):
    """统一把「探活 /v1/models」与「拼装 _build_setup_models」两个调用 mock 掉。"""
    from app import main as main_mod
    from app import newapi_client as na_mod

    async def fake_request(method, path, *, headers=None, json=None, params=None, client_ip=None):
        if path == "/v1/models":
            if probe_fails:
                raise NewApiError("凭证已失效，请重新登录", 401)
            return {"data": [{"id": "test-model-a"}]}
        raise AssertionError(f"unexpected request {method} {path}")

    async def fake_build(pat):
        assert pat == _PAT
        return models

    monkeypatch.setattr(na_mod, "request", fake_request)
    monkeypatch.setattr(main_mod, "_build_setup_models", fake_build)


def test_prep_session_requires_login(client):
    """无 cookie 必须 401（require_session 兜底）。"""
    r = client.post("/api/setup/import-doc-prep-session")
    assert r.status_code == 401, r.text


def test_prep_session_round_trip(client, monkeypatch):
    """登录态一键生成 → 返回链接不含明文 PAT → fetch 拿到含 JSON 的 markdown。"""
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
    fake_resp = {
        "models": fake_models,
        "user": {"id": 7, "username": "tester"},
        "vendors": {"test-model-a": "Custom"},
    }
    _patch_probe_and_build(monkeypatch, fake_resp)

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

    tok = link.split("tok=", 1)[1]
    r2 = client.get("/api/setup/import-doc", params={"tok": tok})
    assert r2.status_code == 200, r2.text
    assert "text/markdown" in r2.headers.get("content-type", "")
    md = r2.text
    assert _PAT in md, "文档内必须含 apiKey（WorkBuddy 写 models.json 需 key）"
    assert '"models"' in md
    assert '"id": "test-model-a"' in md
    assert f'"apiKey": "{_PAT}"' in md


def test_prep_session_tok_tampering_rejected(client, monkeypatch):
    """tok 被篡改后必须 404，不能泄露 PAT。"""
    _patch_probe_and_build(
        monkeypatch,
        {"models": [], "user": {"id": 0, "username": ""}, "vendors": {}},
    )

    cookie = _session_cookie()
    r = client.post("/api/setup/import-doc-prep-session", cookies={"bff_session": cookie})
    tok = r.json()["data"]["link"].split("tok=", 1)[1]
    bad = tok[:-2] + "AA"
    r2 = client.get("/api/setup/import-doc", params={"tok": bad})
    assert r2.status_code == 404, r2.text


def test_prep_session_dead_pat_rejected(client, monkeypatch):
    """cookie 里的 PAT 在 new-api 端已死（探活 /v1/models 401）→ 端点直接 401 提示重登，
    不再生成一条注定失败的链接。"""
    _patch_probe_and_build(
        monkeypatch,
        {"models": [], "user": {"id": 0, "username": ""}, "vendors": {}},
        probe_fails=True,
    )

    cookie = _session_cookie()
    r = client.post(
        "/api/setup/import-doc-prep-session",
        cookies={"bff_session": cookie},
    )
    assert r.status_code == 401, r.text
    detail = r.json().get("detail", "")
    assert "失效" in detail
    assert "重新登录" in detail


def test_import_doc_dead_pat_clear_error(client, monkeypatch):
    """探活通过（prep-session 成功），但 import-doc 拼装时 PAT 已死（_build_setup_models 抛 401）→
    返回 410 + 清晰指引，而非透传 new-api 那句含糊的「凭证已失效」。"""
    from app import main as main_mod
    from app import newapi_client as na_mod

    async def fake_request_ok(method, path, *, headers=None, json=None, params=None, client_ip=None):
        if path == "/v1/models":
            return {"data": [{"id": "test-model-a"}]}
        raise AssertionError(f"unexpected request {method} {path}")

    async def fake_build_dead(pat):
        raise NewApiError("凭证已失效，请重新登录", 401)

    monkeypatch.setattr(na_mod, "request", fake_request_ok)
    monkeypatch.setattr(main_mod, "_build_setup_models", fake_build_dead)

    cookie = _session_cookie()
    r = client.post(
        "/api/setup/import-doc-prep-session",
        cookies={"bff_session": cookie},
    )
    assert r.status_code == 200, r.text
    tok = r.json()["data"]["link"].split("tok=", 1)[1]
    r2 = client.get("/api/setup/import-doc", params={"tok": tok})
    assert r2.status_code == 410, r2.text
    detail = r2.json().get("detail", "")
    assert "失效" in detail
    assert "重新登录" in detail
