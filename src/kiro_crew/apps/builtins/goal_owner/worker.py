"""Bounded worker contracts and independent acceptance for goal-owner.

Workers can report progress or ``done`` through the existing work ledger. They
cannot write conductor state or accept their own work. This module only builds
contracts and applies evaluator results; session creation stays a later adapter.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiro_crew import work_ledger

from .domain import (
    COMPLETION_FAIL,
    COMPLETION_PASS,
    COMPLETION_UNKNOWN,
    CompletionCheck,
    CompletionEvidence,
    GoalRecord,
    GoalStatus,
)
from .store import GoalStore

WORKER_AGENT = "kirocrew-worker"
_ACCEPTANCE_VERDICTS = frozenset(work_ledger.VERDICTS)

OUTCOME_NO_WORK = "no_work"
OUTCOME_RECORDED = "recorded"
OUTCOME_COMPLETED = "completed"


class WorkerContractError(ValueError):
    """Raised when a worker or evaluator payload cannot be trusted."""

    def __init__(self, message: str, *, code: str = "invalid_worker_contract") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkerContract:
    """One DoD criterion mapped to one conductor-owned work item."""

    goal_id: str
    item_id: str
    criterion: str
    acceptance: Mapping[str, Any]
    agent: str = WORKER_AGENT
    worker_session_key: str = ""


@dataclass(frozen=True)
class AcceptanceResult:
    """One result returned by an independent acceptance evaluator."""

    item_id: str
    verdict: str
    evidence: str
    evaluated_at: str


@dataclass(frozen=True)
class AcceptanceReport:
    """What one acceptance application changed, without hiding pending work."""

    goal_id: str
    outcome: str
    reason: str
    accepted: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    evidence: CompletionEvidence | None = None
    goal_completed: bool = False
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "accepted": list(self.accepted),
            "pending": list(self.pending),
            "failed": list(self.failed),
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "goal_completed": self.goal_completed,
            "changed": self.changed,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_items(items: Iterable[work_ledger.WorkItem]) -> list[work_ledger.WorkItem]:
    result = list(items)
    seen: set[str] = set()
    for item in result:
        if not isinstance(item, work_ledger.WorkItem):
            raise WorkerContractError("items must contain WorkItem records", code="invalid_item")
        if not item.item_id:
            raise WorkerContractError("work item has no item_id", code="invalid_item")
        if item.item_id in seen:
            raise WorkerContractError(
                f"duplicate work item {item.item_id!r}", code="duplicate_item"
            )
        seen.add(item.item_id)
    return result


def _goal_items(
    goal: GoalRecord, items: Iterable[work_ledger.WorkItem]
) -> tuple[list[work_ledger.WorkItem], dict[str, work_ledger.WorkItem]]:
    if not isinstance(goal, GoalRecord):
        raise WorkerContractError("goal must be a GoalRecord", code="invalid_goal")
    errors = goal.validate()
    if errors:
        raise WorkerContractError("invalid goal: " + "; ".join(errors), code="invalid_goal")
    checked = _require_items(items)
    by_id = {item.item_id: item for item in checked}
    if len(goal.task_refs) != len(goal.definition_of_done):
        raise WorkerContractError(
            "each definition-of-done criterion needs exactly one task_ref",
            code="task_ref_cardinality",
        )
    if len(set(goal.task_refs)) != len(goal.task_refs):
        raise WorkerContractError("task_refs must be unique", code="duplicate_task_ref")

    planned: list[work_ledger.WorkItem] = []
    for criterion, item_id in zip(goal.definition_of_done, goal.task_refs):
        item = by_id.get(item_id)
        if item is None:
            raise WorkerContractError(
                f"missing work item for task_ref {item_id!r}", code="missing_task_ref"
            )
        if item.title.strip() != criterion.strip():
            raise WorkerContractError(
                f"work item {item_id!r} is not assigned to its DoD criterion",
                code="task_ref_mismatch",
            )
        if not isinstance(item.acceptance, Mapping) or not item.acceptance:
            raise WorkerContractError(
                f"work item {item_id!r} has no acceptance specification",
                code="acceptance_required",
            )
        planned.append(item)
    return planned, by_id


def _check_dispatch_budget(
    goal: GoalRecord,
    planned: Iterable[work_ledger.WorkItem],
    reserved_item_ids: Iterable[str] = (),
) -> None:
    reserved = set(reserved_item_ids)
    unbound = sum(
        1
        for item in planned
        if (
            not item.is_terminal
            and not (item.worker_session_key or "").strip()
            and item.item_id not in reserved
        )
    )
    remaining = goal.budgets.max_worker_dispatches - goal.counters.worker_dispatches
    if unbound > remaining:
        raise WorkerContractError(
            f"worker dispatch budget would be exceeded: need {unbound}, have {max(remaining, 0)}",
            code="dispatch_budget_exhausted",
        )


def derive_contracts(
    goal: GoalRecord,
    items: Iterable[work_ledger.WorkItem],
    *,
    reserved_item_ids: Iterable[str] = (),
) -> tuple[WorkerContract, ...]:
    """Build one fixed-agent contract per planned DoD item.

    Unplanned ledger items remain visible to the owner as discovered work but are
    not silently assigned to this goal. Unbound open items consume dispatch
    budget; bound or terminal items do not consume it again.
    """
    planned, _ = _goal_items(goal, items)
    _check_dispatch_budget(goal, planned, reserved_item_ids)
    return tuple(
        WorkerContract(
            goal_id=goal.goal_id,
            item_id=item.item_id,
            criterion=criterion,
            acceptance=dict(item.acceptance),
            agent=WORKER_AGENT,
            worker_session_key=item.worker_session_key or "",
        )
        for criterion, item in zip(goal.definition_of_done, planned)
    )


def _validate_contract(contract: WorkerContract) -> None:
    if not isinstance(contract, WorkerContract):
        raise WorkerContractError("contract must be a WorkerContract", code="invalid_contract")
    if contract.agent != WORKER_AGENT:
        raise WorkerContractError("worker agent is fixed", code="invalid_agent")
    if not contract.criterion.strip() or not contract.item_id.strip():
        raise WorkerContractError("contract needs item_id and criterion", code="invalid_contract")
    if not isinstance(contract.acceptance, Mapping) or not contract.acceptance:
        raise WorkerContractError("contract needs acceptance", code="acceptance_required")


def worker_seed(contract: WorkerContract) -> str:
    """Render worker-visible input without goal, budget, or session metadata."""
    _validate_contract(contract)
    try:
        acceptance = json.dumps(
            dict(contract.acceptance), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise WorkerContractError(
            f"acceptance is not JSON-serializable: {exc}", code="invalid_acceptance"
        ) from exc
    return (
        f"Work item: {contract.criterion}\n"
        f"Acceptance specification (read-only): {acceptance}\n"
        "Report through work_report with status progress, done, blocked, or question; "
        "include concise summary and artifact references. Do not change acceptance or "
        "claim acceptance: an independent evaluator decides the verdict."
    )


def build_done_batch(items: Iterable[work_ledger.WorkItem]) -> dict[str, Any]:
    """Build evaluator input from open worker ``done`` claims only."""
    checked = _require_items(items)
    done = [item for item in checked if item.status == "done" and not item.is_terminal]
    for item in done:
        if not item.acceptance:
            raise WorkerContractError(
                f"done item {item.item_id!r} has no acceptance", code="acceptance_required"
            )
    return work_ledger.accept_batch(done)


def _coerce_result(raw: AcceptanceResult | Mapping[str, Any], now: str) -> AcceptanceResult:
    if isinstance(raw, AcceptanceResult):
        result = raw
    elif isinstance(raw, Mapping):
        item_id = raw.get("item_id", raw.get("id"))
        raw_verdict = raw.get("verdict")
        raw_evidence = raw.get("evidence")
        raw_evaluated_at = raw.get("evaluated_at")
        result = AcceptanceResult(
            item_id=item_id if isinstance(item_id, str) else "",
            verdict=raw_verdict if isinstance(raw_verdict, str) else "",
            evidence=raw_evidence if isinstance(raw_evidence, str) else "",
            evaluated_at=raw_evaluated_at if isinstance(raw_evaluated_at, str) else now,
        )
    else:
        raise WorkerContractError("results must contain evaluator records", code="invalid_result")
    verdict = result.verdict.strip().lower()
    if not result.item_id.strip() or verdict not in _ACCEPTANCE_VERDICTS:
        raise WorkerContractError("invalid evaluator result", code="invalid_result")
    if verdict == "pass" and not result.evidence.strip():
        raise WorkerContractError("pass result needs evidence", code="evidence_required")
    evaluated_at = result.evaluated_at.strip() or now
    return AcceptanceResult(
        result.item_id.strip(), verdict, result.evidence[:500], evaluated_at[:64]
    )


def _completion_result(verdict: str) -> str:
    if verdict == "pass":
        return COMPLETION_PASS
    if verdict == "fail":
        return COMPLETION_FAIL
    return COMPLETION_UNKNOWN


def _build_evidence(
    goal: GoalRecord,
    planned: list[work_ledger.WorkItem],
    results: Mapping[str, AcceptanceResult],
    current: Mapping[str, work_ledger.WorkItem],
    *,
    now: str,
) -> CompletionEvidence:
    previous = {
        check.criterion.strip(): check
        for check in (goal.completion_evidence.checks if goal.completion_evidence else [])
        if check.criterion.strip()
    }
    checks: list[CompletionCheck] = []
    for criterion, item in zip(goal.definition_of_done, planned):
        result = results.get(item.item_id)
        if result is not None:
            checks.append(
                CompletionCheck(
                    criterion=criterion,
                    result=_completion_result(result.verdict),
                    reference=result.evidence,
                    observed_at=result.evaluated_at,
                )
            )
            continue
        prior = previous.get(criterion.strip())
        live = current.get(item.item_id)
        if (
            prior is not None
            and live is not None
            and live.state == "accepted"
            and live.verdict == "pass"
        ):
            checks.append(prior)
        else:
            checks.append(
                CompletionCheck(
                    criterion=criterion,
                    result=COMPLETION_UNKNOWN,
                    reference="no evaluator result",
                    observed_at=now,
                )
            )
    return CompletionEvidence(checks=checks, verified_at=now, evaluator="accept_eval")


def _copy_record(target: GoalRecord, source: GoalRecord) -> None:
    target.__dict__.update(source.__dict__)


def apply_acceptance(
    store: GoalStore,
    goal: GoalRecord,
    items: Iterable[work_ledger.WorkItem],
    results: Iterable[AcceptanceResult | Mapping[str, Any]],
    *,
    now: str,
) -> AcceptanceReport:
    """Apply evaluator results, then persist goal evidence and completion.

    All result IDs and terminal conflicts are checked before the first ledger
    write. Work items close before the GoalRecord is saved, so a crash leaves a
    repeatable accepted-item state rather than a false completed goal.
    """
    stamp = now.strip() if isinstance(now, str) and now.strip() else _now_iso()
    checked_items = _require_items(items)
    planned, _ = _goal_items(goal, checked_items)
    _check_dispatch_budget(goal, planned)
    owner_key = goal.owner_session.session_key.strip()
    if not owner_key:
        raise WorkerContractError("goal has no owner session", code="owner_unbound")

    batch = build_done_batch(checked_items)
    required_ids = {entry["id"] for entry in batch["items"]}
    accepted_ids = {
        item.item_id
        for item in planned
        if item.status == "done" and item.state == "accepted" and item.verdict == "pass"
    }
    coerced = [_coerce_result(raw, stamp) for raw in results]
    result_map: dict[str, AcceptanceResult] = {}
    for result in coerced:
        if result.item_id in result_map:
            raise WorkerContractError(
                f"duplicate evaluator result {result.item_id!r}", code="duplicate_result"
            )
        result_map[result.item_id] = result
    allowed_ids = required_ids | accepted_ids
    if not required_ids.issubset(result_map) or not set(result_map).issubset(allowed_ids):
        raise WorkerContractError(
            "evaluator result IDs do not exactly cover open done items",
            code="result_set_mismatch",
        )
    for item_id in accepted_ids & result_map.keys():
        if result_map[item_id].verdict != "pass":
            raise WorkerContractError(
                f"accepted item {item_id!r} cannot receive a non-pass result",
                code="terminal_conflict",
            )

    current: dict[str, work_ledger.WorkItem] = {}
    for item_id in result_map:
        try:
            item = work_ledger.read_work_item(owner_key, item_id, strict=True)
        except Exception as exc:  # noqa: BLE001 - acceptance must fail closed
            raise WorkerContractError(
                f"could not read work item {item_id!r}: {exc}", code="ledger_read_failed"
            ) from exc
        if item is None:
            raise WorkerContractError(
                f"evaluator result names unknown work item {item_id!r}", code="unknown_item"
            )
        current[item_id] = item
        result = result_map[item_id]
        if item.state == "accepted":
            if item.verdict != "pass" or result.verdict != "pass":
                raise WorkerContractError(
                    f"terminal work item {item_id!r} conflicts with evaluator result",
                    code="terminal_conflict",
                )
        elif item.state != "open" or item.status != "done":
            raise WorkerContractError(
                f"work item {item_id!r} is not an open done claim",
                code="stale_item",
            )

    for item_id, result in result_map.items():
        item = current[item_id]
        if item.state == "accepted":
            continue
        if item.verdict != result.verdict:
            fails = item.fails + (1 if result.verdict == "fail" else 0)
            work_ledger.apply_conductor_action(
                owner_key,
                "verdict",
                item_id=item_id,
                verdict=result.verdict,
                fails=fails,
            )
        if result.verdict == "pass":
            work_ledger.apply_conductor_action(
                owner_key,
                "close",
                item_id=item_id,
                state="accepted",
                decision="accepted by independent evaluator",
            )

    refreshed: dict[str, work_ledger.WorkItem] = {}
    for item in planned:
        try:
            live = work_ledger.read_work_item(owner_key, item.item_id, strict=True)
        except Exception as exc:  # noqa: BLE001 - do not claim completion
            raise WorkerContractError(
                f"could not reread work item {item.item_id!r}: {exc}",
                code="ledger_read_failed",
            ) from exc
        if live is not None:
            refreshed[item.item_id] = live

    evidence = _build_evidence(goal, planned, result_map, refreshed, now=stamp)
    all_accepted = all(
        refreshed.get(item.item_id) is not None
        and refreshed[item.item_id].state == "accepted"
        and refreshed[item.item_id].verdict == "pass"
        for item in planned
    )
    completed = all_accepted and evidence.satisfies(goal.definition_of_done)

    candidate = GoalRecord.from_dict(goal.to_dict())
    if completed and candidate.status is not GoalStatus.COMPLETED:
        candidate.transition(
            GoalStatus.COMPLETED,
            completion_evidence=evidence,
            now=stamp,
        )
    elif candidate.status is not GoalStatus.COMPLETED:
        candidate.completion_evidence = evidence
        if (
            candidate.completion_evidence.to_dict() != goal.completion_evidence.to_dict()
            if goal.completion_evidence
            else True
        ):
            candidate.timestamps.updated_at = stamp
    elif not candidate.has_valid_completion_evidence:
        raise WorkerContractError(
            "completed goal lacks valid completion evidence", code="invalid_goal"
        )

    changed = candidate.to_dict() != goal.to_dict()
    if changed:
        store.save(candidate)
        _copy_record(goal, candidate)

    accepted = tuple(
        item.item_id
        for item in refreshed.values()
        if item.state == "accepted" and item.verdict == "pass"
    )
    failed = tuple(item_id for item_id, result in result_map.items() if result.verdict == "fail")
    pending = tuple(
        item_id
        for item_id, result in result_map.items()
        if result.verdict in {"pending", "refused", "error"}
    )
    outcome = (
        OUTCOME_COMPLETED
        if candidate.status is GoalStatus.COMPLETED
        else (OUTCOME_RECORDED if result_map or changed else OUTCOME_NO_WORK)
    )
    return AcceptanceReport(
        goal_id=goal.goal_id,
        outcome=outcome,
        reason=(
            "all definition-of-done items independently accepted"
            if candidate.status is GoalStatus.COMPLETED
            else "acceptance results recorded"
        ),
        accepted=accepted,
        pending=pending,
        failed=failed,
        evidence=evidence,
        goal_completed=candidate.status is GoalStatus.COMPLETED,
        changed=changed,
    )


__all__ = [
    "AcceptanceReport",
    "AcceptanceResult",
    "OUTCOME_COMPLETED",
    "OUTCOME_NO_WORK",
    "OUTCOME_RECORDED",
    "WORKER_AGENT",
    "WorkerContract",
    "WorkerContractError",
    "apply_acceptance",
    "build_done_batch",
    "derive_contracts",
    "worker_seed",
]
