"""Compatibility for the former KAS managed-server hoist API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def hoist_managed_servers(
    custom_agents: list[dict[str, Any]] | None,
    agent_id: str,
    session_servers: list[dict[str, Any]],
    session_token: str = "",
    *,
    resume: bool = False,
    include_user_servers: bool = False,
    reserved_names: frozenset[str] = frozenset(),
    managed_entries: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Preserve the earlier resume argument while using the session hoist."""
    from kiro_crew.acp.kas_agents import hoist_servers_for_session

    return hoist_servers_for_session(
        custom_agents,
        agent_id,
        session_servers,
        session_token,
        include_user_servers=resume or include_user_servers,
        reserved_names=reserved_names,
        managed_entries=managed_entries,
    )
