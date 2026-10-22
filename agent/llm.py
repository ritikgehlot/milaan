"""Thin OpenAI-compatible chat client (works with Gemini, OpenAI, Groq, OpenRouter).

Design rule: the LLM is never on the path where a failure would lose data
or send a wrong message. Every caller has a deterministic fallback, and
every call is traced with token usage for the cost report.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Settings, get_settings


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMReply:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    raw_message: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, settings: Settings | None = None, transport=None):
        self.s = settings or get_settings()
        self._post = transport or requests.post
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    @property
    def enabled(self) -> bool:
        return self.s.llm_enabled

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             model: str | None = None, json_mode: bool = False,
             temperature: float = 0.1, max_tokens: int = 1500,
             retries: int = 2) -> LLMReply:
        if not self.enabled:
            raise LLMUnavailable("LLM_API_KEY not configured")
        if self.is_gemini:
            return self._gemini_native(messages, tools=tools, model=model, json_mode=json_mode,
                                       temperature=temperature, max_tokens=max_tokens, retries=retries)
        if self.is_anthropic and not tools:
            return self._anthropic_native(messages, model=model, max_tokens=max_tokens, retries=retries)
        url = self.s.llm_base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": model or self.s.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if not self.is_anthropic:  # newer Claude models reject temperature < 1
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode and not self.is_anthropic:
            body["response_format"] = {"type": "json_object"}
        last_error = ""
        for attempt in range(retries + 1):
            try:
                resp = self._post(url, json=body, timeout=self.s.llm_timeout_s,
                                  headers={"Authorization": f"Bearer {self.s.llm_api_key}"})
            except requests.RequestException as exc:  # network failure
                last_error = f"network: {type(exc).__name__}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"http {resp.status_code}"
                time.sleep(min(2 ** (attempt + 1), 10))
                continue
            if resp.status_code >= 400:
                raise LLMUnavailable(f"http {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            msg = data["choices"][0]["message"]
            usage = data.get("usage") or {}
            self.usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            self.usage_total["calls"] += 1
            return LLMReply(content=msg.get("content") or "",
                            tool_calls=msg.get("tool_calls") or [],
                            usage=usage, raw_message=msg)
        raise LLMUnavailable(f"LLM failed after retries ({last_error})")

    @property
    def is_anthropic(self) -> bool:
        return "anthropic.com" in self.s.llm_base_url

    @property
    def is_gemini(self) -> bool:
        return "generativelanguage.googleapis.com" in self.s.llm_base_url

    def _gemini_native(self, messages: list[dict], *, tools, model, json_mode, temperature, max_tokens,
                       retries) -> "LLMReply":
        """Gemini native generateContent. Works with both AIza... and new AQ. keys
        (AQ. keys are rejected by the OpenAI-compatible endpoint)."""
        model = model or self.s.llm_model
        system_parts, contents, call_names = [], [], {}
        for m in messages:
            role = m["role"]
            if role == "system":
                system_parts.append({"text": m["content"]})
                continue
            if role == "tool":
                name = call_names.get(m.get("tool_call_id"), m.get("name") or "tool")
                try:
                    resp = json.loads(m["content"])
                except (TypeError, ValueError):
                    resp = m["content"]
                part = {"functionResponse": {"name": name,
                                             "response": resp if isinstance(resp, dict) else {"result": resp}}}
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            parts = []
            content = m.get("content")
            if isinstance(content, str) and content:
                parts.append({"text": content})
            elif isinstance(content, list):
                for p_ in content:
                    if p_.get("type") == "text":
                        parts.append({"text": p_["text"]})
                    elif p_.get("type") == "image_url":
                        head, data = p_["image_url"]["url"].split(",", 1)
                        parts.append({"inlineData": {"mimeType": head[5:].split(";")[0], "data": data}})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                call_names[tc.get("id")] = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                fc = {"functionCall": {"name": fn["name"], "args": args}}
                if tc.get("_thought_signature"):
                    fc["thoughtSignature"] = tc["_thought_signature"]
                parts.append(fc)
            if not parts:
                parts = [{"text": "(empty)"}]
            g_role = "model" if role == "assistant" else "user"
            if contents and contents[-1]["role"] == g_role and g_role == "user" and \
                    "functionResponse" not in contents[-1]["parts"][0]:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": g_role, "parts": parts})
        gen: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max(max_tokens, 1024)}
        if "2.5-flash" in model:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            gen["maxOutputTokens"] = max(max_tokens + 4096, 8192)
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body: dict[str, Any] = {"contents": contents, "generationConfig": gen}
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {k: v for k, v in t["function"].items() if k in ("name", "description", "parameters")
                 and not (k == "parameters" and not v.get("properties"))}
                for t in tools]}]
        base = self.s.llm_base_url.split("/v1beta")[0] + "/v1beta"
        url = f"{base}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.s.llm_api_key, "content-type": "application/json"}
        last_error = ""
        for attempt in range(retries + 1):
            try:
                resp = self._post(url, json=body, timeout=self.s.llm_timeout_s, headers=headers)
            except requests.RequestException as exc:
                last_error = f"network: {type(exc).__name__}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"http {resp.status_code}"
                time.sleep(min(2 ** (attempt + 1), 10))
                continue
            if resp.status_code >= 400:
                raise LLMUnavailable(f"http {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                raise LLMUnavailable(f"no candidates: {str(data.get('promptFeedback'))[:200]}")
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p_.get("text", "") for p_ in parts if "text" in p_ and not p_.get("thought"))
            tool_calls = []
            for i, p_ in enumerate(parts):
                if "functionCall" in p_:
                    fc = p_["functionCall"]
                    call = {"id": f"call_{i}_{fc['name']}", "type": "function",
                            "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args") or {})}}
                    if p_.get("thoughtSignature"):
                        call["_thought_signature"] = p_["thoughtSignature"]
                    tool_calls.append(call)
            u = data.get("usageMetadata") or {}
            usage = {"prompt_tokens": u.get("promptTokenCount", 0),
                     "completion_tokens": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)}
            self.usage_total["prompt_tokens"] += usage["prompt_tokens"]
            self.usage_total["completion_tokens"] += usage["completion_tokens"]
            self.usage_total["calls"] += 1
            return LLMReply(content=text, tool_calls=tool_calls, usage=usage,
                            raw_message={"role": "assistant", "content": text or None, "tool_calls": tool_calls})
        raise LLMUnavailable(f"LLM failed after retries ({last_error})")

    def _anthropic_native(self, messages: list[dict], *, model: str | None, max_tokens: int,
                          retries: int) -> "LLMReply":
        """Claude Messages API for text + images (base64 image blocks), no tools."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system" and isinstance(m["content"], str))
        conv = []
        for m in messages:
            if m["role"] == "system":
                continue
            content = m["content"]
            if isinstance(content, list):
                blocks = []
                for part in content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part["text"]})
                    elif part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        head, data = url.split(",", 1)
                        mime = head[5:].split(";")[0]
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
                content = blocks
            role = "assistant" if m["role"] == "assistant" else "user"
            if conv and conv[-1]["role"] == role and isinstance(conv[-1]["content"], str) and isinstance(content, str):
                conv[-1]["content"] += "\n" + content
            else:
                conv.append({"role": role, "content": content or "(empty)"})
        body: dict[str, Any] = {"model": model or self.s.llm_model, "max_tokens": max_tokens, "messages": conv}
        if system:
            body["system"] = system
        url = self.s.llm_base_url.rstrip("/") + "/messages"
        headers = {"x-api-key": self.s.llm_api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        last_error = ""
        for attempt in range(retries + 1):
            try:
                resp = self._post(url, json=body, timeout=self.s.llm_timeout_s, headers=headers)
            except requests.RequestException as exc:
                last_error = f"network: {type(exc).__name__}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in (429, 500, 502, 503, 504, 529):
                last_error = f"http {resp.status_code}"
                time.sleep(min(2 ** (attempt + 1), 10))
                continue
            if resp.status_code >= 400:
                raise LLMUnavailable(f"http {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            u = data.get("usage") or {}
            usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
            self.usage_total["prompt_tokens"] += usage["prompt_tokens"]
            self.usage_total["completion_tokens"] += usage["completion_tokens"]
            self.usage_total["calls"] += 1
            return LLMReply(content=text, usage=usage, raw_message={"role": "assistant", "content": text})
        raise LLMUnavailable(f"LLM failed after retries ({last_error})")

    def json_chat(self, messages: list[dict], **kw) -> tuple[dict, dict]:
        """Chat expecting a JSON object. Returns (parsed, usage)."""
        reply = self.chat(messages, json_mode=True, **kw)
        return parse_json_object(reply.content), reply.usage

    def transcribe(self, audio: bytes, mime: str) -> str:
        """Voice note -> text. Gemini native audio input (handles WhatsApp OGG/Opus)."""
        key = self.s.gemini_api_key
        if key:
            model = self.s.llm_model if self.s.llm_model.startswith("gemini") else "gemini-3.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            body = {"contents": [{"parts": [
                {"text": "Transcribe this WhatsApp voice note exactly. It is usually Hindi or "
                         "Hindi-English code-mixed speech from a shopkeeper. Write Hindi words in "
                         "Devanagari and English words in Latin script. Output only the transcript."},
                {"inline_data": {"mime_type": mime.split(";")[0], "data": base64.b64encode(audio).decode()}},
            ]}]}
            resp = self._post(url, json=body, timeout=self.s.llm_timeout_s,
                              headers={"x-goog-api-key": key, "content-type": "application/json"})
            if resp.status_code >= 400:
                raise LLMUnavailable(f"transcription http {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            return " ".join(p.get("text", "") for p in parts).strip()
        if self.enabled and "openai.com" in self.s.llm_base_url:
            url = self.s.llm_base_url.rstrip("/") + "/audio/transcriptions"
            resp = requests.post(url, headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
                                 files={"file": ("voice.ogg", audio, mime)},
                                 data={"model": "whisper-1"}, timeout=self.s.llm_timeout_s)
            if resp.status_code >= 400:
                raise LLMUnavailable(f"transcription http {resp.status_code}")
            return resp.json().get("text", "").strip()
        raise LLMUnavailable("no speech-to-text provider configured")


def image_part(image: bytes, mime: str) -> dict:
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}}


def parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise LLMUnavailable("model did not return JSON")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise LLMUnavailable("model JSON was not an object")
    return value
