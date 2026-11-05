# /v1 OpenAI 兼容透传层（openai_proxy）

> 2026-10-10 新增。给 LibreChat 等标准 OpenAI 客户端开一条直连 BFF 的对话通道。

## 线上拓扑侦察结论（2026-10-10 实测）

- `newapi-bff.oneapis.cn` 实际是 **New API v1.0.0-rc.40 实例**（`/api/status` 版本号、
  `/healthz` 返回 New API UI、`/v1/models` 假 key 返回 new_api_error "Invalid token"）
- 本仓库 master（最后 push 2026-09-22）**没有** agent_id 也没有任何 /v1 路由 ——
  线上 `/v1/chat/completions` 的 "agent_id is required in request body" 校验
  **不来自本仓库代码，也不是官方 new-api 功能**，是服务器上自研 agent 网关
  （对应 docs/products/agent-plan.yml 产品线）在 nginx 层或独立服务里加的
- 该校验发生在 token 鉴权**之后**（假 key 先报 401 Invalid token，真 key 才到
  agent 校验），说明拦截方在 new-api 鉴权链后面
- `api.oneapis.cn` 是 RuoYi Java 网关（model-plaza 后端），与 new-api 无关

## 背景与问题

chat 项目（LibreChat 基座）的 custom endpoint 只会说标准 OpenAI 协议：
`baseURL + Bearer sk-`。而 BFF 的 `/v1/chat/completions` 此前被 agent 业务层占用
（要求请求体带 `agent_id`），标准格式请求被 400 拦截：
`agent_id is required in request body`。

处理模式对标 **flovart-bff `app/routers/chat.py`**（会话鉴权 → 按需发放用户 sk- →
httpx 流式透传 new-api → 日志落库），按 LibreChat 场景裁剪：

| 差异点 | flovart-bff | 本模块 | 原因 |
|---|---|---|---|
| 入口鉴权 | BFF 会话 Cookie | Bearer sk- 原样转发 | LibreChat 是标准 OpenAI 客户端，持 sk- 持不了 Cookie |
| 用户 Key | BFF 按用户 mint + AES 落盘 | 不碰（sk- 归属即用户归属） | LibreChat 自管 Key 配置，new-api 侧天然按 token 归属计费 |
| 错误形态 | 非 2xx 包成 SSE error 事件 | 状态码+响应体原样回传 | LibreChat 按原生 OpenAI 错误处理，不要事件包装 |
| 日志 | 云端 request_log 全落库 | logger.info（P2 补落库） | P0 先通链路，归属查询/落库放 P2 |

## 路由与行为

| 路由 | 行为 |
|---|---|
| `GET /v1/models` | 透传（LibreChat `models.fetch: true` 靠它动态同步模型列表） |
| `POST /v1/chat/completions` | `stream=false` 整包透传；`stream=true` SSE 字节流原样透传（`X-Accel-Buffering: no` 防反代攒包）；带 `"web_search": true` → SearXNG 工具循环 |
| `POST /v1/responses` | **Responses API 适配（2026-10-10 晚）**：LibreChat custom endpoint「联网搜索」开关的实际落点——LibreChat 检测到该开关即切到 Responses API 并附托管工具 `{"type":"web_search"}`，本层翻译为 chat/completions + SearXNG 工具循环；带搜索时内部非流式跑循环、流式客户端拿合成事件序列，无搜索时真实流式逐 delta 转换（保留打字机效果） |
| 带 `agent_id` 的请求 | 400 明确拒绝（agent 语义不进通用通道；合并进含 agent 层的版本时把该分支替换为调用原 agent handler） |
| 鉴权失败/坏请求体 | 401/400 OpenAI 风格 `{"error": {...}}`，不碰上游 |

配套改动：
- `app/config.py`：新增 `CHAT_STREAM_TIMEOUT`（env `BFF_CHAT_STREAM_TIMEOUT`，默认
  300s）。透传连接池读超时单独放宽——推理型模型首 token 前可能等数分钟，
  管理面 15s 超时会掐断所有流式请求。
- `app/main.py`：`include_router(openai_proxy.router)` + lifespan 收尾 `close()`。

## /v1 增强能力：时间注入 + 联网搜索（2026-10-10）

背景：透传通道的客户端没有 agent 框架的工具循环，「几点了」「今天新闻」类
问题无法回答。这两个能力做在 BFF 层，**所有接入 /v1 的客户端（LibreChat、
自有前端、任意 OpenAI 兼容 SDK）自动获得，客户端零改造**。

### 时间注入（默认开启）

- 每次请求向 system 消息追加「【当前时间】YYYY-MM-DD HH:MM:SS 周X (UTC+0800)」
  （时区 env `BFF_TIMEZONE`，默认 Asia/Shanghai）；无 system 消息时自动补一条。
- messages 已含日期（如 LibreChat agent 的 `{{current_datetime}}` 已解析）时
  自动跳过，不重复注入。
- 关闭：`BFF_TIME_INJECTION=0`。

### 联网搜索（web_search 工具循环）

- 客户端在请求体加自定义字段 `"web_search": true`（BFF 消费后剥除，不上送
  上游、不影响其他字段透传）。
- 执行方式：注入 web_search 工具定义 → 非流式跑 function-calling 循环
  （每轮：上游补全 → 有 tool_calls 则调 SearXNG 回填 tool 消息 → 继续）→
  最终答案返回。**循环内强制 `stream=false`**（流式响应无法判断工具调用时机）。
- 客户端 `stream=true` 时：循环照常非流式跑，最终答案合成标准 SSE 分片
  （role → reasoning_content → content → finish_reason=stop + usage → [DONE]），
  对客户端呈现为一次普通流式对话。
- 迭代上限 `BFF_WEB_SEARCH_MAX_ITERATIONS`（默认 4），超限后去掉工具强制收敛。
- 未配置 `BFF_SEARXNG_BASE_URL` 时：工具返回「未配置」提示文本，对话照常
  继续，不因搜索故障 500/报错。

### 部署新增 env

| 变量 | 默认 | 说明 |
|---|---|---|
| `BFF_TIME_INJECTION` | `1` | 时间注入开关 |
| `BFF_TIMEZONE` | `Asia/Shanghai` | 注入时间的时区 |
| `BFF_SEARXNG_BASE_URL` | 空（搜索关闭） | SearXNG 实例地址，如 `http://searxng:8080` |
| `BFF_WEB_SEARCH_MAX_ITERATIONS` | `4` | 工具循环最大轮数 |
| `BFF_WEB_SEARCH_RESULTS_LIMIT` | `5` | 单次搜索回填结果条数 |

服务器上建议加一个 SearXNG 容器（与 BFF 同网络），`settings.yml` 需允许
json format 并关 limiter（参考 `D:\code\chat\librechat\searxng\settings.yml`）：

```
docker run -d --name searxng --restart unless-stopped --network <bff同网络> \
  -v /opt/searxng/settings.yml:/etc/searxng/settings.yml searxng/searxng:latest
```

BFF 容器加 `-e BFF_SEARXNG_BASE_URL=http://searxng:8080` 即接通。

curl 验证（客户端开关就这一个字段）：

```
curl -X POST https://<bff>/v1/chat/completions -H "Authorization: Bearer sk-..." -H "Content-Type: application/json" -d "{\"model\":\"deepseek-v4-flash\",\"stream\":true,\"web_search\":true,\"messages\":[{\"role\":\"user\",\"content\":\"今天有什么大新闻\"}]}"
```

## 部署

镜像已本地构建并冒烟验证：`newapi-bff:chat-passthrough`（2026-10-10）。
CI 链路：push master → GHCR 构建（tag：`master` + `sha-<short>`）；打 v* tag 才动 `latest`。

按线上拓扑（agent 拦截在 new-api 之后的独立层），两种接法：

**接法 A（推荐，最快）：nginx 分流加一条**——把 LibreChat 的 OpenAI 兼容流量
引到透传 BFF，agent 网关入口原样保留：
1. 服务器上构建/载入镜像（`docker load` 或 `docker build`，源码=本地 newapi-bff 仓库）
2. 起容器：`docker run -d --name chat-bff --restart unless-stopped --network <new-api所在网络> -e NEWAPI_BASE_URL=<new-api地址> -e BFF_SECRET_KEY=<随机值> -p 127.0.0.1:8001:8000 newapi-bff:chat-passthrough`
3. nginx 加 location：`/openai/ { proxy_pass http://127.0.0.1:8001/v1/; }`（或直接
   把 `/v1/` 切给 BFF，agent 入口换个前缀保留）
4. LibreChat `baseURL` 指到新路径

**接法 B：代码合入部署版**——若拦截层代码可改，把 `app/openai_proxy.py` 移植进
那个 agent 网关服务（config +1 常量、main.py 3 处接线，见下），`agent_id` 分支
改为调用原 agent handler。

注意：透传 BFF 只需 NEWAPI_BASE_URL 指向 new-api；/v1 透传不碰管理面，
无需 NEWAPI_ADMIN_PAT 等管理员凭据。

## LibreChat 侧（已就绪，无需再动）

`librechat.yaml`：
```yaml
endpoints:
  custom:
    - name: "oneapis"
      apiKey: "<oneapis/new-api 的 sk->"
      baseURL: "https://newapi-bff.oneapis.cn/v1"
      models:
        fetch: true        # 走 BFF /v1/models 动态同步
```
模型的上/下架、分组、限额全部在 new-api/oneapis 侧管理端操作，
LibreChat 不改配置不重启。

## 测试

```
pytest tests/test_openai_proxy.py -v      # 21 个用例（透传 6 + 时间注入 3 + 搜索循环 5 + Responses 适配 7）
pytest tests/ -q                          # 全量回归（2026-10-10 晚：195 passed）
```

注意：本机沙箱的 httpx 可能带「ByteStream 构造即 read()」的补丁，mock SSE 响应
必须用显式 `stream=` 构造（见 `tests/test_openai_proxy.py` 的 `_ListAsyncStream`），
否则 `aiter_raw` 会报 StreamConsumed。

## TODO（P2）

- [ ] admin API 反查 token→uid，调用日志挂进 BFF 积分/用量视图
- [ ] SSE 流内 usage 解析 + 计费对账（flovart `_sse_extract` 可直接搬）
- [ ] 客户端断开检测（cancelled 状态落库）
