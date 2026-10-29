"""Manifest mcpTools scopes only managed tools on the app's own agents."""

from __future__ import annotations

from kiro_crew.apps.manifest import AppManifest, Permissions
from kiro_crew.apps.permissions import ManagedToolExclusion, managed_tool_exclusion


def _manifest(*tools: str) -> AppManifest:
    return AppManifest(
        name="scoped-app",
        version="1.0.0",
        displayName="Scoped App",
        description="test",
        permissions=Permissions(mcpTools=list(tools)),
    )


def test_nonempty_mcp_tools_excludes_undeclared_host_tools_but_keeps_required_search(
    monkeypatch,
) -> None:
    from kiro_crew import mcp_discovery, mcp_discovery_managed

    monkeypatch.setattr(mcp_discovery, "_MANAGED_SERVER_TOOL_MODULES", ("core", "unreadable"))
    monkeypatch.setattr(
        mcp_discovery_managed,
        "managed_tool_names_for_policy",
        lambda server: {
            "core": ["skill_search", "cron_add", "read_file"],
            "unreadable": None,
        }[server],
    )

    assert managed_tool_exclusion(_manifest("cron_add")) == ManagedToolExclusion(
        ["read_file"], ["unreadable"]
    )


def test_empty_mcp_tools_stays_unrestricted_without_reading_server_declarations(
    monkeypatch,
) -> None:
    from kiro_crew import mcp_discovery, mcp_discovery_managed

    looked_up: list[str] = []
    monkeypatch.setattr(mcp_discovery, "_MANAGED_SERVER_TOOL_MODULES", ("core",))
    monkeypatch.setattr(
        mcp_discovery_managed,
        "managed_tool_names_for_policy",
        lambda server: looked_up.append(server),
    )

    assert managed_tool_exclusion(_manifest()) == ManagedToolExclusion([], [])
    assert looked_up == []


def test_agent_policy_unions_authored_exclusions_and_leaves_app_servers_alone(
    monkeypatch,
) -> None:
    from kiro_crew.apps import permissions
    from kiro_crew.apps.bridges_agents import _enforce_declared_mcp_tools

    monkeypatch.setattr(
        permissions,
        "managed_tool_exclusion",
        lambda _manifest: ManagedToolExclusion(["read_file"], ["unreadable"]),
    )
    own_servers = {"scoped-app:private": {"command": "app-server"}}
    data = {
        "mcpServers": own_servers,
        "managedToolPolicy": {"exclude": ["authored"], "excludeServers": ["also-authored"]},
    }

    result = _enforce_declared_mcp_tools(data, _manifest("cron_add"))

    assert result["managedToolPolicy"] == {
        "exclude": ["authored", "read_file"],
        "excludeServers": ["also-authored", "unreadable"],
    }
    assert result["mcpServers"] == own_servers
    assert data["managedToolPolicy"]["exclude"] == ["authored"]


def test_malformed_authored_policy_is_left_for_the_fail_closed_session_gate(monkeypatch) -> None:
    from kiro_crew.apps import permissions
    from kiro_crew.apps.bridges_agents import _enforce_declared_mcp_tools

    monkeypatch.setattr(
        permissions,
        "managed_tool_exclusion",
        lambda _manifest: ManagedToolExclusion(["read_file"], []),
    )
    data = {"managedToolPolicy": {"exclude": "not-a-list"}}

    assert _enforce_declared_mcp_tools(data, _manifest("cron_add")) is data
