"""Mandatory tool deny policy for host-isolated Hermes deployments."""

from __future__ import annotations

import os
from typing import Any, Optional


# ⚠️ These are TOOL names, not toolset names, and upstream renames them.
# The 2026-09-06 sync renamed `process` -> `process_manage`; a stale entry here
# fails OPEN (the tool is simply not matched) with no error anywhere, so this
# set must be re-checked against tools/ on every upstream sync.
# `tests/plugins/test_orchestrator_security.py` pins the ones that matter.
_BLOCKED_TOOLS = frozenset({
    "terminal",
    "process_manage",
    "read_terminal",
    "close_terminal",
    "focus_pane",
    "open_preview",
    "close_preview",
    "read_preview",
    "drive_preview",
    "annotate_preview",
    "desktop_preview",
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
