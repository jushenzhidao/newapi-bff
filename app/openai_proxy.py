"""OpenAI 兼容 /v1 透传层（供 LibreChat 等标准 OpenAI 客户端直连 BFF）。

背景：
  LibreChat 的 custom endpoint 是「静态 apiKey + baseURL」的标准 OpenAI 客户端，
  说不了 BFF 的会话 Cookie 协议，只认 /v1 + Bearer sk-。而 BFF 的 /v1/chat/completions
  若被 agent 业务层占用（要求 agent_id），标准 OpenAI 格式请求会被 400 拦截 ——
  本模块给这类标准客户端开一条纯透传通道，chat 项目（LibreChat 基座）的对话
  流量从这里走。

设计（对标 flovart-bff app/routers/chat.py 的聊天代理，按 LibreChat 场景裁剪）：
  - 鉴权：不解析、不缓存 —— Bearer sk- 原样转发，由 new-api 鉴权与限额。token
    在 new-api 侧本就归属用户，计费天然落到该用户 token 配额，BFF 不碰用户 Key。
    （flovart 的「按用户 mint sk- + AES 落盘」用不上：LibreChat 自管 Key 配置。）
  - 透传：请求体原始字节转发（保留字段顺序与未知字段，new-api 侧自行忽略）；
    上游非 2xx 时状态码与响应体**原样**返回 —— LibreChat 是标准 OpenAI 客户端，
    要的是原生 JSON 错误，不是 flovart 前端那种 SSE error 事件包装。
  - agent_id 分支：请求体带 agent_id 说明调用方要的是 agent 产品语义，此构建
    不含该层，明确拒绝并给出指引，而不是静默透传让 new-api 忽略 agent_id 产生
    难排查的行为差异。若合并进线上含 agent 层的新版：把本分支替换为调用原
    agent handler 即可（见 docs/v1-passthrough.md 的合并指引）。
  - 客户端 IP：转发 X-Forwarded-For/X-Real-IP。new-api 的 CriticalRateLimit 以
    mark+ClientIP() 计数，不转发的话所有用户共享 BFF 出口 IP 的限额窗口。
  - 日志：入站/出站各一条 info。日志失败不影响主流程。
    TODO(P2)：经 admin API 反查 token→uid，把调用日志挂进 BFF 的积分/用量视图。

2026-10-10 增强（时间注入 + 联网搜索，见 config 的 TIME_INJECTION / SEARXNG_*）：
  - 时间注入：每次请求向 system 消息追加当前精确时间（默认 Asia/Shanghai），
    根治「模型不知道几点」。messages 已含日期时自动跳过 —— LibreChat agent
    的 {{current_datetime}} 解析后含日期，不会重复注入。BFF_TIME_INJECTION=0 关闭。
  - 联网搜索：请求体带自定义字段 "web_search": true 时进入 function-calling
    工具循环：注入 web_search 工具 → 模型发起 tool_calls → BFF 调 SearXNG →
    结果回填 → 继续生成。该字段只在本层消费，绝不透传上游。
    客户端 stream=true 时内部以非流式跑循环，最终答案合成为标准 SSE 分片
    （role/content/finish/[DONE]），对客户端呈现为一次普通流式对话。
    未配置 BFF_SEARXNG_BASE_URL 时工具返回「不可用」提示，对话照常继续，
    不因搜索故障 500。

路由：
  GET  /v1/models               模型列表透传（LibreChat models.fetch: true）
  POST /v1/chat/completions     聊天补全透传 + 时间注入 + 可选联网搜索循环
  POST /v1/responses            Responses API 适配（LibreChat「联网搜索」原生开关
                                的实际落点：web_search 托管工具 → SearXNG 循环）
"""
import copy
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Optional, Union
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, observability

logger = logging.getLogger("bff.openai_proxy")
router = APIRouter()

_CHAT_CLIENT: Optional[httpx.AsyncClient] = None
_SEARX_CLIENT: Optional[httpx.AsyncClient] = None


def _chat_client() -> httpx.AsyncClient:
    """聊天透传专用连接池。读超时单独放宽，不能复用管理面 15s 的 na.get_client()：
    推理型模型首 token 前可能等待数分钟，15s 会掐断所有流式请求。"""
    global _CHAT_CLIENT
    if _CHAT_CLIENT is None:
        _CHAT_CLIENT = httpx.AsyncClient(
            base_url=config.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(config.CHAT_STREAM_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            trust_env=False,  # 不走本机代理，与 na.get_client 口径一致
        )
        # 出站耗时与失败是 BFF 最主要故障来源，与 na 同样埋点；未配 Logfire 时为空操作。
        observability.instrument_httpx(_CHAT_CLIENT)
    return _CHAT_CLIENT


def _searx_client() -> httpx.AsyncClient:
    """SearXNG 专用短超时连接池。搜索必须快失败：搜不到就让模型基于已有知识
    继续答，不能让它挂着把整个对话拖死。"""
    global _SEARX_CLIENT
    if _SEARX_CLIENT is None:
        _SEARX_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0), trust_env=False,
            follow_redirects=True,
        )
    return _SEARX_CLIENT


async def close() -> None:
    """归还透传/搜索连接池（lifespan 关闭时调用）。"""
    global _CHAT_CLIENT, _SEARX_CLIENT
    if _CHAT_CLIENT is not None:
        await _CHAT_CLIENT.aclose()
        _CHAT_CLIENT = None
    if _SEARX_CLIENT is not None:
        await _SEARX_CLIENT.aclose()
        _SEARX_CLIENT = None


# ==================== 时间注入 ====================

_DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


def _now_text() -> str:
    try:
        now = datetime.now(ZoneInfo(config.TIMEZONE))
    except Exception:  # noqa: BLE001 — 时区名写错时退回系统本地时区，别让报时崩对话
        now = datetime.now().astimezone()
    zh_week = "一二三四五六日"[now.weekday()]
    return f"{now:%Y-%m-%d %H:%M:%S} 周{zh_week} (UTC{now:%z})"


def _inject_time(messages) -> bool:
    """向 system 消息追加当前时间。已含日期时跳过（避免与客户端注入重复）。

    返回是否修改了 messages（调用方据此决定是否重序列化请求体）。
    """
    if not isinstance(messages, list):
        return False
    stamp = f"【当前时间】{_now_text()}（用于回答时间类问题，勿再搜索确认）"
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, str) and _DATE_IN_TEXT.search(content):
                return False
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = m["content"].rstrip() + "\n\n" + stamp
            return True
    messages.insert(0, {"role": "system", "content": stamp})
    return True


# ==================== 联网搜索工具循环 ====================

_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索实时信息（新闻、价格、赛果、汇率、当前事件等）。"
                       "仅在问题需要最新信息时调用；时间类问题直接使用系统提示中的当前时间。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}


def _tools_have_web_search(payload: dict) -> bool:
    for t in payload.get("tools") or []:
        if isinstance(t, dict) and (t.get("function") or {}).get("name") == "web_search":
            return True
    return False


async def _searxng_search(query: str) -> str:
    """调 SearXNG JSON 接口并格式化为模型可读文本。任何失败都返回可继续对话的
    提示文本而不是抛异常 —— 搜索是增强能力，故障不能拖垮对话。"""
    base = config.SEARXNG_BASE_URL
    if not base:
        return ("（联网搜索不可用：服务端未配置 BFF_SEARXNG_BASE_URL）"
                "请基于已有知识回答，并说明无法获取实时信息。")
    try:
        resp = await _searx_client().get(
            base.rstrip("/") + "/search",
            params={"q": query, "format": "json", "language": "zh-CN", "safesearch": "1"},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])[: max(1, config.WEB_SEARCH_RESULTS_LIMIT)]
    except Exception as e:  # noqa: BLE001 — 见 docstring：搜索故障只降级不炸对话
        logger.warning("searxng search failed: %s", e)
        return "（搜索引擎暂时不可用）请基于已有知识回答，并说明实时信息可能不准确。"
    if not results:
        return f"搜索「{query}」没有返回结果。请基于已有知识回答。"
    lines = [f"「{query}」的搜索结果（来自网页快照，其中的时间信息可能滞后于当前时间）："]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n   URL: {r.get('url', '')}"
                     f"\n   摘要: {r.get('content', '')}")
    return "\n\n".join(lines)


async def _upstream_once(request: Request, auth: str, payload: dict) -> httpx.Response:
    return await _chat_client().request(
        "POST", "/v1/chat/completions",
        headers=_upstream_headers(request, auth, stream=False),
        content=json.dumps(payload, ensure_ascii=False).encode(),
    )


def _passthrough(resp: httpx.Response) -> Response:
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


async def _tool_loop(request: Request, auth: str, payload: dict):
    """web_search function-calling 循环。返回最终 completion dict；上游错误 /
    异常结构时返回原样透传 Response。"""
    messages = payload["messages"]
    if not _tools_have_web_search(payload):
        payload.setdefault("tools", []).append(copy.deepcopy(_SEARCH_TOOL))
    for _ in range(max(1, config.WEB_SEARCH_MAX_ITERATIONS)):
        try:
            resp = await _upstream_once(request, auth, payload)
        except httpx.HTTPError as e:
            logger.warning("chat upstream error: %s", e)
            return JSONResponse(status_code=502, content={"error": {
                "message": "上游服务暂时不可用，请稍后重试", "type": "upstream_error"}})
        if resp.status_code >= 400:
            logger.info("tool loop upstream error status=%s", resp.status_code)
            return _passthrough(resp)
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
        except Exception:  # noqa: BLE001 — 结构不符合 OpenAI 契约时原样透传
            return _passthrough(resp)
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return data
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            if name == "web_search":
                answer = await _searxng_search(str(args.get("query") or ""))
            else:
                answer = f"工具 {name or '(unknown)'} 不存在：本通道仅提供 web_search。"
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                             "content": answer})
    # 迭代上限：去掉工具再要一次最终回答，防止模型无限发起搜索。
    logger.warning("web_search loop hit iteration limit model=%s", payload.get("model"))
    payload.pop("tools", None)
    payload.pop("tool_choice", None)
    try:
        resp = await _upstream_once(request, auth, payload)
    except httpx.HTTPError as e:
        logger.warning("chat upstream error: %s", e)
        return JSONResponse(status_code=502, content={"error": {
            "message": "上游服务暂时不可用，请稍后重试", "type": "upstream_error"}})
    if resp.status_code >= 400:
        return _passthrough(resp)
    return _passthrough(resp) if not _is_json(resp) else resp.json()


def _is_json(resp: httpx.Response) -> bool:
    return "application/json" in (resp.headers.get("content-type") or "")


def _synthetic_stream(final: dict, model: str):
    """把工具循环的最终 completion dict 合成为标准 SSE 分片序列。

    为什么不在循环里透传流式：工具调用何时发生要等完整响应才知道，边流边判
    等于自己实现一套流式工具协议。非流式跑循环 + 最终答案合成 SSE，客户端
    侧表现就是「转一会儿然后整段出字」，对 LibreChat/普通 SDK 都完全兼容。
    """
    cid = final.get("id") or f"chatcmpl-bff-{uuid.uuid4().hex[:12]}"
    created = final.get("created") or int(time.time())
    usage = final.get("usage") if isinstance(final.get("usage"), dict) else None

    def chunk(delta: dict, finish=None) -> bytes:
        c = {"id": cid, "object": "chat.completion.chunk", "created": created,
             "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage and finish:
            c["usage"] = usage
        return f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode()

    try:
        msg = final["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        msg = {"content": ""}
    yield chunk({"role": "assistant", "content": ""})
    if msg.get("reasoning_content"):
        yield chunk({"reasoning_content": msg["reasoning_content"]})
    content = msg.get("content") or ""
    if content:
        yield chunk({"content": content})
    yield chunk({}, finish="stop")
    yield b"data: [DONE]\n\n"


def _bearer(request: Request) -> Union[str, JSONResponse]:
    """取 Authorization: Bearer <token>。只做格式提取，合法性由 new-api 判定。"""
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer ") and len(auth) > 7:
        return auth
    return JSONResponse(
        status_code=401,
        content={"error": {"message": "Missing or malformed Authorization header "
                                   "(expected: Bearer sk-...)", "type": "auth_error"}},
    )


def _client_ip(request: Request) -> str:
    # 宿主机 nginx/Caddy 终止 TLS 后经 uvicorn --proxy-headers 转发时，
    # request.client.host 已是真实客户端 IP；直连场景则是 BFF 出口 IP（次优但无害）。
    return request.client.host if request.client else "unknown"


def _upstream_headers(request: Request, auth: str, *, stream: bool) -> dict:
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
        "X-Forwarded-For": _client_ip(request),
        "X-Real-IP": _client_ip(request),
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


@router.get("/v1/models")
async def list_models(request: Request):
    """模型列表透传。LibreChat models.fetch: true 靠它动态同步模型下拉。"""
    auth = _bearer(request)
    if isinstance(auth, JSONResponse):
        return auth
    try:
        resp = await _chat_client().request(
            "GET", "/v1/models", headers=_upstream_headers(request, auth, stream=False))
    except httpx.HTTPError:
        logger.warning("/v1/models upstream unreachable")
        return JSONResponse(status_code=502, content={"error": {
            "message": "上游服务暂时不可用，请稍后重试", "type": "upstream_error"}})
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """聊天补全透传。stream=true 走 SSE 字节流原样透传，否则整包转发。

    带 "web_search": true 时改走工具循环（见 _tool_loop / _synthetic_stream）。
    """
    auth = _bearer(request)
    if isinstance(auth, JSONResponse):
        return auth
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 — 原始字节进、原样转出，解析只为识别 agent_id/stream
        payload = None
    if not isinstance(payload, dict) or not payload.get("messages"):
        return JSONResponse(status_code=400, content={"error": {
            "message": "Request body must be a JSON object with a non-empty "
                       "'messages' array", "type": "invalid_request_error"}})
    if "agent_id" in payload:
        # agent 产品语义不走通用通道：静默透传会让 new-api 忽略 agent_id，
        # 表现为「带 agent_id 也回了话但行为不对」，比明确报错更难排查。
        return JSONResponse(status_code=400, content={"error": {
            "message": "agent_id 请求属 agent 产品语义，本通道为 OpenAI 兼容纯透传；"
                       "agent 调用请走对应业务端点", "type": "invalid_request_error"}})
    stream = bool(payload.get("stream"))
    model = payload.get("model") or "(default)"

    # 自定义增强字段：只在本层消费，无论开关与否都剥除，不让它到上游。
    want_search = bool(payload.pop("web_search", None))
    changed = want_search
    if config.TIME_INJECTION and _inject_time(payload["messages"]):
        changed = True
    if changed:
        raw = json.dumps(payload, ensure_ascii=False).encode()

    logger.info("/v1/chat/completions model=%s stream=%s web_search=%s ip=%s",
                model, stream, want_search, _client_ip(request))

    if want_search:
        # 工具循环内部固定按非流式跑（流式响应无法判断何时发起工具调用），
        # 必须改写请求体里的 stream 标记，否则上游仍按 SSE 返回、resp.json()
        # 解析失败会静默降级成原始流透传 —— 客户端收到 tool_calls 裸流。
        payload["stream"] = False
        payload.pop("stream_options", None)
        result = await _tool_loop(request, auth, payload)
        if isinstance(result, Response):
            return result
        if stream:
            return StreamingResponse(
                _synthetic_stream(result, model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",   # nginx 反代禁缓冲，否则 SSE 会被攒包
                    "Connection": "keep-alive",
                },
            )
        return Response(content=json.dumps(result, ensure_ascii=False).encode(),
                        media_type="application/json")

    try:
        if not stream:
            resp = await _chat_client().request(
                "POST", "/v1/chat/completions",
                headers=_upstream_headers(request, auth, stream=False), content=raw)
            logger.info("/v1/chat/completions done model=%s status=%s", model, resp.status_code)
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))

        req = _chat_client().build_request(
            "POST", "/v1/chat/completions",
            headers=_upstream_headers(request, auth, stream=True), content=raw)
        upstream = await _chat_client().send(req, stream=True)
    except httpx.HTTPError as e:
        logger.warning("chat upstream error: %s", e)
        return JSONResponse(status_code=502, content={"error": {
            "message": "上游服务暂时不可用，请稍后重试", "type": "upstream_error"}})

    if upstream.status_code >= 400:
        # 非流式错误原样回传（OpenAI 客户端按状态码+JSON 处理），必须先读完再关。
        body = await upstream.aread()
        await upstream.aclose()
        logger.info("/v1/chat/completions upstream error status=%s", upstream.status_code)
        return Response(content=body, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as e:
            # 流中途断掉只能以 SSE error 事件收尾（此时响应头已发出，改不了状态码）。
            logger.warning("chat stream interrupted: %s", e)
            yield (b'data: {"error": {"message": "upstream stream interrupted", '
                   b'"type": "upstream_error"}}\n\n')
        finally:
            await upstream.aclose()
            logger.info("/v1/chat/completions stream finished model=%s", model)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",   # nginx 反代禁缓冲，否则 SSE 会被攒包
            "Connection": "keep-alive",
        },
    )


# ==================== Responses API 适配（LibreChat web_search 原生开关） ====================
#
# 为什么需要：LibreChat custom endpoint 的「联网搜索」开关（customParams.web_search）
# 不是透传字段 —— 源码 getOpenAILLMConfig 检测到 web_search=true 时切换到 OpenAI
# Responses API（POST {baseURL}/responses）并附带托管工具 {"type":"web_search"}。
# 上游 new-api 渠道只说 chat/completions，本适配层负责两种协议的翻译：
#   请求：instructions + input（Responses 消息数组）→ chat/completions messages
#   响应：chat completion → response 对象（output_text / usage 字段换算）
# web_search 托管工具映射为与 /v1/chat/completions 同一套 SearXNG 工具循环。
# 这样 LibreChat 的原生开关（以及任何说 Responses API 的客户端）零配置接入。

_RESPONSE_MSG_ROLES = {"system", "developer", "user", "assistant"}


def _responses_has_web_search(payload: dict) -> bool:
    for t in payload.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "web_search":
            return True
    return False


def _part_text(content) -> str:
    """Responses 消息 content（字符串或分片数组）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for p in content:
        if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"]:
            parts.append(p["text"])
    return "\n".join(parts)


def _responses_input_to_messages(payload: dict) -> list:
    """Responses API 的 instructions/input → chat/completions messages。

    支持：input 为字符串；message 分片数组；历史轮次的 function_call /
    function_call_output（还原为 assistant tool_calls / tool 消息，保证多轮
    工具对话上下文不丢）。reasoning / web_search_call 等对补全无意义的项丢弃。
    """
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    raw = payload.get("input")
    if isinstance(raw, str):
        if raw:
            messages.append({"role": "user", "content": raw})
        return messages
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type") or "message"
        if itype == "message":
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"
            elif role not in _RESPONSE_MSG_ROLES:
                role = "user"
            text = _part_text(item.get("content"))
            if role == "assistant" and not text:
                continue  # 空 assistant 占位项对补全无意义
            messages.append({"role": role, "content": text})
        elif itype == "function_call":
            messages.append({"role": "assistant", "content": "", "tool_calls": [{
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": item.get("name") or "web_search",
                             "arguments": item.get("arguments") or "{}"},
            }]})
        elif itype == "function_call_output":
            out = item.get("output")
            messages.append({"role": "tool",
                             "tool_call_id": item.get("call_id") or "",
                             "content": out if isinstance(out, str) else json.dumps(out or "", ensure_ascii=False)})
    return messages


def _responses_to_chat_payload(payload: dict) -> dict:
    """Responses 请求体 → chat/completions 请求体（只保留上游认识的白名单字段）。"""
    chat = {"model": payload.get("model") or "",
            "messages": _responses_input_to_messages(payload)}
    for k in ("temperature", "top_p", "user"):
        if payload.get(k) is not None:
            chat[k] = payload[k]
    mt = payload.get("max_output_tokens") or payload.get("max_tokens")
    if mt:
        chat["max_tokens"] = mt
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat["reasoning_effort"] = reasoning["effort"]
    return chat


def _resp_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _usage_from_chat(usage) -> Optional[dict]:
    """chat/completions usage → Responses usage 字段换算。"""
    if not isinstance(usage, dict):
        return None
    it = usage.get("prompt_tokens") or 0
    ot = usage.get("completion_tokens") or 0
    total = usage.get("total_tokens")
    return {"input_tokens": it, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": ot, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": total if total is not None else it + ot}


def _response_object(rid: str, model: str, text: str, *, usage=None,
                     search_calls: int = 0, status: str = "completed") -> dict:
    output = []
    if search_calls:
        output.append({"type": "web_search_call", "id": f"ws_{rid[-8:]}",
                       "status": "completed"})
    output.append({"type": "message", "id": f"msg_{rid[-8:]}", "role": "assistant",
                   "status": status,
                   "content": [{"type": "output_text", "text": text, "annotations": []}]})
    obj = {"id": rid, "object": "response", "created_at": int(time.time()),
           "status": status, "model": model, "output": output, "store": False}
    if usage:
        obj["usage"] = usage
    return obj


def _sse_event(etype: str, data: dict) -> bytes:
    return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _completion_text(final: dict) -> str:
    try:
        return final["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


_STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _synthetic_responses_stream(final: dict, model: str, search_calls: int = 0):
    """工具循环最终结果 → Responses SSE 事件序列（标准事件全序列，客户端
    拿到的是一段完整的 output_text）。"""
    rid = _resp_id()
    msg_id = f"msg_{rid[-8:]}"
    usage = _usage_from_chat(final.get("usage"))
    text = _completion_text(final)
    yield _sse_event("response.created", {"type": "response.created", "response":
                    _response_object(rid, model, "", status="in_progress")})
    yield _sse_event("response.output_item.added", {
        "type": "response.output_item.added", "output_index": 0,
        "item": {"type": "message", "id": msg_id, "role": "assistant",
                 "status": "in_progress", "content": []}})
    yield _sse_event("response.content_part.added", {
        "type": "response.content_part.added", "item_id": msg_id, "output_index": 0,
        "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
    if text:
        yield _sse_event("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": msg_id,
            "output_index": 0, "content_index": 0, "delta": text})
    yield _sse_event("response.output_text.done", {
        "type": "response.output_text.done", "item_id": msg_id,
        "output_index": 0, "content_index": 0, "text": text})
    done_part = {"type": "output_text", "text": text, "annotations": []}
    yield _sse_event("response.content_part.done", {
        "type": "response.content_part.done", "item_id": msg_id,
        "output_index": 0, "content_index": 0, "part": done_part})
    yield _sse_event("response.output_item.done", {
        "type": "response.output_item.done", "output_index": 0,
        "item": {"type": "message", "id": msg_id, "role": "assistant",
                 "status": "completed", "content": [done_part]}})
    yield _sse_event("response.completed", {"type": "response.completed", "response":
                    _response_object(rid, model, text, usage=usage,
                                     search_calls=search_calls)})


async def _convert_chat_sse_to_responses(upstream: httpx.Response, model: str):
    """无搜索场景的实时流：上游 chat/completions SSE → Responses 事件流，
    逐 delta 转换，保留真实打字机效果。"""
    rid = _resp_id()
    msg_id = f"msg_{rid[-8:]}"
    yield _sse_event("response.created", {"type": "response.created", "response":
                    _response_object(rid, model, "", status="in_progress")})
    yield _sse_event("response.output_item.added", {
        "type": "response.output_item.added", "output_index": 0,
        "item": {"type": "message", "id": msg_id, "role": "assistant",
                 "status": "in_progress", "content": []}})
    yield _sse_event("response.content_part.added", {
        "type": "response.content_part.added", "item_id": msg_id, "output_index": 0,
        "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
    text_all, finish, usage = "", None, None
    buf = b""
    try:
        async for chunk in upstream.aiter_bytes():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    ev = json.loads(data)
                except Exception:  # noqa: BLE001, S112 — 非 JSON 心跳行直接跳过
                    continue
                if ev.get("error"):
                    yield _sse_event("error", {"type": "error",
                                               "code": "upstream_error",
                                               "message": "upstream stream error"})
                    return
                choices = ev.get("choices") or []
                if not choices:
                    if ev.get("usage"):
                        usage = _usage_from_chat(ev["usage"])
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    text_all += piece
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": msg_id,
                        "output_index": 0, "content_index": 0, "delta": piece})
                if choices[0].get("finish_reason"):
                    finish = choices[0]["finish_reason"]
    except httpx.HTTPError as e:
        logger.warning("responses stream interrupted: %s", e)
        yield _sse_event("error", {"type": "error", "code": "upstream_error",
                                   "message": "upstream stream interrupted"})
        return
    finally:
        await upstream.aclose()
    text_all = text_all if finish != "length" else text_all + "（因长度截断）"
    yield _sse_event("response.output_text.done", {
        "type": "response.output_text.done", "item_id": msg_id,
        "output_index": 0, "content_index": 0, "text": text_all})
    done_part = {"type": "output_text", "text": text_all, "annotations": []}
    yield _sse_event("response.content_part.done", {
        "type": "response.content_part.done", "item_id": msg_id,
        "output_index": 0, "content_index": 0, "part": done_part})
    yield _sse_event("response.output_item.done", {
        "type": "response.output_item.done", "output_index": 0,
        "item": {"type": "message", "id": msg_id, "role": "assistant",
                 "status": "completed", "content": [done_part]}})
    yield _sse_event("response.completed", {"type": "response.completed", "response":
                    _response_object(rid, model, text_all, usage=usage)})


@router.post("/v1/responses")
async def responses_api(request: Request):
    """Responses API 适配端点。web_search 托管工具 → SearXNG 工具循环。

    带搜索：内部固定非流式跑循环（同 /v1/chat/completions 的理由），客户端
    stream=true 时合成完整 Responses 事件序列。
    无搜索：真实流式转发 + 逐 delta 协议转换（保留打字机效果）；非流式转换
    为 response 对象后返回。
    """
    auth = _bearer(request)
    if isinstance(auth, JSONResponse):
        return auth
    try:
        payload = json.loads(await request.body())
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict) or "input" not in payload:
        return JSONResponse(status_code=400, content={"error": {
            "message": "Request body must be a JSON object with an 'input' field",
            "type": "invalid_request_error"}})
    model = payload.get("model") or "(default)"
    stream = bool(payload.get("stream"))
    want_search = _responses_has_web_search(payload)
    chat = _responses_to_chat_payload(payload)
    if config.TIME_INJECTION:
        _inject_time(chat["messages"])
    logger.info("/v1/responses model=%s stream=%s web_search=%s ip=%s",
                model, stream, want_search, _client_ip(request))

    if want_search:
        chat["stream"] = False
        chat.pop("stream_options", None)
        result = await _tool_loop(request, auth, chat)
        if isinstance(result, Response):
            return result
        search_calls = sum(1 for m in chat["messages"]
                           if isinstance(m, dict) and m.get("role") == "tool")
        if stream:
            return StreamingResponse(
                _synthetic_responses_stream(result, model, search_calls),
                media_type="text/event-stream", headers=_STREAM_HEADERS)
        return Response(content=json.dumps(
            _response_object(_resp_id(), model, _completion_text(result),
                             usage=_usage_from_chat(result.get("usage")),
                             search_calls=search_calls),
            ensure_ascii=False).encode(), media_type="application/json")

    req = _chat_client().build_request(
        "POST", "/v1/chat/completions",
        headers=_upstream_headers(request, auth, stream=stream),
        content=json.dumps(chat, ensure_ascii=False).encode())
    try:
        upstream = await _chat_client().send(req, stream=stream)
    except httpx.HTTPError as e:
        logger.warning("responses upstream error: %s", e)
        return JSONResponse(status_code=502, content={"error": {
            "message": "上游服务暂时不可用，请稍后重试", "type": "upstream_error"}})
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        logger.info("/v1/responses upstream error status=%s", upstream.status_code)
        return Response(content=body, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))
    if stream:
        return StreamingResponse(
            _convert_chat_sse_to_responses(upstream, model),
            media_type="text/event-stream", headers=_STREAM_HEADERS)
    try:
        completion = upstream.json()
        text = _completion_text(completion)
        usage = _usage_from_chat(completion.get("usage"))
    except Exception:  # noqa: BLE001 — 结构不符 OpenAI 契约时原样回传
        return Response(content=upstream.content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))
    return Response(content=json.dumps(
        _response_object(_resp_id(), model, text, usage=usage),
        ensure_ascii=False).encode(), media_type="application/json")
