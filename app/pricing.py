"""模型定价表：拉取上游价格并归一化成对外的积分口径。

## 上游契约（scripts/probe_pricing.py 实测结论，勿凭猜改动）

`GET {NEWAPI_BASE_URL}/api/pricing` **匿名 200 可读**，无需任何凭证。
返回 `{"success": true, "data": [...], "group_ratio": {...}, "usable_group": {...}}`。

`data[]` 每项的关键字段：

- `model_name`          模型标识
- `quota_type`          **0 = 按 Token 计费，1 = 按次计费**。这是真正的计费模式分歧字段
- `model_ratio`         输入倍率（quota_type=0 时有效）
- `completion_ratio`    输出/输入倍率（乘在 model_ratio 上，不是独立倍率）
- `model_price`         每次调用的价格，单位美元（quota_type=1 时有效）
- `enable_groups`       该模型可用的分组名列表

`group_ratio` 是分组倍率表（如 `{"default": 1.0, "vip": 0.8}`），最终价格
必须再乘分组倍率，否则 VIP 用户看到的价格与实际扣费不符。

## 换算链路

new-api 内部的价格基准是「美元 → quota」，倍率 1.0 对应每 1K token 收
`0.002 USD`（即 500000 quota/USD × 0.002 = 1000 quota）。因此：

    每 1K token 的 quota = model_ratio × 1000 × group_ratio

再经 `config.quota_to_points_exact` 转成积分。**必须走 config 的换算函数**，
不能在本模块自己乘 POINTS_PER_CNY —— README 已明确这条，否则会出现
文档页与账单页两套口径，而这种错误用户会先发现。

## 失败策略

上游挂了**绝不让文档页 500**。文档是售前页面，打不开等于卖不出去；
一张标注了日期的旧价格表远好过一个白屏。所以本模块的对外入口永不抛异常，
失败时回落到快照并把 `stale=True` 交给前端显式提示。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import config

logger = logging.getLogger(__name__)

# 倍率 1.0 的基准：每 1K token 对应的 quota。
# 来自 new-api 的 `QuotaPerUnit=500000` 与 `0.002 USD/1K` 基准价。
_QUOTA_PER_1K_AT_RATIO_1 = 1000

# 快照文件：上游不可达时的兜底。由 scripts/refresh_pricing_snapshot.py 更新。
# 走配置而非写死相对路径：容器里状态卷挂在 /data，而 parent.parent/"data"
# 解析出的是 /app/data —— 两者不是同一个目录，写死会让生产环境永远读不到快照，
# 且因为 _load_snapshot() 只 warning 不抛异常，这个偏差不会有任何显性症状。
_SNAPSHOT = Path(config.PRICING_SNAPSHOT_FILE)

_cache: dict[str, Any] | None = None
_cache_at: float = 0.0
_lock = asyncio.Lock()


def invalidate() -> None:
    """清缓存。settings 改动积分口径或分组白名单后由 settings 调用。"""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def _points_per_1k(ratio: float, group_ratio: float) -> float:
    """倍率 → 每 1K token 的积分数。"""
    quota = ratio * _QUOTA_PER_1K_AT_RATIO_1 * group_ratio
    return config.quota_to_points_exact(quota)


def _normalize(payload: dict) -> dict:
    """上游原始结构 → 前端直接可渲染的表。

    按 quota_type 分流成两张表，而不是塞进同一张表用空值区分：
    按次计费的模型没有「输入/输出」概念，混在一张表里必然出现空列，
    而空列会让用户以为是数据缺失。
    """
    rows = payload.get("data") or []
    group_ratios = payload.get("group_ratio") or {}
    allow = set(config.PRICING_GROUPS)

    # 分组倍率：只保留白名单内的分组，并保证 default 兜底存在。
    groups = {
        name: float(r) for name, r in group_ratios.items()
        if not allow or name in allow
    } or {"default": 1.0}

    token_models: list[dict] = []
    per_call_models: list[dict] = []

    for item in rows:
        name = (item.get("model_name") or "").strip()
        if not name:
            continue
        enable = set(item.get("enable_groups") or [])
        # 模型至少要在一个展示分组里可用，否则列出来是误导 —— 用户会去调一个
        # 自己分组根本调不通的模型。
        visible = [g for g in groups if not enable or g in enable]
        if not visible:
            continue

        if int(item.get("quota_type") or 0) == 1:
            price = float(item.get("model_price") or 0)
            per_call_models.append({
                "model": name,
                "groups": visible,
                "points_per_call": {
                    g: round(config.quota_to_points_exact(
                        price * config.QUOTA_PER_CNY * groups[g]
                    ), 2)
                    for g in visible
                },
            })
        else:
            m_ratio = float(item.get("model_ratio") or 0)
            c_ratio = float(item.get("completion_ratio") or 1)
            token_models.append({
                "model": name,
                "groups": visible,
                "ratio": round(m_ratio, 4),
                "completion_ratio": round(c_ratio, 4),
                "points_in": {
                    g: round(_points_per_1k(m_ratio, groups[g]), 4) for g in visible
                },
                "points_out": {
                    g: round(_points_per_1k(m_ratio * c_ratio, groups[g]), 4)
                    for g in visible
                },
            })

    token_models.sort(key=lambda r: r["model"])
    per_call_models.sort(key=lambda r: r["model"])
    return {
        "groups": groups,
        "token_models": token_models,
        "per_call_models": per_call_models,
        "unit": config.POINTS_UNIT_NAME,
        "stale": False,
        "snapshot_date": "",
    }


def _load_snapshot() -> dict:
    """读快照并归一化。快照缺失时返回空表而不是抛异常。"""
    try:
        raw = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("定价快照不可用：%s", e)
        return {
            "groups": {"default": 1.0}, "token_models": [], "per_call_models": [],
            "unit": config.POINTS_UNIT_NAME, "stale": True, "snapshot_date": "",
        }
    out = _normalize(raw.get("payload") or raw)
    out["stale"] = True
    # 无日期的 stale 数据比没有数据更危险 —— 用户会当成现价。
    out["snapshot_date"] = raw.get("fetched_at", "")
    return out


async def _fetch() -> dict:
    base = config.NEWAPI_BASE_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{base}/api/pricing")
        resp.raise_for_status()
        payload = resp.json()
    if not payload.get("success"):
        raise ValueError(f"上游返回 success=false: {payload.get('message', '')}")
    return _normalize(payload)


async def table() -> dict:
    """对外入口：取定价表。**永不抛异常。**"""
    global _cache, _cache_at
    ttl = config.PRICING_TTL
    now = time.monotonic()
    if _cache is not None and ttl > 0 and now - _cache_at < ttl:
        return _cache

    async with _lock:
        # 双检：并发请求下只让一个协程回源，其余复用结果。
        now = time.monotonic()
        if _cache is not None and ttl > 0 and now - _cache_at < ttl:
            return _cache
        try:
            data = await _fetch()
        except Exception as e:                       # noqa: BLE001 - 见模块头注释
            logger.warning("拉取上游定价失败，回落快照：%s", e)
            data = _load_snapshot()
        _cache = data
        _cache_at = time.monotonic()
        return data
