"""Lifecycle-hook contracts for Goal Owner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.builtins.goal_owner import hooks
from kiro_crew.apps.builtins.goal_owner.store import GoalStore

pytestmark = pytest.mark.asyncio


class _Cron:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    def list_jobs(self) -> list[SimpleNamespace]:
        return list(self.jobs)

    async def add_job_if_absent_async(self, *, name: str, **kwargs):
        del kwargs
        for job in self.jobs:
            if job.name == name:
                return None
        job = SimpleNamespace(id=f"job-{len(self.jobs) + 1}", name=name, persistent_session=True)
        self.jobs.append(job)
        return job

    async def remove_job_async(self, job_id: str) -> bool:
        self.jobs[:] = [job for job in self.jobs if job.id != job_id]
        return True


def _ctx(tmp_path: Path, cron: _Cron) -> SimpleNamespace:
    data_dir = tmp_path / "data"
    return SimpleNamespace(
        data_dir=data_dir,
        storage=AppStorage("goal-owner", data_dir),
        cron=cron,
        health=SimpleNamespace(mark_degraded=lambda _message: None),
    )


async def test_startup_is_idempotent_and_shutdown_clears_runtime(tmp_path: Path) -> None:
    hooks._reset_for_tests()
    ctx = _ctx(tmp_path, _Cron())
    await hooks.on_startup(ctx)
    first = hooks.get_runtime()
    await hooks.on_startup(ctx)
    assert hooks.get_runtime() is first
    await hooks.on_shutdown(ctx)
    assert hooks.get_runtime() is None


async def test_startup_ignores_worker_gateway_injection(tmp_path: Path) -> None:
    hooks._reset_for_tests()
    ctx = _ctx(tmp_path, _Cron())
    ctx.worker_gateway = object()

    await hooks.on_startup(ctx)

    runtime = hooks.get_runtime()
    assert runtime is not None
    assert not hasattr(runtime, "worker_gateway")
    await hooks.on_shutdown(ctx)


async def test_startup_reconciles_existing_running_goal(tmp_path: Path) -> None:
    hooks._reset_for_tests()
    cron = _Cron()
    ctx = _ctx(tmp_path, cron)
    store = GoalStore(ctx.storage)
    record = store.create("Persist goal", "done", now="2026-09-11T00:00:00Z")

    await hooks.on_startup(ctx)

    assert len(cron.jobs) == 1
    saved = store.get(record.goal_id)
    assert saved is not None
    assert saved.owner_session.job_id == cron.jobs[0].id
    await hooks.on_shutdown(ctx)
