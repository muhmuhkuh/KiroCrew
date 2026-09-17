from __future__ import annotations

import json

import pytest

from kiro_crew import session_ledger, work_ledger
from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.builtins.goal_owner.cycle import (
    OUTCOME_BUDGET_EXHAUSTED,
    OUTCOME_GATEWAY_UNAVAILABLE,
    OUTCOME_OWNER_UNBOUND,
    OUTCOME_RECORDED,
    OUTCOME_TERMINAL,
    run_owner_cycle,
    run_owner_wake,
)
from kiro_crew.apps.builtins.goal_owner.dispatch import DISPATCHED
from kiro_crew.apps.builtins.goal_owner.domain import GoalRecord, GoalStatus
from kiro_crew.apps.builtins.goal_owner.store import GoalStore


def _store(tmp_path) -> GoalStore:
    return GoalStore(AppStorage("goal-owner", tmp_path / "app"))


def _goal(
    store: GoalStore,
    *,
    owner_key: str = "cron:job-cycle",
    goal_id: str = "goal_cycle",
    **kwargs,
):
    return store.create(
        "Ship reliable owner",
        ["tests pass"],
        goal_id=goal_id,
        owner_session={"session_key": owner_key, "job_id": "job-cycle"},
        **kwargs,
    )


def _planned_item(store: GoalStore, goal: GoalRecord) -> work_ledger.WorkItem:
    owner_key = goal.owner_session.session_key
    work_ledger.ensure_conductor(owner_key, goal=goal.goal)
    created = work_ledger.apply_conductor_action(
        owner_key,
        "create",
        title="tests pass",
        acceptance={"kind": "file", "path": "/does-not-exist", "exists": True},
    )
    item = created["item"]
    assert item is not None
    goal.task_refs = [item.item_id]
    store.save(goal)
    return item


class _WakeGateway:
    def __init__(self, store: GoalStore, goal: GoalRecord, item: work_ledger.WorkItem) -> None:
        self.store = store
        self.goal = goal
        self.item = item
        self.calls: list[tuple[str, str]] = []

    async def create_session(self, *, request_id: str, title: str, agent: str) -> str:
        live = self.store.get(self.goal.goal_id)
        assert live is not None and live.counters.cycles == 1
        self.calls.append(("create", request_id))
        assert title == "tests pass"
        assert agent == "kirocrew-worker"
        return "worker_wake"

    async def send(self, session_key: str, message: str, *, delivery_id: str) -> None:
        self.calls.append(("send", delivery_id))
        bound = work_ledger.read_work_item(self.goal.owner_session.session_key, self.item.item_id)
        assert bound is not None and bound.worker_session_key == session_key
        assert "Ship reliable owner" not in message

    async def close_session(self, session_key: str) -> None:
        self.calls.append(("close", session_key))


def test_cycle_observes_existing_ledgers_and_persists_one_projection(tmp_path, monkeypatch):
    ledger_home = tmp_path / "ledgers"
    monkeypatch.setattr(work_ledger, "data_home", lambda: ledger_home)
    monkeypatch.setattr(session_ledger, "data_home", lambda: ledger_home)

    store = _store(tmp_path)
    goal = _goal(store)
    owner_key = goal.owner_session.session_key
    session_ledger.record(
        owner_key,
        phase="working",
        next_step="wait for worker",
        event="owner wake",
        event_kind="progress",
    )
    work_ledger.ensure_conductor(owner_key, goal=goal.goal)
    created = work_ledger.apply_conductor_action(
        owner_key,
        "create",
        title="Run tests",
        acceptance={"kind": "file", "path": "/does-not-exist", "exists": True},
    )
    item = created["item"]
    assert item is not None
    goal.task_refs = [item.item_id]
    store.save(goal)
    events_before = work_ledger.read_events(owner_key, item.item_id)

    result = run_owner_cycle(store, goal.goal_id, now="2026-09-14T07:00:00Z")

    assert result.outcome == OUTCOME_RECORDED
    assert result.cycle == 1
    assert result.owner_phase == "working"
    assert result.projection is not None
    assert result.projection.pending == [item.item_id]
    assert result.next_action is not None
    assert result.next_action.kind == "verify"
    saved = store.get(goal.goal_id)
    assert saved is not None
    assert saved.counters.cycles == 1
    assert saved.assessment.gap == "Pending work still needs independent acceptance evidence."
    assert work_ledger.read_events(owner_key, item.item_id) == events_before
    json.dumps(result.to_dict())


def test_cycle_without_work_ledger_records_safe_initialize_action(tmp_path):
    store = _store(tmp_path)
    goal = _goal(store, owner_key="cron:missing-ledger")

    result = run_owner_cycle(store, goal.goal_id, now="2026-09-14T07:00:00Z")

    assert result.outcome == OUTCOME_RECORDED
    assert result.assessment is not None
    assert result.assessment.confidence == "low"
    assert result.next_action is not None
    assert result.next_action.kind == "initialize"
    saved = store.get(goal.goal_id)
    assert saved is not None
    assert saved.counters.cycles == 1


@pytest.mark.asyncio
async def test_wake_commits_observation_before_dispatch(tmp_path, monkeypatch):
    ledger_home = tmp_path / "ledgers"
    monkeypatch.setattr(work_ledger, "data_home", lambda: ledger_home)
    monkeypatch.setattr(session_ledger, "data_home", lambda: ledger_home)

    store = _store(tmp_path)
    goal = _goal(store)
    owner_key = goal.owner_session.session_key
    session_ledger.record(
        owner_key,
        phase="working",
        next_step="dispatch",
        event="owner wake",
        event_kind="progress",
    )
    item = _planned_item(store, goal)
    gateway = _WakeGateway(store, goal, item)

    result = await run_owner_wake(store, goal.goal_id, gateway, now="2026-09-14T07:00:00Z")

    assert result.outcome == OUTCOME_RECORDED
    assert result.cycle.cycle == 1
    assert result.dispatch is not None
    assert result.dispatch.records[0].outcome == DISPATCHED
    assert [call[0] for call in gateway.calls] == ["create", "send"]


@pytest.mark.asyncio
async def test_wake_records_observation_but_fails_closed_without_gateway(tmp_path, monkeypatch):
    ledger_home = tmp_path / "ledgers"
    monkeypatch.setattr(work_ledger, "data_home", lambda: ledger_home)
    monkeypatch.setattr(session_ledger, "data_home", lambda: ledger_home)

    store = _store(tmp_path)
    goal = _goal(store)
    owner_key = goal.owner_session.session_key
    session_ledger.record(
        owner_key,
        phase="working",
        next_step="dispatch",
        event="owner wake",
        event_kind="progress",
    )
    _planned_item(store, goal)

    result = await run_owner_wake(store, goal.goal_id, gateway=None, now="2026-09-14T07:00:00Z")

    assert result.outcome == OUTCOME_GATEWAY_UNAVAILABLE
    assert result.dispatch is None
    saved = store.get(goal.goal_id)
    assert saved is not None and saved.counters.cycles == 1


def test_cycle_refuses_unbound_terminal_and_exhausted_goals(tmp_path):
    store = _store(tmp_path)
    unbound = store.create("Unbound", "done", goal_id="goal_unbound")
    unbound_result = run_owner_cycle(store, unbound.goal_id)
    assert unbound_result.outcome == OUTCOME_OWNER_UNBOUND

    terminal = GoalRecord.new("Stopped", "done", goal_id="goal_terminal")
    terminal.transition(GoalStatus.FAILED, failure_reason="human stopped it")
    store.save(terminal)
    terminal_result = run_owner_cycle(store, terminal.goal_id)
    assert terminal_result.outcome == OUTCOME_TERMINAL

    limited = _goal(
        store,
        goal_id="goal_limited",
        budgets={"max_cycles": 1},
    )
    first = run_owner_cycle(store, limited.goal_id)
    second = run_owner_cycle(store, limited.goal_id)
    assert first.outcome == OUTCOME_RECORDED
    assert second.outcome == OUTCOME_BUDGET_EXHAUSTED
    saved = store.get(limited.goal_id)
    assert saved is not None
    assert saved.counters.cycles == 1
