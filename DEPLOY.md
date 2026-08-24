# 部署

容器化部署与 CI/CD 说明。本地开发直接跑 uvicorn 见 README。

## 快速开始

```bash
cp .env.example .env
openssl rand -hex 32          # 把输出填进 .env 的 BFF_SECRET_KEY
vim .env                      # 再填 NEWAPI_ADMIN_PAT / NEWAPI_ADMIN_UID

docker compose up -d --build
curl -fsS localhost:8000/readyz    # 应返回 {"status":"ready",...}
```

`/readyz` 返回 503 就是配置有问题，`failed` 数组会指出具体哪一项。

## 两个必须配对的环境变量

### BFF_SECRET_KEY

会话 Cookie 的签名密钥，**必填**。

Cookie 里存着用户的 new-api PAT。密钥可猜 = 任何人都能伪造 Cookie 冒充任意
用户并拿到其 PAT。所以 `/readyz` 对它从严判定：空值、少于 32 字符、或命中已知
弱值列表，一律返回 503 让容器进不了健康状态。宁可不启动，也不带病上线。

compose 里用了 `${BFF_SECRET_KEY:?...}`，没设置时 `docker compose up` 直接报错
退出，不会起一个能被伪造会话的服务。

### NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID

强烈建议配置，用来替代管理员账密。

new-api 的会话上限是 50，**硬拒绝、不淘汰最旧会话、TTL 30 天**。BFF 用账密
`login` 换 PAT 时每次消耗一个会话配额，一旦打满，`login` 会连续 30 天返回 409，
建号、加额度、兑换码全线瘫痪 —— 这是整个系统最高危的单点故障。

PAT 走 `users.access_token` 列，完全不经过会话系统。配了它，BFF 就再也不需要
调 `login`，从根本上绕开该问题。

获取方式：管理员在 new-api 前端「个人设置 → 系统访问令牌」生成。

## 反向代理

compose 只把端口发布到 `127.0.0.1`，TLS 由宿主机的 Nginx/Caddy 终止。
直接暴露 `0.0.0.0:8000` 意味着明文 HTTP 对公网可达，而会话 Cookie 里带着用户
的 PAT，被抓到等于账号失守。

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

### 上 HTTPS 后要改的一处代码

`app/security.py:25` 的 Cookie `secure=False`。走 HTTPS 后应改为 `True`，
否则 Cookie 仍会在明文 HTTP 请求中被发送。这一项不在本次部署改动范围内，
上线前需确认。

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
