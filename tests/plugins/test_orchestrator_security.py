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
    "terminal", "process", "read_file", "write_file", "patch",
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
