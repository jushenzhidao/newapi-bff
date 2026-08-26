# 依赖漏洞清单与处置

数据源：OSV.dev（GitHub Advisory 同步库），查询日期 2026-08-26。
基线环境：`.venv`（Python 3.13.12），58 个已安装包全量扫描。

> 用 OSV 而非 Dependabot API，是因为后者需要仓库认证令牌；两者的 GHSA 编号一致，
> 结论可互相核对。Dependabot 只统计声明在 manifest 里的依赖，OSV 扫的是实际
> 已安装版本，所以 OSV 会多出 `pip` / `pytest` 这类工具链条目。

## 一、结论

19 条去重后告警集中在 3 个包。**其中只有 1 条在本项目当前代码路径下真实可利用**，
其余因项目不使用对应 API 而不可达。但仍建议全部升级 —— 不可达是当前代码的偶然属性，
一旦后续引入 `request.form()` 或证书校验就会变成真实缺口。

| 包 | 当前 | 目标 | 告警数 | 真实可利用 |
|---|---|---|---|---|
| starlette | 0.46.2 | 1.6.0 | 7 | 1（Range DoS） |
| cryptography | 43.0.3 | 50.0.1 | 6 | 1（wheel 内 OpenSSL） |
| fastapi | 0.115.14 | 0.141.1 | 0 | — |

FastAPI 自身零告警，升级它纯粹是为了解开 starlette 的版本上限（见第三节）。

## 二、逐条可利用性判定

### starlette 0.46.2

| 严重度 | GHSA | 修复版本 | 本项目是否可利用 |
|---|---|---|---|
| HIGH | GHSA-7f5h-v6xp-fcq8 | 0.49.1 | **可利用**。Range 头合并 O(n²) DoS。`app/main.py` 挂载了 `StaticFiles`，攻击者对 `/static/*` 构造畸形 Range 头即可打满 CPU |
| HIGH | GHSA-82w8-qh3p-5jfq | 1.3.1 | 不可达。需 `request.form()` 解析 urlencoded 表单，项目零使用（全部接口走 JSON body） |
| HIGH | GHSA-wqp7-x3pw-xc5r | 1.1.0 | 不可达。UNC 路径 SSRF / NTLM 凭证窃取仅影响 Windows，本项目 Docker 部署为 Linux |
| MODERATE | GHSA-2c2j-9gv5-cj73 | 0.47.2 | 不可达。需 multipart 大文件解析，项目无上传接口 |
| MODERATE | GHSA-86qp-5c8j-p5mr | 1.0.1 | 不可达。Host 头污染 `request.url.path`，可绕过路径鉴权；本项目该字段仅用于日志（`app/main.py:90`），不参与任何鉴权决策 |
| MODERATE | GHSA-x746-7m8f-x49c | 1.1.0 | 不可达。需 `HTTPEndpoint` 类视图，项目全部用函数式路由 |
| LOW | GHSA-jp82-jpqv-5vv3 | 1.3.0 | 不可达。同 GHSA-86qp，`request.url` 不用于安全判断 |

### cryptography 43.0.3

`app/security.py` 只用 AESGCM + HKDF 做会话 Cookie 对称加解密，不做任何 X.509
证书链校验、不解析 PEM、不用椭圆曲线。因此 5 条证书相关告警全部不可达。

| 严重度 | GHSA | 修复版本 | 本项目是否可利用 |
|---|---|---|---|
| HIGH | GHSA-537c-gmf6-5ccf | 48.0.1 | **间接可利用**。wheel 内置的 OpenSSL 有漏洞，httpx 对 new-api 的出站 TLS 依赖它 |
| HIGH | GHSA-jwv3-5hgf-82ww | 49.0.0 | 不可达。自签名中间证书重复导致指数级路径构建，需证书链校验 |
| HIGH | GHSA-r6ph-v2qm-q3c2 | 46.0.5 | 不可达。子群攻击，需 DH / EC 密钥交换 |
| MODERATE | GHSA-m2h6-j472-rp4c | 49.0.0 | 不可达。verifier 接受通配 DNS 名，需证书校验 |
| LOW | GHSA-79v4-65xg-pq4g | 44.0.1 | 间接。同 GHSA-537c，OpenSSL 打包问题 |
| LOW | GHSA-m959-cc7f-wv43 | 46.0.6 | 不可达。DNS name constraint 校验不完整，需证书校验 |

## 三、升级约束

**必须同时升级 FastAPI**。`fastapi 0.115.14` 声明 `starlette<0.47.0`，而修复 Range DoS
需要 `starlette>=0.49.1` —— 版本上限直接挡住。`fastapi 0.141.1` 放开为 `starlette>=0.46.0`
（无上界），组合才可解。

单独升 FastAPI 到 0.141.1 **不会**自动拉起 starlette（它接受现有 0.46.2），
所以 `requirements.txt` 必须显式钉 starlette 版本，否则漏洞依旧存在。

## 四、验证结果

在独立 venv（`/tmp/upg_venv`，Python 3.13.12）安装升级组合后实测：

- `pip check`：No broken requirements found
- `pytest tests/`：**92 passed**（与升级前基线一致，无回归）
- 完整 lifespan（startup + shutdown）走通，httpx 连接池正常关闭
- `GET /api/config` 返回 200

解析出的实际版本：fastapi 0.141.1 / starlette 1.6.0 / cryptography 50.0.1 /
pydantic 2.13.4 / httpx 0.27.2 / uvicorn 0.30.6

## 五、遗留技术债

升级后暴露两处废弃 API，均不阻塞本次升级，建议单独排期：

1. **`app/main.py:76` 的 `@app.on_event("shutdown")`**。starlette 1.6.0 已移除
   `Starlette.on_event`（实测 `hasattr` 为 False），当前能跑是因为 FastAPI 0.141
   自己保留了兼容层。该兼容层未来移除后会导致 httpx 连接池泄漏。应改用 `lifespan`
   上下文管理器。
   注意：现有测试**不覆盖** shutdown 路径，这类问题不会被测试套件拦住。

2. **`starlette.testclient` 要求改用 `httpx2`**。仅影响测试代码
   （`tests/conftest.py`、`tests/test_health.py`、`tests/test_admin_settings.py`），
   生产代码里的 `httpx.AsyncClient`（`app/newapi_client.py`）不受影响。
