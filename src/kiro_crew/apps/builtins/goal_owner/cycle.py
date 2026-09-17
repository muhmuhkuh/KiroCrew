"""Bounded Goal Owner observation and worker-dispatch wakes.

``run_owner_cycle`` remains observation-only. ``run_owner_wake`` composes one
such observation with durable-ledger recovery and bounded worker dispatch.
Acceptance evaluation and lifecycle transitions remain separate owner actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiro_crew import session_ledger, work_ledger

from .dispatch import DispatchReport, WorkerDispatchError, WorkerSessionGateway, dispatch_workers
from .domain import (
    GoalAssessment,
    GoalRecord,
    GoalStatus,
    InvalidGoalTransition,
    NextAction,
    TaskProjection,
    project_tasks,
)
from .store import GoalStore

OUTCOME_RECORDED = "recorded"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_INVALID = "invalid_record"
OUTCOME_NOT_RUNNABLE = "not_runnable"
OUTCOME_TERMINAL = "terminal"
OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"
OUTCOME_OWNER_UNBOUND = "owner_unbound"
OUTCOME_OBSERVATION_FAILED = "observation_failed"
OUTCOME_PERSISTENCE_FAILED = "persistence_failed"
OUTCOME_GATEWAY_UNAVAILABLE = "gateway_unavailable"
OUTCOME_DISPATCH_FAILED = "dispatch_failed"

_MAX_RESULT_REASON_CHARS = 500


@dataclass(frozen=True)
class OwnerCycleResult:
    """Result of one bounded owner observation pass."""

    goal_id: str
    outcome: str
    reason: str
    cycle: int = 0
    assessment: GoalAssessment | None = None
    next_action: NextAction | None = None
    projection: TaskProjection | None = None
    owner_phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "outcome": self.outcome,
            "reason": self.reason[:_MAX_RESULT_REASON_CHARS],
            "cycle": self.cycle,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "next_action": self.next_action.to_dict() if self.next_action else None,
            "projection": self.projection.to_dict() if self.projection else None,
            "owner_phase": self.owner_phase[:128],
        }


@dataclass(frozen=True)
class OwnerWakeResult:
    """Result of one observation plus optional bounded worker dispatch."""

    goal_id: str
    outcome: str
    reason: str
    cycle: OwnerCycleResult
    dispatch: DispatchReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "outcome": self.outcome,
            "reason": self.reason[:_MAX_RESULT_REASON_CHARS],
            "cycle": self.cycle.to_dict(),
            "dispatch": self.dispatch.to_dict() if self.dispatch else None,
        }


def _result(goal_id: str, outcome: str, reason: str, **kwargs: Any) -> OwnerCycleResult:
    return OwnerCycleResult(
        goal_id=goal_id,
        outcome=outcome,
        reason=reason[:_MAX_RESULT_REASON_CHARS],
        **kwargs,
    )


def _owner_phase(state: dict[str, Any]) -> str:
    phase = state.get("phase", "")
    return phase[:128] if isinstance(phase, str) else ""


def _assessment_and_action(
    goal: GoalRecord,
    projection: TaskProjection,
    *,
    has_work_ledger: bool,
    owner_phase: str,
    now: str | None,
) -> tuple[GoalAssessment, NextAction]:
    counts = (
        f"{len(projection.completed)} completed, "
        f"{len(projection.pending)} pending, "
        f"{len(projection.blocked)} blocked, "
        f"{len(projection.discovered)} discovered"
    )
    session_note = f" Owner session phase: {owner_phase}." if owner_phase else ""

    if not has_work_ledger:
        return (
            GoalAssessment(
                summary="Observed owner session; work ledger is not initialized." + session_note,
                gap="No conductor-owned work ledger is available for this goal.",
                confidence="low",
                observed_at=now or "",
            ),
            NextAction(
                kind="initialize",
                summary="Initialize work ledger before dispatching workers.",
            ),
        )

    if projection.blocked:
        return (
            GoalAssessment(
                summary=f"Observed work ledger: {counts}." + session_note,
                gap="Blocked work requires a new signal or human action.",
                confidence="high",
                observed_at=now or "",
            ),
            NextAction(
                kind="unblock",
                summary="Resolve blocker signal before retrying blocked work.",
            ),
        )

    if projection.pending:
        return (
            GoalAssessment(
                summary=f"Observed work ledger: {counts}." + session_note,
                gap="Pending work still needs independent acceptance evidence.",
                confidence="high",
                observed_at=now or "",
            ),
            NextAction(
                kind="verify",
                summary="Verify pending worker reports before dispatching more work.",
            ),
        )

    if projection.discovered:
        return (
            GoalAssessment(
                summary=f"Observed work ledger: {counts}." + session_note,
                gap="Discovered work has no planned goal reference.",
                confidence="high",
                observed_at=now or "",
            ),
            NextAction(
                kind="classify",
                summary="Classify discovered work before changing the goal plan.",
            ),
        )

    if projection.completed:
        return (
            GoalAssessment(
                summary=f"Observed work ledger: {counts}." + session_note,
                gap="Definition of done still needs independent evidence.",
                confidence="high",
                observed_at=now or "",
            ),
            NextAction(
                kind="verify",
                summary="Evaluate definition of done independently; do not infer completion.",
            ),
        )

    return (
        GoalAssessment(
            summary=f"Observed work ledger: {counts}." + session_note,
            gap="Goal has no planned work references yet.",
            confidence="medium",
            observed_at=now or "",
        ),
        NextAction(
            kind="plan",
            summary="Create a bounded plan before dispatching workers.",
        ),
    )


def run_owner_cycle(
    store: GoalStore,
    goal_id: str,
    *,
    now: str | None = None,
) -> OwnerCycleResult:
    """Run exactly one observation-only owner cycle for ``goal_id``.

    The method intentionally has no loop, worker callback, spawn adapter, or
    status transition. A non-runnable, terminal, unbound, malformed, or
    unreadable state returns without changing any ledger. A valid running goal
    consumes one cycle budget and persists one assessment plus one next action.
    """
    try:
        goal = store.get(goal_id)
    except (TypeError, ValueError) as exc:
        return _result(goal_id, OUTCOME_INVALID, str(exc))
    if goal is None:
        return _result(goal_id, OUTCOME_NOT_FOUND, "goal not found")
    if goal.goal_id != goal_id or goal.validate():
        return _result(goal_id, OUTCOME_INVALID, "stored goal record is invalid")
    if goal.is_terminal:
        return _result(
            goal_id,
            OUTCOME_TERMINAL,
            f"terminal goal cannot run: {goal.status.value}",
            cycle=goal.counters.cycles,
        )
    if goal.status is not GoalStatus.RUNNING:
        return _result(
            goal_id,
            OUTCOME_NOT_RUNNABLE,
            f"goal is not runnable: {goal.status.value}",
            cycle=goal.counters.cycles,
        )
    if goal.counters.cycles >= goal.budgets.max_cycles:
        return _result(
            goal_id,
            OUTCOME_BUDGET_EXHAUSTED,
            "owner-cycle budget exhausted",
            cycle=goal.counters.cycles,
        )

    owner_key = goal.owner_session.session_key.strip()
    if not owner_key:
        return _result(
            goal_id,
            OUTCOME_OWNER_UNBOUND,
            "goal has no persistent owner session",
            cycle=goal.counters.cycles,
        )

    try:
        owner_state = session_ledger.read_state(owner_key)
        conductor = work_ledger.read_conductor(owner_key)
        work_items = work_ledger.list_work_items(owner_key)
    except Exception as exc:  # noqa: BLE001 - a cycle must fail closed
        return _result(
            goal_id,
            OUTCOME_OBSERVATION_FAILED,
            f"could not observe owner ledgers: {exc}",
            cycle=goal.counters.cycles,
        )

    projection = project_tasks(goal, work_items)
    phase = _owner_phase(owner_state)
    assessment, next_action = _assessment_and_action(
        goal,
        projection,
        has_work_ledger=conductor is not None,
        owner_phase=phase,
        now=now,
    )

    try:
        goal.record_cycle(assessment=assessment, next_action=next_action, now=now)
        store.save(goal)
    except InvalidGoalTransition as exc:
        return _result(
            goal_id,
            OUTCOME_NOT_RUNNABLE,
            str(exc),
            cycle=goal.counters.cycles - 1,
        )
    except Exception as exc:  # noqa: BLE001 - report failed persistence, never claim success
        return _result(
            goal_id,
            OUTCOME_PERSISTENCE_FAILED,
            f"could not persist owner cycle: {exc}",
            cycle=goal.counters.cycles - 1,
        )

    return _result(
        goal_id,
        OUTCOME_RECORDED,
        "one bounded observation cycle recorded",
        cycle=goal.counters.cycles,
        assessment=assessment,
        next_action=next_action,
        projection=projection,
        owner_phase=phase,
    )


def _wake_stamp(now: str | None) -> str:
    if isinstance(now, str) and now.strip():
        return now.strip()
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def run_owner_wake(
    store: GoalStore,
    goal_id: str,
    gateway: WorkerSessionGateway | None = None,
    *,
    now: str | None = None,
) -> OwnerWakeResult:
    """Run one observation, then recover and dispatch existing planned work.

    Observation is committed before any session side effect. No task creation,
    acceptance, verdict, or completion transition happens here.
    """
    stamp = _wake_stamp(now)
    cycle = run_owner_cycle(store, goal_id, now=stamp)
    if cycle.outcome != OUTCOME_RECORDED:
        return OwnerWakeResult(goal_id, cycle.outcome, cycle.reason, cycle)

    goal = store.get(goal_id)
    if goal is None or goal.goal_id != goal_id or goal.validate():
        return OwnerWakeResult(
            goal_id,
            OUTCOME_PERSISTENCE_FAILED,
            "goal changed or became unreadable after observation",
            cycle,
        )
    if not goal.task_refs:
        return OwnerWakeResult(
            goal_id,
            OUTCOME_RECORDED,
            "observation recorded; no planned work to dispatch",
            cycle,
        )
    if gateway is None:
        return OwnerWakeResult(
            goal_id,
            OUTCOME_GATEWAY_UNAVAILABLE,
            "observation recorded; worker session gateway is unavailable",
            cycle,
        )

    owner_key = goal.owner_session.session_key.strip()
    try:
        work_items = work_ledger.list_work_items(owner_key)
        dispatch = await dispatch_workers(
            store,
            goal,
            work_items,
            gateway,
            now=stamp,
            recover=True,
        )
    except WorkerDispatchError as exc:
        return OwnerWakeResult(
            goal_id,
            OUTCOME_DISPATCH_FAILED,
            f"observation recorded; dispatch refused: {exc}",
            cycle,
        )
    except Exception as exc:  # noqa: BLE001 - wake must remain bounded and retryable
        return OwnerWakeResult(
            goal_id,
            OUTCOME_DISPATCH_FAILED,
            f"observation recorded; dispatch failed: {exc}",
            cycle,
        )

    return OwnerWakeResult(
        goal_id,
        OUTCOME_RECORDED,
        "one bounded observation and worker-dispatch wake recorded",
        cycle,
        dispatch,
    )


__all__ = [
    "OUTCOME_BUDGET_EXHAUSTED",
    "OUTCOME_DISPATCH_FAILED",
    "OUTCOME_GATEWAY_UNAVAILABLE",
    "OUTCOME_INVALID",
    "OUTCOME_NOT_FOUND",
    "OUTCOME_NOT_RUNNABLE",
    "OUTCOME_OBSERVATION_FAILED",
    "OUTCOME_OWNER_UNBOUND",
    "OUTCOME_PERSISTENCE_FAILED",
    "OUTCOME_RECORDED",
    "OUTCOME_TERMINAL",
    "OwnerCycleResult",
    "OwnerWakeResult",
    "run_owner_cycle",
    "run_owner_wake",
]
