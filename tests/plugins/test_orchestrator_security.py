from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def policy_module():
    path = Path(__file__).parents[2] / "plugins" / "orchestrator-security" / "__init__.py"
    spec = importlib.util.spec_from_file_location("orchestrator_security", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tool_name", [
    "terminal", "process_manage", "read_file", "write_file", "patch",
    "execute_code", "delegate_task", "skill_manage", "computer_use",
])
def test_orchestrator_profile_blocks_host_tools(monkeypatch, policy_module, tool_name):
    monkeypatch.setenv("HERMES_SECURITY_PROFILE", "orchestrator")
    result = policy_module._on_pre_tool_call(tool_name=tool_name, args={})
    assert result and result["action"] == "block"


@pytest.mark.parametrize("tool_name", [
    "web_search", "memory", "skills_list", "skill_view", "cronjob", "a2a_call",
])
def test_orchestrator_profile_allows_orchestration_tools(monkeypatch, policy_module, tool_name):
    monkeypatch.setenv("HERMES_SECURITY_PROFILE", "orchestrator")
    assert policy_module._on_pre_tool_call(tool_name=tool_name, args={}) is None


def test_policy_inactive_without_profile(monkeypatch, policy_module):
    monkeypatch.delenv("HERMES_SECURITY_PROFILE", raising=False)
    assert policy_module._on_pre_tool_call(tool_name="terminal", args={}) is None


def test_every_blocked_name_is_a_real_tool(policy_module):
    """A renamed tool silently fails OPEN: `_on_pre_tool_call` simply never matches
    it, with no error anywhere. Upstream renamed process -> process_manage in the
    2026-09-06 sync and nothing but this assertion would have caught it.

    Checked against the tool *schemas* declared under tools/ rather than the live
    registry: desktop-only tools (open_preview, focus_pane, ...) are registered
    conditionally by the GUI gateway, so a runtime registry would report them
    missing and this guard would fail for the wrong reason.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "tools"
    declared = set()
    for path in root.rglob("*.py"):
        declared.update(re.findall(r'"name":\s*"([a-z0-9_]+)"', path.read_text(errors="ignore")))
    assert declared, "no tool schemas found under tools/ — the guard would pass vacuously"
    stale = sorted(name for name in policy_module._BLOCKED_TOOLS if name not in declared)
    assert not stale, f"blocked names no longer declared under tools/ (fail-open): {stale}"
