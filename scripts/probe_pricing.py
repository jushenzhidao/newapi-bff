"""探测 new-api 定价接口契约，为 app/pricing.py 的归一化逻辑定型。

## 为什么必须先探测

文档页要展示「各模型积分消耗系数」，而 new-api 的定价字段是多套并存的历史产物：
tiered_expr（阶梯表达式）、model_ratio（倍率）、model_price（按次固定价）
三者可能同时出现在一条记录上，优先级只能实测确定。照猜的结构写归一化，
返工成本远高于先跑一次。

## 本脚本要回答的四个问题

1. 匿名能否读取 —— 决定 pricing.py 走无鉴权 GET 还是 NEWAPI_ADMIN_PAT。
   文档页是售前页面，未登录必须可见；若必须鉴权，也**绝不能走 _admin_login**
   （会话上限 50 是全系统最高危单点，见 config.py 的说明）。
2. billing_mode 有几种取值 —— 决定归一化需要几条分支。
3. tiered_expr 正则覆盖率，以及「首档即最低价」假设是否成立 ——
   若存在首档非最低的记录，取第一个匹配就是错的，必须改成取最小值。
4. enable_groups 分布 —— 决定分组白名单的默认值和表格行数量级。

只读 GET，无副作用。
"""
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

BASE = os.getenv("NEWAPI_BASE_URL", "https://api.aihuobao.cn")

# 形如 "p * 0.6 + c * 2.4"：p=prompt 系数，c=completion 系数。
# 用 findall 而非 search，才能验证「首档最低」假设。
_TIER_RE = re.compile(r"p\s*\*\s*([\d.]+)\s*\+\s*c\s*\*\s*([\d.]+)")

CANDIDATE_PATHS = ("/api/pricing", "/api/models/price", "/api/model/pricing")


async def _probe_paths(client: httpx.AsyncClient) -> tuple[str, list] | tuple[None, None]:
    """逐个候选路径探测，返回第一个匿名 200 且含模型列表的路径。"""
    for path in CANDIDATE_PATHS:
        try:
            r = await client.get(path)
        except httpx.HTTPError as e:
            print(f"  {path} -> 请求失败 {type(e).__name__}: {e}")
            continue
        print(f"  {path} -> HTTP {r.status_code}")
        if r.status_code != 200:
            continue
        try:
            raw = r.json()
        except ValueError:
            print(f"    响应非 JSON，前 200 字：{r.text[:200]!r}")
            continue
        items = raw if isinstance(raw, list) else (raw.get("data") or [])
        if isinstance(items, list) and items:
            print(f"    命中：{len(items)} 条记录，顶层类型 {type(raw).__name__}")
            return path, items
        print(f"    200 但无数据：{json.dumps(raw, ensure_ascii=False)[:200]}")
    return None, None


def _dist(title: str, counter: Counter) -> None:
    print(f"\n=== {title}")
    if not counter:
        print("  （无数据）")
        return
    for k, n in counter.most_common():
        print(f"  {k}: {n}")


def _report_tiered(items: list) -> None:
    """验证 tiered_expr 正则覆盖率与「首档最低」假设。"""
    tiered = [it for it in items if it.get("billing_mode") == "tiered_expr"]
    print(f"\n=== tiered_expr 分析（{len(tiered)} 条）")
    if not tiered:
        print("  无 tiered_expr 记录")
        return
    unmatched, non_ascending = 0, 0
    for it in tiered:
        name = it.get("model_name") or it.get("model") or "<无名>"
        expr = str(it.get("billing_expr") or it.get("tiered_expr") or "")
        pairs = _TIER_RE.findall(expr)
        if not pairs:
            unmatched += 1
            print(f"  [未匹配] {name}: {expr[:120]!r}")
            continue
        first_p = float(pairs[0][0])
        min_p = min(float(p) for p, _ in pairs)
        flag = ""
        if first_p != min_p:
            non_ascending += 1
            flag = f"  ← 首档 {first_p} 非最低 {min_p}"
        print(f"  {name}: 档数={len(pairs)} 首档 in={first_p} out={pairs[0][1]}"
              f" model_ratio={it.get('model_ratio')}{flag}")
    print(f"\n  小结：未匹配 {unmatched} 条，首档非最低 {non_ascending} 条")
    if non_ascending:
        print("  [风险] 「取第一个匹配」不成立，pricing.py 必须改成取最小值")
    else:
        print("  [通过] 首档即最低价，取第一个匹配安全")


async def main() -> None:
    print(f"目标：{BASE}\n=== 候选路径探测（匿名）")
    # trust_env=False：沿用 probe_* 惯例，避免本机代理干扰探测结论
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0, trust_env=False) as client:
        path, items = await _probe_paths(client)
        if not items:
            print("\n匿名不可读或无候选路径命中。")
            print("下一步：确认真实路径与鉴权方式（务必用 PAT，不要触发 _admin_login）")
            return

        keys = sorted({k for it in items if isinstance(it, dict) for k in it})
        print(f"\n=== 字段并集（{len(keys)} 个）\n  {keys}")

        _dist("billing_mode 分布", Counter(
            str(it.get("billing_mode", "<缺失>")) for it in items))
        _dist("quota_type 分布", Counter(
            str(it.get("quota_type", "<缺失>")) for it in items))
        _dist("enable_groups 分布", Counter(
            str(g) for it in items for g in (it.get("enable_groups") or [])))

        _report_tiered(items)

        print("\n=== 样本原始记录（前 2 条）")
        print(json.dumps(items[:2], ensure_ascii=False, indent=2))
        print(f"\n=== 结论：可用路径 {path}，共 {len(items)} 个模型")


asyncio.run(main())
