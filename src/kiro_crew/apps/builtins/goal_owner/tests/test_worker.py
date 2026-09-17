from __future__ import annotations

import pytest

from kiro_crew import work_ledger
from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.builtins.goal_owner.domain import GoalRecord, GoalStatus
from kiro_crew.apps.builtins.goal_owner.store import GoalStore
from kiro_crew.apps.builtins.goal_owner.worker import (
    OUTCOME_COMPLETED,
    OUTCOME_RECORDED,
    AcceptanceResult,
    WorkerContractError,
    apply_acceptance,
    build_done_batch,
    derive_contracts,
    worker_seed,
)


def _plain_item(item_id: str, title: str, *, status: str | None = None) -> work_ledger.WorkItem:
    return work_ledger.WorkItem(
        item_id=item_id,
        title=title,
        acceptance={"kind": "file", "path": f"/evidence/{item_id}", "exists": True},
        status=status,
    )


def _goal(dod: list[str], *, budget: int = 8) -> GoalRecord:
    goal = GoalRecord.new(
        "Ship reliable owner",
        dod,
        goal_id="goal_worker",
        budgets={"max_worker_dispatches": budget},
        owner_session={"session_key": "cron:owner", "job_id": "job-owner"},
        now="2026-09-14T07:00:00Z",
    )
    return goal


def _ledger_goal(tmp_path, monkeypatch, dod: list[str]):
    ledger_home = tmp_path / "ledgers"
    monkeypatch.setattr(work_ledger, "data_home", lambda: ledger_home)
    store = GoalStore(AppStorage("goal-owner", tmp_path / "app"))
    goal = _goal(dod)
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
        bound = work_ledger.apply_conductor_action(
            owner_key,
            "bind",
            item_id=item.item_id,
            worker_session_key=f"worker_{item.item_id}",
        )
        items.append(bound["item"])
    goal.task_refs = [item.item_id for item in items]
    store.save(goal)
    return store, goal, items


def _done(owner_key: str, item: work_ledger.WorkItem, *, pr: int | None = None) -> None:
    work_ledger.apply_worker_report(
        owner_key,
        item.item_id,
        status="done",
        summary="worker finished",
        artifacts={"log": "ok"},
        pr=pr,
    )


def test_contract_has_fixed_agent_and_seed_hides_goal_and_session():
    goal = _goal(["Run tests"])
    item = _plain_item("item_one", "Run tests")
    goal.task_refs = [item.item_id]

    contract = derive_contracts(goal, [item])[0]
    seed = worker_seed(contract)

    assert contract.agent == "kirocrew-worker"
    assert contract.worker_session_key == ""
    assert "Run tests" in seed
    assert "acceptance" in seed
    assert goal.goal not in seed
    assert goal.owner_session.session_key not in seed
    assert contract.goal_id not in seed


def test_contract_rejects_missing_duplicate_and_misassigned_refs():
    goal = _goal(["one", "two"])
    items = [_plain_item("item_one", "one"), _plain_item("item_two", "two")]

    goal.task_refs = ["item_one"]
    with pytest.raises(WorkerContractError, match="exactly one"):
        derive_contracts(goal, items)

    goal.task_refs = ["item_one", "item_one"]
    with pytest.raises(WorkerContractError, match="unique"):
        derive_contracts(goal, items)

    goal.task_refs = ["item_one", "missing"]
    with pytest.raises(WorkerContractError, match="missing"):
        derive_contracts(goal, items)

    goal.task_refs = ["item_one", "item_two"]
    items[1].title = "wrong criterion"
    with pytest.raises(WorkerContractError, match="not assigned"):
        derive_contracts(goal, items)


def test_contract_refuses_dispatch_budget_overrun():
    goal = _goal(["one", "two"], budget=1)
    goal.task_refs = ["item_one", "item_two"]

    with pytest.raises(WorkerContractError, match="budget"):
        derive_contracts(goal, [_plain_item("item_one", "one"), _plain_item("item_two", "two")])


def test_done_batch_filters_worker_status_and_never_promotes_worker_pr():
    done = _plain_item("done", "done", status="done")
    done.pr = 123
    progress = _plain_item("progress", "progress", status="progress")
    blocked = _plain_item("blocked", "blocked", status="blocked")

    batch = build_done_batch([done, progress, blocked])

    assert [item["id"] for item in batch["items"]] == ["done"]
    assert batch["items"][0]["accept"] == done.acceptance
    assert "pr" not in batch["items"][0]["accept"]


def test_worker_done_claim_does_not_close_item(tmp_path, monkeypatch):
    store, goal, items = _ledger_goal(tmp_path, monkeypatch, ["Run tests"])
    _done(goal.owner_session.session_key, items[0], pr=123)

    live = work_ledger.read_work_item(goal.owner_session.session_key, items[0].item_id)
    assert live is not None
    assert live.status == "done"
    assert live.state == "open"
    assert live.verdict is None
    assert store.get(goal.goal_id).status is GoalStatus.RUNNING


def test_pass_is_independently_accepted_and_completes_goal(tmp_path, monkeypatch):
    store, goal, items = _ledger_goal(tmp_path, monkeypatch, ["Run tests"])
    _done(goal.owner_session.session_key, items[0])

    report = apply_acceptance(
        store,
        goal,
        work_ledger.list_work_items(goal.owner_session.session_key),
        [AcceptanceResult(items[0].item_id, "pass", "exit 0", "2026-09-14T07:01:00Z")],
        now="2026-09-14T07:02:00Z",
    )

    live = work_ledger.read_work_item(goal.owner_session.session_key, items[0].item_id)
    saved = store.get(goal.goal_id)
    assert report.outcome == OUTCOME_COMPLETED
    assert report.goal_completed is True
    assert live is not None and live.state == "accepted" and live.verdict == "pass"
    assert saved is not None and saved.status is GoalStatus.COMPLETED
    assert saved.has_valid_completion_evidence


@pytest.mark.parametrize("verdict", ["fail", "pending", "refused", "error"])
def test_non_pass_verdict_never_closes_item(tmp_path, monkeypatch, verdict):
    store, goal, items = _ledger_goal(tmp_path, monkeypatch, ["Run tests"])
    _done(goal.owner_session.session_key, items[0])

    report = apply_acceptance(
        store,
        goal,
        work_ledger.list_work_items(goal.owner_session.session_key),
        [AcceptanceResult(items[0].item_id, verdict, "evaluator evidence", "2026-09-14T07:01:00Z")],
        now="2026-09-14T07:02:00Z",
    )

    live = work_ledger.read_work_item(goal.owner_session.session_key, items[0].item_id)
    saved = store.get(goal.goal_id)
    assert report.outcome == OUTCOME_RECORDED
    assert live is not None and live.state == "open" and live.verdict == verdict
    assert saved is not None and saved.status is GoalStatus.RUNNING
    if verdict == "fail":
        assert live.fails == 1


def test_missing_duplicate_and_unknown_evaluator_ids_write_nothing(tmp_path, monkeypatch):
    store, goal, items = _ledger_goal(tmp_path, monkeypatch, ["one", "two"])
    owner_key = goal.owner_session.session_key
    for item in items:
        _done(owner_key, item)
    listed = work_ledger.list_work_items(owner_key)
    before = [(item.item_id, item.state, item.verdict, item.fails) for item in listed]

    with pytest.raises(WorkerContractError, match="exactly cover"):
        apply_acceptance(
            store,
            goal,
            listed,
            [AcceptanceResult(items[0].item_id, "pass", "ok", "2026-09-14T07:01:00Z")],
            now="2026-09-14T07:02:00Z",
        )
    assert [
        (item.item_id, item.state, item.verdict, item.fails)
        for item in work_ledger.list_work_items(owner_key)
    ] == before

    all_results = [
        AcceptanceResult(item.item_id, "pending", "waiting", "2026-09-14T07:01:00Z")
        for item in items
    ]
    with pytest.raises(WorkerContractError, match="duplicate"):
        apply_acceptance(
            store, goal, listed, all_results + [all_results[0]], now="2026-09-14T07:02:00Z"
        )

    with pytest.raises(WorkerContractError, match="exactly cover"):
        apply_acceptance(
            store,
            goal,
            listed,
            all_results + [AcceptanceResult("unknown", "pending", "x", "2026-09-14T07:01:00Z")],
            now="2026-09-14T07:02:00Z",
        )


def test_partial_evidence_keeps_goal_running_then_completes(tmp_path, monkeypatch):
    store, goal, items = _ledger_goal(tmp_path, monkeypatch, ["one", "two"])
    owner_key = goal.owner_session.session_key
    _done(owner_key, items[0])

    first = apply_acceptance(
        store,
        goal,
        work_ledger.list_work_items(owner_key),
        [AcceptanceResult(items[0].item_id, "pass", "one passed", "2026-09-14T07:01:00Z")],
        now="2026-09-14T07:02:00Z",
    )
    saved = store.get(goal.goal_id)
    assert first.goal_completed is False
    assert saved is not None and saved.status is GoalStatus.RUNNING
    assert saved.completion_evidence is not None
    assert not saved.has_valid_completion_evidence

    _done(owner_key, items[1])
    second = apply_acceptance(
        store,
        goal,
        work_ledger.list_work_items(owner_key),
        [AcceptanceResult(items[1].item_id, "pass", "two passed", "2026-09-14T07:03:00Z")],
        now="2026-09-14T07:04:00Z",
    )
    saved = store.get(goal.goal_id)
    assert second.goal_completed is True
    assert saved is not None and saved.status is GoalStatus.COMPLETED


def test_repeating_same_acceptance_is_idempotent(tmp_path, monkeypatch):
    store, goal, items = _ledger_goal(tmp_path, monkeypatch, ["Run tests"])
    owner_key = goal.owner_session.session_key
    _done(owner_key, items[0])
    result = AcceptanceResult(items[0].item_id, "pass", "exit 0", "2026-09-14T07:01:00Z")

    apply_acceptance(
        store, goal, work_ledger.list_work_items(owner_key), [result], now="2026-09-14T07:02:00Z"
    )
    events_before = work_ledger.read_events(owner_key, items[0].item_id)
    second = apply_acceptance(
        store,
        goal,
        work_ledger.list_work_items(owner_key),
        [result],
        now="2026-09-14T07:02:00Z",
    )

    assert second.outcome == OUTCOME_COMPLETED
    assert second.changed is False
    assert work_ledger.read_events(owner_key, items[0].item_id) == events_before
