"""Lifecycle hooks for the Goal Owner builtin app."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .scheduler import GoalScheduler, ReconcileReport
from .store import GoalStore

logger = logging.getLogger(__name__)


@dataclass
class GoalRuntime:
    """App-local runtime shared by lifecycle diagnostics and future cycles."""

    store: GoalStore
    scheduler: GoalScheduler | None = None
    last_reconcile: ReconcileReport | None = None


_runtime: GoalRuntime | None = None


def get_runtime() -> GoalRuntime | None:
    """Return active runtime, or ``None`` before enable/after disable."""
    return _runtime


def _degrade(ctx: Any, message: str) -> None:
    health = getattr(ctx, "health", None)
    if health is not None:
        health.mark_degraded(message)


async def on_startup(ctx: Any) -> None:
    """Load durable state and reconcile owner wakes once per enable."""
    global _runtime

    if _runtime is not None:
        return

    storage = getattr(ctx, "storage", None)
    if storage is None:
        _degrade(ctx, "Goal Owner storage permission is unavailable")
        return

    store = GoalStore(storage)
    cron = getattr(ctx, "cron", None)
    scheduler = GoalScheduler(store, cron) if cron is not None else None
    runtime = GoalRuntime(store=store, scheduler=scheduler)
    _runtime = runtime

    if scheduler is None:
        _degrade(ctx, "Goal Owner cron runtime is unavailable")
        return

    try:
        report = await scheduler.reconcile()
    except Exception:  # noqa: BLE001 - startup must remain bounded and retryable
        logger.exception("goal-owner startup reconciliation failed")
        _degrade(ctx, "Goal Owner startup reconciliation failed")
        return

    runtime.last_reconcile = report
    if not report.ok:
        _degrade(ctx, "Goal Owner startup reconciliation was incomplete")


def _reset_for_tests() -> None:
    """Clear process-local runtime state. Tests only."""
    global _runtime
    _runtime = None


async def on_shutdown(ctx: Any) -> None:  # noqa: ARG001 - hook ABI
    """Drop process-local handles; gateway owns cron removal during teardown."""
    _reset_for_tests()


__all__ = ["GoalRuntime", "get_runtime", "on_startup", "on_shutdown"]
