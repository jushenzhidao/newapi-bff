"""产品文档档案层：把「教程/文档」从写死的单页拆成一产品一档案。

## 为什么要这层

原先教程内容硬编码在前端单页里，每加一个在售产品（积分 / Codex /
DeepSeek harness / Token·Coding·Agent Plan）都要改 JS 和 CSS。
现在改成：新增产品 = 往 `docs/products/` 丢一个 YAML，前后端零改动。

## 档案约定

- 文件名即产品 id（`points.yml` → id `points`），不在档案里重复写 id，
  避免文件名与内部 id 不一致这种最难查的错。
- `sections[].type` 决定前端用哪个渲染器。**未知 type 直接判为校验失败**，
  不静默跳过 —— 静默跳过的结果是运营写错一个字母，那一段内容就凭空消失，
  而页面看起来完全正常，没人会发现。
- 文案里可写 `{{var}}` 占位，由 `_interpolate` 注入品牌名、站点地址、
  积分单位等动态值，避免改个品牌名要手改六份档案。

## 排序与可见性

`order` 升序，同序按 id 字典序（保证渲染顺序稳定，否则文件系统遍历
顺序会让页面每次部署都换一个排列）。`config.DOC_PRODUCTS` 为空表示
全部启用 —— 这个默认值是刻意的：新增档案不该因为忘记改环境变量而静默不可见。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from . import config

logger = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "products"

# 前端已实现的段落渲染器。新增 type 必须同步 static/docs.js 的 RENDERERS。
KNOWN_SECTION_TYPES = frozenset({
    "markdown",      # 富文本说明
    "steps",         # 编号安装步骤（支持每步 code）
    "platform_tabs", # 分平台命令（macOS / Windows / Linux）
    "pricing_table", # 注入实时模型系数表
    "table",         # 静态表格（额度规则等）
    "faq",           # 折叠问答
    "callout",       # 提示 / 警告条
})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_VAR_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

_cache: dict[str, dict] | None = None


class CatalogError(Exception):
    """档案校验失败。启动期抛出，不允许带着坏档案上线。"""


def invalidate() -> None:
    global _cache
    _cache = None


def _variables() -> dict[str, str]:
    """可在档案文案中引用的动态值。"""
    # 只引用 config 里真实存在的字段。api_base / default_model 复用既有的
    # DOC_* 配置，避免为文档另造一套同义配置项。
    return {
        "brand": config.BRAND_NAME,
        "api_base": config.API_BASE_URL,
        "default_model": config.DOC_DEFAULT_MODEL,
        "points_unit": config.POINTS_UNIT_NAME,
        "points_per_cny": str(config.POINTS_PER_CNY),
        "contact": config.BRAND_CONTACT,
    }


def _interpolate(node: Any, variables: dict[str, str]) -> Any:
    """递归替换 {{var}}。未知变量原样保留，便于在页面上直接看出漏配。"""
    if isinstance(node, str):
        return _VAR_RE.sub(
            lambda m: variables.get(m.group(1), m.group(0)), node
        )
    if isinstance(node, list):
        return [_interpolate(x, variables) for x in node]
    if isinstance(node, dict):
        return {k: _interpolate(v, variables) for k, v in node.items()}
    return node


def _validate(pid: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise CatalogError(f"{pid}: 档案根节点必须是映射")

    title = str(raw.get("title") or "").strip()
    if not title:
        raise CatalogError(f"{pid}: 缺少 title")

    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        raise CatalogError(f"{pid}: sections 必须是非空列表")

    clean_sections = []
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            raise CatalogError(f"{pid}: sections[{i}] 必须是映射")
        stype = str(sec.get("type") or "").strip()
        if stype not in KNOWN_SECTION_TYPES:
            raise CatalogError(
                f"{pid}: sections[{i}] 的 type={stype!r} 不支持，"
                f"可选：{', '.join(sorted(KNOWN_SECTION_TYPES))}"
            )
        clean_sections.append(dict(sec))

    return {
        "id": pid,
        "title": title,
        "summary": str(raw.get("summary") or "").strip(),
        # 图标存的是 static/app.js ICONS 注册表的键名，不是 emoji。
        # 全站统一 SVG，项目有 emoji 门禁（scripts/check_no_emoji.py）。
        "icon": str(raw.get("icon") or "book").strip(),
        "badge": str(raw.get("badge") or "").strip(),
        "order": int(raw.get("order") or 999),
        "draft": bool(raw.get("draft") or False),
        "sections": clean_sections,
    }


def _load_all() -> dict[str, dict]:
    if not DOCS_DIR.is_dir():
        logger.warning("产品档案目录不存在：%s", DOCS_DIR)
        return {}
    out: dict[str, dict] = {}
    variables = _variables()
    for path in sorted(DOCS_DIR.glob("*.yml")):
        pid = path.stem
        if not _ID_RE.match(pid):
            raise CatalogError(f"档案文件名 {path.name} 不是合法 id（小写字母数字与连字符）")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise CatalogError(f"{pid}: 读取失败 {e}") from e
        out[pid] = _interpolate(_validate(pid, raw), variables)
    return out


def all_products() -> dict[str, dict]:
    global _cache
    if _cache is None:
        _cache = _load_all()
    return _cache


def unknown_whitelist_ids() -> list[str]:
    """白名单里配了但档案不存在的 id。

    交给 /readyz 报出来：运营配错一个字母会让产品页整个消失，
    而线上不会有任何报错 —— 这种静默失效必须在健康检查里可见。
    """
    if not config.DOC_PRODUCTS:
        return []
    known = set(all_products())
    return sorted(set(config.DOC_PRODUCTS) - known)


def _visible() -> list[dict]:
    allow = set(config.DOC_PRODUCTS)
    items = [
        p for p in all_products().values()
        if (not allow or p["id"] in allow) and not p["draft"]
    ]
    items.sort(key=lambda p: (p["order"], p["id"]))
    return items


def index() -> list[dict]:
    """索引页数据：不含 sections，避免首屏拉全部正文。"""
    return [
        {k: p[k] for k in ("id", "title", "summary", "icon", "badge")}
        for p in _visible()
    ]


def get(pid: str) -> dict | None:
    for p in _visible():
        if p["id"] == pid:
            return p
    return None
