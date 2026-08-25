# /readyz 503 修复 + Logfire 接入

## 一、readiness 503 根因

```
ERROR:bff:readiness 检查未通过: secret_key_configured
GET /readyz 503 Service Unavailable
```

`app/config.py` 中 `SECRET_KEY` 的默认值是占位串 `dev-secret-key-change-me`，
`/readyz` 把「值等于该占位串」判定为未配置并返回 503。

**这是配置缺失，不是代码缺陷** —— `/readyz` 的行为完全正确，它拦住了一个
拿默认密钥签发会话的实例。若改成放行，等于把可预测的签名密钥带上线，
任何人都能伪造会话 Cookie。

修法：向 `.env` 写入随机密钥（`secrets.token_urlsafe(32)` 生成，32 字节）。
`docker-compose.yml` 已有 `env_file: .env`，无需改动。

## 二、Logfire 接入

新增 `app/observability.py`，在 `app/main.py` 创建 app 后调用 `setup(app)`。

三条设计约束：

1. **未配 token 即完全关闭** —— 不导入 SDK、不注册 hook。可观测性是运维
   增强，不该成为本地开发与 CI 的前置依赖。
2. **任何一步失败只告警不抛异常** —— 失败上限是「看不到 trace」，
   不能是「服务起不来」。用可观测性换掉可用性是亏本交易。
3. **敏感字段在发送前拦掉** —— 会话 Cookie 里装着用户的 new-api PAT，
   凭证一旦离开进程就无法召回，不能依赖后端配置兜底。

覆盖范围：FastAPI 请求 trace、httpx 出站调用（new-api）、stdlib logging 接管。
scrub 规则覆盖 authorization / cookie / password / token / secret / api_key。

## 三、踩坑：可观测性静默失效

第一版实现日志打印「logfire 已启用」，但实测 httpx client 上**没有任何
instrumentation 痕迹，两个埋点一个都没挂上**。两层原因叠加：

1. `.venv` 只装了裸 `logfire`，缺 `[fastapi,httpx]` extras
   （`requirements.txt` 声明是对的，环境没同步），instrumentation 包不存在
   → `instrument_*` 抛 RuntimeError
2. 我的 `try/except Exception` 吞掉异常后**照打「已启用」日志**，
   且两个埋点共用一个 try 块 —— fastapi 失败会连带跳过 httpx 和日志接管

修法：
- 拆成逐项独立容错的 `_try_instrument` helper，每项失败单独 WARNING，
  提示语带上 `pip install 'logfire[fastapi,httpx]'`
- 成功日志改为如实汇报 `fastapi_traces=on/off`，不再一律说「已启用」
- 加 `_configured` 幂等保护 —— 重复 setup 会让 `instrument_fastapi` 抛
  「already been instrumented」，被误报成埋点失败

**教训**：可观测性代码里 `except Exception` 配固定成功日志是最坏组合 ——
埋点失效时服务照跑、日志还说一切正常，这种缺陷永远不会被发现。
验收埋点必须看机械证据，不能看自己打的日志。

## 四、验证证据

| 项目 | 方法 | 结果 |
|------|------|------|
| readiness | 实际起服务打 `/readyz` | 200，四项检查全 true |
| 启用态启动 | 配无效 token 起服务 | 正常启动，`/readyz` 仍 200 |
| httpx 埋点 | 读 `_is_instrumented_by_opentelemetry` | True |
| FastAPI 埋点 | `TestExporter` 抓 span | 5 个 span，含 `GET /readyz` |
| scrub | 注入 3 个真实凭证串比对导出内容 | 全部 leaked=False，无害字段保留 |
| 关闭态 | 清空 LOGFIRE_* 环境变量 | `setup()` 返回 False，零副作用 |
| 回归 | `pytest tests/` | 79 passed |

注：中途一次验证脚本自身有缺陷（重复 configure + 重复 instrument）导致
span 数为 0，排查后确认是脚本问题而非实现问题，改用干净进程单次
初始化复验通过。

## 五、变更文件

| 文件 | 变更 |
|------|------|
| `app/observability.py` | 新增，Logfire 接线 + scrub + 逐项容错埋点 |
| `app/config.py` | 新增 `LOGFIRE_TOKEN` / `LOGFIRE_ENVIRONMENT` 读取 |
| `app/main.py` | 创建 app 后调用 `observability.setup(app)` |
| `app/newapi_client.py` | 单例 client 创建后挂 httpx 埋点 |
| `.env` | 写入随机 `BFF_SECRET_KEY`（修复 503） |

`requirements.txt` 中 `logfire[fastapi,httpx]==4.41.0` 原已声明，未改动。

## 六、待办

- 生产环境需注入 `BFF_SECRET_KEY`，与本地 `.env` 用不同值；
  轮换该密钥会使所有存量会话失效，需安排在低峰期。
- 生产环境如需 trace，配 `LOGFIRE_TOKEN` + `LOGFIRE_ENVIRONMENT=production`；
  不配则可观测性静默关闭，其余功能不受影响。
- 部署后建议核对启动日志中的 `fastapi_traces=on`，确认埋点真实挂载。
