"""运行时动态配置覆盖层。

## 定位

config.py 里的值全是 **import 期求值的模块级常量**，改环境变量必须重启进程。
本模块提供一层「运行时覆盖」：管理员在页面上改的值落盘到 JSON，config 读取时
优先取覆盖值，取不到才回落到环境变量默认值。

优先级：**运行时覆盖 > 环境变量 / .env > 代码默认值**

## 为什么覆盖层优先于环境变量

反过来（环境变量优先）会让页面上的修改看起来「保存成功但没生效」——
部署方一旦在 .env 里写过某项，管理员就再也改不动它，且界面无法解释原因。
覆盖层是**显式的人工决策**，理应压过静态配置；要收回控制权就删除该项覆盖
（reset），让它重新跟随环境变量。

## 为什么不引入数据库

BFF 本身无 DB（见 promo.py 同款设计）。落盘用「临时文件 + 原子 os.replace」，
保证进程被杀时不会留下半截 JSON。

## 已知限制：多实例不共享

与 promo_state.json 一样，本文件是**单机状态**。多副本部署时，改配置只在
接收请求的那个副本生效，其余副本要到重启才会读到新值。需要跨实例一致时，
本模块的 _load/_save 是唯一改动点（换成 Redis 即可）。

## 安全边界

只有 SPECS 白名单里的键可被覆盖。凭证类（SECRET_KEY、管理员 PAT/账密）、
上游地址、Cookie 安全属性一律不在其中 —— 这些值改错会造成会话全体失效、
凭证泄露或服务不可用，只应由部署方通过环境变量注入。详见 config.py 的说明。
"""
import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("bff.settings")

# 刻意不 import config：config 在模块级 import 本模块用于解析取值，
# 反向 import 会形成循环。故此处自行读取环境变量确定存储路径。
SETTINGS_FILE: str = os.getenv(
    "BFF_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "settings.json"),
)

_lock = asyncio.Lock()
_cache: dict[str, Any] | None = None


class ValidationError(ValueError):
    """字段校验失败。message 直接面向管理员展示，必须可读且可行动。"""


# ==================== 字段规格 ====================
# 每项 = (分组, 中文标签, 解析函数, 说明)。解析函数负责**类型收敛 + 范围校验**，
# 非法值一律抛 ValidationError，绝不静默纠正 —— 静默纠正会让管理员以为改成功了。
def _s(max_len: int = 200, allow_empty: bool = True) -> Callable[[Any], str]:
    def parse(v: Any) -> str:
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise ValidationError("必须是字符串")
        v = v.strip()
        if not allow_empty and not v:
            raise ValidationError("不能为空")
        if len(v) > max_len:
            raise ValidationError(f"长度不能超过 {max_len} 字")
        return v
    return parse


def _i(lo: int, hi: int) -> Callable[[Any], int]:
    def parse(v: Any) -> int:
        if isinstance(v, bool):
            raise ValidationError("必须是整数")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValidationError("必须是整数") from None
        if not lo <= n <= hi:
            raise ValidationError(f"取值需在 {lo} ~ {hi} 之间")
        return n
    return parse


def _f(lo: float, hi: float) -> Callable[[Any], float]:
    def parse(v: Any) -> float:
        if isinstance(v, bool):
            raise ValidationError("必须是数字")
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValidationError("必须是数字") from None
        if not lo <= x <= hi:
            raise ValidationError(f"取值需在 {lo} ~ {hi} 之间")
        return round(x, 4)
    return parse


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off"):
        return False
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise ValidationError("必须是布尔值")


def _int_list(lo: int, hi: int, max_items: int = 12) -> Callable[[Any], list]:
    """充值档位 / 模型清单这类列表。接受数组或逗号分隔串（前端两种都可能传）。"""
    def parse(v: Any) -> list:
        if isinstance(v, str):
            v = [x for x in v.split(",") if x.strip()]
        if not isinstance(v, list | tuple):
            raise ValidationError("必须是数组或逗号分隔的文本")
        out: list[int] = []
        for item in v:
            try:
                n = int(str(item).strip())
            except (TypeError, ValueError):
                raise ValidationError(f"「{item}」不是整数") from None
            if not lo <= n <= hi:
                raise ValidationError(f"{n} 超出 {lo} ~ {hi} 范围")
            if n not in out:
                out.append(n)
        if not out:
            raise ValidationError("至少需要一项")
        if len(out) > max_items:
            raise ValidationError(f"最多 {max_items} 项")
        return sorted(out)
    return parse


def _str_list(max_items: int = 40) -> Callable[[Any], list]:
    def parse(v: Any) -> list:
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, list | tuple):
            raise ValidationError("必须是数组或逗号分隔的文本")
        out: list[str] = []
        for item in v:
            s = str(item).strip()
            if not s:
                continue
            if len(s) > 80:
                raise ValidationError(f"「{s[:20]}…」过长")
            if s not in out:
                out.append(s)
        if not out:
            raise ValidationError("至少需要一项")
        if len(out) > max_items:
            raise ValidationError(f"最多 {max_items} 项")
        return out
    return parse


# 需要二次确认的高风险项：改动会影响**已有用户看到的数字口径**，
# 不是错配置，而是「确实会变、且必须由人明确知晓」。
RATE_SENSITIVE = ("POINTS_PER_CNY",)

# key -> (分组, 标签, 解析器, 提示)
SPECS: dict[str, tuple[str, str, Callable[[Any], Any], str]] = {
    # ---- 品牌与文案 ----
    "BRAND_NAME": ("brand", "品牌名", _s(40, allow_empty=False), "浏览器标题与页面 Logo 旁的名称"),
    "BRAND_LOGO_TEXT": ("brand", "Logo 字母", _s(4), "留空则自动取品牌名首字"),
    "BRAND_TAGLINE": ("brand", "登录页副标题", _s(80), ""),
    "BRAND_HERO_TITLE": ("brand", "首页标题", _s(80), ""),
    "BRAND_HERO_H1": ("brand", "大标题·前段", _s(40), "普通颜色显示"),
    "BRAND_HERO_H1_PREFIX": ("brand", "大标题·连接词", _s(20), ""),
    "BRAND_HERO_H1_ACCENT": ("brand", "大标题·高亮段", _s(40), "渐变色显示"),
    "BRAND_HERO_SUB": ("brand", "首页描述", _s(200), ""),
    "BRAND_HERO_BADGE": ("brand", "首页角标", _s(60), ""),
    "BRAND_ICP": ("brand", "ICP 备案号", _s(60), "留空则页脚不显示"),
    "BRAND_CONTACT": ("brand", "客服联系方式", _s(120), "留空则不显示"),

    # ---- 积分体系 ----
    "POINTS_UNIT_NAME": ("points", "积分单位名", _s(8, allow_empty=False), "全站对外计价单位的叫法"),
    "POINTS_PER_CNY": ("points", "1 元 = 多少积分", _i(1, 10_000_000),
                       "重要：改动会立即改变所有用户看到的余额数字，需二次确认"),

    # ---- 充值 ----
    "PAY_AMOUNTS": ("pay", "充值档位（元）", _int_list(1, 100_000),
                    "逗号分隔，自动去重升序"),

    # ---- 运营活动 ----
    "PROMO_SIGNUP_ENABLED": ("promo", "开启注册礼包", _b, ""),
    "PROMO_SIGNUP_POINTS": ("promo", "注册赠送积分", _i(0, 100_000_000), "0 等同关闭"),
    "PROMO_FIRST_TOPUP_ENABLED": ("promo", "开启首充活动", _b, ""),
    "PROMO_TITLE": ("promo", "首充活动标题", _s(40), ""),
    "PROMO_FIRST_TOPUP_RATE": ("promo", "首充赠送比例", _f(0, 10),
                               "1.0 = 充多少送多少"),
    "PROMO_FIRST_TOPUP_MIN_CNY": ("promo", "首充门槛（元）", _i(0, 100_000), ""),
    "PROMO_FIRST_TOPUP_MAX_POINTS": ("promo", "首充赠送上限", _i(0, 1_000_000_000), ""),

    # ---- 文档与功能开关 ----
    "API_BASE_URL": ("doc", "对外 API 地址", _s(200, allow_empty=False),
                     "示例代码里展示的 base_url"),
    "DOC_DEFAULT_MODEL": ("doc", "示例默认模型", _s(80, allow_empty=False), ""),
    "DOC_MODELS": ("doc", "展示模型清单", _str_list(), "逗号分隔"),
    "REDEEM_LOGIN_ENABLED": ("feature", "开放兑换码登录", _b,
                             "关闭后登录页不显示该入口，接口也拒绝"),
}

GROUP_LABELS = {
    "brand": "品牌与文案",
    "points": "积分体系",
    "pay": "充值档位",
    "promo": "运营活动",
    "doc": "接入文档",
    "feature": "功能开关",
}


# ==================== 存取 ====================
def _load() -> dict:
    """读覆盖值（带进程内缓存）。

    加载期做一次校验并丢弃非法项：文件可能被手工编辑过，一个坏值不该让
    整个覆盖层失效，更不该把非法值喂给业务代码。
    """
    global _cache
    if _cache is not None:
        return _cache
    data: dict[str, Any] = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k not in SPECS:
                    continue
                try:
                    data[k] = SPECS[k][2](v)
                except ValidationError:
                    logger.warning("忽略非法的动态配置项 %s=%r", k, v)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("读取动态配置失败，本次按环境变量默认值运行")
    _cache = data
    return _cache


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETTINGS_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        logger.exception("保存动态配置失败")
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def get(key: str, default: Any) -> Any:
    """取覆盖值，未覆盖则返回 default（即 config 的环境变量默认值）。

    config.py 的 __getattr__ 通过它实现「覆盖优先」，是整个动态配置的取值入口。
    """
    return _load().get(key, default)


def has_override(key: str) -> bool:
    return key in _load()


def overrides() -> dict:
    return dict(_load())


def invalidate() -> None:
    """清进程内缓存。测试切换存储路径后必须调用，否则读到上一个用例的值。"""
    global _cache
    _cache = None


def validate(patch: dict) -> dict:
    """校验并收敛一批待写入的值。任一项非法即整批拒绝（不做部分写入）。

    未知键直接报错而不是静默忽略：静默忽略会让「字段名写错」表现为
    「保存成功但没生效」，这是最难自查的一类问题。
    """
    if not isinstance(patch, dict) or not patch:
        raise ValidationError("没有需要修改的配置项")
    unknown = [k for k in patch if k not in SPECS]
    if unknown:
        raise ValidationError(f"不支持的配置项：{', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        try:
            out[k] = SPECS[k][2](v)
        except ValidationError as e:
            raise ValidationError(f"{SPECS[k][1]}：{e}") from None
    return out


async def update(patch: dict) -> dict:
    """写入覆盖值并落盘，返回实际生效的完整覆盖集。"""
    clean = validate(patch)
    async with _lock:
        data = dict(_load())
        data.update(clean)
        _save(data)
        globals()["_cache"] = data
    logger.info("动态配置已更新：%s", ", ".join(sorted(clean)))
    return dict(data)


async def reset(keys: list[str]) -> dict:
    """删除指定项的覆盖，使其重新回落到环境变量默认值。"""
    unknown = [k for k in keys if k not in SPECS]
    if unknown:
        raise ValidationError(f"不支持的配置项：{', '.join(sorted(unknown))}")
    async with _lock:
        data = dict(_load())
        for k in keys:
            data.pop(k, None)
        _save(data)
        globals()["_cache"] = data
    logger.info("动态配置已重置：%s", ", ".join(sorted(keys)) or "-")
    return dict(data)
