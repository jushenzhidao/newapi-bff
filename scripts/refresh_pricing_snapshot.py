"""刷新定价快照：把上游 /api/pricing 的原始返回落盘为兜底数据。

为什么需要独立脚本：正常运行时上游可达，快照永远不会被写入，
兜底文件会一直停留在初次提交的版本 —— 等真的需要它时，
拿出来的是一份半年前的价格。这类「兜底数据本身已腐坏」的问题
在故障现场才暴露，代价最高。

建议接入定时任务（每日一次即可），或在每次发布前手工执行。

用法：
    python3 scripts/refresh_pricing_snapshot.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

OUT = ROOT / "data" / "pricing_snapshot.json"


def main() -> int:
    url = config.NEWAPI_BASE_URL.rstrip("/") + "/api/pricing"
    # base_url 来自环境变量，配成 file:// 会让 urlopen 去读本地文件并当成上游响应
    if not url.startswith(("http://", "https://")):
        print(f"拒绝非 http(s) 地址：{url}", file=sys.stderr)
        return 1
    print(f"拉取 {url}")
    try:
        # scheme 已在上面显式限定为 http(s)，静态检查看不到该守卫
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:                           # noqa: BLE001
        print(f"失败：{e}", file=sys.stderr)
        return 1

    if not payload.get("success"):
        print(f"上游 success=false：{payload.get('message', '')}", file=sys.stderr)
        return 1

    rows = payload.get("data") or []
    if not rows:
        # 空表覆盖掉一份有效快照，等于亲手销毁兜底数据。
        print("上游返回空 data，拒绝覆盖既有快照", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "source": url,
        "payload": payload,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {OUT}（{len(rows)} 个模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
