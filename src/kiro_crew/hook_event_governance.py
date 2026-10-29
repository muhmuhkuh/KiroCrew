"""Policy floor for the trigger events on which script hooks may fire."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SCRIPT_HOOKS_SCOPE = "capabilities.script_hooks"


def event_refusal(session_key: str, event: str) -> str | None:
    """Return why policy refuses *event*, or ``None`` when it permits it.

    An absent event or unnamed inner ruleset preserves existing behavior. Policy
    evaluation runs off the event loop at asynchronous call sites. Enterprise
    evaluation errors fail closed; OSS keeps the capability gate's fail-soft rule.
    """
    if not event:
        return None

    from kiro_crew.edition_locks import locked_off
    from kiro_crew.platform.context import PlatformCompositionError

    enterprise = locked_off()
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            SCRIPT_HOOKS_SCOPE,
            f"events:{event}",
            session_key=session_key,
            log_warning=False,
            fail_closed=enterprise,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("hook event governance unavailable", exc_info=True)
        return "script_hooks governance unavailable (fail-closed)" if enterprise else None
    if not getattr(decision, "permitted", False):
        return f"hook event {event!r} refused by the security policy"
    return None
