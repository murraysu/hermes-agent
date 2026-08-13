from toolsets import resolve_toolset


BLOCKED = {
    "terminal", "process", "read_file", "write_file", "patch", "search_files",
    "execute_code", "delegate_task", "skill_manage", "computer_use",
}


def test_orchestrator_toolset_has_a2a_and_no_host_tools():
    tools = set(resolve_toolset("hermes_orchestrator"))
    assert {"a2a_call", "a2a_list", "skills_list", "skill_view", "cronjob"} <= tools
    assert not tools & BLOCKED


def test_cron_safe_cannot_spawn_cron_or_host_tools():
    tools = set(resolve_toolset("hermes_cron_safe"))
    assert {"a2a_call", "a2a_list", "skills_list", "skill_view"} <= tools
    assert "cronjob" not in tools
    assert not tools & BLOCKED
