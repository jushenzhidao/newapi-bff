"""OpenAI 兼容 /v1 透传层测试（tests 对标 test_health.py 的「契约守卫」风格）。

守的契约：
  - 鉴权只提取不校验：无/坏 Authorization → 401 OpenAI 风格错误（不碰上游）
  - agent_id 请求明确拒绝（agent 语义不进通用通道）
  - 请求体逐字节透传（含未知字段，时间注入关闭时），Authorization 原样转发
  - 上游非 2xx → 状态码与响应体原样回传（LibreChat 按原生 OpenAI 错误处理）
  - stream=true → SSE 字节流透传 + no-transform / X-Accel-Buffering 头
  - 时间注入：默认向 system 消息追加当前时间；已含日期时跳过；可整体关闭
  - web_search: true → 工具循环（自定义字段剥除不上送；tool_calls 执行 SearXNG
    回填；stream 客户端拿到合成 SSE；未配搜索后端优雅降级；迭代上限强制收敛）
"""
import json

import httpx
import pytest

from app import config, openai_proxy

SK = "Bearer sk-test-passthrough"


class UpstreamRecorder:
    """记录上游收到的请求，按预设返回响应。"""

    def __init__(self, respond):
        self.requests = []
        self._respond = respond

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


@pytest.fixture
def upstream(monkeypatch):
    """把透传连接池替换为 MockTransport 版本，避免真实出网。"""
    def install(respond):
        rec = UpstreamRecorder(respond)
        mock_client = httpx.AsyncClient(
            base_url="https://new-api.test",
            transport=httpx.MockTransport(rec.handler),
        )
        monkeypatch.setattr(openai_proxy, "_CHAT_CLIENT", mock_client)
        return rec
    yield install
    # 生命周期收尾会把全局连接池关掉，清回 None 防止跨用例串味
    openai_proxy._CHAT_CLIENT = None


class _ListAsyncStream(httpx.AsyncByteStream):
    """显式字节流。不用 content= 构造 SSE mock 响应：部分环境给 httpx 打了
    「ByteStream 构造即 read()」的补丁，content= 会把流标成已消费，aiter_raw 直接
    StreamConsumed。stream= 构造在原版与补丁版 httpx 下行为一致。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def sse_respond(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_ListAsyncStream([
            b'data: {"choices": [{"delta": {"content": "OK"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]),
    )


def test_missing_auth_returns_401_without_upstream(client, upstream):
    rec = upstream(sse_respond)
    r = client.post("/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "auth_error"
    assert not rec.requests  # 鉴权格式错时绝不碰上游


def test_agent_id_rejected(client, upstream):
    rec = upstream(sse_respond)
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": SK},
                    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
                          "agent_id": "ag-1"})
    assert r.status_code == 400
    assert "agent_id" in r.json()["error"]["message"]
    assert not rec.requests  # agent 语义在 BFF 层拦截，不到 new-api


def test_nonstream_passthrough_byte_exact(client, upstream, monkeypatch):
    # 本用例守「逐字节透传」契约，先关掉会改写 body 的时间注入
    monkeypatch.setattr(config, "TIME_INJECTION", False)

    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "chatcmpl-1", "choices": [
            {"message": {"content": "你好"}}]})

    rec = upstream(respond)
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
            "stream": False, "unknown_future_field": {"keep": True}}
    r = client.post("/v1/chat/completions", headers={"Authorization": SK}, json=body)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "你好"
    # 上游收到的是原样转发的鉴权头与未知字段
    sent = rec.requests[0]
    assert sent.headers["authorization"] == SK
    assert b"unknown_future_field" in sent.content


def test_upstream_error_passthrough_verbatim(client, upstream):
    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited",
                                                   "type": "rate_limit_error"}})

    upstream(respond)
    r = client.post("/v1/chat/completions", headers={"Authorization": SK},
                    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"


def test_stream_passthrough_sse_headers(client, upstream):
    rec = upstream(sse_respond)
    with client.stream(
        "POST", "/v1/chat/completions", headers={"Authorization": SK},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
              "stream": True},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "no-transform" in r.headers["cache-control"]
        assert r.headers["x-accel-buffering"] == "no"
        chunks = list(r.iter_raw())
    joined = b"".join(chunks)
    assert b'"delta"' in joined and b"[DONE]" in joined
    sent = rec.requests[0]
    assert sent.headers["authorization"] == SK
    assert sent.headers["accept"] == "text/event-stream"
    # XFF 转发：new-api 的 CriticalRateLimit 按客户端 IP 计数，不能共享 BFF 出口窗口
    assert sent.headers.get("x-forwarded-for")


def test_models_passthrough(client, upstream):
    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "claude-4-5"}]})

    upstream(respond)
    r = client.get("/v1/models", headers={"Authorization": SK})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["gpt-4o", "claude-4-5"]


# ==================== 时间注入 ====================

def test_time_injected_into_system_message(client, upstream):
    """默认开启：无日期信息的请求，时间被追加进已有 system 消息。"""
    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": [
            {"message": {"content": "hi"}}]})

    rec = upstream(respond)
    r = client.post("/v1/chat/completions", headers={"Authorization": SK},
                    json={"model": "m", "messages": [
                        {"role": "system", "content": "你是一个助手"},
                        {"role": "user", "content": "现在几点"}]})
    assert r.status_code == 200
    sent = json.loads(rec.requests[0].content)
    sys_msg = sent["messages"][0]["content"]
    assert "【当前时间】" in sys_msg
    assert "你是一个助手" in sys_msg          # 原指令不被覆盖
    assert sent["messages"][1]["content"] == "现在几点"  # 用户消息不动


def test_time_injection_prepends_system_when_absent(client, upstream):
    """没有 system 消息时自动补一条，插在最前面。"""
    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": [
            {"message": {"content": "hi"}}]})

    rec = upstream(respond)
    client.post("/v1/chat/completions", headers={"Authorization": SK},
                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    sent = json.loads(rec.requests[0].content)
    assert sent["messages"][0]["role"] == "system"
    assert "【当前时间】" in sent["messages"][0]["content"]


def test_time_injection_skipped_when_date_present(client, upstream):
    """system 已含日期（如 LibreChat agent 的 {{current_datetime}} 已解析）→ 跳过。"""
    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": [
            {"message": {"content": "hi"}}]})

    rec = upstream(respond)
    client.post("/v1/chat/completions", headers={"Authorization": SK},
                json={"model": "m", "messages": [
                    {"role": "system", "content": "当前时间：2026-01-01 00:00:00"},
                    {"role": "user", "content": "hi"}]})
    sent = json.loads(rec.requests[0].content)
    assert "【当前时间】" not in sent["messages"][0]["content"]
    assert sent["messages"][0]["content"] == "当前时间：2026-01-01 00:00:00"


# ==================== 联网搜索工具循环 ====================

_TOOL_RESP = {"id": "c1", "choices": [{"message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_1", "type": "function",
                    "function": {"name": "web_search",
                                 "arguments": "{\"query\": \"今天新闻\"}"}}]}}]}
_FINAL_RESP = {"id": "c2", "choices": [{"message": {
    "role": "assistant", "content": "这是答案"}}],
    "usage": {"total_tokens": 42}}


def _search_recorder_factory(always_tool_calls: bool = False):
    """两段式上游：第一轮回 tool_calls，其后回最终回答；always_tool_calls=True
    则永远回 tool_calls（用于测迭代上限强制收敛）。"""
    state = {"n": 0}

    def respond(_req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if always_tool_calls or state["n"] == 1:
            return httpx.Response(200, json=_TOOL_RESP)
        return httpx.Response(200, json=_FINAL_RESP)
    return respond


def test_web_search_loop_executes_and_backfills(client, upstream, monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "http://searx.test")
    monkeypatch.setattr(openai_proxy, "_searxng_search",
                        _async_return("搜索结果：某新闻"))

    rec = upstream(_search_recorder_factory())
    r = client.post("/v1/chat/completions", headers={"Authorization": SK},
                    json={"model": "m", "stream": False, "web_search": True,
                          "messages": [{"role": "user", "content": "今天有什么新闻"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "这是答案"
    # 两轮上游调用：第一轮 tool_calls → 第二轮回填搜索结果后的最终生成
    assert len(rec.requests) == 2
    second = json.loads(rec.requests[1].content)
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_1",
                                      "content": "搜索结果：某新闻"}
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_1"
    # 自定义字段绝不透传上游（工具定义里的函数名 web_search 是合法存在的）
    assert "web_search" not in json.loads(rec.requests[0].content)
    assert "web_search" not in json.loads(rec.requests[1].content)


def test_web_search_stream_synthesizes_sse(client, upstream, monkeypatch):
    """stream 客户端 + web_search：内部循环后合成标准 SSE 分片。"""
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "http://searx.test")
    monkeypatch.setattr(openai_proxy, "_searxng_search", _async_return("结果"))

    rec = upstream(_search_recorder_factory())
    with client.stream(
        "POST", "/v1/chat/completions", headers={"Authorization": SK},
        json={"model": "m", "stream": True, "web_search": True,
              "messages": [{"role": "user", "content": "今天有什么新闻"}]},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        joined = b"".join(list(r.iter_raw())).decode()
    compact = joined.replace(" ", "")
    assert '"delta":{"role":"assistant"' in compact
    assert "这是答案" in joined
    assert '"finish_reason":"stop"' in compact
    assert "[DONE]" in joined
    # 循环照常发生，只是输出被合成；且上游收到的 body 必须已改成 stream=false
    assert len(rec.requests) == 2
    assert json.loads(rec.requests[0].content)["stream"] is False


def test_web_search_without_searxng_degrades_gracefully(client, upstream, monkeypatch):
    """未配搜索后端：工具返回「未配置」提示文本，对话继续而不是 500。"""
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "")

    rec = upstream(_search_recorder_factory())
    r = client.post("/v1/chat/completions", headers={"Authorization": SK},
                    json={"model": "m", "web_search": True,
                          "messages": [{"role": "user", "content": "今天有什么新闻"}]})
    assert r.status_code == 200
    second = json.loads(rec.requests[1].content)
    assert "未配置" in second["messages"][-1]["content"]


def test_web_search_iteration_limit_forces_final(client, upstream, monkeypatch):
    """模型无限发起搜索 → 到达轮数上限后去掉工具强制收敛。"""
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "http://searx.test")
    monkeypatch.setattr(config, "WEB_SEARCH_MAX_ITERATIONS", 1)
    monkeypatch.setattr(openai_proxy, "_searxng_search", _async_return("结果"))

    rec = upstream(_search_recorder_factory(always_tool_calls=True))
    r = client.post("/v1/chat/completions", headers={"Authorization": SK},
                    json={"model": "m", "web_search": True,
                          "messages": [{"role": "user", "content": "搜"}]})
    assert r.status_code == 200
    last = json.loads(rec.requests[-1].content)
    assert "tools" not in last        # 强制收敛：不再给模型工具
    assert len(rec.requests) == 2     # 1 轮循环 + 1 次强制收尾


def test_real_searxng_response_parsed(client, upstream, monkeypatch):
    """真实 SearXNG JSON 结构解析：标题/URL/摘要进 tool 结果。"""
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "http://searx.test")
    searx = httpx.AsyncClient(
        base_url="http://searx.test",
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={
            "results": [{"title": "北京时间新闻", "url": "https://news.test/1",
                         "content": "今天发生了……"}]})),
    )
    monkeypatch.setattr(openai_proxy, "_SEARX_CLIENT", searx)

    rec = upstream(_search_recorder_factory())
    client.post("/v1/chat/completions", headers={"Authorization": SK},
                json={"model": "m", "web_search": True,
                      "messages": [{"role": "user", "content": "今天有什么新闻"}]})
    second = json.loads(rec.requests[1].content)
    tool_text = second["messages"][-1]["content"]
    assert "北京时间新闻" in tool_text
    assert "https://news.test/1" in tool_text
    openai_proxy._SEARX_CLIENT = None  # 防跨用例串味


def _async_return(value):
    async def _search(_query):
        return value
    return _search


# ==================== Responses API 适配（LibreChat web_search 原生开关） ====================

def test_responses_plain_converts_instructions_and_input(client, upstream, monkeypatch):
    """无搜索：instructions→system 首条、input 字符串→user 消息；响应转 response 对象。"""
    monkeypatch.setattr(config, "TIME_INJECTION", False)  # 隔离时间注入，专注协议转换

    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "chatcmpl-9", "choices": [
            {"message": {"content": "你好呀"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    rec = upstream(respond)
    r = client.post("/v1/responses", headers={"Authorization": SK},
                    json={"model": "m", "stream": False,
                          "instructions": "你是助手",
                          "input": "在吗",
                          "store": False, "reasoning": {"effort": "low"}})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "response" and body["status"] == "completed"
    msg = body["output"][0]
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["content"][0]["type"] == "output_text"
    assert msg["content"][0]["text"] == "你好呀"
    assert body["usage"]["total_tokens"] == 8
    # 上游收到的是 chat/completions 格式：instructions→system、input→user
    sent = json.loads(rec.requests[0].content)
    assert sent["messages"] == [{"role": "system", "content": "你是助手"},
                                {"role": "user", "content": "在吗"}]
    assert "input" not in sent and "instructions" not in sent
    assert "store" not in sent and sent["model"] == "m"


def test_responses_input_array_and_tool_history(client, upstream, monkeypatch):
    """input 数组：消息分片取 text 汇总；历史 function_call/output 还原为
    assistant tool_calls / tool 消息（多轮工具对话上下文不丢）。"""
    monkeypatch.setattr(config, "TIME_INJECTION", False)

    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "c", "choices": [
            {"message": {"content": "done"}}]})

    rec = upstream(respond)
    r = client.post("/v1/responses", headers={"Authorization": SK}, json={
        "model": "m", "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "第一段"},
                          {"type": "input_text", "text": "第二段"}]},
            {"type": "message", "role": "assistant", "content": "我先搜"},
            {"type": "function_call", "call_id": "call_7", "name": "web_search",
             "arguments": "{\"query\": \"q\"}"},
            {"type": "function_call_output", "call_id": "call_7", "output": "搜索结果文本"},
            {"type": "reasoning", "summary": []},  # 无意义项应被丢弃
        ]})
    assert r.status_code == 200
    sent = json.loads(rec.requests[0].content)["messages"]
    assert sent[0] == {"role": "user", "content": "第一段\n第二段"}
    assert sent[1] == {"role": "assistant", "content": "我先搜"}
    assert sent[2]["tool_calls"][0]["id"] == "call_7"
    assert sent[3] == {"role": "tool", "tool_call_id": "call_7", "content": "搜索结果文本"}
    assert len(sent) == 4  # reasoning 项没有混进来


def test_responses_web_search_runs_tool_loop(client, upstream, monkeypatch):
    """tools 含 {"type":"web_search"}（Responses 托管工具格式）→ 走 SearXNG 循环，
    非流式返回 response 对象（含 web_search_call 项与 usage 换算）。"""
    monkeypatch.setattr(config, "TIME_INJECTION", False)
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "http://searx.test")
    monkeypatch.setattr(openai_proxy, "_searxng_search", _async_return("搜索结果：某新闻"))

    rec = upstream(_search_recorder_factory())
    r = client.post("/v1/responses", headers={"Authorization": SK},
                    json={"model": "m", "stream": False,
                          "input": [{"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": "今天有什么新闻"}]}],
                          "tools": [{"type": "web_search"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    types = [o["type"] for o in body["output"]]
    assert "web_search_call" in types and "message" in types
    text_part = body["output"][-1]["content"][0]
    assert text_part["text"] == "这是答案"
    assert body["usage"]["total_tokens"] == 42  # chat usage → Responses usage 换算
    # 上游两轮调用：首轮带 function 工具、末轮带搜索回填的 tool 消息
    assert len(rec.requests) == 2
    first = json.loads(rec.requests[0].content)
    assert first["messages"][0]["content"] == "今天有什么新闻"
    assert first["tools"][0]["type"] == "function"  # 托管格式已换成 function 工具
    last = json.loads(rec.requests[1].content)
    assert last["messages"][-1] == {"role": "tool", "tool_call_id": "call_1",
                                    "content": "搜索结果：某新闻"}


def test_responses_stream_web_search_synthesizes_events(client, upstream, monkeypatch):
    """stream + web_search：合成完整 Responses 事件序列，上游按非流式跑循环。"""
    monkeypatch.setattr(config, "TIME_INJECTION", False)
    monkeypatch.setattr(config, "SEARXNG_BASE_URL", "http://searx.test")
    monkeypatch.setattr(openai_proxy, "_searxng_search", _async_return("结果"))

    rec = upstream(_search_recorder_factory())
    with client.stream("POST", "/v1/responses", headers={"Authorization": SK},
                       json={"model": "m", "stream": True,
                             "input": "今天有什么新闻",
                             "tools": [{"type": "web_search"}]}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        joined = b"".join(list(r.iter_raw())).decode()
    for etype in ("response.created", "response.output_item.added",
                  "response.output_text.delta", "response.output_text.done",
                  "response.completed"):
        assert f"event: {etype}" in joined
    assert "这是答案" in joined
    assert len(rec.requests) == 2
    assert json.loads(rec.requests[0].content)["stream"] is False


def test_responses_stream_plain_converts_chunks_live(client, upstream, monkeypatch):
    """无搜索 stream：上游 chat SSE 逐 delta 实时转换，保留打字机效果。"""
    monkeypatch.setattr(config, "TIME_INJECTION", False)  # 隔离时间注入，专注断言消息转换
    rec = upstream(sse_respond)  # 上游回 content "OK" 两片流
    with client.stream("POST", "/v1/responses", headers={"Authorization": SK},
                       json={"model": "m", "stream": True, "input": "hi"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        joined = b"".join(list(r.iter_raw())).decode()
    assert 'event: response.output_text.delta' in joined
    assert '"delta": "OK"' in joined.replace("  ", " ") or '"delta":"OK"' in joined.replace(" ", "")
    assert "event: response.completed" in joined
    assert json.loads(rec.requests[0].content)["messages"] == [
        {"role": "user", "content": "hi"}]


def test_responses_upstream_error_passthrough(client, upstream):
    def respond(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited",
                                                   "type": "rate_limit_error"}})

    upstream(respond)
    r = client.post("/v1/responses", headers={"Authorization": SK},
                    json={"model": "m", "input": "hi"})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"


def test_responses_missing_input_rejected(client, upstream):
    rec = upstream(sse_respond)
    r = client.post("/v1/responses", headers={"Authorization": SK},
                    json={"model": "m"})
    assert r.status_code == 400
    assert not rec.requests
