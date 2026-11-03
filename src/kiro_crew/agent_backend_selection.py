"""Member defaults feed the existing harness gate; explicit session choices win."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from kiro_crew.config import KiroCrewConfig


def member_backend_for_write(
    cfg: KiroCrewConfig, member: str, body: dict[str, Any], *, for_workers: bool = False
) -> str:
    """Validate model pins against the harness the same write will select."""
    from kiro_crew.acp_backends import resolve_selected_backend

    record = cfg.agents.get(member)
    pin = validate_agent_backend(
        body.get("acp_backend", record.acp_backend if record is not None else None)
    )
    if pin is not None:
        return pin
    member_backend = resolve_selected_backend(cfg.agent.member_acp_backend)
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    if for_workers or (
        capabilities_for(member_backend).model_id_namespace
        != capabilities_for(cfg.agent.acp_backend).model_id_namespace
    ):
        return member_backend
    return cfg.agent.acp_backend


def validate_agent_backend(value: object) -> str | None:
    """Null inherits. Invalid or newly denied pins refuse a start instead of rerouting it."""
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_KIRO,
        STORED_BACKEND_KIRO,
        selectable_backends,
    )

    if value is None:
        return None
    # Probe rows use config ids; slot/profile overrides use runtime ids.
    # Do not use backend_from_stored here: it turns explicit "" into inheritance.
    if value == STORED_BACKEND_KIRO:
        value = ACP_BACKEND_KIRO
    if not isinstance(value, str) or value not in selectable_backends():
        raise ValueError("backend is not selectable")
    return value


def agent_backend_pin(
    cfg: KiroCrewConfig,
    agent: str | None,
    crew_agent: str | None = None,
    *,
    session_key: str | None = None,
) -> str | None:
    record = cfg._crew_record(agent, crew_agent)
    if record is None and session_key:
        from kiro_crew.members import is_crewmate_worker_session

        if is_crewmate_worker_session(session_key) and any(
            member.acp_backend not in (None, "") for member in cfg.agents.values()
        ):
            from kiro_crew.execution_context import member_config_for_id, read_session_execution
            from kiro_crew.mate_worker import MATE_WORKER_AGENT

            execution = read_session_execution(session_key, required=True)
            if (
                execution is not None
                and getattr(execution, "template_id", "") == MATE_WORKER_AGENT
                and execution.member_id
            ):
                _, record = member_config_for_id(cfg, execution.member_id)
    if record is None or record.acp_backend in (None, ""):
        return None
    return validate_agent_backend(record.acp_backend)


def session_backend(
    cfg: KiroCrewConfig,
    agent: str | None,
    crew_agent: str | None = None,
    *,
    session_key: str | None = None,
    backend_override: str | None = None,
) -> str:
    from kiro_crew.config.loader_agents import resolve_crew_identity
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import select_provider_backend

    return select_provider_backend(
        session_key,
        cfg.agent.member_acp_backend,
        cfg.agent.acp_backend,
        backend_override=backend_override,
        crew_backend=(
            crewmate_backend_pin(
                cfg, session_key, crew=resolve_crew_identity(cfg, agent, crew_agent)
            )
            if backend_override is None
            else None
        ),
    )
