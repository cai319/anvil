"""LLM utility 调用（design §1.2：裸端点只承担 utility 角色）。

用环境里的 Anthropic 兼容端点（ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN /
ANTHROPIC_MODEL），直连 Messages API，不引入额外 SDK。
"""
from __future__ import annotations

import os

import httpx


def generate(prompt: str, max_tokens: int = 1024) -> str:
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or ""
    model = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or "claude-sonnet-4-5"
    )
    if not token:
        raise RuntimeError("环境中缺少 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY")

    r = httpx.post(
        base_url + "/v1/messages",
        headers={
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    # 取第一个 text 块
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return ""
