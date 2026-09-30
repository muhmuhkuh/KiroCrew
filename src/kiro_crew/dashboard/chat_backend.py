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


def chat_session_selection(
    slot: _ChatSlot, cfg: KiroCrewConfig | None, inherited_model: str | None
) -> dict[str, Any]:
    """Resolve both spawn paths without carrying a pin across harness namespaces."""
    if not backend_selection_supported(slot):
        if slot.acp_backend is not None:
            raise ValueError("backend selection is unavailable for managed chats")
        return {"model": slot.model or inherited_model}
    if cfg is None:
        if slot.acp_backend is not None:
            raise ValueError("chat backend configuration is unavailable")
        return {"model": slot.model or inherited_model}
    if slot.acp_backend is None and slot.model_backend is None:
        # Untouched chats retain the existing global allocation / warm-pool path.
        slot.model_backend = cfg.agent.acp_backend
        return {"model": slot.model or inherited_model}
    backend = effective_chat_backend(slot, cfg)
    if slot.model_backend is not None and slot.model_backend != backend:
        slot.model = "auto"
        slot._model_pick_gen += 1
        slot.forget_session_model_state()
        slot._dirty = True
    slot.model_backend = backend
    selection: dict[str, Any] = {
        "model": slot.model or ("auto" if slot.acp_backend is not None else inherited_model)
    }
    if slot.acp_backend is not None:
        selection["acp_backend_override"] = backend
    return selection
