"""调用日志的积分换算契约测试。

守的是这样一次线上事故：明细列的「积分」全是「-」，但顶部「累计消耗积分」
却是正常正数。根因是单条日志用了向下取整的 quota_to_points() —— 1 积分 = 50
quota，单次小请求的 quota 常在 10~49 之间，取整后恒为 0；而累计值是对总 quota
一次性换算，所以不受影响。修复是单条日志改用 quota_to_points_exact()。
"""


def test_small_quota_floors_to_zero_by_design():
    """整数换算对小额 quota 归零 —— 这是聚合口径的预期行为，不是 bug。"""
    from app import config

    assert config.quota_to_points(49) == 0
    assert config.quota_to_points(50) == 1


def test_exact_conversion_keeps_small_quota_visible():
    """明细口径必须保留小数，否则小额调用在 UI 上不可见。"""
    from app import config

    assert config.quota_to_points_exact(1) > 0
    assert config.quota_to_points_exact(49) > 0
    assert config.quota_to_points_exact(50) == 1.0
    assert config.quota_to_points_exact(0) == 0.0


def test_conversion_roundtrip_is_stable():
    """积分 → quota → 积分 不应出现量级漂移。"""
    from app import config

    for points in (1, 10, 1000):
        assert config.quota_to_points(config.points_to_quota(points)) == points


def _login(client):
    r = client.post("/api/user/login",
                    json={"username": "tester01", "password": "pass1234"})
    assert r.status_code == 200, r.text
    return r


def test_consume_log_items_expose_nonzero_points(client):
    """回归主断言：存在消费记录时，明细的 points 不能全为 0。

    这条断言直接对应用户截图里「积分列全是 -」的现象。
    """
    _login(client)
    data = client.get("/api/log/self?p=1&page_size=20").json()["data"]

    consume = [i for i in data["items"] if i.get("type") == 2]
    assert consume, "mock 数据里应当有消费记录，否则本用例失去意义"
    assert any(float(i["points"]) > 0 for i in consume), \
        "消费明细的 points 全为 0，说明单条日志又用回了向下取整的换算"


def test_aggregate_and_detail_points_are_consistent(client):
    """累计值与明细之和应处在同一量级，不能出现「总额有值、每条都空」。"""
    _login(client)
    data = client.get("/api/log/self?p=1&page_size=100").json()["data"]

    if data["stat"]["points"] > 0:
        detail_sum = sum(float(i["points"]) for i in data["items"])
        assert detail_sum > 0, "累计消耗为正但明细积分之和为 0，量级不一致"
