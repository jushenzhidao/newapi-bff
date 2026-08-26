# newapi-bff 生产镜像：多阶段构建，运行层不含编译工具链与 pip 缓存。
# 刻意不写 `# syntax=` 指令：本文件只用标准 Dockerfile 语法，
# 省掉每次构建都要联网拉 frontend 镜像这一步。
#
# 阶段划分的意义：builder 里装依赖（可能拉编译器），runtime 只复制装好的
# site-packages，最终镜像里没有 gcc、没有 pip cache、没有 .git。

# 基础镜像版本参数化：便于在拉不到 3.13 的环境（内网/镜像站滞后）降到 3.12，
# 代码本身兼容 >=3.12（见 pyproject.toml）
ARG PYTHON_IMAGE=python:3.13-slim

# ---------- builder ----------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 依赖单独一层：只要 requirements.txt 不变，改业务代码时这层命中缓存
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------- runtime ----------
# 重复声明：ARG 在 FROM 之前定义的作用域到此失效，这是 Dockerfile 的既定行为
ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE} AS runtime

# 镜像元信息，便于回溯线上跑的到底是哪个 commit
ARG APP_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="newapi-bff" \
      org.opencontainers.image.description="new-api 用户侧 BFF（FastAPI + 原生 JS SPA）" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/jushenzhidao/newapi-bff"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    BFF_VERSION="${APP_VERSION}" \
    # 可写数据目录。根文件系统以 read_only 运行，/app 不可写，所有落盘状态
    # 必须在挂载卷里，否则写入抛 OSError: [Errno 30] Read-only file system。
    # 下面逐项显式声明而非只靠 BFF_DATA_DIR：显式路径在 docker inspect 里
    # 一眼可见，运维排查「文件到底写哪了」不用去翻代码的默认值。
    BFF_DATA_DIR=/data \
    # 状态文件指向挂载卷，容器重建后首充/注册赠送的幂等记录不丢
    BFF_PROMO_STATE_FILE=/data/promo_state.json \
    BFF_ADMIN_CRED_FILE=/data/admin_cred.json \
    BFF_PRICING_SNAPSHOT_FILE=/data/pricing_snapshot.json \
    # 后台「保存配置」写这里。漏掉它会让管理页面每次保存都 500。
    BFF_SETTINGS_FILE=/data/settings.json \
    # 只信任本机来的 X-Forwarded-For。app/main.py 的 client_ip() 取 XFF 首段
    # 转给 new-api 做按 IP 限流，若信任任意来源，客户端自带一个伪造 XFF 就能
    # 绕过限流。反代不在同一 network namespace 时，改为反代的实际来源 IP。
    FORWARDED_ALLOW_IPS=127.0.0.1

# 刻意不 apt install curl：健康检查改用 Python 标准库（见下方 HEALTHCHECK）。
# 理由有两条，都不是洁癖：
#   1) 构建不再依赖 apt 源可达 —— 内网/受限网络下 apt-get update 失败会直接
#      让整个镜像构建挂掉，而它换来的只是一个探针用的 curl。
#   2) 少装一个带 TLS 栈的二进制，就少一条要跟 CVE 的依赖。

# 非 root 运行：容器逃逸时攻击者拿到的是无特权账号
RUN groupadd --system --gid 10001 bff \
 && useradd --system --uid 10001 --gid bff --no-create-home bff

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# 只拷运行必需的东西。scripts/ 是运维探针脚本、demo/ 与 dist/ 是本地演示，
# 都不该进生产镜像（少一个文件少一处攻击面）。
COPY --chown=bff:bff app/ ./app/
COPY --chown=bff:bff static/ ./static/
# docs/ 是运行期数据，不是文档：app/docs_catalog.py 从 /app/docs/products 读产品
# 档案，漏拷会让 /readyz 的 doc_products_loadable 直接判定不通过。
COPY --chown=bff:bff docs/ ./docs/

# /data 存放 promo_state.json 与 admin_cred.json（含管理员 PAT）。
# 目录权限收到 700 —— PAT 等同管理员密码。
RUN mkdir -p /data && chown bff:bff /data && chmod 700 /data
VOLUME ["/data"]

USER bff
EXPOSE 8000

# 探针打 /healthz（不触碰上游），上游抖动不会导致容器被判定不健康而重启。
# 用 python -c 而非 curl：镜像里没装 curl（理由见上），Python 一定在。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# 单 worker + 外层多副本：应用用本地 JSON 文件保证赠送幂等（app/promo.py 的
# asyncio.Lock 只在进程内有效），多 worker 会让同一用户重复领到赠送。
# 要扩容就横向加副本 + 共享存储，或先把状态迁到 Redis/DB。
#
# 用 exec 形式的 shell 包装，只为让 FORWARDED_ALLOW_IPS 可被 compose 覆盖；
# exec 保证 uvicorn 是 PID 1，能直接收到 SIGTERM 优雅退出（触发 app 的
# shutdown 钩子关闭 httpx 连接池）。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]
