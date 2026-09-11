from __future__ import annotations

import asyncio
from types import SimpleNamespace

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.builtins.goal_owner.domain import GoalRecord, GoalStatus
from kiro_crew.apps.builtins.goal_owner.scheduler import GoalScheduler, goal_job_name
from kiro_crew.apps.builtins.goal_owner.store import GoalStore


class _Cron:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []
        self.created = 0

    def list_jobs(self) -> list[SimpleNamespace]:
        return list(self.jobs)

    async def add_job_if_absent_async(self, **kwargs) -> SimpleNamespace | None:
        if any(job.name == kwargs["name"] for job in self.jobs):
            return None
        self.created += 1
        job = SimpleNamespace(
            id=f"job-{self.created}",
            name=kwargs["name"],
            persistent_session=kwargs["persistent_session"],
        )
        self.jobs.append(job)
        return job

    async def remove_job_async(self, job_id: str) -> bool:
        self.jobs = [job for job in self.jobs if job.id != job_id]
        return True


def _goal(goal_id: str = "goal_test") -> GoalRecord:
    return GoalRecord.new(
        "Ship reliable owner",
        ["tests pass"],
        goal_id=goal_id,
        now="2026-09-11T08:00:00Z",
    )


def test_store_round_trip_and_bad_record_are_not_runnable(tmp_path):
    storage = AppStorage("goal-owner", tmp_path)
    store = GoalStore(storage)
    goal = _goal()

    store.save(goal)
    assert store.get(goal.goal_id) == goal

    storage.set(
        "goal-goal-bad",
        {
            "schema_version": 1,
            "goal_id": "goal-bad",
            "goal": "",
            "definition_of_done": ["done"],
            "status": "running",
        },
    )
    bad = store.get("goal-bad")
    assert bad is not None
    assert bad.status is GoalStatus.PAUSED
    assert store.snapshot().complete is False


def test_reconcile_is_idempotent_and_binds_stable_owner_session(tmp_path):
    store = GoalStore(AppStorage("goal-owner", tmp_path))
    store.create("Ship reliable owner", ["tests pass"], goal_id="goal_test")
    cron = _Cron()
    scheduler = GoalScheduler(store, cron, every_secs=60)

    first = asyncio.run(scheduler.reconcile())
    second = asyncio.run(scheduler.reconcile())

    assert first.created == ["job-1"]
    assert first.adopted == ["goal_test"]
    assert second.created == []
    assert second.adopted == []
    assert len(cron.jobs) == 1
    bound = store.get("goal_test")
    assert bound is not None
    assert bound.owner_session.job_id == "job-1"
    assert bound.owner_session.session_key == "cron:job-1"


def test_reconcile_adopts_job_after_crash_between_cron_and_store_write(tmp_path):
    store = GoalStore(AppStorage("goal-owner", tmp_path))
    store.create("Recover owner", ["tests pass"], goal_id="goal_recover")
    cron = _Cron()
    cron.jobs.append(
        SimpleNamespace(
            id="job-recovered",
            name=goal_job_name("goal_recover"),
            persistent_session=True,
        )
    )

    report = asyncio.run(GoalScheduler(store, cron).reconcile())

    assert report.created == []
    assert report.adopted == ["goal_recover"]
    recovered = store.get("goal_recover")
    assert recovered is not None
    assert recovered.owner_session.job_id == "job-recovered"


def test_reconcile_removes_wake_when_goal_is_paused(tmp_path):
    store = GoalStore(AppStorage("goal-owner", tmp_path))
    store.create("Pause owner", ["tests pass"], goal_id="goal_pause")
    cron = _Cron()
    scheduler = GoalScheduler(store, cron)
    asyncio.run(scheduler.reconcile())

    paused_goal = store.get("goal_pause")
    assert paused_goal is not None
    paused_goal.transition(GoalStatus.PAUSED, now="2026-09-11T08:01:00Z")
    store.save(paused_goal)
    report = asyncio.run(scheduler.reconcile())

    assert report.removed == ["job-1"]
    assert cron.jobs == []
    paused = store.get("goal_pause")
    assert paused is not None
    assert paused.owner_session.job_id == ""


def test_uncertain_store_read_does_not_prune_existing_owner_job(tmp_path):
    storage = AppStorage("goal-owner", tmp_path)
    store = GoalStore(storage)
    storage.set(
        "goal-corrupt",
        {"schema_version": 1, "goal_id": "goal-corrupt", "status": "running"},
    )
    cron = _Cron()
    cron.jobs.append(
        SimpleNamespace(
            id="job-orphan",
            name=goal_job_name("goal-orphan"),
            persistent_session=True,
        )
    )

    report = asyncio.run(GoalScheduler(store, cron).reconcile())

    assert report.errors
    assert [job.id for job in cron.jobs] == ["job-orphan"]
