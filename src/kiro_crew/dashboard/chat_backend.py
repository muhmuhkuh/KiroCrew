"""Per-chat harness intent; the selectable backend registry remains authoritative."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.backends import selectable_backends

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.state import _ChatSlot


def backend_selection_supported(slot: _ChatSlot) -> bool:
    return not (
        slot.executor != "local"
        or slot.mode not in ("", "orchestrator")
        or slot._app
        or slot.linked_session_key
        or slot.channel_origin
    )


def effective_chat_backend(slot: _ChatSlot, cfg: KiroCrewConfig) -> str:
    backend = cfg.agent.acp_backend if slot.acp_backend is None else slot.acp_backend
    if backend not in selectable_backends():
        raise ValueError("backend is not selectable")
    return backend


def profile_backend_for_slot(slot: _ChatSlot, cfg: KiroCrewConfig) -> str:
    """Return the profile-level backend for the crew this slot is running, or ``""``.

    Reads ``KiroCrewAgentConfig.backend`` for the slot's bound agent. An empty
    value or an agent with no record means "no profile pin" — the caller falls back
    to the global ``agent.acp_backend``.
    """
    agent_name = getattr(slot, "agent", "") or ""
    if not agent_name:
        return ""
    agent_cfg = cfg.agents.get(agent_name)
    if agent_cfg is None:
        return ""
    return getattr(agent_cfg, "backend", "") or ""


def chat_session_selection(
    slot: _ChatSlot, cfg: KiroCrewConfig | None, inherited_model: str | None
) -> dict[str, Any]:
    """Resolve both spawn paths without carrying a pin across harness namespaces.

    Backend precedence (highest → lowest):
    1. Explicit per-chat slot override (``slot.acp_backend``)
    2. Crew-profile backend (``KiroCrewAgentConfig.backend``)
    3. Global ``agent.acp_backend``
    """
    if not backend_selection_supported(slot):
        if slot.acp_backend is not None:
            raise ValueError("backend selection is unavailable for managed chats")
        return {"model": slot.model or inherited_model}
    if cfg is None:
        if slot.acp_backend is not None:
            raise ValueError("chat backend configuration is unavailable")
        return {"model": slot.model or inherited_model}
    if slot.acp_backend is None and slot.model_backend is None:
        # No explicit slot override — check for a profile-level backend first.
        profile_backend = profile_backend_for_slot(slot, cfg)
        if profile_backend and profile_backend in selectable_backends():
            # Treat the profile backend like an implicit slot override: it drives
            # backend and model resolution exactly as an explicit pick would, but
            # without persisting an acp_backend on the slot itself (so the user
            # can still override it per-session from the chat header).
            slot.model_backend = profile_backend
            selection: dict[str, Any] = {
                "model": slot.model or "auto",
                "acp_backend_override": profile_backend,
            }
            return selection
        # No profile backend either — retain the existing global allocation path.
        slot.model_backend = cfg.agent.acp_backend
        return {"model": slot.model or inherited_model}
    backend = effective_chat_backend(slot, cfg)
    if slot.model_backend is not None and slot.model_backend != backend:
        slot.model = "auto"
        slot._model_pick_gen += 1
        slot.forget_session_model_state()
        slot._dirty = True
    slot.model_backend = backend
    selection = {
        "model": slot.model or ("auto" if slot.acp_backend is not None else inherited_model)
    }
    if slot.acp_backend is not None:
        selection["acp_backend_override"] = backend
    return selection
