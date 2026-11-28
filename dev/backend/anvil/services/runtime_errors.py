"""运行时启动失败的识别（fail-fast）。

工具层面的 ``EPERM`` / ``ENOENT`` 启动失败（例如 Claude Code 在 Seatbelt 内
``mkdir /private/tmp/claude-*`` 被拒绝）不是 agent 的业务失败，也不是“等待人回复”的
挂起；它说明运行时环境坏了。这里把 stream-json 里的 tool_result 错误翻译成
``runtime_env_error``，由 daemon/编排器把 run 直接判失败并展示原因。

只匹配“启动/沙箱/临时目录”语境，避免把角色正常命令（例如测试里的 EPERM 断言）
误判成环境错误。
"""
from __future__ import annotations

# 运行时 CLI 启动/临时目录/sandbox 相关的失败指纹。
_STARTUP_MARKERS = (
    "operation not permitted, mkdir",
    "eperm: operation not permitted, mkdir",
    "enoent: no such file or directory, mkdir",
    "sandbox-exec",
    "deny(1) file-write-create",
    "sandbox:",
    "claude_code_tmpdir",
    "spawn eperm",
    "spawn eacces",
    "spawn enoent",
)

# 单独出现 EPERM/ENOENT 不足以判定；必须同时有启动语境。
_RUNTIME_CONTEXT = ("claude", "sandbox", "mkdir", "spawn", "tmpdir", "temp dir")


def classify_tool_error(text: str) -> str | None:
    """返回可展示的失败原因；非启动类错误返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if not any(marker in low for marker in _STARTUP_MARKERS):
        return None
    if not any(token in low for token in _RUNTIME_CONTEXT):
        return None
    first = next((line.strip() for line in raw.splitlines() if line.strip()), raw)
    return first[:500]


def _block_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(p for p in parts if p)
    return str(content or block.get("text") or "")


def runtime_env_error_from_event(ev: dict) -> str | None:
    """从 Claude Code stream-json 事件里提取运行时环境错误原因。

    ``tool_result`` 以 ``user`` 事件回传；``is_error=true`` 时检查其文本。
    """
    if not isinstance(ev, dict) or ev.get("type") != "user":
        return None
    message = ev.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if not block.get("is_error"):
            continue
        reason = classify_tool_error(_block_text(block))
        if reason:
            return reason
    return None
