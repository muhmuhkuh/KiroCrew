from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import work_ledger
from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.builtins.goal_owner.dispatch import (
    BOUND_UNSENT,
    DISPATCHED,
    DISPATCH_FAILED,
    RECOVERED,
    WorkerDispatchError,
    dispatch_workers,
    recover_worker_dispatches,
)
from kiro_crew.apps.builtins.goal_owner.domain import GoalRecord, GoalStatus
from kiro_crew.apps.builtins.goal_owner.store import GoalStore

pytestmark = pytest.mark.asyncio


class _Crash(BaseException):
    pass


class _Gateway:
    def __init__(self, owner_key: str) -> None:
        self.owner_key = owner_key
        self.calls: list[tuple[str, str, str]] = []
        self.created: dict[str, str] = {}
        self.fail_create = False
        self.crash_create = False
        self.fail_send = False
        self.fail_close = False

    async def create_session(self, *, request_id: str, title: str, agent: str) -> str:
        self.calls.append(("create", request_id, f"{title}|{agent}"))
        if self.crash_create:
            raise _Crash("process interrupted")
        if self.fail_create:
            raise RuntimeError("create refused")
        return self.created.setdefault(request_id, f"worker_{request_id.rsplit(':', 1)[-1]}")

    async def send(self, session_key: str, message: str, *, delivery_id: str) -> None:
        self.calls.append(("send", session_key, delivery_id))
        item_id = delivery_id.rsplit(":", 1)[-1]
        bound = work_ledger.read_work_item(self.owner_key, item_id)
        assert bound is not None and bound.worker_session_key == session_key
        assert "Ship reliable owner" not in message
        if self.fail_send:
            raise RuntimeError("send interrupted")

    async def close_session(self, session_key: str) -> None:
        self.calls.append(("close", session_key, ""))
        if self.fail_close:
            raise RuntimeError("close refused")


def _setup(tmp_path: Path, monkeypatch, dod: list[str], *, budget: int = 8):
    monkeypatch.setattr(work_ledger, "data_home", lambda: tmp_path / "ledgers")
    store = GoalStore(AppStorage("goal-owner", tmp_path / "app"))
    goal = GoalRecord.new(
        "Ship reliable owner",
        dod,
        goal_id="goal_dispatch",
        budgets={"max_worker_dispatches": budget},
        owner_session={"session_key": "cron:owner", "job_id": "job-owner"},
        now="2026-09-14T07:00:00Z",
    )
    owner_key = goal.owner_session.session_key
    work_ledger.ensure_conductor(owner_key, goal=goal.goal)
    items: list[work_ledger.WorkItem] = []
    for title in dod:
        created = work_ledger.apply_conductor_action(
            owner_key,
            "create",
            title=title,
            acceptance={"kind": "file", "path": f"/evidence/{title}"},
        )
        item = created["item"]
        assert item is not None
        items.append(item)
    goal.task_refs = [item.item_id for item in items]
    store.save(goal)
    return store, goal, items


async def test_dispatch_reserves_binds_before_seed_and_isolates_worker(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["Run tests"])
    gateway = _Gateway(goal.owner_session.session_key)

    report = await dispatch_workers(
        store,
        goal,
        (item for item in items),
        gateway,
        now="2026-09-14T07:01:00Z",
    )

    live = work_ledger.read_work_item(goal.owner_session.session_key, items[0].item_id)
    saved = store.get(goal.goal_id)
    assert report.records[0].outcome == DISPATCHED
    assert [call[0] for call in gateway.calls] == ["create", "send"]
    assert live is not None and live.worker_session_key == report.records[0].session_key
    assert saved is not None and saved.counters.worker_dispatches == 1
    assert goal.counters.worker_dispatches == 1


async def test_crash_after_reservation_reuses_request_without_budget_overrun(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["Run tests"], budget=1)
    gateway = _Gateway(goal.owner_session.session_key)
    gateway.crash_create = True

    with pytest.raises(_Crash):
        await dispatch_workers(store, goal, items, gateway, now="2026-09-14T07:01:00Z")

    crashed = store.get(goal.goal_id)
    assert crashed is not None
    assert crashed.counters.worker_dispatches == 1
    assert crashed.dispatch_reservations[0].state == "reserved"
    request_id = crashed.dispatch_reservations[0].request_id

    gateway.crash_create = False
    recovered = await dispatch_workers(
        store,
        goal,
        work_ledger.list_work_items(goal.owner_session.session_key),
        gateway,
        now="2026-09-14T07:02:00Z",
    )

    assert recovered.records[0].outcome == DISPATCHED
    assert [call[0] for call in gateway.calls] == ["create", "create", "send"]
    assert gateway.calls[0][1] == gateway.calls[1][1] == request_id
    saved = store.get(goal.goal_id)
    assert saved is not None and saved.counters.worker_dispatches == 1
    assert saved.dispatch_reservations[0].state == "seeded"


async def test_send_failure_leaves_durable_binding_for_recovery(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["Run tests"])
    gateway = _Gateway(goal.owner_session.session_key)
    gateway.fail_send = True

    first = await dispatch_workers(store, goal, items, gateway, now="2026-09-14T07:01:00Z")
    assert first.records[0].outcome == BOUND_UNSENT
    assert store.get(goal.goal_id).counters.worker_dispatches == 1
    bound = work_ledger.list_work_items(goal.owner_session.session_key)
    assert bound[0].worker_session_key

    gateway.fail_send = False
    second = await recover_worker_dispatches(
        store, goal, bound, gateway, now="2026-09-14T07:02:00Z"
    )

    assert second.records[0].outcome == RECOVERED
    assert [call[0] for call in gateway.calls] == ["create", "send", "send"]
    assert gateway.calls[1][2] == gateway.calls[2][2]


async def test_create_failure_releases_reservation_and_allows_retry(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["Run tests"], budget=1)
    gateway = _Gateway(goal.owner_session.session_key)
    gateway.fail_create = True

    first = await dispatch_workers(store, goal, items, gateway, now="2026-09-14T07:01:00Z")
    assert first.records[0].outcome == DISPATCH_FAILED
    assert store.get(goal.goal_id).counters.worker_dispatches == 0

    gateway.fail_create = False
    second = await dispatch_workers(
        store,
        goal,
        work_ledger.list_work_items(goal.owner_session.session_key),
        gateway,
        now="2026-09-14T07:02:00Z",
    )
    assert second.records[0].outcome == DISPATCHED
    assert store.get(goal.goal_id).counters.worker_dispatches == 1


async def test_bind_failure_closes_session_and_releases_reservation(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["Run tests"], budget=1)
    gateway = _Gateway(goal.owner_session.session_key)
    original = work_ledger.apply_conductor_action

    def refuse_bind(slot_key, action, **kwargs):
        if action == "bind":
            raise RuntimeError("bind refused")
        return original(slot_key, action, **kwargs)

    monkeypatch.setattr(work_ledger, "apply_conductor_action", refuse_bind)
    report = await dispatch_workers(store, goal, items, gateway, now="2026-09-14T07:01:00Z")

    assert report.records[0].outcome == DISPATCH_FAILED
    assert [call[0] for call in gateway.calls] == ["create", "close"]
    assert store.get(goal.goal_id).counters.worker_dispatches == 0


async def test_budget_overrun_fails_before_session_side_effects(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["one", "two"], budget=1)
    gateway = _Gateway(goal.owner_session.session_key)

    with pytest.raises(WorkerDispatchError, match="budget") as raised:
        await dispatch_workers(store, goal, items, gateway, now="2026-09-14T07:01:00Z")

    assert raised.value.code == "dispatch_budget_exhausted"
    assert gateway.calls == []
    assert store.get(goal.goal_id).counters.worker_dispatches == 0


async def test_non_runnable_goal_refuses_without_side_effects(tmp_path, monkeypatch):
    store, goal, items = _setup(tmp_path, monkeypatch, ["Run tests"])
    goal.transition(GoalStatus.PAUSED)
    store.save(goal)
    gateway = _Gateway(goal.owner_session.session_key)

    with pytest.raises(WorkerDispatchError, match="not dispatchable"):
        await dispatch_workers(store, goal, items, gateway, now="2026-09-14T07:01:00Z")

    assert gateway.calls == []
