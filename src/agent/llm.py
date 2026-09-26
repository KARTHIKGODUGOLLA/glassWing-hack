"""Sciforium (OpenAI-compatible) client with a hard fallback.

The LLM only ever talks and extracts. If it is unavailable, slow, or returns
something unparseable, callers fall back to the offline extractor and templates
so a live demo never stalls.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from src import config

log = logging.getLogger("shiftvoice.llm")
_client = None
_last_error: str | None = None


def available() -> bool:
    return bool(config.SCIFORIUM_API_KEY and config.SCIFORIUM_MODEL)


def status() -> dict:
    model = config.SCIFORIUM_MODEL.rsplit("/", 1)[-1] if config.SCIFORIUM_MODEL else None
    return {"online": available(), "model": model, "last_error": _last_error}


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(base_url=config.SCIFORIUM_BASE_URL, api_key=config.SCIFORIUM_API_KEY,
                         timeout=config.LLM_TIMEOUT_S, max_retries=1)
    return _client


def _strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json(text: str) -> dict | None:
    text = _strip_reasoning(text)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for raw in candidates:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def chat(messages: list[dict[str, str]], *, temperature: float = 0.2, max_tokens: int = 400) -> str | None:
    global _last_error
    if not available():
        return None
    try:
        r = _get_client().chat.completions.create(
            model=config.SCIFORIUM_MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens,
        )
        _last_error = None
        return _strip_reasoning(r.choices[0].message.content or "")
    except Exception as exc:  # network, auth, model-not-found: all fall back
        _last_error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("LLM call failed: %s", _last_error)
        return None


def chat_json(system: str, user: str, **kw: Any) -> dict | None:
    text = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
    if text is None:
        return None
    parsed = parse_json(text)
    if parsed is None:
        global _last_error
        _last_error = f"unparseable JSON: {text[:120]}"
    return parsed
