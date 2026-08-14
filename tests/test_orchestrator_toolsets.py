from toolsets import resolve_toolset


BLOCKED = {
    "terminal", "process", "read_file", "write_file", "patch", "search_files",
    "execute_code", "delegate_task", "skill_manage", "computer_use",
}


def test_orchestrator_toolset_has_a2a_and_no_host_tools():
    tools = set(resolve_toolset("hermes_orchestrator"))
    assert {"a2a_call", "a2a_list", "skills_list", "skill_view", "cronjob"} <= tools
    assert not tools & BLOCKED


def test_orchestrator_files_adds_reads_only_and_leaves_strict_set_alone():
    """The files variant may read, never write — and must not leak back.

    The whole point of a separate toolset is that the publicly reachable
    surfaces keep the strict set, so this also pins that adding the variant
    did not mutate ``hermes_orchestrator`` itself.
    """
    tools = set(resolve_toolset("hermes_orchestrator_files"))
    strict = set(resolve_toolset("hermes_orchestrator"))

    assert {"read_file", "search_files"} <= tools
    assert not tools & {"write_file", "patch", "terminal", "process",
                        "execute_code", "computer_use"}
    # everything the strict set has, plus exactly the two read tools
    assert strict < tools
    assert tools - strict == {"read_file", "search_files"}
    # the strict set stays host-isolated
    assert not strict & BLOCKED


def test_cron_safe_cannot_spawn_cron_or_host_tools():
    tools = set(resolve_toolset("hermes_cron_safe"))
    assert {"a2a_call", "a2a_list", "skills_list", "skill_view"} <= tools
    assert "cronjob" not in tools
    assert not tools & BLOCKED
