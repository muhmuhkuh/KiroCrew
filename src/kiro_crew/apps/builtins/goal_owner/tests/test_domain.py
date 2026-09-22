from __future__ import annotations

import json

import pytest

from kiro_crew.apps.builtins.goal_owner.domain import (
    COMPLETION_PASS,
    CompletionCheck,
    CompletionEvidence,
    GoalRecord,
    GoalStatus,
    InvalidGoalRecord,
    InvalidGoalTransition,
    project_tasks,
)


def _goal() -> GoalRecord:
    return GoalRecord.new(
        "Ship reliable owner",
        ["tests pass", "review is complete"],
        goal_id="goal_test",
        now="2026-09-10T12:00:00Z",
    )


def _evidence() -> CompletionEvidence:
    return CompletionEvidence(
        checks=[
            CompletionCheck("tests pass", COMPLETION_PASS, "pytest", "2026-09-10T12:01:00Z"),
            CompletionCheck(
                "review is complete", COMPLETION_PASS, "review", "2026-09-10T12:01:00Z"
            ),
        ],
        verified_at="2026-09-10T12:01:00Z",
        evaluator="accept_eval",
    )


def test_new_record_has_required_shape_and_round_trips_as_json():
    goal = _goal()
    goal.plan = ["inspect", "delegate", "verify"]
    goal.task_refs = ["it_00000001"]
    goal.record_cycle(now="2026-09-10T12:00:01Z")
    goal.record_decision(
        "delegate",
        "worker has bounded acceptance",
        task_refs=goal.task_refs,
        now="2026-09-10T12:00:02Z",
    )
    goal.record_replan("worker_blocked", "external dependency", now="2026-09-10T12:00:03Z")

    encoded = json.dumps(goal.to_dict())
    restored = GoalRecord.from_dict(json.loads(encoded))

    assert restored == goal
    assert restored.validate() == []
    assert restored.counters.cycles == 1
    assert restored.decisions[0].task_refs == ["it_00000001"]


def test_status_machine_blocks_invalid_edges_and_terminal_goals():
    goal = _goal()
    goal.transition(GoalStatus.WAITING, now="2026-09-10T12:01:00Z")
    goal.transition(GoalStatus.RUNNING, now="2026-09-10T12:02:00Z")
    goal.transition(
        GoalStatus.BLOCKED,
        blocker={"kind": "approval", "summary": "needs human approval"},
        signal_fingerprint="approval-request-1",
        now="2026-09-10T12:03:00Z",
    )

    assert goal.status is GoalStatus.BLOCKED
    assert not goal.can_retry()
    with pytest.raises(InvalidGoalTransition) as exc:
        goal.transition(GoalStatus.RUNNING)
    assert exc.value.code == "new_signal_required"

    goal.transition(
        GoalStatus.RUNNING,
        signal_fingerprint="approval-answer-1",
        now="2026-09-10T12:04:00Z",
    )
    goal.transition(
        GoalStatus.COMPLETED, completion_evidence=_evidence(), now="2026-09-10T12:05:00Z"
    )

    assert goal.is_terminal
    assert not goal.may_run
    with pytest.raises(InvalidGoalTransition) as exc:
        goal.transition(GoalStatus.RUNNING)
    assert exc.value.code == "terminal"


def test_blocked_goal_rejects_repeated_signal():
    goal = _goal()
    goal.transition(
        GoalStatus.BLOCKED,
        blocker={"fingerprint": "blocker-1", "summary": "waiting"},
        now="2026-09-10T12:01:00Z",
    )
    with pytest.raises(InvalidGoalTransition) as exc:
        goal.transition(GoalStatus.RUNNING, signal_fingerprint="blocker-1")
    assert exc.value.code == "duplicate_signal"


def test_completed_requires_evidence_for_every_dod_item():
    goal = _goal()
    with pytest.raises(InvalidGoalTransition) as exc:
        goal.transition(GoalStatus.COMPLETED)
    assert exc.value.code == "dod_evidence_required"

    partial = CompletionEvidence(
        checks=[CompletionCheck("tests pass", COMPLETION_PASS)],
        verified_at="2026-09-10T12:01:00Z",
    )
    with pytest.raises(InvalidGoalTransition):
        goal.transition(GoalStatus.COMPLETED, completion_evidence=partial)

    assert goal.status is GoalStatus.RUNNING
    goal.transition(GoalStatus.COMPLETED, completion_evidence=_evidence())
    assert goal.has_valid_completion_evidence


def test_work_ledger_entries_are_projected_without_copying_worker_state():
    goal = _goal()
    goal.task_refs = ["it_done", "it_open", "it_blocked", "it_missing"]
    projection = project_tasks(
        goal,
        [
            {"item_id": "it_done", "state": "accepted", "status": "done"},
            {"item_id": "it_open", "state": "open", "status": "done"},
            {"item_id": "it_blocked", "state": "open", "status": "question"},
            {"item_id": "it_discovered", "state": "open", "status": "progress"},
        ],
    )

    assert projection.completed == ["it_done"]
    assert projection.pending == ["it_open", "it_missing"]
    assert projection.blocked == ["it_blocked"]
    assert projection.discovered == ["it_discovered"]


def test_malformed_and_newer_records_read_as_safe_paused_defaults():
    assert GoalRecord.from_dict(["not", "a", "record"]).status is GoalStatus.PAUSED
    assert (
        GoalRecord.from_dict({"schema_version": 99, "status": "completed"}).status
        is GoalStatus.PAUSED
    )

    malformed_completed = {
        "schema_version": 1,
        "goal_id": "goal_old",
        "goal": "old goal",
        "definition_of_done": ["one"],
        "status": "COMPLETED",
    }
    restored = GoalRecord.from_dict(malformed_completed)
    assert restored.status is GoalStatus.PAUSED
    assert not restored.has_valid_completion_evidence


def test_legacy_field_names_migrate_without_raising():
    restored = GoalRecord.from_dict(
        {
            "schema": 0,
            "goal_id": "goal_legacy",
            "objective": "legacy objective",
            "dod": "legacy check",
            "status": "active",
            "next": "inspect ledger",
            "owner_session_key": "cron:owner",
            "created_at": "2026-09-10T12:00:00Z",
        }
    )

    assert restored.status is GoalStatus.RUNNING
    assert restored.goal == "legacy objective"
    assert restored.definition_of_done == ["legacy check"]
    assert restored.next_action.summary == "inspect ledger"
    assert restored.owner_session.session_key == "cron:owner"
    assert restored.timestamps.created_at == "2026-09-10T12:00:00Z"


def test_new_rejects_empty_goal_or_definition_of_done():
    with pytest.raises(InvalidGoalRecord):
        GoalRecord.new("", ["done"])
    with pytest.raises(InvalidGoalRecord):
        GoalRecord.new("goal", [])
