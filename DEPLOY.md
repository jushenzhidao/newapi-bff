# 部署

容器化部署与 CI/CD 说明。本地开发直接跑 uvicorn 见 README。

## 快速开始

```bash
cp .env.example .env
openssl rand -hex 32          # 输出填进 .env 的 BFF_SECRET_KEY
vim .env                      # 填完下面「必填项」一节列出的全部变量

export APP_VERSION=$(git describe --tags --always) VCS_REF=$(git rev-parse --short HEAD)
docker compose up -d --build
curl -fsS localhost:8000/readyz    # 应返回 {"status":"ready",...}
```

`/readyz` 返回 503 就是配置有问题，`failed` 数组会指出具体哪一项。

## compose 是纯生产编排，没有降级开关

`docker-compose.yml` 刻意不提供任何兼容 / 降级选项：

| 项 | 处理 | 原因 |
|---|---|---|
| `BFF_MOCK_MODE` | 写死 `0` | 误开等于对着内存假余额、假日志运营 |
| `BFF_COOKIE_SECURE` | 写死 `1` | 关掉会让登录凭证在明文 HTTP 上传输 |
| 必填变量 | `${VAR:?}` 语法 | 缺失时 `docker compose up` 直接报错退出，不起一个错配的服务 |
| `PYTHON_IMAGE` | **保留可覆盖** | 构建期基础设施，非运行期行为（见下） |

**为什么 `PYTHON_IMAGE` 是例外**：它影响的是构建能否完成，不是服务的运行时
行为。实测 Docker Hub 会返回 `Bad Gateway` 拉不到 `python:3.13-slim`，此时写死
版本只会让部署整个卡住，没有任何安全收益。代码 `requires-python = ">=3.12"`，
降级到 3.12 是官方支持的：

```bash
PYTHON_IMAGE=python:3.12-slim docker compose build
# 或指向私有 registry
PYTHON_IMAGE=my-mirror/library/python:3.13-slim docker compose build
```

**本地开发不要改这个文件**，直接跑 uvicorn 并用环境变量覆盖：

```bash
BFF_MOCK_MODE=1 BFF_COOKIE_SECURE=0 BFF_SECRET_KEY=$(openssl rand -hex 32) \
  uvicorn app.main:app --reload
```

## 必填项

以下变量缺任何一个，compose 都会拒绝启动并打印带修复提示的错误。

### BFF_SECRET_KEY

会话 Cookie 的**加密**密钥。经 HKDF-SHA256 派生出 AES-256-GCM 密钥。

Cookie 里存着用户的 new-api PAT。密钥可猜 = 任何人都能伪造 Cookie 冒充任意
用户，**并解密出其 PAT**。所以 `/readyz` 对它从严判定：空值、少于 32 字符、或命中已知
弱值列表，一律返回 503 让容器进不了健康状态。宁可不启动，也不带病上线。

**轮换这个值会让全部在线会话立即失效**（旧密文解不开），请安排在低峰期。

### NEWAPI_BASE_URL

上游 new-api 地址。**不给默认值是刻意的**：默认指向某个具体站点时，
一旦部署方忘配，BFF 会静默把全部用户流量打到别人的实例上。

### 管理员凭证：PAT 与账密都必填

这两组**不是替代关系**，各自解决不同问题，生产环境都要配。

**`NEWAPI_ADMIN_PAT` + `NEWAPI_ADMIN_UID`（主通道）**

new-api 的会话上限是 50，**硬拒绝、不淘汰最旧会话、TTL 30 天**。用账密
`login` 换 PAT 每次消耗一个会话配额，一旦打满，`login` 连续 30 天返回 409，
建号、加额度、兑换码全线瘫痪 —— 这是整个系统最高危的单点故障。

PAT 走 `users.access_token` 列，完全不经过会话系统，配了它常态运行不再调 `login`。
获取方式：管理员在 new-api 前端「个人设置 → 系统访问令牌」生成。

**`NEWAPI_ADMIN_USERNAME` + `NEWAPI_ADMIN_PASSWORD`（自愈通道）**

`GET /api/user/token` 每次调用都会**重新生成 PAT 并作废旧值**，管理员在官方
前端点一次「系统访问令牌」，配置里的 PAT 就失效了。此时 `newapi_client.py`
的 401 分支会用账密自动重新换取并落盘缓存。

没有账密 = PAT 一失效就永久瘫痪，只能人工改配置 + 重启才能恢复。

### APP_VERSION + VCS_REF

镜像版本标签与 git commit。不给默认值的理由：让它悄悄落到 `dev`/`unknown`
等于放弃线上问题的回溯能力，出事时无法确定跑的是哪份代码。

## 反向代理

compose 只把端口发布到 `127.0.0.1`，TLS 由宿主机的 Nginx/Caddy 终止。

这里防的不是「反代链路被窃听」—— 用域名反代时浏览器到反代是密文，反代到容器
走回环不出网卡。要封的是**绕过反代的旁路**：只要监听 `0.0.0.0:8000`，任何人都能
`curl http://<公网IP>:8000` 全程明文，域名反代对这条并行入口毫无约束。

还有两个容易忽略的点：

- **Docker 会绕过宿主防火墙。** 发布端口时它直接往 iptables 的 `DOCKER` 链插
  DNAT 规则，位置在 `INPUT` 之前，你在 UFW/firewalld 里配的 `deny 8000` 拦不住。
  绑回环是唯一可靠的兜底。
- **`FORWARDED_ALLOW_IPS` 的前提。** 它成立的基础是请求只可能从回环进来。
  端口对外开放时，攻击者可直连并自带伪造的 `X-Forwarded-For`，按 IP 限流被绕过。

Nginx 示例：

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### FORWARDED_ALLOW_IPS 要配对

`app/main.py` 的 `client_ip()` 取 `X-Forwarded-For` 首段转给 new-api 做按 IP
限流。uvicorn 只接受来自 `--forwarded-allow-ips` 的 XFF，默认 `127.0.0.1`。

- 反代与容器共享网络命名空间（`network_mode: host` 等）：默认值即可
- 反代经 docker 网桥访问：源 IP 是网桥网段，需按实际网段放行，
  如 `FORWARDED_ALLOW_IPS=172.16.0.0/12`，否则 XFF 会被丢弃、
  所有请求在上游看来都来自同一个 IP，限流会误伤

不要填 `*`：那等于允许任意客户端自带一个伪造 XFF 绕过上游限流。

### 会话 Cookie：加密 + Secure

Cookie 载荷是 `{uid, username, pat}`，其中 `pat` 是**用户的 new-api PAT，长期有效**。
它有两层保护，二者解决的问题不同，都不能省：

**第一层：AES-256-GCM 加密载荷（`app/security.py`）**

密钥由 `BFF_SECRET_KEY` 经 HKDF-SHA256 派生。Cookie 形如 `v2.<nonce>.<密文>`，
拿到字符串也解不出 PAT，GCM 的认证标签同时保证不可篡改。

历史问题：早期用 `itsdangerous` 只做**签名不做加密**，载荷是 base64 明文 JSON，
**不需要密钥就能直接解出 PAT**。签名只保证载荷没被改过，不保证不可读。
这条泄露路径远不止「中间人抓包」——devtools、崩溃转储、把请求头贴进工单、
CDN/WAF 请求留存、用户截图求助，全都算。

由此产生两条运维约束：
- `BFF_SECRET_KEY` 现在同时是**加密密钥**，弱密钥等于没加密。`/readyz` 会拦
  空值、短于 32 字符、以及已知弱值，不通过直接 503。
- **换 `BFF_SECRET_KEY` 会让全部在线会话失效**（旧密文解不开），用户需重新登录。
  轮换密钥请安排在低峰期。

**第二层：`BFF_COOKIE_SECURE`（默认 1，生产保持默认）**

加密保护的是「Cookie 内容不可读」，Secure 保护的是「Cookie 不走明文信道」。
即使载荷已加密，密文本身仍是有效的登录凭证——被截获即可重放冒充该用户。

置 0 的后果：浏览器会在明文 HTTP 请求里也带上会话 Cookie。用户手输
`http://域名`、页面混合内容、SSL Strip 都会导致凭证暴露。端口绑 `127.0.0.1`
只封住服务端旁路，管不到客户端这一侧。

唯一该置 0 的场景是本地开发用 `http://127.0.0.1` 直连 —— Secure Cookie 在明文
http 下会被浏览器丢弃，不置 0 则登录静默失败（表现为登录接口 200 但后续请求 401）。

**第三层：`BFF_COOKIE_SAMESITE`（默认 `lax`）**

**做在线支付就不要改成 `strict`。**

用户在支付网关点「返回商户」跳回本站，属于**跨站顶层导航**。`strict` 下浏览器
不带 Cookie，用户落地后显示未登录、看不到充值结果 —— 而钱其实**已经到账**
（走 `notify_url`，与浏览器跳哪儿无关，见下一节）。这个落差极易被当成
「付了钱没到账」而引发客诉。

`lax` 的防护已经够用：它只放行顶层导航的安全方法（GET/HEAD），本项目所有写
操作都是 POST，跨站发起时一律不带 Cookie。`lax → strict` 换来的安全增量很小，
代价却是支付回跳必须重新登录。

仅当本站完全不做在线支付时，才值得收紧到 `strict`。取值只接受
`lax`/`strict`/`none`，**非法值静默回落到 `lax`** —— 因为浏览器会忽略无法识别的
属性值，等于降级到无防护，回落到安全默认值更可预期。

**升级到本版本时**：旧的签名格式 Cookie 一律失效（刻意不做兼容），
所有在线用户会被登出一次。考虑到明文 PAT 已存在泄露风险，强制刷新一轮会话
本身就是正确的处置。

## 在线支付：两条回调互不相干

易支付有两个回调地址，由 new-api 在下单时生成（`controller/topup.go:299`），
**BFF 无法在下单时改写它们**：

| | 生成方式 | 作用 | 配错的后果 |
|---|---|---|---|
| `notify_url` | `CustomCallbackAddress` + `/api/user/epay/notify` | 支付网关**服务端直连**，真正加余额 | 钱不到账（严重） |
| `return_url` | `ServerAddress` + `/usage-logs` | 仅浏览器跳转 | 用户看到 404，但**钱照常到账** |

两者走不同通道。所以「回调页面失败但充值成功」不是矛盾，是必然 ——
到账取决于 notify，跟浏览器跳到哪儿毫无关系。

`ServerAddress` 若留空，new-api 会用写死的默认值 `http://localhost:3000`
（`setting/system_setting/system_setting_old.go:3`），回跳就落到本机 3000 端口。

### BFF 侧已做的解耦（无需配置即生效）

1. **接住回跳**：`/usage-logs`、`/pay/return`、`/console/log` 三个路径都会 302 到
   `/#/topup?trade_no=<订单号>`，不再 404。BFF 是 hash 路由，这些真实路径原本不存在。
2. **按订单号判定到账**：`/api/user/pay/status` 用
   `GET /api/user/topup/self?keyword=<trade_no>` 查上游订单真实状态，
   不再依赖「余额变多了」。因此用户关掉支付页、回跳地址配错、甚至根本没回跳，
   都不影响到账判定 —— 回来刷新一下就能看到余额。

落地页**刻意不做任何发钱动作**，也不校验易支付签名：回跳参数完全由用户可控，
若据此发钱，手拼一个 `trade_status=TRADE_SUCCESS` 就能白拿额度。
签名校验与加余额由 new-api 的 notify 端负责（`EpayNotify` 里 `client.Verify`）。

### 可选：把回跳地址指向 BFF

上面两条已让回跳不再影响功能。若还想让回跳落在自己域名下，
在 new-api 后台把 `ServerAddress` 设为 BFF 的对外地址：

```bash
curl -X PUT "$NEWAPI_BASE_URL/api/option/" \
  -H "Authorization: Bearer $NEWAPI_ADMIN_PAT" -H "New-Api-User: 1" \
  -H "Content-Type: application/json" \
  -d '{"key":"ServerAddress","value":"https://你的BFF域名"}'
```

改之前先确认副作用：`ServerAddress` 还被邮件找回链接、OAuth redirect_uri、
Midjourney 图片地址、Passkey Origin 复用（见 `grep -rn system_setting.ServerAddress`）。
如果这些功能正在用且指向 new-api 自己的域名，就别改 —— 保持现状即可，
BFF 已经能接住默认的 `/usage-logs` 回跳。

**不要动 `CustomCallbackAddress`**，它当前指向 `https://api.aihuobao.cn`，
是钱能到账的原因。

## 数据卷

`/data` 挂 `bff-data` 卷，存两个文件：

| 文件 | 内容 | 丢失后果 |
|---|---|---|
| `promo_state.json` | 注册礼包 / 首充赠送的发放记录 | 所有用户首充资格被重置，可重复领取赠送 |
| `admin_cred.json` | 管理员 PAT 缓存（权限 600） | 冷启动多消耗一个会话配额 |

BFF 没有数据库，赠送幂等完全靠 `promo_state.json`。**这个卷是有状态的，
别用 `docker compose down -v` 清生产环境。** 备份：

```bash
docker run --rm -v newapi-bff_bff-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/bff-data-$(date +%F).tar.gz -C /data .
```

## 扩容

镜像固定单 worker。`app/promo.py` 的赠送幂等用进程内 `asyncio.Lock` +
本地 JSON 文件，多 worker 会让同一用户重复领到赠送。

要扩容需先把状态迁到 Redis 或数据库，再横向加副本。单纯调大 `--workers`
会直接造成资损。

## 镜像说明

- 多阶段构建，运行层不含编译工具链和 pip 缓存
- 非 root（uid 10001）运行，`cap_drop: ALL`，`no-new-privileges`
- 根文件系统只读，仅 `/data`（卷）与 `/tmp`（tmpfs）可写，
  挡住「写 webshell 进静态目录」这类利用
- 不装 curl：健康检查用 Python 标准库。少一个依赖，且构建不依赖 apt 源可达
- `scripts/`、`demo/`、`dist/` 不进镜像

拉不到 `python:3.13-slim` 时（内网、镜像站滞后）可降级，代码兼容 3.12+：

```bash
PYTHON_IMAGE=python:3.12-slim docker compose up -d --build
```

## 健康检查端点

| 端点 | 用途 | 说明 |
|---|---|---|
| `/healthz` | 存活探针 | 纯本地判断，不触碰上游，可高频调用 |
| `/readyz` | 就绪探针 | 校验静态资源、状态目录可写、密钥强度、管理员凭证；任一不过返回 503 |

真实模式下必须有管理员凭证（PAT 或账密，二者其一），否则注册、首充赠送、
兑换码登录会全部失败。`app/config.py` 已移除默认账密，所以这项由 `/readyz` 兜住，
不让配置缺失静默上线。

`/healthz` 刻意不检查上游可用性。探针一旦依赖 new-api，上游抖动就会让编排系统
反复重启容器，把一次可恢复的上游故障放大成本服务雪崩。

## CI/CD

`.github/workflows/ci.yml`（push / PR 到 master）：

1. `ruff check app tests` — 只检 app 与 tests，`scripts/` 是一次性运维探针，
   其风格问题不阻塞发布
2. `python scripts/check_no_emoji.py` — P0-1 硬规则，emoji 不得作功能图标
3. `pytest` — 24 个用例，mock 模式，无需上游
4. `scripts/e2e_redeem_mock.py` — 30 项业务回归，覆盖「伪造兑换码不能开号」
   这条核心安全语义
5. 构建镜像并**真实启动容器**跑冒烟：`/readyz` 通过、首页含带版本号的静态资源
   引用、未登录接口返回 401、容器非 root
6. 校验 4 种弱密钥（含空串）都被 `/readyz` 拒绝
7. 校验 compose 配置有效，且缺 `BFF_SECRET_KEY` 时被拦下

第 5 步是关键：Dockerfile 语法正确不代表容器能跑起来。真实启动能抓出「静态资源
没拷进镜像」「非 root 写不了 /data」「依赖缺失」这些只在运行时暴露的问题。

`.github/workflows/release.yml`（打 `v*` tag）：重跑全部门禁（含 E2E 与 emoji）
→ 构建 → 冒烟 → 推 GHCR。冒烟不过不推，避免把起不来的镜像发出去。

## 本地跑门禁

```bash
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/ruff check app tests
.venv/bin/python scripts/check_no_emoji.py
.venv/bin/pytest -q

# 业务 E2E（需先起 mock 服务）
BFF_MOCK_MODE=1 BFF_SECRET_KEY=local-dev \
  .venv/bin/uvicorn app.main:app --port 8301 &
.venv/bin/python scripts/e2e_redeem_mock.py
```

`check_no_emoji.py --list` 只列出不阻塞，便于本地排查。

## 脚本不含任何凭证

`scripts/` 里的脚本全部从环境变量读凭证，源码不留默认值 —— 本仓库有公开 remote，
写进源码等于随仓库分发管理员密码。`app/config.py` 同样不再有默认账密。

跑需要管理员权限的脚本：

```bash
export NEWAPI_ADMIN_USERNAME=<管理员用户名>
export NEWAPI_ADMIN_PASSWORD=<管理员密码>
.venv/bin/python scripts/e2e_redeem_login.py
```

缺失时脚本会打印所需变量名并退出，不会拿空凭证去撞上游。离线回归用
`e2e_redeem_mock.py`，它不需要任何凭证。

## 排查

**容器一直 unhealthy**

```bash
curl -s localhost:8000/readyz | python3 -m json.tool   # 看 failed 数组
docker compose logs --tail 50 bff
```

**建号 / 加额度 / 兑换码全部失败（409 AUTH_SESSION_LIMIT）**

日志里出现「管理员会话数已达 new-api 上限」即是此故障。

先分清两种情况，处理方式完全不同：

| 情况 | 判断方法 | 处理 |
|---|---|---|
| 还能登进去 | `python scripts/fix_session_limit.py` 显示 `[OK]` | 工具会自动清理历史会话并打印 PAT，直接配到环境变量 |
| 已登不进去（死锁） | 同一命令显示 409 | 只能改库，见下 |

**为什么死锁时不能"直接配 PAT"**：PAT 要在前端「个人设置 → 系统访问令牌」
生成，而进前端就得先 `login` —— 正是被 409 挡住的那一步。这条路在死锁下不通，
别在这里绕圈。同理，把会话上限调高也要先登进管理后台，一样不通。

死锁的成因是闭环：清会话的接口全部要求 `access_token`，拿 token 必须 `login`，
`login` 又被会话数硬拒绝。实测确认无解的旁路（v1.0.0-rc.24）：

- `/api/user/refresh`、`/api/auth/refresh` → 404，不存在
- `login` 带 `remember:false` / `stateless:true` 等参数 → 仍然 409
- HTTP Basic 直接取 `/api/user/token` → 401
- `/api/user/sessions*` 全部 → 401
- 邮箱找回 → 上游未配 SMTP，返回 `invalid SMTP account`

**死锁的两条出路**

方案一，用另一个 root/管理员账号救援（前提是有这样的账号）：
在用户管理里编辑被锁账号，提交一个更大的 `auth_version` ——
`UpdateUser` 会触发 `RevokeAllUserSessions` 吊销其全部会话。

方案二，直接改数据库（没有其他管理员时的唯一出路）：

```bash
python scripts/fix_session_limit.py --sql <管理员用户名>
```

输出的语句已把账号名填好、用子查询定位 uid，**无需手工替换任何占位符**，
MySQL / PostgreSQL / SQLite 三种方言都给了，挑对应的粘贴执行。原理是把
`user_sessions.status` 从 `active` 改为 `revoked` —— 依据 `model/user_session.go`，
计入上限的条件是 `status='active' AND expires_at>now`，不检查 `revoked_at`。

两个容易漏掉的坑：

- **启用了 Redis 就必须重启 new-api 或清 Redis**，否则会话快照还在缓存里，
  表现为「SQL 明明跑了但还是 409」
- 若改完变成 `429 AUTH_SESSION_ISSUANCE_LIMIT`，那是另一套按 `created_at`
  计数的签发窗口限流，撤销对它无效，只能等窗口过或 `DELETE` 掉已撤销的行

**恢复后立刻做这件事，否则一定复发**

生成 PAT 配到 `NEWAPI_ADMIN_PAT` + `NEWAPI_ADMIN_UID`。PAT 走
`users.access_token` 列，不经过会话系统，BFF 从此不再调 `login`，
会话配额永远不会被消耗。代码侧的会话归还（换到 PAT 后立即 `DELETE`
本次会话）已实装，实测连续 10 次登录净增 0，但配了 PAT 才是根治。

**上游限流误伤所有用户**

`FORWARDED_ALLOW_IPS` 没覆盖反代的真实来源 IP，XFF 被 uvicorn 丢弃，
上游看到的全是同一个容器 IP。按上面「反向代理」一节调整。
