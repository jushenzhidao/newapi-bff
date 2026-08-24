# 兑换码登录 + 站点动态配置 — 交付说明

用户拿到一张 new-api 兑换码即可直接开用：不注册、不填邮箱、不设密码。
输码即开通账号并到账，随时可升级为正式账号。
品牌、图标、接口地址、教程示例、登录 Tab 全部走配置下发，换皮不改代码。

---

## 一、本轮修复的严重缺陷：兑换码不能伪造

**问题**：早期实现允许任意合法格式的字符串走到「建号」这一步，靠核销失败再删号
回滚兜底。这是事后补救——存在真实开出过账号的时间窗；更糟的是 mock 模式下
随便编一串字符就能登录成功，直接传达了「兑换码可以自己造」的错误认知。

**修复**：把校验前置到建号之前。

```
① 校验码    find_redemption(code) → 必须存在且 status=1（未使用）
② 建号      派生账号 + 管理员影子建号
③ 核销到账  POST /api/user/topup
```

伪造码在 ① 就被拦下，连 new-api 的用户表都碰不到。

**实现难点**：`GET /api/redemption/search?keyword=<完整key>` 返回 `total=0` ——
keyword **只匹配 name/id，不匹配 key 字段**（实测）。所以 `find_redemption`
只能走 `GET /api/redemption/` 分页遍历精确比对 key，上限由
`BFF_REDEMPTION_MAX_PAGES`（默认 50 页 × 100 条）控制。

**校验不了就拒绝，不放行**：管理员凭证不可用（比如被会话上限锁死）时，
`_assert_redeemable` 抛 503「兑换码校验失败，请稍后重试」。宁可让用户等，
也不能在校验失效的状态下开闸建号。

错误分级：

| 情况 | 提示 |
|---|---|
| 码不在 new-api 里 | 兑换码不存在，请检查后重试 |
| 码已被核销 | 该兑换码已被使用 |
| 码被禁用等其他状态 | 该兑换码当前不可用 |
| 管理员凭证不可用 | 兑换码校验失败，请稍后重试 |

**mock 与真实语义完全一致**：`store.py` 内置对齐 new-api `redemptions` 表语义的
卡池，启动时预置 3 张演示卡（¥1 / ¥5 / ¥100），登录页直接展示卡号。
mock 下随便编的码同样返回「兑换码不存在」。发新卡走
`POST /api/mock/redemption`（仅 mock 模式开放）。

原来的「核销失败删号回滚」保留为最后一道兜底，应对「校验通过后被他人抢用」
这种极端并发。

---

## 二、站点动态配置：前端零硬编码

原先品牌名 `NexusAPI` 在前端出现 9 处、logo「N」4 处、`base_url` 6 处、
模型名 4 处，换个客户就得全局搜索替换。现在全部由 `GET /api/config` 下发。

```json
{
  "brand":    { "name", "logo_text", "tagline", "hero_h1", "hero_h1_prefix",
                "hero_h1_accent", "hero_sub", "hero_badge", "icp", "contact" },
  "api":      { "base_url", "default_model", "models" },
  "features": { "redeem_login", "mock_mode" },
  "demo_codes": []
}
```

前端 `SITE` 全局对象承接，取值走 `BRAND()` / `LOGO()` / `APIBASE()` / `MODEL()`
四个捷径；`applyBrand()` 同步更新 `document.title` 和内联 SVG favicon；
路由里 `Promise.all([loadPromo(), loadSite()])` 并行加载，不增加首屏延迟。

覆盖范围：导航栏 / 登录卡 / 首页 hero 三行文案 + 胶囊标签 / 页脚 ICP 与联系方式 /
教程页接口地址 / curl、Python、Node 三段代码示例的 base_url 与 model /
API Key 页提示 / 浏览器标题与图标。

**功能开关也动态**：`BFF_REDEEM_LOGIN_ENABLED=0` 时登录页兑换码 Tab 直接消失
（事件绑定加 `?.` 防护），后端接口返回「兑换码登录未开放」，前后端同步生效。

换肤实测（已截图验收）：

```bash
BFF_BRAND_NAME="智算云" BFF_BRAND_LOGO_TEXT="智" \
BFF_BRAND_HERO_H1_PREFIX="一站接入" BFF_BRAND_HERO_H1_ACCENT="国产大模型" \
BFF_API_BASE_URL="https://api.zhisuan.cloud/v1" \
BFF_DOC_DEFAULT_MODEL="deepseek-chat" \
BFF_REDEEM_LOGIN_ENABLED=0 \
.venv/bin/uvicorn app.main:app --port 8300
```

结果：全站文案、logo、favicon、教程页三段代码示例同步变化，兑换码 Tab 消失。

---

## 三、为什么账号是「确定性派生」

不建映射表、也无法按码反查（search 不匹配 key），所以用兑换码算出固定账号：

```
username = "rc_" + HMAC(SECRET_KEY, normalize(code))[:16]
password =        HMAC(SECRET_KEY, normalize(code) + "|pwd")[:20]
```

- `normalize` 去横杠、转小写 —— `4EE5-EE69-…` 与 `4ee5ee69…` 进同一个账号
- 用 HMAC 而非明文散列：拿到用户名也反推不出兑换码（除非有 `SECRET_KEY`）
- 密码额外加盐 —— 用户名会出现在管理后台用户列表，泄露了也推不出密码

> **20 位不是随便取的**：上游对密码有 max 长度校验，实测 24 位报
> `failed on the 'max' tag`，20 位通过。80 bit 熵远超暴力破解阈值。

**绑定升级**：兑换码用户可在仪表盘设置正式用户名密码，走 `PUT /api/user/`，
uid 不变，余额 / API Key / 调用日志全部保留。禁止占用 `rc_` 前缀防止抢注。

---

## 四、途中挖出的一个高危缺陷（会话上限）

测试期间管理员账号突然**再也登不进去**，返回 `409 AUTH_SESSION_LIMIT`。查源码：

```go
// service/auth_session.go
if activeCount >= int64(common.UserSessionActiveLimit) {   // 默认 50
    return nil, model.ErrUserSessionLimit                   // 硬拒绝，不淘汰最旧会话
}
```

每次 `login` 新建一条会话，TTL **30 天**，无管理员豁免。BFF 只要 PAT、根本不用会话，
却每次登录都留一条 —— 任何账号登满 50 次就被锁死 30 天。**管理员一旦中招，
建号 / 加额度 / 兑换码全部瘫痪。** 更麻烦的是会话管理接口要求真实 dashboard
会话上下文，PAT 调用直接 403 ——**登不进去就等于没法用 API 自救**。

三重防护：

1. **借了就还** —— 换到 PAT 后立刻 `DELETE /api/user/sessions/{sid}`。
   实测删会话后 PAT 依然有效（PAT 走 `users.access_token` 列，不经过会话系统）。
   **验证：连续 10 次登录，活跃会话净增 0。**
2. **PAT 直供** —— 配置 `NEWAPI_ADMIN_PAT` + `NEWAPI_ADMIN_UID` 后 BFF 完全不调 `login`。
   **生产强烈建议配置。**
3. **落盘缓存** —— PAT 存 `data/admin_cred.json`（0600），进程重启不消耗配额。

---

## 五、顺手修掉的问题

| 问题 | 影响 |
|---|---|
| 静态资源无版本号、无缓存头 | 发版后用户一直用缓存的旧 `app.js`。已加内容哈希 `?v=<md5[:8]>` |
| 侧栏泄露 `rc_xxx@example.com` | 把内部派生用户名暴露给用户。后端已过滤占位邮箱 |
| CSS 变量 `--text-1` 未定义（2 处） | 金额文字回退成浏览器默认色。统一为 `--text` |
| 3 处 emoji 当功能图标（庆祝/礼物类） | 违反设计规范，全部替换为 SVG |
| mock 充值随机生成面额 | 与真实语义不符。改为走卡池 `use_redemption` |
| 登录页列出全部演示码（含测试卡） | 界面被撑长。预置卡打 `is_preset` 标记，只展示未使用的预置卡 |

---

## 六、验证结果

| 测试 | 结果 |
|---|---|
| Mock E2E（`scripts/e2e_redeem_mock.py`） | **30 / 30**，可对同一服务反复执行 |
| 真实环境 E2E（`scripts/e2e_redeem_login.py`） | 38 项断言，**待管理员恢复后执行** |
| 会话归还 | 连续 10 次登录**净增 0** |
| emoji 门禁 / CSS 变量校验 / JS 语法 | 0 违规 / 0 未定义 / 通过 |
| 浏览器实测（默认品牌） | 伪造码被拒、¥100 卡到账 1,000,000 积分、绑定横幅正常 |
| 浏览器实测（自定义品牌） | 首页 / 登录页 / 教程页全部走动态配置，兑换码 Tab 按开关隐藏 |

新增覆盖场景：伪造码拒绝（含 3 种"看起来很像"的变体）、充值页伪造码拒绝、
同卡不能重复充值、`/api/config` 下发校验、`find_redemption` 直连正确性
（避免"因为查不到所以全拒"这种假通过）。

---

## 七、部署须知

```bash
# 生产强烈建议配置，让 BFF 彻底摆脱对 login 的依赖
export NEWAPI_ADMIN_PAT='<管理员 PAT>'   # 前端「个人设置 → 系统访问令牌」生成
export NEWAPI_ADMIN_UID=<管理员 uid>
export BFF_SECRET_KEY='<随机值>'          # 注意：兑换码派生依赖它，一旦更换所有兑换码账号将无法登录
```

> **`BFF_SECRET_KEY` 必须固定且备份。** 派生算法依赖它，换密钥等于所有兑换码账号全部失联。

兑换码校验依赖管理员凭证，**管理员不可用时兑换码登录会整体不可用**（设计如此，
拒绝优于放行）。上线前务必确认 PAT 已配置且有效。

---

## 待处理

**管理员账号 `chatfire` 目前仍处于锁定状态**（压测会话归还方案时打满的）。
该实例只开启了密码登录，未开邮箱找回 / OAuth / Passkey，无法从前端自助恢复。

两条恢复路径：

- 用另一个 root/管理员账号，在用户管理里编辑该账号并提交更大的 `auth_version`
  （`UpdateUser` 会触发 `RevokeAllUserSessions`）
- 直接改库，语句见 `fix_session_limit.py --sql`

受此阻塞，`find_redemption` 与真实环境 E2E 尚未在线上实测——逻辑已按已核实契约
编写并在 mock 层验证等价行为。恢复后请立刻配置 `NEWAPI_ADMIN_PAT` 防止复发；
代码层面的防护已就位，新代码不会再累积会话。

---

## 变更文件

```
新增  app/redeem_code.py              前置校验 / 派生 / 建号核销 / 绑定升级
新增  scripts/e2e_redeem_login.py     真实环境 E2E（38 项）
新增  scripts/e2e_redeem_mock.py      mock E2E（30 项，可反复跑）
新增  scripts/fix_session_limit.py    会话上限诊断与恢复
新增  scripts/probe_*.py              上游契约探测（结论已写进客户端头注释）
改动  app/newapi_client.py            find_redemption 分页查码 / 会话归还 / PAT 三级凭证链
改动  app/main.py                     /api/config 下发 / mock 发卡端点 / 登录绑定端点
改动  app/config.py                   兑换码开关与正则 / 品牌与站点配置块
改动  app/store.py                    mock 兑换码卡池（对齐 redemptions 表语义）
改动  static/app.js                   SITE 配置层 / 品牌硬编码清零 / Tab 开关控制
改动  static/style.css                Tab、横幅、说明条样式 / CSS 变量修正
改动  README.md                       兑换码前置校验 + 动态配置 + 会话上限专章
```
