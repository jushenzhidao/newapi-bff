#!/usr/bin/env python3
"""Logfire 上报链路分层诊断。

存在动机：「控制台看不到数据」有四个互相独立的断点，混在一起猜会反复返工。
本脚本按依赖顺序逐层定位，直接指出是哪一层断的。

    L1 配置解析   .env 是否被 load_dotenv 读到、token 是否非空
    L2 依赖安装   logfire 及 fastapi/httpx extras 是否可导入
    L3 网络可达   能否连上 logfire-us.pydantic.dev:443
    L4 端到端     configure + 打点 + force_flush 是否真的投递成功

已知易踩的两个坑（都不会报错，只是静默无数据）：
  1. 容器场景：docker-compose.yml 刻意不用 env_file，LOGFIRE_* 必须在
     environment: 段显式声明，否则宿主机 .env 配了也进不去容器。
     核对方式：docker compose exec bff env | grep LOGFIRE
  2. .env 里该行被 # 注释掉 —— 尤其是从聊天记录里复制粘贴时极易发生。

用法：
    python scripts/diag_logfire.py            # 跳过 L4，不产生真实上报
    python scripts/diag_logfire.py --send     # 含 L4，会向你的项目投一条测试 span

退出码：0 全通过（或仅 L4 未执行）；1 有任一层失败。
"""
import argparse
import os
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"
SKIP = "[SKIP]"

LOGFIRE_HOSTS = {
    "us": "logfire-us.pydantic.dev",
    "eu": "logfire-eu.pydantic.dev",
}


def _mask(token: str) -> str:
    """只暴露区域前缀和长度，避免诊断输出被贴到聊天里泄露凭证。"""
    if not token:
        return "(空)"
    return f"{token[:11]}...(共 {len(token)} 字符)"


def _region_of(token: str) -> str:
    """token 形如 pylf_v1_us_xxx / pylf_v1_eu_xxx，区域决定该连哪个 host。

    区域选错的表现和网络不通完全一样（超时），所以必须在 L3 之前解析出来。
    """
    parts = token.split("_")
    if len(parts) >= 3 and parts[2] in LOGFIRE_HOSTS:
        return parts[2]
    return "us"


def layer1_config() -> tuple[bool, str, str]:
    print("--- L1 配置解析 ---")
    env_path = ROOT / ".env"
    if env_path.exists():
        print(f"{PASS} 找到 {env_path}")
        # 直接扫原始文本：这是唯一能区分「没配」和「配了但被注释」的方式，
        # 后者从解析结果看和前者一模一样，最容易误判。
        raw = env_path.read_text(encoding="utf-8", errors="replace")
        for key in ("LOGFIRE_TOKEN", "LOGFIRE_ENVIRONMENT"):
            active = [
                ln for ln in raw.splitlines()
                if ln.strip().startswith(f"{key}=")
            ]
            commented = [
                ln for ln in raw.splitlines()
                if ln.strip().startswith("#") and f"{key}=" in ln
            ]
            if active:
                print(f"{PASS} .env 中 {key} 为生效行")
            elif commented:
                print(f"{FAIL} .env 中 {key} 被 # 注释掉了，去掉行首 # 即可")
            else:
                print(f"{WARN} .env 中未出现 {key}")
    else:
        print(f"{WARN} 无 .env（纯环境变量注入场景属正常）")

    try:
        from app import config
    except Exception as exc:
        print(f"{FAIL} 导入 app.config 失败: {exc}")
        return False, "", ""

    token = config.LOGFIRE_TOKEN or ""
    env_name = config.LOGFIRE_ENVIRONMENT or ""
    print(f"{'     '} LOGFIRE_TOKEN       = {_mask(token)}")
    print(f"{'     '} LOGFIRE_ENVIRONMENT = {env_name or '(空)'}")
    print(f"{'     '} LOGFIRE_ENABLED     = {config.LOGFIRE_ENABLED}")

    if os.getenv("BFF_SKIP_DOTENV"):
        print(f"{WARN} BFF_SKIP_DOTENV 已设置，本进程刻意不读 .env")

    if not config.LOGFIRE_ENABLED:
        print(f"{FAIL} L1 未通过：token 为空，上报被关闭")
        return False, "", ""
    print(f"{PASS} L1 通过")
    return True, token, env_name


def layer2_deps() -> bool:
    print("\n--- L2 依赖安装 ---")
    try:
        import logfire
    except ImportError as exc:
        print(f"{FAIL} 无法导入 logfire: {exc}")
        print("       修复：pip install 'logfire[fastapi,httpx]'")
        return False
    print(f"{PASS} logfire 已安装 version={getattr(logfire, '__version__', '未知')}")

    # extras 缺失只影响自动埋点，手动 span 仍可用，所以是 WARN 而非 FAIL。
    ok = True
    for name, mod in (("fastapi", "opentelemetry.instrumentation.fastapi"),
                      ("httpx", "opentelemetry.instrumentation.httpx")):
        try:
            __import__(mod)
            print(f"{PASS} {name} 埋点依赖就绪")
        except ImportError:
            print(f"{WARN} 缺 {name} 埋点依赖，该维度不上报（服务不受影响）")
            ok = False
    if not ok:
        print("       补齐：pip install 'logfire[fastapi,httpx]'")
    return True


def layer3_network(token: str) -> bool:
    print("\n--- L3 网络可达 ---")
    region = _region_of(token)
    host = LOGFIRE_HOSTS[region]
    print(f"{'     '} token 区域 = {region} -> {host}:443")

    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443)})
        print(f"{PASS} DNS 解析成功: {', '.join(ips)}")
    except OSError as exc:
        print(f"{FAIL} DNS 解析失败: {exc}")
        return False

    # 用裸 socket + TLS 握手而不是 requests：绕开代理变量，测的是真实直连能力。
    # 走代理时 requests 的成功可能掩盖直连不通，而 OTLP exporter 是否吃代理
    # 取决于环境变量，两者不一致就会出现「诊断通过但仍无数据」。
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=12) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                print(f"{PASS} TLS 握手成功 protocol={tls.version()}")
    except Exception as exc:
        print(f"{FAIL} 无法连接 {host}:443 -> {type(exc).__name__}: {exc}")
        print("       多为本机代理/防火墙/公司网络限制；换网络或为该域名配代理白名单")
        proxies = {k: v for k, v in os.environ.items()
                   if k.lower() in ("http_proxy", "https_proxy", "no_proxy")}
        if proxies:
            print(f"       当前代理变量: {proxies}")
        return False
    return True


def layer4_send(token: str, env_name: str) -> bool:
    print("\n--- L4 端到端投递 ---")
    import logfire

    try:
        logfire.configure(
            token=token,
            environment=env_name or "diag",
            service_name="newapi-bff-diag",
            console=False,
        )
    except Exception as exc:
        print(f"{FAIL} configure 失败: {type(exc).__name__}: {exc}")
        return False
    print(f"{PASS} configure 成功")

    with logfire.span("diag_logfire_probe", source="scripts/diag_logfire.py"):
        logfire.info("Logfire 诊断脚本测试事件")

    # 不 flush 的话进程退出时数据可能还在队列里，导致「脚本说成功但控制台没有」。
    flushed = logfire.force_flush(timeout_millis=15000)
    if flushed:
        print(f"{PASS} force_flush 成功，数据已投递")
        print(f"       在控制台按 service_name=newapi-bff-diag "
              f"environment={env_name or 'diag'} 过滤查看")
        return True
    print(f"{FAIL} force_flush 超时，数据未确认送达")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Logfire 上报链路分层诊断")
    parser.add_argument("--send", action="store_true",
                        help="执行 L4，向你的 Logfire 项目投一条真实测试 span")
    args = parser.parse_args()

    print("=" * 60)
    print("Logfire 诊断".center(56))
    print("=" * 60)

    ok1, token, env_name = layer1_config()
    if not ok1:
        print("\n结论：配置层断链，后续层无需检查。")
        return 1

    if not layer2_deps():
        print("\n结论：依赖缺失，装上后重跑。")
        return 1

    net_ok = layer3_network(token)

    if not args.send:
        print(f"\n{SKIP} L4 未执行（加 --send 可做真实投递验证）")
        if not net_ok:
            print("\n结论：配置与依赖正常，网络不可达 —— 这是当前的唯一断点。")
            return 1
        print("\n结论：L1-L3 全通过。加 --send 可确认端到端投递。")
        return 0

    if not net_ok:
        print(f"\n{WARN} 网络不可达，L4 大概率超时，仍按要求尝试")

    ok4 = layer4_send(token, env_name)
    print("\n结论：" + ("全链路通过，控制台应能看到测试数据。"
                        if ok4 else "投递未成功，按上面提示排查。"))
    return 0 if ok4 else 1


if __name__ == "__main__":
    sys.exit(main())
