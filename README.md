# newapi-bff

修订版方案二实现：前端 SPA + Python BFF，代理真实 new-api（默认）或内存 mock 演示。
对外统一以「**积分**」计价，换算比例与运营活动全部可配。

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8300
# 打开 http://127.0.0.1:8300
```

生产部署（Docker Compose + CI/CD）见 [DEPLOY.md](DEPLOY.md)。

## 积分体系

new-api 内部余额单位是 `quota`（1 元 = 500000 quota），这个口径对用户毫无意义。
BFF 在**所有出口**把 quota 换算成积分，前端拿不到也不需要 quota。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BFF_POINTS_PER_CNY` | `10000` | 1 元 = 多少积分 |
| `BFF_QUOTA_PER_CNY` | `500000` | new-api 内部口径，除非改了 new-api 的 QuotaPerUnit 否则别动 |
| `BFF_POINTS_UNIT_NAME` | `积分` | 单位名称，改成「点数」「Token 币」都行，前端跟着变 |
| `BFF_PAY_AMOUNTS` | `10,30,50,100,300,500` | 充值档位（元） |

换算函数集中在 `config.py`：`quota_to_points` / `points_to_quota` / `cny_to_points`。
**新增接口时若返回金额，必须走 `quota_to_points` 转换后再命名为 `points`**，不要把裸 quota 泄露给前端。

## 运营活动

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BFF_PROMO_SIGNUP_ENABLED` | `1` | 注册礼包开关 |
| `BFF_PROMO_SIGNUP_POINTS` | `20000` | 注册赠送积分（=¥2） |
| `BFF_PROMO_FIRST_TOPUP_ENABLED` | `1` | 首充活动开关 |
| `BFF_PROMO_FIRST_TOPUP_RATE` | `1.0` | 赠送比例，1.0 = 充多少送多少 |
| `BFF_PROMO_FIRST_TOPUP_MIN_CNY` | `10` | 参与门槛（元） |
| `BFF_PROMO_FIRST_TOPUP_MAX_POINTS` | `1000000` | 单次赠送上限 |
| `BFF_PROMO_TITLE` | `新用户首充翻倍` | 活动名称，前端横幅展示 |
| `BFF_PROMO_STATE_FILE` | `data/promo_state.json` | 领取记录（保证幂等） |

赠送通过 new-api 管理员接口落账：`POST /api/user/manage {id, action:"add_quota", mode:"add", value}`。
该接口**没有幂等键**，重复调用会重复加钱 —— 幂等完全由 `promo.py` 的状态文件保证（先占位写盘、发放失败回滚）。
`GET /api/promo` 把活动配置下发给前端，前端不硬编码任何比例。

> 多实例部署时状态文件不共享，需换成 Redis/DB 存 `promo_state`，否则赠送可能重复发放。

## 功能

| 页面 | 说明 |
|---|---|
| 产品首页 | AI 科技暗色风，活动条 + 积分价格表 |
| 登录 / 注册 | 真实账密；注册 = 管理员影子建号 + 自动登录 + 注册礼包 |
| 兑换码登录 | 无需注册，一张卡直接开用；可随时绑定为正式账号 |
| 仪表盘 | 可用积分、累计消耗、调用次数、首充横幅、最近调用 |
| 用量看板 | 近 7 天调用趋势、模型积分消耗排行 |
| 充值中心 | 档位显示到账积分与首充赠送、实时合计、真实易支付跳转 + 到账轮询；兑换码 |
| API Key | 创建（明文一次性展示）/ 查看明文 / 复制 / 删除 |
| 调用日志 | 统计卡片 + 分页表格，全部积分口径 |
| 使用教程 | 快速开始、代码示例、积分计费说明、FAQ |

## 兑换码登录

用户拿到一张 new-api 兑换码即可直接使用，不必注册、不必填邮箱。

**难点**：new-api 的 `GET /api/redemption/search` 的 keyword **不匹配 key 字段**（实测只匹配 name/id），
所以无法按兑换码反查它绑定了哪个账号 —— 「查表找回账号」这条路走不通。

**做法**：确定性派生。用兑换码算出一个固定的影子账号，不存任何映射表。

```
username = "rc_" + HMAC(SECRET_KEY, normalize(code))[:16]
password =        HMAC(SECRET_KEY, normalize(code) + "|pwd")[:20]
```

- `normalize` 去横杠、转小写 —— `4EE5-EE69-...` 和 `4ee5ee69...` 进同一个账号
- 用 HMAC 而非明文散列：没有 `SECRET_KEY` 就无法从用户名反推兑换码
- 密码另加盐，即使用户名泄露（它会出现在管理后台用户列表）也推不出密码
- 密码取 20 位是**上游硬约束**：实测 24 位报 `failed on the 'max' tag`，20 位通过

### 兑换码必须是管理员真实创建的

**这条是安全底线**：能登录的兑换码只能来自 new-api 管理后台创建的卡，用户自己编一串
合法格式的字符串**不能**开出账号。

早期实现把校验交给「核销」这一步 —— 先建号、核销失败再删号回滚。这属于事后补救，
存在一个真实开出过账号的时间窗，而且 mock 模式下任意字符串都能登录，会给人
「兑换码可以随便造」的错误认知。现已改为**前置校验**：

```
① 校验码   find_redemption(code) → 必须存在且 status=1（未使用）
② 建号     派生账号 + 管理员影子建号
③ 核销到账 POST /api/user/topup
```

伪造码在 ① 就被拦下，连 new-api 的用户表都碰不到。

**为什么要遍历分页而不是用 search**：`GET /api/redemption/search?keyword=<完整key>`
返回 `total=0` —— keyword 只匹配 name/id，**不匹配 key 字段**（实测）。
因此 `find_redemption` 走 `GET /api/redemption/` 分页遍历精确比对 key，
上限由 `BFF_REDEMPTION_MAX_PAGES`（默认 50 页 × 100 条）控制。

**管理员凭证不可用时拒绝而非放行**：若管理员被会话上限锁死导致查不了卡，
`_assert_redeemable` 抛 503「兑换码校验失败，请稍后重试」。宁可让用户等，
也不能在校验失效的情况下开闸建号。

错误分级（前端直接展示）：

| 情况 | 提示 |
|---|---|
| 码不在 new-api 里 | 兑换码不存在，请检查后重试 |
| 码已被核销 | 该兑换码已被使用 |
| 码被禁用等其他状态 | 该兑换码当前不可用 |
| 管理员凭证不可用 | 兑换码校验失败，请稍后重试 |

**流程**：
1. 派生账号已存在 → 直接登录（码早已核销，不重复到账）
2. 不存在 → **先校验码** → 建号 → 立刻核销兑换码 → 到账
3. 校验不通过 → 直接拒绝，不建号
4. 核销失败（极端并发：校验后被他人抢用）→ **立即删号回滚**，作为最后一道兜底

**绑定升级**：兑换码用户可在仪表盘设置正式用户名密码，走 `PUT /api/user/`，
uid 不变，余额 / API Key / 调用日志全部保留。禁止占用 `rc_` 前缀防止抢注。

## 品牌与站点动态配置

前端**不硬编码任何品牌信息、接口地址或模型名**，全部由 `GET /api/config` 下发，
换个环境变量就能换一套皮，不用改一行代码。

```json
{
  "brand":    { "name", "logo_text", "tagline", "hero_h1", "hero_h1_prefix",
                "hero_h1_accent", "hero_sub", "hero_badge", "icp", "contact" },
  "api":      { "base_url", "default_model", "models" },
  "features": { "redeem_login", "mock_mode" },
  "demo_codes": []
}
```

| 环境变量 | 默认值 | 作用位置 |
|---|---|---|
| `BFF_BRAND_NAME` | `NexusAPI` | 导航栏、登录卡、页脚、浏览器标题 |
| `BFF_BRAND_LOGO_TEXT` | 取品牌名首字 | 方形 logo 与动态 favicon |
| `BFF_BRAND_TAGLINE` | — | 登录卡副标题 |
| `BFF_BRAND_HERO_H1_PREFIX` / `_ACCENT` | — | 首页主标题第二行（前缀 + 高亮词） |
| `BFF_BRAND_HERO_SUB` / `_BADGE` | — | 首页副文案、顶部胶囊标签 |
| `BFF_BRAND_ICP` / `BFF_BRAND_CONTACT` | 空 | 页脚备案号与联系方式（留空则不渲染） |
| `BFF_API_BASE_URL` | `/v1` | 教程页接口地址、三段代码示例、API Key 页提示 |
| `BFF_DOC_DEFAULT_MODEL` | `gpt-4o-mini` | 代码示例里的 model 字段 |
| `BFF_DOC_MODELS` | 逗号分隔 | 模型列表展示 |
| `BFF_REDEEM_LOGIN_ENABLED` | `1` | 关闭后登录页兑换码 Tab 消失，接口返回「兑换码登录未开放」 |
| `BFF_REDEEM_CODE_PATTERN` | `^[A-Za-z0-9\-_]{8,64}$` | 兑换码格式校验正则 |

前端 `SITE` 全局对象承接配置，取值走 `BRAND()` / `LOGO()` / `APIBASE()` / `MODEL()` 四个捷径，
`applyBrand()` 同步更新 `document.title` 和内联 SVG favicon。

验证过的换肤示例：

```bash
BFF_BRAND_NAME="智算云" BFF_BRAND_LOGO_TEXT="智" \
BFF_API_BASE_URL="https://api.zhisuan.cloud/v1" \
BFF_DOC_DEFAULT_MODEL="deepseek-chat" \
BFF_REDEEM_LOGIN_ENABLED=0 \
.venv/bin/uvicorn app.main:app --port 8300
```

全站文案、logo、favicon、教程页三段代码示例同步变化，兑换码 Tab 自动隐藏。

> 静态资源用内容哈希版本号 `?v=<md5[:8]>`，改完配置刷新即可，不会被浏览器缓存卡住。

## 会话上限（最高危的坑）

new-api 每次 `POST /api/user/login` 都新建一条 `UserSession`：

```go
// service/auth_session.go
if activeCount >= int64(common.UserSessionActiveLimit) {   // 默认 50
    return nil, model.ErrUserSessionLimit                   // 硬拒绝，不淘汰最旧会话
}
```

会话 TTL **30 天**，无管理员豁免。BFF 只需要 PAT、根本不用会话，
若登录后放着不管，同一账号登满 50 次就返回 `409 AUTH_SESSION_LIMIT` 且 **30 天内无法再登录**。
管理员账号首当其冲 —— 一旦锁死，建号 / 加额度 / 兑换码全部瘫痪。
更糟的是会话管理接口要求真实 dashboard 会话上下文（PAT 调用返回 403），
**登不进去就等于没法用 API 自救**。（开发期已实测踩中此坑。）

三重防护：

1. **借了就还** —— `login()` 换到 PAT 后立刻 `DELETE /api/user/sessions/{sid}`。
   实测删会话后 PAT 依然有效（PAT 走 `users.access_token` 列，不经过会话系统）。
   验证结果：连续 10 次登录，活跃会话**净增 0**。
2. **PAT 直供** —— 配置下面两个环境变量后，BFF 完全不调 `login`，从根上不消耗会话配额：

   | 环境变量 | 说明 |
   |---|---|
   | `NEWAPI_ADMIN_PAT` | 管理员 PAT（前端「个人设置 → 系统访问令牌」生成） |
   | `NEWAPI_ADMIN_UID` | 管理员 uid |

   **生产强烈建议配置。** 注意在前端再点一次「系统访问令牌」会作废旧值。
3. **落盘缓存** —— PAT 存 `data/admin_cred.json`（0600），进程重启不再消耗会话配额。

已经被锁了？运行诊断工具，它会检测实例开启了哪些登录方式并给出对应恢复方案：

```bash
.venv/bin/python scripts/fix_session_limit.py         # 诊断 + 方案
.venv/bin/python scripts/fix_session_limit.py --sql   # 无其他登录方式时的 DB 修复语句
```

## 关键实现点

- **PAT 而非 access_token**：登录后立刻用 15 分钟 access_token 换长期 PAT，
  存进 **AES-256-GCM 加密**的 Cookie（不是仅签名 —— 签名的载荷是 base64 明文，
  谁拿到 Cookie 都能读出 PAT）。
  注意 `GET /api/user/token` 每次调用都会**重新生成 PAT 并作废旧值**，所以管理员 PAT 不能硬编码，
  由 `newapi_client._admin_login()` 动态换取并缓存，401 自动刷新重试。
- **支付到账判定**：`POST /api/user/pay/status` 用「下单时余额快照」比对。
  只判断"余额变多了"是不够的 —— 用户可能下单 ¥500 却用兑换码充 ¥1 来骗赠送，
  因此要求实际到账 ≥ 订单应到账的 90% 才认定成功并发放首充。
- **客户端 IP 转发**：new-api 的 CriticalRateLimit 以 `mark + ClientIP()` 计数
  （`middleware/rate-limit.go`）。BFF 出口只有一个 IP，不转发 `X-Forwarded-For` 的话，
  **一个用户狂点登录会把全站登录锁死十几分钟**（实测 Retry-After 长达 1007 秒）。
  已在 `na.login()` 转发真实 IP，需在 new-api 侧配置信任代理才生效。
- **429 友好提示**：解析 `Retry-After` 换算成"请 X 分钟后再试"，前端再锁按钮 10 秒防止雪上加霜。
- **支付回调不经过 BFF**：`/api/user/epay/notify` 由 Nginx 直通 new-api，验签由 new-api 自己做。
- 金额白名单校验、异常不泄露内部信息、共享 httpx 连接池。

## 目录

```
app/
  main.py           # 路由（对外只出 points）
  promo.py          # 注册礼包 / 首充赠送（文件幂等）
  redeem_code.py    # 兑换码登录：前置校验 + HMAC 确定性派生 + 建号核销
  newapi_client.py  # 真实代理 + 已核实契约笔记（含 find_redemption 分页查码）
  store.py          # mock 模式内存数据层
  security.py       # 加密 Cookie 会话（AES-256-GCM）
  config.py         # 积分换算 + 活动配置
static/
  index.html / app.js / style.css   # SPA（hash 路由，原生 JS）
data/
  promo_state.json  # 活动领取记录（运行时生成）
  admin_cred.json   # 管理员 PAT 缓存（0600，运行时生成）
scripts/
  e2e_redeem_login.py   # 兑换码登录真实环境 E2E（38 项断言）
  e2e_redeem_mock.py    # 兑换码登录 mock E2E（30 项，可离线反复跑）
  fix_session_limit.py  # 会话上限诊断与恢复
  probe_*.py            # 上游契约探测脚本（契约结论已写进 newapi_client 头注释）
```

## mock 模式

`BFF_MOCK_MODE=1` 走内存数据，无需 new-api，登录任意账密即可，支付走模拟收银台。
积分换算与活动逻辑与真实模式完全一致。

**兑换码在 mock 下同样不能伪造。** `store.py` 内置一个对齐 new-api `redemptions`
表语义的卡池，启动时预置 3 张演示卡（¥1 / ¥5 / ¥100），登录页直接展示卡号。
随便编的码一样返回「兑换码不存在」—— mock 与真实的语义必须一致，
否则 mock 就成了误导来源。

发新卡（仅 mock 模式可用，供演示与测试）：

```bash
curl -X POST localhost:8300/api/mock/redemption \
     -H 'Content-Type: application/json' -d '{"quota_cny": 20, "count": 2}'
```

离线跑全量回归：

```bash
BFF_MOCK_MODE=1 .venv/bin/uvicorn app.main:app --port 8301 &
.venv/bin/python scripts/e2e_redeem_mock.py     # 30/30，可反复执行
```
