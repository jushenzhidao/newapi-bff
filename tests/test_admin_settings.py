"""管理员动态配置的回归测试。

覆盖三件事：鉴权边界（未登录/普通用户/管理员）、校验拒绝非法值与未登记键、
以及覆盖值能立刻影响公开配置且可回滚。

夹具里必须把 settings.SETTINGS_FILE 指到 tmp_path 并清掉 _cache，
否则用例会写进真实 data/settings.json，并且互相串味。
"""
import pytest

from app import config, settings


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """管理员登录态的 TestClient。

    mock 模式下上游无真实 role，管理权限走 BFF_ADMIN_USERNAMES 静态名单，
    所以这里直接改 config.ADMIN_USERNAMES 而不是伪造 role。
    """
    from fastapi.testclient import TestClient

    from app import main, promo

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(config, "PROMO_STATE_FILE", str(tmp_path / "promo_state.json"))
    monkeypatch.setattr(promo, "_state", None)
    monkeypatch.setattr(config, "ADMIN_USERNAMES", frozenset({"boss"}))
    with TestClient(main.app) as c:
        c.post("/api/user/login", json={"username": "boss", "password": "pw"})
        yield c


@pytest.fixture
def user_client(tmp_path, monkeypatch):
    """普通用户登录态。名单里没有 alice，所以拿不到管理权限。"""
    from fastapi.testclient import TestClient

    from app import main, promo

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(config, "PROMO_STATE_FILE", str(tmp_path / "promo_state.json"))
    monkeypatch.setattr(promo, "_state", None)
    monkeypatch.setattr(config, "ADMIN_USERNAMES", frozenset({"boss"}))
    with TestClient(main.app) as c:
        c.post("/api/user/login", json={"username": "alice", "password": "pw"})
        yield c


def test_anonymous_gets_401(client):
    """未登录是 401 而非 403：前端据此跳登录页。"""
    assert client.get("/api/admin/settings").status_code == 401
    assert client.put("/api/admin/settings", json={"values": {}}).status_code == 401


def test_normal_user_gets_403(user_client):
    """已登录但非管理员是 403：重新登录也没用，不该引导去登录页打转。

    读接口也必须拦住 —— 配置项含运营策略（首充倍率等），不该对普通用户可见。
    """
    assert user_client.get("/api/admin/settings").status_code == 403
    assert user_client.put(
        "/api/admin/settings", json={"values": {"BRAND_NAME": "hack"}}
    ).status_code == 403
    assert user_client.post(
        "/api/admin/settings/reset", json={"keys": ["BRAND_NAME"]}
    ).status_code == 403


def test_self_exposes_is_admin_flag(admin_client, user_client):
    """前端靠这个字段决定是否显示管理入口（真正鉴权在服务端）。"""
    assert admin_client.get("/api/user/self").json()["data"]["is_admin"] is True
    assert user_client.get("/api/user/self").json()["data"]["is_admin"] is False


def test_admin_can_list_settings(admin_client):
    body = admin_client.get("/api/admin/settings").json()["data"]
    assert body["groups"] and body["items"]
    first = body["items"][0]
    for field in ("key", "group", "label", "type", "value", "default", "overridden"):
        assert field in first


def test_secrets_are_never_exposed(admin_client):
    """凭证和会话/上游参数不进 SPECS：改了会踢掉所有在线会话或打瘫服务。

    这是安全边界，不是取舍 —— 它们只能走环境变量注入。
    """
    keys = {i["key"] for i in admin_client.get("/api/admin/settings").json()["data"]["items"]}
    forbidden = ("SECRET", "PAT", "PASSWORD", "NEWAPI", "QUOTA", "COOKIE")
    assert not [k for k in keys if any(f in k for f in forbidden)]


def test_update_takes_effect_without_restart(admin_client, client):
    """核心诉求：改完立刻生效，不需要重启进程。"""
    assert admin_client.put(
        "/api/admin/settings", json={"values": {"BRAND_NAME": "新站点"}}
    ).status_code == 200
    assert client.get("/api/config").json()["data"]["brand"]["name"] == "新站点"


def test_reset_restores_default(admin_client, client):
    default = client.get("/api/config").json()["data"]["brand"]["name"]
    admin_client.put("/api/admin/settings", json={"values": {"BRAND_NAME": "临时"}})
    assert admin_client.post(
        "/api/admin/settings/reset", json={"keys": ["BRAND_NAME"]}
    ).status_code == 200
    assert client.get("/api/config").json()["data"]["brand"]["name"] == default


@pytest.mark.parametrize("patch", [
    {"POINTS_PER_CNY": -1},          # 负数会让全站金额算错
    {"POINTS_PER_CNY": "abc"},       # 类型不对
    {"PAY_AMOUNTS": []},             # 空档位会让充值页没有可选项
    {"PROMO_FIRST_TOPUP_RATE": 999}, # 超出上限，等于白送
    {"BFF_SECRET_KEY": "x"},         # 未登记的键，必须拒绝而不是静默写入
])
def test_invalid_values_rejected(admin_client, patch):
    assert admin_client.put("/api/admin/settings", json={"values": patch}).status_code == 400


def test_rejected_patch_leaves_no_partial_write(admin_client, client):
    """一个键非法就整批拒绝，不允许写一半 —— 否则配置会处于中间态。"""
    before = client.get("/api/config").json()["data"]["brand"]["name"]
    r = admin_client.put(
        "/api/admin/settings",
        json={"values": {"BRAND_NAME": "应当不生效", "POINTS_PER_CNY": -1}},
    )
    assert r.status_code == 400
    assert client.get("/api/config").json()["data"]["brand"]["name"] == before


# ==================== 配置变更与文档档案的联动 ====================
# 档案文案里的 {{brand}} 等变量在**加载时**插值并缓存。保存配置后若不失效
# 该缓存，接口会提示「立即生效」而文档页仍是旧文案 —— 提示成功却没有变化，
# 属于最难排查的一类故障。以下三条锁住这个联动。

def test_unit_rename_reflows_into_doc_body(admin_client):
    """改积分单位名后，正文里的 {{points_unit}} 必须立刻跟着变。

    这里刻意不用 {{brand}} 断言：points.yml 的标题按注释所述有意不拼品牌名，
    避免「Workbuddy积分 积分」这类重复。{{points_unit}} 才是正文里的真实插值点。
    """
    from app import docs_catalog

    docs_catalog.invalidate()
    assert admin_client.put(
        "/api/admin/settings", json={"values": {"POINTS_UNIT_NAME": "灵石"}}
    ).status_code == 200

    data = admin_client.get("/api/docs/points").json()["data"]
    assert "灵石" in str(data["sections"])
    assert "灵石" in data["title"]


def test_doc_products_whitelist_takes_effect_at_once(admin_client):
    """白名单收窄后索引页应立即只剩指定产品，无需重启。"""
    from app import docs_catalog

    docs_catalog.invalidate()
    assert admin_client.put(
        "/api/admin/settings", json={"values": {"DOC_PRODUCTS": ["points"]}}
    ).status_code == 200

    ids = [p["id"] for p in admin_client.get("/api/docs").json()["data"]["products"]]
    assert ids == ["points"]
    # 被白名单排除的产品，详情页也必须拿不到，否则等于留了个后门入口
    assert admin_client.get("/api/docs/codex").status_code == 404


def test_reset_restores_doc_visibility(admin_client):
    """重置白名单后，此前被隐藏的产品要重新可见。"""
    from app import docs_catalog

    docs_catalog.invalidate()
    admin_client.put("/api/admin/settings", json={"values": {"DOC_PRODUCTS": ["points"]}})
    assert admin_client.post(
        "/api/admin/settings/reset", json={"keys": ["DOC_PRODUCTS"]}
    ).status_code == 200

    ids = [p["id"] for p in admin_client.get("/api/docs").json()["data"]["products"]]
    assert len(ids) > 1
    assert "codex" in ids
