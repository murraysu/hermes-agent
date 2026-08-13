"""Mandatory tool deny policy for host-isolated Hermes deployments."""

from __future__ import annotations

import os
from typing import Any, Optional


_BLOCKED_TOOLS = frozenset({
    "terminal",
    "process",
    "read_terminal",
    "close_terminal",
    "focus_pane",
    "open_preview",
    "computer_use",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "execute_code",
    "delegate_task",
    "skill_manage",
})


def _orchestrator_profile_enabled() -> bool:
    return os.getenv("HERMES_SECURITY_PROFILE", "").strip().lower() == "orchestrator"


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[dict[str, str]]:
    del args
    if _orchestrator_profile_enabled() and tool_name in _BLOCKED_TOOLS:
        return {
            "action": "block",
            "message": (
                f"Tool '{tool_name}' is disabled by the mandatory orchestrator "
                "security profile. Delegate coding and testing through an A2A peer."
            ),
        }
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
