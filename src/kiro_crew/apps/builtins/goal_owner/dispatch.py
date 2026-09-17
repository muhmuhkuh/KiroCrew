"""Owner-to-worker dispatch adapter for the goal-owner app.

The gateway is injected because app code must not reach dashboard session
control or confuse a background spawn id with a worker session key. Dispatch
order is durable and narrow: reserve budget, create session, bind ledger item,
then send the isolated worker seed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kiro_crew import work_ledger

from .domain import (
    DISPATCH_BOUND,
    DISPATCH_SEEDED,
    MAX_DISPATCH_RESERVATIONS,
    DispatchReservation,
    GoalRecord,
    GoalStatus,
)
from .store import GoalStore
from .worker import WorkerContract, WorkerContractError, derive_contracts, worker_seed

DISPATCHED = "dispatched"
RECOVERED = "recovered"
ALREADY_BOUND = "already_bound"
BOUND_UNSENT = "bound_unsent"
SKIPPED_TERMINAL = "skipped_terminal"
DISPATCH_FAILED = "failed"
BUDGET_EXHAUSTED = "budget_exhausted"


@runtime_checkable
class WorkerSessionGateway(Protocol):
    """Small host adapter; implementations must make IDs idempotent."""

    async def create_session(self, *, request_id: str, title: str, agent: str) -> str: ...

    async def send(self, session_key: str, message: str, *, delivery_id: str) -> None: ...

    async def close_session(self, session_key: str) -> None: ...


class WorkerDispatchError(ValueError):
    """Raised when dispatch cannot safely begin."""

    def __init__(self, message: str, *, code: str = "dispatch_refused") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DispatchRecord:
    item_id: str
    outcome: str
    session_key: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "outcome": self.outcome,
            "session_key": self.session_key,
            "reason": self.reason[:500],
        }


@dataclass(frozen=True)
class DispatchReport:
    goal_id: str
    reserved: int
    records: tuple[DispatchRecord, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.reserved
            or any(record.outcome in {DISPATCHED, RECOVERED} for record in self.records)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "goal_id": self.goal_id,
            "reserved": self.reserved,
            "changed": self.changed,
            "records": [record.to_dict() for record in self.records],
        }


# Reservation is synchronous and short. It closes the in-process lost-update
# window without holding a lock across network/session awaits.
# ponytail: process-local lock; cross-process CAS belongs in GoalStore if live multi-process dispatch appears.
_RESERVATION_LOCK = threading.Lock()


def _copy_record(target: GoalRecord, source: GoalRecord) -> None:
    target.__dict__.update(source.__dict__)


def _dispatch_id(goal_id: str, item_id: str) -> str:
    return f"goal-owner:{goal_id}:{item_id}"


def _record(
    item_id: str, outcome: str, *, session_key: str = "", reason: str = ""
) -> DispatchRecord:
    return DispatchRecord(item_id, outcome, session_key, reason[:500])


def _reservation(goal: GoalRecord, item_id: str) -> DispatchReservation | None:
    return next(
        (item for item in goal.dispatch_reservations if item.item_id == item_id),
        None,
    )


def _reserve_one(store: GoalStore, goal: GoalRecord, item_id: str, *, now: str) -> bool:
    with _RESERVATION_LOCK:
        live = store.get(goal.goal_id)
        if live is None or live.goal_id != goal.goal_id:
            raise WorkerDispatchError("goal disappeared during dispatch", code="goal_not_found")
        if live.status not in {GoalStatus.RUNNING, GoalStatus.WAITING}:
            raise WorkerDispatchError(
                f"goal is not dispatchable: {live.status.value}", code="goal_not_runnable"
            )
        if _reservation(live, item_id) is not None:
            _copy_record(goal, live)
            return False
        if len(live.dispatch_reservations) >= MAX_DISPATCH_RESERVATIONS:
            raise WorkerDispatchError(
                "dispatch reservation capacity exhausted", code="reservation_capacity"
            )
        if live.counters.worker_dispatches >= live.budgets.max_worker_dispatches:
            raise WorkerDispatchError("worker dispatch budget exhausted", code="budget_exhausted")
        candidate = GoalRecord.from_dict(live.to_dict())
        candidate.counters.worker_dispatches += 1
        candidate.dispatch_reservations.append(
            DispatchReservation(
                item_id=item_id,
                request_id=_dispatch_id(candidate.goal_id, item_id),
                reserved_at=now,
            )
        )
        candidate.timestamps.updated_at = now
        store.save(candidate)
        _copy_record(goal, candidate)
        return True


def _mark_reservation(
    store: GoalStore,
    goal: GoalRecord,
    item_id: str,
    state: str,
    *,
    now: str,
    session_key: str = "",
) -> None:
    with _RESERVATION_LOCK:
        live = store.get(goal.goal_id)
        if live is None or live.goal_id != goal.goal_id:
            raise WorkerDispatchError(
                "goal disappeared during reservation update", code="goal_not_found"
            )
        reservation = _reservation(live, item_id)
        if reservation is None:
            raise WorkerDispatchError("dispatch reservation missing", code="reservation_missing")
        candidate = GoalRecord.from_dict(live.to_dict())
        updated = _reservation(candidate, item_id)
        assert updated is not None
        updated.state = state
        if session_key:
            updated.session_key = session_key
        candidate.timestamps.updated_at = now
        store.save(candidate)
        _copy_record(goal, candidate)


def _release_one(store: GoalStore, goal: GoalRecord, item_id: str, *, now: str) -> None:
    """Release reservation when no worker binding survived the attempt."""
    with _RESERVATION_LOCK:
        live = store.get(goal.goal_id)
        if live is None or live.goal_id != goal.goal_id:
            raise WorkerDispatchError(
                "goal disappeared during reservation release", code="goal_not_found"
            )
        index = next(
            (
                index
                for index, item in enumerate(live.dispatch_reservations)
                if item.item_id == item_id
            ),
            None,
        )
        if index is None:
            raise WorkerDispatchError("dispatch reservation missing", code="reservation_missing")
        if live.counters.worker_dispatches <= 0:
            raise WorkerDispatchError(
                "dispatch reservation underflow", code="reservation_underflow"
            )
        candidate = GoalRecord.from_dict(live.to_dict())
        candidate.dispatch_reservations.pop(index)
        candidate.counters.worker_dispatches -= 1
        candidate.timestamps.updated_at = now
        store.save(candidate)
        _copy_record(goal, candidate)


async def _close_after_failed_bind(
    gateway: WorkerSessionGateway, session_key: str, reason: str
) -> str:
    try:
        await gateway.close_session(session_key)
    except Exception as exc:  # noqa: BLE001 - preserve original refusal and cleanup detail
        return f"{reason}; session cleanup failed: {exc}"
    return reason


async def _send_seed(
    gateway: WorkerSessionGateway,
    contract: WorkerContract,
    session_key: str,
) -> None:
    await gateway.send(
        session_key,
        worker_seed(contract),
        delivery_id=_dispatch_id(contract.goal_id, contract.item_id),
    )


async def dispatch_workers(
    store: GoalStore,
    goal: GoalRecord,
    items: Iterable[work_ledger.WorkItem],
    gateway: WorkerSessionGateway,
    *,
    now: str,
    recover: bool = False,
) -> DispatchReport:
    """Dispatch unbound work and optionally retry bound-but-unseeded items.

    ``create_session`` must deduplicate ``request_id`` and ``send`` must
    deduplicate ``delivery_id``. Those two IDs make a retry after a crash safe:
    an already-created or already-seeded worker is observed rather than doubled.
    """
    live_goal = store.get(goal.goal_id)
    if live_goal is None or live_goal.goal_id != goal.goal_id:
        raise WorkerDispatchError("goal disappeared before dispatch", code="goal_not_found")
    _copy_record(goal, live_goal)
    owner_key = goal.owner_session.session_key.strip()
    if not owner_key:
        raise WorkerDispatchError("goal has no owner session", code="owner_unbound")
    if goal.status not in {GoalStatus.RUNNING, GoalStatus.WAITING}:
        raise WorkerDispatchError(
            f"goal is not dispatchable: {goal.status.value}", code="goal_not_runnable"
        )
    checked_items = list(items)
    reserved_item_ids = {item.item_id for item in goal.dispatch_reservations}
    try:
        checked_contracts = derive_contracts(
            goal, checked_items, reserved_item_ids=reserved_item_ids
        )
    except WorkerContractError as exc:
        raise WorkerDispatchError(str(exc), code=exc.code) from exc

    item_by_id = {item.item_id: item for item in checked_items}
    records: list[DispatchRecord] = []
    reserved = 0
    stamp = now.strip() if isinstance(now, str) and now.strip() else ""
    if not stamp:
        raise WorkerDispatchError(
            "now is required for durable dispatch reservation", code="timestamp_required"
        )

    for contract in checked_contracts:
        item = item_by_id[contract.item_id]
        if item.is_terminal:
            records.append(_record(item.item_id, SKIPPED_TERMINAL, reason="work item is terminal"))
            continue

        session_key = (item.worker_session_key or "").strip()
        if session_key:
            if not recover or item.status is not None:
                records.append(_record(item.item_id, ALREADY_BOUND, session_key=session_key))
                continue
            reservation_warning = ""
            if _reservation(goal, item.item_id) is not None:
                try:
                    _mark_reservation(
                        store,
                        goal,
                        item.item_id,
                        DISPATCH_BOUND,
                        now=stamp,
                        session_key=session_key,
                    )
                except Exception as exc:  # noqa: BLE001 - binding remains recovery state
                    reservation_warning = f"reservation update failed: {exc}"
            try:
                await _send_seed(gateway, contract, session_key)
            except Exception as exc:  # noqa: BLE001 - recovery stays bounded
                reason = "; ".join(filter(None, (reservation_warning, str(exc))))
                records.append(
                    _record(item.item_id, BOUND_UNSENT, session_key=session_key, reason=reason)
                )
            else:
                try:
                    _mark_reservation(
                        store,
                        goal,
                        item.item_id,
                        DISPATCH_SEEDED,
                        now=stamp,
                        session_key=session_key,
                    )
                except Exception as exc:  # noqa: BLE001 - delivery already idempotent
                    reservation_warning = "; ".join(
                        filter(None, (reservation_warning, f"reservation update failed: {exc}"))
                    )
                records.append(
                    _record(
                        item.item_id,
                        RECOVERED,
                        session_key=session_key,
                        reason=reservation_warning,
                    )
                )
            continue

        try:
            new_reservation = _reserve_one(store, goal, item.item_id, now=stamp)
        except WorkerDispatchError as exc:
            records.append(_record(item.item_id, BUDGET_EXHAUSTED, reason=str(exc)))
            continue
        if new_reservation:
            reserved += 1
        reservation = _reservation(goal, item.item_id)
        request_id = (
            reservation.request_id
            if reservation is not None
            else _dispatch_id(contract.goal_id, contract.item_id)
        )
        session_key = ""
        try:
            created_key = await gateway.create_session(
                request_id=request_id,
                title=contract.criterion,
                agent=contract.agent,
            )
            session_key = created_key.strip() if isinstance(created_key, str) else ""
            if not session_key or session_key == owner_key:
                raise WorkerDispatchError(
                    "session gateway returned an invalid worker session key",
                    code="invalid_worker_session",
                )
            if "\0" in session_key:
                raise WorkerDispatchError(
                    "session gateway returned an unsafe worker session key",
                    code="invalid_worker_session",
                )
        except Exception as exc:  # noqa: BLE001 - one item must not unbound siblings
            reason = str(exc)
            if session_key and session_key != owner_key and "\0" not in session_key:
                reason = await _close_after_failed_bind(gateway, session_key, reason)
            if new_reservation:
                try:
                    _release_one(store, goal, item.item_id, now=stamp)
                except Exception as release_exc:  # noqa: BLE001 - keep budget fail-closed
                    reason += f"; reservation release failed: {release_exc}"
                else:
                    reserved -= 1
            records.append(_record(item.item_id, DISPATCH_FAILED, reason=reason))
            continue

        try:
            work_ledger.apply_conductor_action(
                owner_key,
                "bind",
                item_id=item.item_id,
                worker_session_key=session_key,
            )
        except Exception as exc:  # noqa: BLE001 - inspect before cleanup
            bound_after_error = work_ledger.read_work_item(owner_key, item.item_id)
            if (
                bound_after_error is not None
                and bound_after_error.worker_session_key == session_key
            ):
                try:
                    _mark_reservation(
                        store,
                        goal,
                        item.item_id,
                        DISPATCH_BOUND,
                        now=stamp,
                        session_key=session_key,
                    )
                    reason = f"binding raised after durable write: {exc}"
                except Exception as state_exc:  # noqa: BLE001 - binding remains durable
                    reason = (
                        f"binding raised after durable write: {exc}; "
                        f"reservation update failed: {state_exc}"
                    )
                records.append(
                    _record(
                        item.item_id,
                        BOUND_UNSENT,
                        session_key=session_key,
                        reason=reason,
                    )
                )
                continue
            reason = await _close_after_failed_bind(
                gateway, session_key, f"could not bind worker session: {exc}"
            )
            if new_reservation:
                try:
                    _release_one(store, goal, item.item_id, now=stamp)
                except Exception as release_exc:  # noqa: BLE001 - keep budget fail-closed
                    reason += f"; reservation release failed: {release_exc}"
                else:
                    reserved -= 1
            records.append(_record(item.item_id, DISPATCH_FAILED, reason=reason))
            continue

        try:
            _mark_reservation(
                store,
                goal,
                item.item_id,
                DISPATCH_BOUND,
                now=stamp,
                session_key=session_key,
            )
        except Exception as exc:  # noqa: BLE001 - binding remains durable
            reservation_warning = f"reservation update failed: {exc}"
        else:
            reservation_warning = ""

        try:
            await _send_seed(gateway, contract, session_key)
        except Exception as exc:  # noqa: BLE001 - binding is durable recovery state
            reason = "; ".join(filter(None, (reservation_warning, str(exc))))
            records.append(
                _record(item.item_id, BOUND_UNSENT, session_key=session_key, reason=reason)
            )
        else:
            try:
                _mark_reservation(
                    store,
                    goal,
                    item.item_id,
                    DISPATCH_SEEDED,
                    now=stamp,
                    session_key=session_key,
                )
            except Exception as exc:  # noqa: BLE001 - delivery already idempotent
                reservation_warning = "; ".join(
                    filter(None, (reservation_warning, f"reservation update failed: {exc}"))
                )
            records.append(
                _record(
                    item.item_id,
                    DISPATCHED,
                    session_key=session_key,
                    reason=reservation_warning,
                )
            )

    return DispatchReport(goal.goal_id, reserved, tuple(records))


async def recover_worker_dispatches(
    store: GoalStore,
    goal: GoalRecord,
    items: Iterable[work_ledger.WorkItem],
    gateway: WorkerSessionGateway,
    *,
    now: str,
) -> DispatchReport:
    """Retry only durable bound-but-unseeded workers after an interrupted dispatch."""
    return await dispatch_workers(store, goal, items, gateway, now=now, recover=True)


__all__ = [
    "ALREADY_BOUND",
    "BOUND_UNSENT",
    "BUDGET_EXHAUSTED",
    "DISPATCHED",
    "DISPATCH_FAILED",
    "RECOVERED",
    "SKIPPED_TERMINAL",
    "DispatchRecord",
    "DispatchReport",
    "WorkerDispatchError",
    "WorkerSessionGateway",
    "dispatch_workers",
    "recover_worker_dispatches",
]
