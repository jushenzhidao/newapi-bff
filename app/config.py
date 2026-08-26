"""BFF 配置。

MOCK_MODE=0（默认）走真实 new-api；BFF_MOCK_MODE=1 走内存 mock 演示。
生产环境务必用环境变量注入 BFF_SECRET_KEY / 管理员账密。
"""
import os
from pathlib import Path

# 加载项目根 .env。必须在本模块任何 os.getenv 之前执行 —— 下面的配置项都是
# import 期求值的模块级常量，晚一步加载等于没加载。
#
# override=False 是刻意的：真实环境变量（compose environment、CI secrets、
# 命令行前缀）优先级必须高于 .env 文件，否则开发机上一份陈旧的 .env 会静默
# 覆盖生产注入值。Docker 部署走 compose 的 env_file，此处 .env 不存在也不报错。
#
# dotenv 缺失时降级为「只读环境变量」而不是崩溃：配置加载失败让整个进程起不来，
# 代价远大于少读一个本地文件。
#
# BFF_SKIP_DOTENV=1 用于测试：测试结论必须只由代码和夹具决定，不能随开发者本机
# .env 的内容而变（真实管理员 PAT、自定义积分汇率都会让断言飘）。
if os.getenv("BFF_SKIP_DOTENV") != "1":
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except ImportError:
        pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    """解析布尔环境变量；未设置或留空时回落到 default。

    空值必须回落而不能当 False：.env 里留一行 `BFF_COOKIE_SECURE=` 或 compose
    把未定义变量展开成空串都很常见，若按 False 处理，等于让一个笔误静默关掉
    安全开关（Secure Cookie 就是这么丢的）。

    无法识别的值（如 `garbage`）仍按 False，与显式关闭同义 —— 这里不抛异常是
    刻意的：配置解析失败让整个进程起不来，收益不如让 /readyz 去做语义校验。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """解析枚举型环境变量；未设置、留空或取值非法时回落到 default。

    非法值回落而不抛异常：这类值（如 Cookie 的 SameSite）写错时，
    让进程带着一个无效属性启动比直接崩更危险 —— 浏览器会静默忽略非法属性，
    等于降级到无防护。回落到安全默认值并保证可预期。
    """
    raw = (os.getenv(name) or "").strip().lower()
    return raw if raw in allowed else default


MOCK_MODE: bool = os.getenv("BFF_MOCK_MODE", "0") == "1"

# new-api 部署地址
NEWAPI_BASE_URL: str = os.getenv("NEWAPI_BASE_URL", "https://api.aihuobao.cn")

# 管理员账密（用于注册=影子建号、首充赠送 add_quota）。
# 注意：不能写死管理员 PAT —— new-api 的 GET /api/user/token 每次调用都会重新生成 PAT，
# 旧 PAT 随即作废。因此 BFF 用账密登录动态换 PAT 并缓存，遇 401 自动刷新重试。
#
# 不留默认值：本仓库有公开 remote，源码里的默认账密会随仓库和镜像一起分发出去，
# 等同于公开后台管理员密码。未配置时留空，由 newapi_client._admin_login()
# 在真正需要管理员权限的调用上报错，而不是拿一个写死的账号去撞上游。
NEWAPI_ADMIN_USERNAME: str = os.getenv("NEWAPI_ADMIN_USERNAME", "").strip()
NEWAPI_ADMIN_PASSWORD: str = os.getenv("NEWAPI_ADMIN_PASSWORD", "")

# 管理员 PAT 直供（可选，强烈建议生产配置）。
# 背景：new-api 的会话上限是 50 且硬拒绝、不淘汰最旧会话、TTL 30 天。
# 一旦管理员会话被打满，POST /api/user/login 永久 409，BFF 的建号/加额度/兑换码
# 全部瘫痪 —— 这是整个系统最高危的单点故障。
# 而 PAT 走 users.access_token 列，**完全不经过会话系统**：会话打满时 PAT 照常可用。
# 所以配置了这个值，BFF 就再也不需要调 login，从根本上绕开会话上限。
# 获取方式：管理员在 new-api 前端「个人设置 → 系统访问令牌」生成。
NEWAPI_ADMIN_PAT: str = os.getenv("NEWAPI_ADMIN_PAT", "").strip()
NEWAPI_ADMIN_UID: int = _int("NEWAPI_ADMIN_UID", 0)

# 管理员 PAT 落盘缓存：进程重启后直接复用，避免每次冷启都 login 消耗一个会话配额。
ADMIN_CRED_FILE: str = os.getenv(
    "BFF_ADMIN_CRED_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "admin_cred.json"),
)

# 会话 Cookie 加密密钥 —— 生产必须用环境变量注入随机值。
# security.py 用它经 HKDF-SHA256 派生 AES-256-GCM 密钥，兼顾机密性（载荷里的
# 用户 PAT 不可读）与完整性（会话不可伪造）。
# 注意：换这个值会让所有在线会话立即失效（旧密文解不开）。
#
# 行尾抑制 S105 的理由：这不是密码，而是「未配置」的哨兵值 —— main.py 的
# _WEAK_SECRETS 拿它做黑名单比对，/readyz 据此拒绝启动，即防弱密钥的机制本身。
# 选行内抑制而非整文件豁免：config.py 是最容易被误塞真凭证的文件，
# 整文件关掉这条检查等于永久放弃对它的看护。
SECRET_KEY_DEFAULT = "dev-only-secret-change-me"  # noqa: S105
SECRET_KEY: str = os.getenv("BFF_SECRET_KEY", SECRET_KEY_DEFAULT)

# BFF 管理页的额外管理员名单（逗号分隔用户名）。
# 常规判定走上游返回的 user.role >= 10；本项是补充通道，覆盖两种情形：
#   1. mock 模式没有真实上游，否则本地永远进不去管理页；
#   2. 上游实例若不返回 role 字段，仅靠 role 判定会导致谁都进不去。
# **刻意不做成可在线修改**：否则管理员能自行扩权、且一旦写错就再没人能进管理页
# （改回来需要的正是管理权限，形成死锁）。只能通过环境变量注入。
ADMIN_USERNAMES: frozenset = frozenset(
    u.strip() for u in os.getenv("BFF_ADMIN_USERNAMES", "").split(",") if u.strip()
)

COOKIE_NAME = "bff_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天；PAT 长期有效，不受 new-api 15min access_token 限制

# 会话 Cookie 的 Secure 属性 —— 默认 True（安全默认值，不依赖部署方记得开）。
# 置 False 后浏览器会在明文 HTTP 请求里也带上这个 Cookie，而 Cookie 载荷含用户的
# new-api PAT：用户手输 http://域名、混合内容、SSL Strip 都会导致 PAT 明文上网。
# 回环绑定只封住服务端旁路，管不到客户端这一侧，故此处必须默认开启。
#
# 唯一该置 0 的场景：本地开发用 http://127.0.0.1 访问。Secure Cookie 在明文
# http 下浏览器直接丢弃（localhost 例外因浏览器而异，别赌），登录会静默失败。
COOKIE_SECURE: bool = _bool("BFF_COOKIE_SECURE", True)

# 会话 Cookie 的 SameSite 属性 —— 默认 lax，这是**刻意的选择**，不是图省事。
#
# 为什么不用 strict：用户在支付网关点「返回商户」跳回本站，属于跨站顶层导航。
# strict 下浏览器不带 Cookie，用户落到 SPA 会显示未登录、看不到充值结果，
# 必须重新登录一次 —— 而钱其实已经到账（走 notify_url，见 main.py 的回跳注释），
# 这个体验落差极容易被当成「付了钱没到账」而引发客诉。
#
# lax 的防护已经够用：它只放行顶层导航的安全方法（GET/HEAD），
# 本项目所有写操作都是 POST，跨站发起时一律不带 Cookie。
# 也就是说 lax → strict 换来的安全增量很小，代价却是支付回跳必须重新登录。
#
# 允许配成 strict 是给「不做在线支付」的部署形态留的口子（此时无跨站回跳，
# strict 没有副作用）。取值只接受 lax / strict / none，非法值回落到 lax。
#
# 注意 none 必须配合 secure=True 才被浏览器接受，且等于完全放弃 SameSite 防护，
# 除非确有跨站嵌入需求，否则不要用。
COOKIE_SAMESITE: str = _choice("BFF_COOKIE_SAMESITE", "lax", ("lax", "strict", "none"))


# ==================== 积分体系（对外唯一计价单位）====================
# new-api 内部余额单位是 quota，1 元 = 500000 quota（其 QuotaPerUnit 默认值，不要改）。
# 对外我们只讲「积分」：1 元 = POINTS_PER_CNY 积分。
# 换算链：quota → points = quota / QUOTA_PER_CNY * POINTS_PER_CNY
QUOTA_PER_CNY: int = _int("BFF_QUOTA_PER_CNY", 500000)      # new-api 内部口径
# 以下两项可被管理员在线修改，故只作为「默认值」保留，实际取值走本模块 __getattr__。
_DEF_POINTS_PER_CNY: int = _int("BFF_POINTS_PER_CNY", 10000)   # 1 元 = 10000 积分
_DEF_POINTS_UNIT_NAME: str = os.getenv("BFF_POINTS_UNIT_NAME", "积分")


def quota_to_points(quota) -> int:
    """内部 quota → 对外积分（向下取整，绝不虚报余额）。用于余额、累计等聚合值。"""
    if not QUOTA_PER_CNY:
        return 0
    return int(float(quota or 0) / QUOTA_PER_CNY * _dyn("POINTS_PER_CNY"))


def quota_to_points_exact(quota) -> float:
    """内部 quota → 对外积分（保留小数）。

    单条调用日志绝不能用 quota_to_points()：1 积分 = 50 quota，一次小请求
    的 quota 常在 10~49 之间，向下取整后恒为 0，明细里就全变成「-」，
    但累计消耗（对总 quota 一次性换算）却是正常的正数 —— 用户看到的就是
    「总额有值、每条都空」。这里保留 4 位小数，够表达 1 quota（=0.02 积分）。
    """
    if not QUOTA_PER_CNY:
        return 0.0
    return round(float(quota or 0) / QUOTA_PER_CNY * _dyn("POINTS_PER_CNY"), 4)


def quota_per_point() -> float:
    """1 积分 = 多少 quota。随 POINTS_PER_CNY 动态变化，故为函数而非常量。"""
    ppc = _dyn("POINTS_PER_CNY")
    return QUOTA_PER_CNY / ppc if ppc else 0.0


def points_to_quota(points) -> int:
    """对外积分 → 内部 quota（四舍五入，赠送场景宁可多给一点点）。"""
    return int(round(float(points or 0) * quota_per_point()))


def cny_to_points(cny) -> int:
    return int(round(float(cny or 0) * _dyn("POINTS_PER_CNY")))


# ==================== 运营活动 ====================
# 本节全部支持管理员在线修改，故均为 _DEF_ 前缀的默认值，取值统一走 __getattr__。
# 注册礼包：新用户注册即赠送积分（0 表示关闭）
_DEF_PROMO_SIGNUP_ENABLED: bool = _bool("BFF_PROMO_SIGNUP_ENABLED", True)
_DEF_PROMO_SIGNUP_POINTS: int = _int("BFF_PROMO_SIGNUP_POINTS", 20000)  # 2 万积分（=¥2）

# 首充活动：用户首次充值成功后，按比例额外赠送积分
_DEF_PROMO_FIRST_TOPUP_ENABLED: bool = _bool("BFF_PROMO_FIRST_TOPUP_ENABLED", True)
_DEF_PROMO_FIRST_TOPUP_RATE: float = float(os.getenv("BFF_PROMO_FIRST_TOPUP_RATE", "1.0"))
_DEF_PROMO_FIRST_TOPUP_MIN_CNY: int = _int("BFF_PROMO_FIRST_TOPUP_MIN_CNY", 10)   # 门槛（元）
_DEF_PROMO_FIRST_TOPUP_MAX_POINTS: int = _int("BFF_PROMO_FIRST_TOPUP_MAX_POINTS", 1000000)
_DEF_PROMO_TITLE: str = os.getenv("BFF_PROMO_TITLE", "新用户首充翻倍")

# 首充记录持久化文件（BFF 无 DB，用本地 JSON 保证赠送幂等）
PROMO_STATE_FILE: str = os.getenv(
    "BFF_PROMO_STATE_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "promo_state.json"),
)

# 充值档位（元）
_DEF_PAY_AMOUNTS: tuple = tuple(
    int(x) for x in os.getenv("BFF_PAY_AMOUNTS", "10,30,50,100,300,500").split(",") if x.strip()
)


# ==================== 兑换码登录 ====================
# 是否开放「兑换码登录」入口（关掉后登录页不显示该 Tab，接口也直接拒绝）
_DEF_REDEEM_LOGIN_ENABLED: bool = _bool("BFF_REDEEM_LOGIN_ENABLED", True)

# 兑换码校验的分页遍历上限（100 条/页）。
# new-api 的 search 不匹配 key 字段，只能翻列表比对，码量大时需调高此值。
REDEMPTION_MAX_PAGES: int = _int("BFF_REDEMPTION_MAX_PAGES", 50)

# 兑换码格式白名单（正则）。new-api 默认生成 32 位 hex，
# 这里放宽到允许用户带横杠/下划线抄写。改用自定义码时需同步调整。
REDEEM_CODE_PATTERN: str = os.getenv("BFF_REDEEM_CODE_PATTERN", r"^[A-Za-z0-9\-_]{8,64}$")


# ==================== 品牌与站点配置 ====================
# 全部支持环境变量覆盖 + 管理员在线修改 —— 换品牌、换域名、换模型不需要改代码。
_DEF_BRAND_NAME: str = os.getenv("BFF_BRAND_NAME", "NexusAPI")
# Logo 方块里的字母；留空则自动取品牌名首字符
_DEF_BRAND_LOGO_TEXT: str = os.getenv("BFF_BRAND_LOGO_TEXT", "").strip()
_DEF_BRAND_TAGLINE: str = os.getenv("BFF_BRAND_TAGLINE", "登录以管理你的 API 额度与密钥")
_DEF_BRAND_HERO_TITLE: str = os.getenv("BFF_BRAND_HERO_TITLE", "一个 Key，接入全部主流大模型")
# 首页大标题拆两段：前半普通色 + 后半渐变高亮
_DEF_BRAND_HERO_H1: str = os.getenv("BFF_BRAND_HERO_H1", "一个 API Key")
_DEF_BRAND_HERO_H1_ACCENT: str = os.getenv("BFF_BRAND_HERO_H1_ACCENT", "所有主流大模型")
_DEF_BRAND_HERO_H1_PREFIX: str = os.getenv("BFF_BRAND_HERO_H1_PREFIX", "接入")
_DEF_BRAND_HERO_SUB: str = os.getenv(
    "BFF_BRAND_HERO_SUB",
    "GPT、Claude、DeepSeek、Qwen…统一 OpenAI 兼容接口，计费透明可控。",
)
_DEF_BRAND_HERO_BADGE: str = os.getenv("BFF_BRAND_HERO_BADGE", "全线模型在线 · 99.9% 可用性")
_DEF_BRAND_ICP: str = os.getenv("BFF_BRAND_ICP", "").strip()          # 备案号，留空不显示
_DEF_BRAND_CONTACT: str = os.getenv("BFF_BRAND_CONTACT", "").strip()  # 客服联系方式

# 教程页 / 代码示例参数
_DEF_API_BASE_URL: str = os.getenv("BFF_API_BASE_URL", NEWAPI_BASE_URL.rstrip("/") + "/v1")
_DEF_DOC_DEFAULT_MODEL: str = os.getenv("BFF_DOC_DEFAULT_MODEL", "gpt-4o-mini")
# 首页/文档页展示的模型清单（逗号分隔）
_DEF_DOC_MODELS: tuple = tuple(
    m.strip() for m in os.getenv(
        "BFF_DOC_MODELS",
        "gpt-4o,gpt-4o-mini,claude-sonnet-4,deepseek-chat,gemini-2.0-flash",
    ).split(",") if m.strip()
)


def brand_logo_text() -> str:
    """Logo 字母：显式配置优先，否则取品牌名首字符。"""
    name = _dyn("BRAND_NAME")
    return _dyn("BRAND_LOGO_TEXT") or (name[:1].upper() if name else "A")


# 镜像版本：Dockerfile 把构建期的 APP_VERSION 转成运行时的 BFF_VERSION。
# 放在 config 而非 main，是因为健康检查与 Logfire 上报都要用同一个值 ——
# 两处各读一次环境变量早晚会漂移成两个不同的版本号。
APP_VERSION: str = os.getenv("BFF_VERSION", "dev")


# ==================== 可观测性（Logfire）====================
# token 留空 = 完全关闭，连 SDK 都不导入。可观测性是运维增强，不该成为
# 本地开发和测试的前置依赖 —— 没配 token 就不该有任何行为差异。
LOGFIRE_TOKEN: str = os.getenv("LOGFIRE_TOKEN", "").strip()
LOGFIRE_ENVIRONMENT: str = os.getenv("LOGFIRE_ENVIRONMENT", "local").strip()
LOGFIRE_ENABLED: bool = bool(LOGFIRE_TOKEN)


# ==================== 动态配置解析（运行时覆盖）====================
# 上面带 _DEF_ 前缀的都是「环境变量默认值」。真实取值经这里解析：
# **管理员在线覆盖 > 环境变量 / .env > 代码默认值**。
#
# ## 为什么用模块级 __getattr__（PEP 562）而不是把每处都改成 getter
#
# 全仓库读配置的写法统一是 `from . import config` + `config.XXX`（已逐一核对
# store / promo / main / security / newapi_client / redeem_code 六个模块），
# 没有任何一处 `from .config import XXX`。这意味着只要拦住属性访问，
# 50+ 个调用点全部自动变成动态读取，无需改动业务代码 ——
# 改法越小，出错面越小，这是刻意选择。
#
# 反之若改成 brand_name() 之类的 getter，就要同步改 main.py 里几十处引用、
# promo.py 的活动计算、以及全部现存测试的断言，收益相同而风险高得多。
#
# 注意：__getattr__ 只在「属性不存在于模块命名空间」时触发，所以可覆盖项
# 必须以 _DEF_ 前缀命名，不能同时存在同名模块级常量，否则永远读不到覆盖值。
def _dyn(name: str):
    """取动态配置值。供本模块内部函数使用（模块内的全局查找不走 __getattr__）。"""
    default = globals()[f"_DEF_{name}"]
    try:
        from . import settings
    except ImportError:      # 极早期导入阶段的保护，正常运行不会走到
        return default
    value = settings.get(name, default)
    # 环境变量默认值是 tuple（PAY_AMOUNTS / DOC_MODELS），而 JSON 里存的是 list。
    # 统一成 tuple，避免调用方对 `in` 之外的行为（如可哈希性）产生分歧。
    if isinstance(default, tuple) and isinstance(value, list):
        return tuple(value)
    return value


# 可动态覆盖的键集合。以 _DEF_ 常量为唯一事实源自动推导，
# 避免新增字段时忘记同步登记（漏登记的表现是「页面能改、后端不认」）。
DYNAMIC_KEYS: frozenset = frozenset(
    k[len("_DEF_"):] for k in tuple(globals()) if k.startswith("_DEF_")
)


def __getattr__(name: str):
    """模块级属性兜底：把可覆盖项的读取转向运行时配置。"""
    if name in DYNAMIC_KEYS:
        return _dyn(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def defaults() -> dict:
    """各可覆盖项的环境变量默认值。管理页用它显示「重置后会变成什么」。"""
    return {k: globals()[f"_DEF_{k}"] for k in DYNAMIC_KEYS}
