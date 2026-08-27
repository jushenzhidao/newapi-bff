"""产品档案与教程接口。

覆盖的是「静默失效」类风险：档案不会因为语法错误而报警，只会从页面上消失；
定价注入不会因为污染缓存而报错，只会让第二个用户看到第一个用户的数据。
这两类问题跑通一次冒烟测试是发现不了的。
"""
import pytest

from app import config, docs_catalog

# ==================== 档案层 ====================

def test_所有档案全部可加载():
    """档案语法错误的后果是产品从页面消失而不是报错，所以要逐个断言存在。

    新增 docs/products/*.yml 后在此补 id 即可（刻意显式列出而非动态，
    防止档案被误删时测试静默变绿）。
    """
    loaded = docs_catalog.all_products()
    assert set(loaded) == {
        "points", "codex", "deepseek-harness",
        "token-plan", "coding-plan", "agent-plan",
        "client-config",
    }


def test_占位档案可见且标记待补充():
    """占位产品必须能被看到 —— 看不到就无法验证渲染链路。

    真实内容补齐后应改掉 badge，这条断言会跟着失败，提醒同步更新。
    """
    index = {p["id"]: p for p in docs_catalog.index()}
    assert len(index) == len(docs_catalog.all_products()), \
        f"有档案被索引过滤掉了：{set(docs_catalog.all_products()) - set(index)}"
    for pid in ["codex", "deepseek-harness", "token-plan", "coding-plan", "agent-plan"]:
        assert index[pid]["badge"] == "待补充"


def test_索引不含正文():
    """首屏不该把档案列表的正文全拉下来。"""
    for item in docs_catalog.index():
        assert "sections" not in item


def test_档案变量插值已生效():
    """档案里写 {brand} 之类占位符，渲染到前端时必须已经是真实值。

    只匹配 {单词} 形式的占位符。档案正文里有 curl 的 JSON 示例，
    那些花括号是合法内容，不能一概当成漏插值。
    """
    import re

    leftover = set()
    for pid in docs_catalog.all_products():
        text = str(docs_catalog.get(pid)["sections"])
        leftover |= set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", text))
    assert not leftover, f"存在未插值的占位符：{sorted(leftover)}"


# ==================== 接口层 ====================

def test_教程索引接口(client):
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert len(r.json()["data"]["products"]) == len(docs_catalog.all_products())


@pytest.mark.parametrize("pid", sorted(docs_catalog.all_products()))
def test_每个产品详情都能打开(client, pid):
    r = client.get(f"/api/docs/{pid}")
    assert r.status_code == 200
    assert r.json()["data"]["sections"], f"{pid} 正文为空"


def test_不存在的产品返回404(client):
    assert client.get("/api/docs/nope").status_code == 404


def test_定价注入不污染档案缓存(client):
    """docs_detail 用浅拷贝避免把注入结果写回缓存。

    一旦回归，第二个请求会读到第一个请求注入过的对象 —— 表面正常，
    实际是上游已经挂了却还在返回 stale=False 的旧数据。
    """
    client.get("/api/docs/points")
    cached = docs_catalog.get("points")
    tables = [s for s in cached["sections"] if s["type"] == "pricing_table"]
    assert tables, "points 档案应含 pricing_table 段"
    assert tables[0].get("data") in (None, {}), "定价数据被写回了档案缓存"


def test_定价上游失败仍返回页面(client, monkeypatch):
    """文档是售前页面，打不开等于卖不出去 —— 上游挂了也必须出页面。"""
    async def boom():
        raise RuntimeError("上游 500")

    from app import pricing
    monkeypatch.setattr(pricing, "_fetch", boom)
    monkeypatch.setattr(pricing, "_cache", None, raising=False)

    r = client.get("/api/docs/points")
    assert r.status_code == 200, "定价失败把整个文档页拖挂了"


# ==================== 就绪检查 ====================

def test_档案为空时就绪检查给出可读原因(monkeypatch):
    """这个分支只在档案全丢时执行，正常测试跑不到 —— 之前因此藏了个
    AttributeError（引用了不存在的 PRODUCT_DIR），健康检查在最需要它
    说话的时候会变成 500。
    """
    from app import main

    monkeypatch.setattr(docs_catalog, "all_products", lambda: {})
    okay, reason = main._check_doc_products()
    assert okay is False
    assert "没有任何有效档案" in reason


def test_白名单含错误id时拦在就绪阶段(monkeypatch):
    """白名单拼错一个字母，产品会静默消失，运营不会知道。"""
    from app import main

    monkeypatch.setattr(config, "DOC_PRODUCTS", ("points", "codexx"))
    okay, reason = main._check_doc_products()
    assert okay is False
    assert "codexx" in reason


# ==================== 图标键名 ====================

def test_图标键名从appjs现场解析():
    """键集必须来自 static/app.js，硬编码副本会跟前端漂移。

    括号配平若失效会吞掉 ICONS 之后的整段代码，键数会远超实际，所以卡上界。
    """
    from app import main

    keys = main._icon_keys()
    assert {"book", "wallet", "code", "gauge"} <= keys
    assert len(keys) < 40, f"疑似吞掉了 ICONS 之后的代码：{sorted(keys)}"


def test_全部档案图标键名合法():
    """写错键名只会静默回落成 book，页面看着正常但图标是错的。"""
    from app import main

    okay, reason = main._check_doc_products()
    assert okay is True, reason


def test_错误图标键名拦在就绪阶段(monkeypatch):
    from app import main

    monkeypatch.setattr(
        docs_catalog, "all_products",
        lambda: {"points": {"id": "points", "icon": "walllet"}},
    )
    okay, reason = main._check_doc_products()
    assert okay is False
    assert "walllet" in reason


def test_appjs不可读时跳过图标校验而非判定不就绪(monkeypatch):
    """图标校验是锦上添花，正则没匹配上不该让整个服务 unready。"""
    from app import main

    monkeypatch.setattr(main, "_icon_keys", set)
    okay, _ = main._check_doc_products()
    assert okay is True
