#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置 API 协议路由的端到端测试（假上游，不需要外网）。"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.api_format_router import (
    FMT_ANTHROPIC,
    FMT_CHAT,
    FMT_RESPONSES,
    anthropic_response_to_chat,
    chat_request_to_ir,
    chat_to_anthropic_response,
    chat_to_responses_response,
    ensure_router,
    ir_to_anthropic_request,
    ir_to_responses_request,
    messages_request_to_ir,
    responses_request_to_ir,
    responses_response_to_chat,
    stop_router,
)


# ------------------------------------------------------------
# 假上游服务器
# ------------------------------------------------------------

class _UpstreamHandler(BaseHTTPRequestHandler):
    fmt = FMT_CHAT
    stream = False

    def log_message(self, *_args):
        pass

    def _json(self, payload, status=200, content_type="application/json"):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        self.server.last_request = body  # type: ignore[attr-defined]
        if self.stream:
            return self._sse()
        if self.fmt == FMT_ANTHROPIC:
            return self._json({
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", ""),
                "content": [{"type": "text", "text": "anthropic 回答"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            })
        if self.fmt == FMT_RESPONSES:
            return self._json({
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": body.get("model", ""),
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "responses 回答"}],
                }],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            })
        return self._json({
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", ""),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "chat 回答"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                      "total_tokens": 5},
        })

    def _sse(self):
        events = [
            ("message_start", {"type": "message_start",
                               "message": {"id": "msg_1"}}),
            ("content_block_start",
             {"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta",
             {"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "流式"}}),
            ("content_block_delta",
             {"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "回答"}}),
            ("message_delta",
             {"type": "message_delta",
              "delta": {"stop_reason": "end_turn"}}),
        ]
        payload = b""
        for event, data in events:
            payload += (f"event: {event}\ndata: "
                        f"{json.dumps(data, ensure_ascii=False)}\n\n"
                        ).encode("utf-8")
        payload += b"event: message_stop\ndata: {}\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_upstream(fmt: str, stream: bool = False) -> ThreadingHTTPServer:
    handler = type("H", (_UpstreamHandler,), {"fmt": fmt, "stream": stream})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.last_request = None  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(url: str, payload: dict, stream: bool = False):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return resp.status, resp.headers.get("Content-Type", ""), raw


ROUTER_PORT = 15799


@pytest.fixture()
def router():
    yield f"http://127.0.0.1:{ROUTER_PORT}/v1"
    stop_router(ROUTER_PORT)


# ------------------------------------------------------------
# 转换函数单测
# ------------------------------------------------------------

def test_ir_from_chat():
    ir = chat_request_to_ir({
        "model": "m", "stream": True,
        "messages": [{"role": "system", "content": "s"},
                     {"role": "user", "content": "u"}],
    })
    assert ir["model"] == "m" and ir["stream"] is True
    assert len(ir["messages"]) == 2


def test_ir_from_anthropic_messages():
    ir = messages_request_to_ir({
        "model": "claude", "max_tokens": 100,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "你好"}]}],
        "tools": [{"name": "get", "description": "d",
                   "input_schema": {"type": "object"}}],
    })
    assert ir["messages"][0] == {"role": "system", "content": "sys"}
    assert ir["messages"][1]["content"] == "你好"
    assert ir["tools"][0]["function"]["name"] == "get"
    assert ir["max_tokens"] == 100


def test_ir_from_responses():
    ir = responses_request_to_ir({
        "model": "r", "instructions": "sys",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "hi"}]}],
    })
    assert ir["messages"][0] == {"role": "system", "content": "sys"}
    assert ir["messages"][1]["content"] == "hi"


def test_ir_to_anthropic_serializes_tools():
    ir = chat_request_to_ir({
        "model": "m",
        "messages": [{"role": "user", "content": "u"}],
        "tools": [{"type": "function",
                   "function": {"name": "f",
                                "parameters": {"type": "object"}}}],
    })
    body = ir_to_anthropic_request(ir, "claude-x")
    assert body["model"] == "claude-x"
    assert body["max_tokens"] >= 1
    assert body["tools"][0]["name"] == "f"
    assert body["tools"][0]["input_schema"] == {"type": "object"}


def test_ir_to_responses_serializes():
    ir = chat_request_to_ir({
        "model": "m",
        "messages": [{"role": "system", "content": "s"},
                     {"role": "user", "content": "u"}],
        "max_tokens": 50,
    })
    body = ir_to_responses_request(ir, "gpt-x")
    assert body["instructions"] == "s"
    assert body["input"][0]["type"] == "message"
    assert body["max_output_tokens"] == 50


def test_anthropic_response_to_chat():
    chat = anthropic_response_to_chat({
        "id": "msg_1", "model": "claude", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "答"}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    assert chat["choices"][0]["message"]["content"] == "答"
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert chat["usage"]["prompt_tokens"] == 1


def test_responses_response_to_chat():
    chat = responses_response_to_chat({
        "id": "r1", "model": "gpt", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "答"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    assert chat["choices"][0]["message"]["content"] == "答"


def test_chat_to_anthropic_response():
    out = chat_to_anthropic_response({
        "id": "c1", "model": "m",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "答"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }, {"model": "m"})
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "答"
    assert out["stop_reason"] == "end_turn"


def test_chat_to_responses_response():
    out = chat_to_responses_response({
        "id": "c1", "model": "m",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "答"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }, {"model": "m"})
    assert out["object"] == "response"
    assert out["output"][0]["content"][0]["text"] == "答"


# ------------------------------------------------------------
# 端到端：chat 入站 → 各格式上游
# ------------------------------------------------------------

def test_chat_inbound_to_anthropic_upstream(router):
    upstream = _start_upstream(FMT_ANTHROPIC)
    try:
        endpoint = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        ensure_router([{"id": "claude-a", "endpoint": endpoint,
                        "api_key": "k", "model": "claude-x",
                        "format": FMT_ANTHROPIC}], port=ROUTER_PORT)
        status, _ctype, raw = _post(
            f"{router}/chat/completions",
            {"model": "claude-a",
             "messages": [{"role": "user", "content": "你好"}]})
        assert status == 200
        data = json.loads(raw.decode("utf-8"))
        assert data["choices"][0]["message"]["content"] == "anthropic 回答"
        # 上游确实收到 anthropic 格式
        sent = upstream.last_request
        assert sent is not None
        assert "max_tokens" in sent
        assert sent["messages"][0]["role"] == "user"
    finally:
        upstream.shutdown()


def test_chat_inbound_to_responses_upstream(router):
    upstream = _start_upstream(FMT_RESPONSES)
    try:
        endpoint = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        ensure_router([{"id": "resp-a", "endpoint": endpoint,
                        "api_key": "k", "model": "gpt-x",
                        "format": FMT_RESPONSES}], port=ROUTER_PORT)
        status, _ctype, raw = _post(
            f"{router}/chat/completions",
            {"model": "resp-a",
             "messages": [{"role": "user", "content": "你好"}]})
        assert status == 200
        data = json.loads(raw.decode("utf-8"))
        assert data["choices"][0]["message"]["content"] == "responses 回答"
        assert upstream.last_request["input"]
    finally:
        upstream.shutdown()


def test_chat_inbound_streaming_from_anthropic(router):
    upstream = _start_upstream(FMT_ANTHROPIC, stream=True)
    try:
        endpoint = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        ensure_router([{"id": "claude-s", "endpoint": endpoint,
                        "api_key": "k", "model": "claude-x",
                        "format": FMT_ANTHROPIC}], port=ROUTER_PORT)
        status, ctype, raw = _post(
            f"{router}/chat/completions",
            {"model": "claude-s", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]})
        assert status == 200
        assert "text/event-stream" in ctype
        text = raw.decode("utf-8")
        assert "流式" in text and "回答" in text
        assert "data: [DONE]" in text
        assert '"finish_reason": "stop"' in text
    finally:
        upstream.shutdown()


def test_messages_inbound_to_chat_upstream(router):
    upstream = _start_upstream(FMT_CHAT)
    try:
        endpoint = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        ensure_router([{"id": "chat-a", "endpoint": endpoint,
                        "api_key": "k", "model": "gpt-x",
                        "format": FMT_CHAT}], port=ROUTER_PORT)
        status, _ctype, raw = _post(
            f"{router}/messages",
            {"model": "chat-a", "max_tokens": 100,
             "messages": [{"role": "user", "content": [
                 {"type": "text", "text": "你好"}]}]})
        assert status == 200
        data = json.loads(raw.decode("utf-8"))
        assert data["type"] == "message"
        assert data["content"][0]["text"] == "chat 回答"
    finally:
        upstream.shutdown()


def test_models_listing(router):
    ensure_router([{"id": "m1", "endpoint": "http://127.0.0.1:9/v1",
                    "api_key": "", "model": "m1", "format": FMT_CHAT}],
                  port=ROUTER_PORT)
    with urllib.request.urlopen(f"{router}/models", timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert any(m["id"] == "m1" for m in data["data"])
