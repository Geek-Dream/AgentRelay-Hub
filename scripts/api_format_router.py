#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
API 协议格式路由（内置本地路由，对标 cc-switch 的本地代理）。

场景：有些 API 上游只认特定消息格式——OpenAI Chat Completions、
OpenAI Responses 或 Anthropic Messages。AgentRelay 内部统一用 Chat
格式发请求，直接连这些上游会 404/格式错误。本模块在
127.0.0.1:<port> 起一个轻量 HTTP 路由，对外同时暴露三种接口：

    POST /v1/chat/completions   OpenAI Chat
    POST /v1/responses          OpenAI Responses
    POST /v1/messages           Anthropic Messages
    GET  /v1/models             模型列表

请求按 model 字段路由到对应上游，自动完成双向协议转换（含流式 SSE）。
默认端口 15731，与本机 cc-switch 的 15721 错开；可用
AGENTRELAY_API_ROUTER_PORT 或 ensure_router(port=...) 修改。

两种用法：
1. 进程内：orchestrator 等调用 ensure_router(providers) 后在后台线程运行；
2. 独立进程：python api_format_router.py（供外部工具把 base_url 指过来）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ROUTER_PORT = 15731

# 上游消息格式
FMT_CHAT = "chat"
FMT_RESPONSES = "responses"
FMT_ANTHROPIC = "anthropic"
SUPPORTED_FORMATS = (FMT_CHAT, FMT_RESPONSES, FMT_ANTHROPIC)


# ============================================================
# 请求规范化：三种入站格式都转成统一的内部消息列表
# ============================================================

def _text_of(content) -> str:
    """OpenAI content（str 或 part 列表）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    parts.append("[图片]")
        return "\n".join(p for p in parts if p)
    return ""


def chat_request_to_ir(body: dict) -> dict:
    """OpenAI Chat 请求 → 内部表示（消息保持 OpenAI 形态）。"""
    return {
        "model": str(body.get("model") or ""),
        "messages": list(body.get("messages") or []),
        "tools": list(body.get("tools") or []),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_tokens": body.get("max_tokens"),
        "stop": body.get("stop"),
        "stream": bool(body.get("stream")),
    }


def messages_request_to_ir(body: dict) -> dict:
    """Anthropic Messages 请求 → 内部表示（转成 OpenAI 形态的消息）。"""
    out_messages = []
    system = body.get("system")
    if system:
        text = system if isinstance(system, str) else "\n".join(
            str(b.get("text") or "") for b in system
            if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            out_messages.append({"role": "system", "content": text})
    for msg in body.get("messages") or []:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, str):
            out_messages.append({"role": role, "content": content})
            continue
        texts, tool_calls, tool_results = [], [], []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(
                            block.get("input") or {},
                            ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                tool_results.append(block)
        if tool_calls:
            out_messages.append({
                "role": "assistant",
                "content": "\n".join(texts),
                "tool_calls": tool_calls,
            })
        elif tool_results:
            for tr in tool_results:
                out_messages.append({
                    "role": "tool",
                    "tool_call_id": str(tr.get("tool_use_id") or ""),
                    "content": tr.get("content")
                    if isinstance(tr.get("content"), str)
                    else json.dumps(tr.get("content") or "",
                                    ensure_ascii=False),
                })
        else:
            out_messages.append({"role": role,
                                 "content": "\n".join(texts)})
    tools = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name"):
            tools.append({
                "type": "function",
                "function": {
                    "name": str(tool.get("name")),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {},
                },
            })
    return {
        "model": str(body.get("model") or ""),
        "messages": out_messages,
        "tools": tools,
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_tokens": body.get("max_tokens"),
        "stop": body.get("stop_sequences"),
        "stream": bool(body.get("stream")),
    }


def responses_request_to_ir(body: dict) -> dict:
    """OpenAI Responses 请求 → 内部表示。"""
    out_messages = []
    instructions = str(body.get("instructions") or "").strip()
    if instructions:
        out_messages.append({"role": "system", "content": instructions})
    input_items = body.get("input")
    if isinstance(input_items, str):
        out_messages.append({"role": "user", "content": input_items})
    for item in input_items or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            role = str(item.get("role") or "user")
            texts = [str(c.get("text") or "") for c in item.get("content") or []
                     if isinstance(c, dict) and c.get("type") in
                     ("input_text", "output_text", "text")]
            out_messages.append({"role": role,
                                 "content": "\n".join(texts)})
        elif itype == "function_call":
            out_messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    },
                }],
            })
        elif itype == "function_call_output":
            out_messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": str(item.get("output") or ""),
            })
    tools = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            tools.append({
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") or {},
                },
            })
    return {
        "model": str(body.get("model") or ""),
        "messages": out_messages,
        "tools": tools,
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_tokens": body.get("max_output_tokens"),
        "stop": None,
        "stream": bool(body.get("stream")),
    }


# ============================================================
# 出站序列化：内部表示 → 上游原生格式
# ============================================================

def _system_text(ir: dict) -> str:
    return "\n".join(_text_of(m.get("content"))
                     for m in ir["messages"]
                     if m.get("role") == "system").strip()


def _conversation_messages(ir: dict) -> list:
    return [m for m in ir["messages"] if m.get("role") != "system"]


def ir_to_chat_request(ir: dict, model: str) -> dict:
    body = {
        "model": model,
        "messages": _conversation_messages(ir),
        "stream": ir.get("stream") or False,
    }
    if ir.get("tools"):
        body["tools"] = ir["tools"]
    for key in ("temperature", "top_p", "max_tokens", "stop"):
        if ir.get(key) is not None:
            body[key] = ir[key]
    return body


def ir_to_anthropic_request(ir: dict, model: str) -> dict:
    system = _system_text(ir)
    messages = []
    for m in _conversation_messages(ir):
        role = m.get("role")
        if role == "tool":
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(m.get("tool_call_id") or ""),
                    "content": _text_of(m.get("content")),
                }],
            })
            continue
        blocks = []
        text = _text_of(m.get("content"))
        if text:
            blocks.append({"type": "text", "text": text})
        for call in m.get("tool_calls") or []:
            try:
                arguments = json.loads(
                    call.get("function", {}).get("arguments") or "{}")
            except (ValueError, TypeError):
                arguments = {}
            blocks.append({
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(call.get("function", {}).get("name") or ""),
                "input": arguments,
            })
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        messages.append({"role": "assistant" if role == "assistant"
                         else "user", "content": blocks})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": int(ir.get("max_tokens") or 4096),
        "stream": bool(ir.get("stream")),
    }
    if system:
        body["system"] = system
    if ir.get("tools"):
        body["tools"] = [{
            "name": t.get("function", {}).get("name", ""),
            "description": t.get("function", {}).get("description", ""),
            "input_schema": t.get("function", {}).get("parameters") or {},
        } for t in ir["tools"]]
    for key in ("temperature", "top_p", "stop"):
        if ir.get(key) is not None:
            body[key] = ir[key]
    return body


def ir_to_responses_request(ir: dict, model: str) -> dict:
    system = _system_text(ir)
    input_items = []
    for m in _conversation_messages(ir):
        role = m.get("role")
        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": str(m.get("tool_call_id") or ""),
                "output": _text_of(m.get("content")),
            })
            continue
        if m.get("tool_calls"):
            for call in m["tool_calls"]:
                input_items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(call.get("function", {}).get("name") or ""),
                    "arguments": str(
                        call.get("function", {}).get("arguments") or ""),
                })
            text = _text_of(m.get("content"))
            if text:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            continue
        text = _text_of(m.get("content"))
        if text:
            input_items.append({
                "type": "message",
                "role": "user" if role != "assistant" else "assistant",
                "content": [{
                    "type": "input_text" if role != "assistant"
                    else "output_text",
                    "text": text,
                }],
            })
    body = {
        "model": model,
        "input": input_items,
        "stream": bool(ir.get("stream")),
    }
    if system:
        body["instructions"] = system
    if ir.get("tools"):
        body["tools"] = [{
            "type": "function",
            "name": t.get("function", {}).get("name", ""),
            "description": t.get("function", {}).get("description", ""),
            "parameters": t.get("function", {}).get("parameters") or {},
        } for t in ir["tools"]]
    for key in ("temperature", "top_p", "max_tokens"):
        if ir.get(key) is not None:
            body[{"max_tokens": "max_output_tokens"}.get(key, key)] = ir[key]
    return body


SERIALIZERS = {
    FMT_CHAT: ir_to_chat_request,
    FMT_ANTHROPIC: ir_to_anthropic_request,
    FMT_RESPONSES: ir_to_responses_request,
}


# ============================================================
# 响应转换：上游原生响应 → 调用方格式（这里统一转成 Chat，再按需反转）
# ============================================================

def _finish_reason(stop_reason: str | None) -> str:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "length": "length",
    }.get(str(stop_reason or ""), "stop")


def anthropic_response_to_chat(body: dict) -> dict:
    # Messages API 的非流式响应本身就是 message 对象（无包裹层）；
    # 有些代理会多包一层 "message"，两种都兼容
    message = body.get("message") if isinstance(body.get("message"), dict) \
        else body
    texts, tool_calls = [], []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(
                        block.get("input") or {}, ensure_ascii=False),
                },
            })
    out_message = {"role": "assistant",
                   "content": "\n".join(texts)}
    if tool_calls:
        out_message["tool_calls"] = tool_calls
    usage = body.get("usage") or {}
    return {
        "id": str(body.get("id") or f"chatcmpl-{int(time.time() * 1000)}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model") or ""),
        "choices": [{
            "index": 0,
            "message": out_message,
            "finish_reason": _finish_reason(body.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0),
        },
    }


def responses_response_to_chat(body: dict) -> dict:
    texts, tool_calls = [], []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    texts.append(str(c.get("text") or ""))
        elif itype == "function_call":
            tool_calls.append({
                "id": str(item.get("call_id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or ""),
                },
            })
    out_message = {"role": "assistant",
                   "content": "\n".join(texts)}
    if tool_calls:
        out_message["tool_calls"] = tool_calls
    usage = body.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens")
                        or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens")
                            or usage.get("completion_tokens") or 0)
    return {
        "id": str(body.get("id") or f"chatcmpl-{int(time.time() * 1000)}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model") or ""),
        "choices": [{
            "index": 0,
            "message": out_message,
            "finish_reason": _finish_reason(body.get("status")),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


RESPONSE_TO_CHAT = {
    FMT_CHAT: lambda body: body,
    FMT_ANTHROPIC: anthropic_response_to_chat,
    FMT_RESPONSES: responses_response_to_chat,
}


def chat_to_anthropic_response(chat: dict, body: dict) -> dict:
    """Chat 响应 → Anthropic Messages 响应。"""
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    text = message.get("content") or ""
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(
                call.get("function", {}).get("arguments") or "{}")
        except (ValueError, TypeError):
            arguments = {}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or ""),
            "name": str(call.get("function", {}).get("name") or ""),
            "input": arguments,
        })
    stop = choice.get("finish_reason")
    usage = chat.get("usage") or {}
    return {
        "id": str(chat.get("id") or ""),
        "type": "message",
        "role": "assistant",
        "model": str(chat.get("model") or body.get("model") or ""),
        "content": content,
        "stop_reason": {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }.get(str(stop), "end_turn"),
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def chat_to_responses_response(chat: dict, body: dict) -> dict:
    """Chat 响应 → Responses 响应。"""
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = []
    text = message.get("content") or ""
    if text:
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        })
    for call in message.get("tool_calls") or []:
        output.append({
            "type": "function_call",
            "call_id": str(call.get("id") or ""),
            "name": str(call.get("function", {}).get("name") or ""),
            "arguments": str(call.get("function", {}).get("arguments") or ""),
        })
    usage = chat.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "id": str(chat.get("id") or ""),
        "object": "response",
        "created_at": int(time.time()),
        "model": str(chat.get("model") or body.get("model") or ""),
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


CHAT_TO_RESPONSE = {
    FMT_CHAT: lambda chat, body: chat,
    FMT_ANTHROPIC: chat_to_anthropic_response,
    FMT_RESPONSES: chat_to_responses_response,
}


# ============================================================
# 流式 SSE 转换
# ============================================================

def _sse_bytes(event: str, data) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(
        data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def iter_sse_events(raw: bytes):
    """解析 SSE 字节流，yield (event, data_str)。"""
    event, data_lines = "", []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = "", []
    if data_lines:
        yield event, "\n".join(data_lines)


def anthropic_stream_to_chat_chunks(raw: bytes, model: str):
    """Anthropic SSE → Chat SSE chunk 字节块（含结尾 [DONE]）。"""
    message_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())
    index = 0
    tool_args: dict[int, list] = {}
    tool_meta: dict[int, dict] = {}
    stop_reason = "end_turn"

    def chunk(delta, finish=None):
        return _sse_bytes("data", {
            "id": message_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                **({"finish_reason": finish} if finish else {}),
            }],
        })

    for event, data in iter_sse_events(raw):
        if event == "message_start":
            try:
                payload = json.loads(data)
                message_id = str(payload.get("message", {}).get("id")
                                 or message_id)
            except ValueError:
                pass
            yield chunk({"role": "assistant"})
        elif event == "content_block_start":
            try:
                payload = json.loads(data)
                index = int(payload.get("index") or 0)
                block = payload.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_meta[index] = {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                    }
                    tool_args[index] = []
                    yield chunk({"tool_calls": [{
                        "index": index,
                        "id": tool_meta[index]["id"],
                        "type": "function",
                        "function": {"name": tool_meta[index]["name"],
                                     "arguments": ""},
                    }]})
            except ValueError:
                pass
        elif event == "content_block_delta":
            try:
                payload = json.loads(data)
                index = int(payload.get("index") or 0)
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield chunk({"content": str(delta.get("text") or "")})
                elif delta.get("type") == "input_json_delta":
                    tool_args.setdefault(index, []).append(
                        str(delta.get("partial_json") or ""))
                    yield chunk({"tool_calls": [{
                        "index": index,
                        "function": {
                            "arguments": str(
                                delta.get("partial_json") or ""),
                        },
                    }]})
            except ValueError:
                pass
        elif event == "message_delta":
            try:
                payload = json.loads(data)
                stop_reason = str(payload.get("delta", {}).get("stop_reason")
                                  or stop_reason)
            except ValueError:
                pass
        elif event == "error":
            yield _sse_bytes("data", json.dumps(
                {"error": {"message": data}}, ensure_ascii=False))
    yield chunk({}, finish=_finish_reason(stop_reason))
    yield b"data: [DONE]\n\n"


def responses_stream_to_chat_chunks(raw: bytes, model: str):
    """Responses SSE → Chat SSE chunk 字节块（含结尾 [DONE]）。"""
    message_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())
    finish = "stop"

    def chunk(delta, finish_reason=None):
        return _sse_bytes("data", {
            "id": message_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                **({"finish_reason": finish_reason}
                   if finish_reason else {}),
            }],
        })

    for event, data in iter_sse_events(raw):
        if data.strip() == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if event == "response.output_text.delta":
            yield chunk({"content": str(payload.get("delta") or "")})
        elif event == "response.output_item.done":
            item = payload.get("item") or {}
            if item.get("type") == "function_call":
                yield chunk({"tool_calls": [{
                    "index": 0,
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    },
                }]})
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        yield chunk({"content": str(c.get("text") or "")})
        elif event == "response.completed":
            response = payload.get("response") or {}
            status = str(response.get("status") or "")
            finish = _finish_reason(status)
            yield chunk({}, finish_reason=finish)
        elif event == "response.created":
            response = payload.get("response") or {}
            message_id = str(response.get("id") or message_id)
        elif event in ("response.failed", "error"):
            error = payload.get("response", {}).get("error") \
                if isinstance(payload.get("response"), dict) else payload
            yield _sse_bytes("data", json.dumps(
                {"error": {"message": str(error)}}, ensure_ascii=False))
    yield b"data: [DONE]\n\n"


def chat_stream_passthrough(raw: bytes, model: str):
    yield raw


STREAM_TO_CHAT = {
    FMT_CHAT: chat_stream_passthrough,
    FMT_ANTHROPIC: anthropic_stream_to_chat_chunks,
    FMT_RESPONSES: responses_stream_to_chat_chunks,
}


# ============================================================
# 上游 HTTP 调用
# ============================================================

def upstream_url(provider: dict) -> str:
    endpoint = str(provider.get("endpoint") or "").rstrip("/")
    fmt = provider.get("format") or FMT_CHAT
    if endpoint.endswith(("/chat/completions", "/messages", "/responses")):
        return endpoint
    if fmt == FMT_CHAT:
        suffix = "/chat/completions"
    elif fmt == FMT_ANTHROPIC:
        suffix = "/messages" if endpoint.endswith("/v1") else "/v1/messages"
    else:
        suffix = "/responses"
    return endpoint + suffix


def call_upstream(provider: dict, payload: dict, timeout: int = 300):
    """返回 (status, content_type, raw_bytes)。抛出 urllib 异常给调用方。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = str(provider.get("api_key") or "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if (provider.get("format") or FMT_CHAT) == FMT_ANTHROPIC:
        headers.setdefault("anthropic-version", "2023-06-01")
        if api_key:
            headers.setdefault("x-api-key", api_key)
    request = urllib.request.Request(
        upstream_url(provider), data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return (response.status,
                response.headers.get("Content-Type", ""),
                response.read())


# ============================================================
# 路由服务
# ============================================================

class RouterState:
    def __init__(self, providers=None):
        self.providers: list[dict] = list(providers or [])
        self.lock = threading.Lock()

    def upsert(self, providers) -> None:
        with self.lock:
            for item in providers or []:
                pid = str(item.get("id") or "")
                if not pid:
                    continue
                self.providers[:] = [
                    p for p in self.providers if p.get("id") != pid]
                self.providers.append(dict(item))

    def resolve(self, model: str) -> dict | None:
        with self.lock:
            providers = list(self.providers)
        for p in providers:
            if model and model in (str(p.get("id") or ""),
                                   str(p.get("model") or "")):
                return p
        if len(providers) == 1:
            return providers[0]
        return None


class FormatRouterHandler(BaseHTTPRequestHandler):
    server_version = "AgentRelayFormatRouter/1.0"

    @property
    def state(self) -> RouterState:
        return self.server.router_state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
        return body if isinstance(body, dict) else {}

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            models = [{"id": str(p.get("id") or ""),
                       "object": "model",
                       "created": 0,
                       "owned_by": "agentrelay-router"}
                      for p in self.state.providers]
            return self._send_json(200, {"object": "list", "data": models})
        self._send_json(404, {"error": {"message": f"未知路径 {self.path}"}})

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._send_json(400, {"error": {"message": str(exc)}})

        inbound = {
            "/v1/chat/completions": FMT_CHAT,
            "/v1/responses": FMT_RESPONSES,
            "/v1/messages": FMT_ANTHROPIC,
        }.get(path)
        if not inbound:
            return self._send_json(
                404, {"error": {"message": f"未知路径 {self.path}"}})

        model = str(body.get("model") or "")
        provider = self.state.resolve(model)
        if not provider:
            return self._send_json(404, {"error": {
                "message": f"路由找不到模型 {model or '(空)'} 对应的上游，"
                           f"请检查 api_providers 配置"}})

        target_fmt = provider.get("format") or FMT_CHAT
        try:
            ir = {
                FMT_CHAT: chat_request_to_ir,
                FMT_ANTHROPIC: messages_request_to_ir,
                FMT_RESPONSES: responses_request_to_ir,
            }[inbound](body)
        except Exception as exc:
            return self._send_json(400, {"error": {
                "message": f"入站 {inbound} 请求解析失败：{exc}"}})

        upstream_model = str(provider.get("model") or model)
        payload = SERIALIZERS.get(target_fmt, ir_to_chat_request)(
            ir, upstream_model)
        timeout = int(provider.get("timeout") or 300)
        try:
            status, _ctype, raw = call_upstream(provider, payload, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            return self._send_json(exc.code, {"error": {
                "message": f"上游返回 {exc.code}：{detail}"}})
        except Exception as exc:
            return self._send_json(502, {"error": {
                "message": f"上游调用失败：{exc}"}})

        wants_stream = bool(body.get("stream")) and inbound == FMT_CHAT
        if target_fmt == inbound and not wants_stream:
            # 同格式非流式：原样透传
            return self._send_raw(status, raw)

        if wants_stream and target_fmt in STREAM_TO_CHAT:
            # 上游流式 → 转成入站格式的 SSE
            return self._send_stream(inbound, target_fmt, raw,
                                     upstream_model)

        # 非流式：解析上游响应 → Chat → 再转成入站格式
        try:
            upstream_json = json.loads(raw.decode("utf-8"))
            chat = RESPONSE_TO_CHAT[target_fmt](upstream_json)
        except Exception as exc:
            return self._send_json(502, {"error": {
                "message": f"上游响应转换失败：{exc}"}})
        if inbound == FMT_CHAT:
            return self._send_json(200, chat)
        try:
            out = CHAT_TO_RESPONSE[inbound](chat, body)
        except Exception as exc:
            return self._send_json(502, {"error": {
                "message": f"响应反转失败：{exc}"}})
        return self._send_json(200, out)

    def _send_raw(self, status: int, raw: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_stream(self, inbound: str, target_fmt: str, raw: bytes,
                     model: str) -> None:
        """把上游 SSE 流转成 Chat SSE chunk（入站流式仅支持 chat 格式）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = STREAM_TO_CHAT[target_fmt](raw, model)
        try:
            for block in chunks:
                self.wfile.write(block)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


_started: dict[int, ThreadingHTTPServer] = {}
_start_lock = threading.Lock()


def ensure_router(providers=None, port: int | None = None,
                  host: str = "127.0.0.1") -> str:
    """幂等启动内置路由，返回 base url（如 http://127.0.0.1:15731）。

    同端口重复调用只更新上游配置，不重复起服务。端口被其他进程占用
    时抛 RuntimeError（提示换端口，避免和本机 cc-switch 的 15721 冲突）。
    """
    if port is None:
        port = int(os.environ.get("AGENTRELAY_API_ROUTER_PORT", "")
                   or DEFAULT_ROUTER_PORT)
    with _start_lock:
        server = _started.get(port)
        if server is None:
            server = ThreadingHTTPServer(
                (host, port), FormatRouterHandler)
            server.router_state = RouterState(providers)  # type: ignore
            thread = threading.Thread(
                target=server.serve_forever, daemon=True,
                name=f"api-format-router-{port}")
            thread.start()
            _started[port] = server
        elif providers:
            server.router_state.upsert(providers)  # type: ignore
    return f"http://127.0.0.1:{port}/v1"


def stop_router(port: int | None = None) -> None:
    with _start_lock:
        ports = [port] if port else list(_started)
        for item in ports:
            server = _started.pop(item, None)
            if server is not None:
                server.shutdown()
                server.server_close()


def _load_providers_from_env() -> list[dict]:
    try:
        items = json.loads(os.environ.get("AGENTRELAY_API_PROVIDERS_JSON", "[]"))
    except ValueError:
        items = []
    providers = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        fmt = str(item.get("format") or FMT_CHAT)
        if fmt not in SUPPORTED_FORMATS:
            fmt = FMT_CHAT
        endpoint = str(item.get("endpoint") or "")
        if not endpoint:
            continue
        providers.append({
            "id": str(item.get("id")),
            "endpoint": endpoint,
            "api_key": str(item.get("api_key") or ""),
            "model": str(item.get("model") or item.get("id")),
            "format": fmt,
            "timeout": int(item.get("timeout") or 300),
        })
    return providers


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="AgentRelay API 协议格式路由")
    parser.add_argument("--port", type=int, default=DEFAULT_ROUTER_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    providers = _load_providers_from_env()
    url = ensure_router(providers, port=args.port, host=args.host)
    print(f"API 格式路由已启动：{url}")
    print(f"已加载上游：{[p['id'] + '(' + p['format'] + ')' for p in providers] or '无'}")
    print("按 Ctrl+C 停止")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_router(args.port)
        print("已停止")


if __name__ == "__main__":
    main()
