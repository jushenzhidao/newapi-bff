"""BFF 配置。

MOCK_MODE=0（默认）走真实 new-api；BFF_MOCK_MODE=1 走内存 mock 演示。
生产环境务必用环境变量注入 BFF_SECRET_KEY / 管理员账密。
"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


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

# 会话 Cookie 签名密钥 —— 生产必须用环境变量注入随机值
SECRET_KEY: str = os.getenv("BFF_SECRET_KEY", "dev-only-secret-change-me")

COOKIE_NAME = "bff_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天；PAT 长期有效，不受 new-api 15min access_token 限制


# ==================== 积分体系（对外唯一计价单位）====================
# new-api 内部余额单位是 quota，1 元 = 500000 quota（其 QuotaPerUnit 默认值，不要改）。
# 对外我们只讲「积分」：1 元 = POINTS_PER_CNY 积分。
# 换算链：quota → points = quota / QUOTA_PER_CNY * POINTS_PER_CNY
QUOTA_PER_CNY: int = _int("BFF_QUOTA_PER_CNY", 500000)      # new-api 内部口径
POINTS_PER_CNY: int = _int("BFF_POINTS_PER_CNY", 10000)     # 1 元 = 10000 积分（可配）
POINTS_UNIT_NAME: str = os.getenv("BFF_POINTS_UNIT_NAME", "积分")

# 1 积分 = 多少 quota（用于积分→quota 反算，如赠送积分调 add_quota）
QUOTA_PER_POINT: float = QUOTA_PER_CNY / POINTS_PER_CNY if POINTS_PER_CNY else 0.0


def quota_to_points(quota) -> int:
    """内部 quota → 对外积分（向下取整，绝不虚报余额）。"""
    if not QUOTA_PER_CNY:
        return 0
    return int(float(quota or 0) / QUOTA_PER_CNY * POINTS_PER_CNY)


def points_to_quota(points) -> int:
    """对外积分 → 内部 quota（四舍五入，赠送场景宁可多给一点点）。"""
    return int(round(float(points or 0) * QUOTA_PER_POINT))


def cny_to_points(cny) -> int:
    return int(round(float(cny or 0) * POINTS_PER_CNY))


# ==================== 运营活动 ====================
# 注册礼包：新用户注册即赠送积分（0 表示关闭）
PROMO_SIGNUP_ENABLED: bool = _bool("BFF_PROMO_SIGNUP_ENABLED", True)
PROMO_SIGNUP_POINTS: int = _int("BFF_PROMO_SIGNUP_POINTS", 20000)  # 送 2 万积分（=¥2）

# 首充活动：用户首次充值成功后，按比例额外赠送积分
PROMO_FIRST_TOPUP_ENABLED: bool = _bool("BFF_PROMO_FIRST_TOPUP_ENABLED", True)
PROMO_FIRST_TOPUP_RATE: float = float(os.getenv("BFF_PROMO_FIRST_TOPUP_RATE", "1.0"))  # 1.0 = 充多少送多少
PROMO_FIRST_TOPUP_MIN_CNY: int = _int("BFF_PROMO_FIRST_TOPUP_MIN_CNY", 10)   # 起充门槛（元）
PROMO_FIRST_TOPUP_MAX_POINTS: int = _int("BFF_PROMO_FIRST_TOPUP_MAX_POINTS", 1000000)  # 赠送上限（100万积分=¥100）
PROMO_TITLE: str = os.getenv("BFF_PROMO_TITLE", "新用户首充翻倍")

# 首充记录持久化文件（BFF 无 DB，用本地 JSON 保证赠送幂等）
PROMO_STATE_FILE: str = os.getenv(
    "BFF_PROMO_STATE_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "promo_state.json"),
)

# 充值档位（元）
PAY_AMOUNTS: tuple = tuple(
    int(x) for x in os.getenv("BFF_PAY_AMOUNTS", "10,30,50,100,300,500").split(",") if x.strip()
)


# ==================== 兑换码登录 ====================
# 是否开放「兑换码登录」入口（关掉后登录页不显示该 Tab，接口也直接拒绝）
REDEEM_LOGIN_ENABLED: bool = _bool("BFF_REDEEM_LOGIN_ENABLED", True)

# 兑换码校验的分页遍历上限（100 条/页）。
# new-api 的 search 不匹配 key 字段，只能翻列表比对，码量大时需调高此值。
REDEMPTION_MAX_PAGES: int = _int("BFF_REDEMPTION_MAX_PAGES", 50)

# 兑换码格式白名单（正则）。new-api 默认生成 32 位 hex，
# 这里放宽到允许用户带横杠/下划线抄写。改用自定义码时需同步调整。
REDEEM_CODE_PATTERN: str = os.getenv("BFF_REDEEM_CODE_PATTERN", r"^[A-Za-z0-9\-_]{8,64}$")


# ==================== 品牌与站点配置 ====================
# 全部支持环境变量覆盖 —— 换品牌、换域名、换模型不需要改代码。
BRAND_NAME: str = os.getenv("BFF_BRAND_NAME", "NexusAPI")
# Logo 方块里的字母；留空则自动取品牌名首字符
BRAND_LOGO_TEXT: str = os.getenv("BFF_BRAND_LOGO_TEXT", "").strip()
BRAND_TAGLINE: str = os.getenv("BFF_BRAND_TAGLINE", "登录以管理你的 API 额度与密钥")
BRAND_HERO_TITLE: str = os.getenv("BFF_BRAND_HERO_TITLE", "一个 Key，接入全部主流大模型")
# 首页大标题拆两段：前半普通色 + 后半渐变高亮
BRAND_HERO_H1: str = os.getenv("BFF_BRAND_HERO_H1", "一个 API Key")
BRAND_HERO_H1_ACCENT: str = os.getenv("BFF_BRAND_HERO_H1_ACCENT", "所有主流大模型")
BRAND_HERO_H1_PREFIX: str = os.getenv("BFF_BRAND_HERO_H1_PREFIX", "接入")
BRAND_HERO_SUB: str = os.getenv(
    "BFF_BRAND_HERO_SUB",
    "GPT、Claude、DeepSeek、Qwen…统一 OpenAI 兼容接口，计费透明可控。",
)
BRAND_HERO_BADGE: str = os.getenv("BFF_BRAND_HERO_BADGE", "全线模型在线 · 99.9% 可用性")
BRAND_ICP: str = os.getenv("BFF_BRAND_ICP", "").strip()          # 备案号，留空不显示
BRAND_CONTACT: str = os.getenv("BFF_BRAND_CONTACT", "").strip()  # 客服联系方式

# 教程页 / 代码示例参数
API_BASE_URL: str = os.getenv("BFF_API_BASE_URL", NEWAPI_BASE_URL.rstrip("/") + "/v1")
DOC_DEFAULT_MODEL: str = os.getenv("BFF_DOC_DEFAULT_MODEL", "gpt-4o-mini")
# 首页/文档页展示的模型清单（逗号分隔）
DOC_MODELS: tuple = tuple(
    m.strip() for m in os.getenv(
        "BFF_DOC_MODELS",
        "gpt-4o,gpt-4o-mini,claude-sonnet-4,deepseek-chat,gemini-2.0-flash",
    ).split(",") if m.strip()
)


def brand_logo_text() -> str:
    """Logo 字母：显式配置优先，否则取品牌名首字符。"""
    return BRAND_LOGO_TEXT or (BRAND_NAME[:1].upper() if BRAND_NAME else "A")
