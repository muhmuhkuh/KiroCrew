"""Crash-safe reconciliation between GoalRecords and owner cron jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .domain import _GOAL_ID_RE, GoalRecord, GoalStatus, OwnerSession
from .store import GoalStore

JOB_NAME_PREFIX = "goal-owner:"
DEFAULT_WAKE_INTERVAL_SECS = 300
DEFAULT_OWNER_AGENT = "goal-owner"


def goal_job_name(goal_id: str) -> str:
    """Return deterministic cron identity for one goal."""
    if not isinstance(goal_id, str) or not _GOAL_ID_RE.fullmatch(goal_id) or ".." in goal_id:
        raise ValueError("goal_id has unsafe shape")
    return f"{JOB_NAME_PREFIX}{goal_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class ReconcileReport:
    """Observable changes from one startup/recovery pass."""

    created: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class GoalScheduler:
    """Keep one persistent owner wake per runnable goal.

    Reconciliation is idempotent and safe to run after a process crash. Cron's
    add-if-absent transaction closes the create race; the deterministic name
    lets a later pass bind a job whose GoalRecord update was interrupted.
    """

    def __init__(
        self,
        store: GoalStore,
        cron: Any,
        *,
        every_secs: int = DEFAULT_WAKE_INTERVAL_SECS,
        agent: str = DEFAULT_OWNER_AGENT,
        message_factory: Callable[[GoalRecord], str] | None = None,
    ) -> None:
        if every_secs <= 0:
            raise ValueError("every_secs must be positive")
        self._store = store
        self._cron = cron
        self._every_secs = every_secs
        self._agent = agent
        self._message_factory = message_factory or self._default_message

    @staticmethod
    def _default_message(goal: GoalRecord) -> str:
        return (
            f"Run one bounded owner cycle for goal {goal.goal_id}. "
            "Read its persisted GoalRecord before acting."
        )

    def _jobs(self) -> list[Any]:
        """Read app-owned jobs through CronSDK's ownership boundary."""
        return list(self._cron.list_jobs())

    def _named_jobs(self, name: str) -> list[Any]:
        return [job for job in self._jobs() if getattr(job, "name", "") == name]

    async def _remove(self, job: Any, report: ReconcileReport) -> bool:
        job_id = getattr(job, "id", "")
        if not job_id:
            report.errors.append("cron job has no id")
            return False
        try:
            await self._cron.remove_job_async(job_id)
        except PermissionError:
            # Another reconciler already removed it. Desired state holds.
            return True
        except Exception as exc:  # noqa: BLE001 - retain reference for retry
            report.errors.append(f"remove {job_id}: {exc}")
            return False
        report.removed.append(job_id)
        return True

    async def _ensure(self, goal: GoalRecord, report: ReconcileReport) -> None:
        name = goal_job_name(goal.goal_id)
        jobs = self._named_jobs(name)
        job = jobs[0] if jobs else None

        # A prior implementation or hand-edited record may have made the owner
        # wake stateless. Replace it before binding a session reference.
        if job is not None and not getattr(job, "persistent_session", True):
            if not await self._remove(job, report):
                return
            jobs = []
            job = None

        if job is None:
            try:
                job = await self._cron.add_job_if_absent_async(
                    name=name,
                    message=self._message_factory(goal),
                    every_secs=self._every_secs,
                    agent=self._agent,
                    persistent_session=True,
                    enabled=True,
                )
            except Exception as exc:  # noqa: BLE001 - next pass retries
                report.errors.append(f"create {goal.goal_id}: {exc}")
                return
            if job is not None:
                report.created.append(getattr(job, "id", name))
            else:
                # The atomic registrar found a job created by another process.
                jobs = self._named_jobs(name)
                job = jobs[0] if jobs else None
                if job is None:
                    report.errors.append(f"adopt {goal.goal_id}: cron job not visible")
                    return

        # Remove pre-existing duplicates, but keep first deterministic winner.
        for duplicate in jobs[1:]:
            await self._remove(duplicate, report)

        job_id = getattr(job, "id", "")
        if not job_id:
            report.errors.append(f"bind {goal.goal_id}: cron job has no id")
            return
        expected = OwnerSession(
            session_key=f"cron:{job_id}",
            job_id=job_id,
            agent=self._agent,
            attached_at=goal.owner_session.attached_at or _now_iso(),
        )
        if goal.owner_session != expected:
            goal.owner_session = expected
            try:
                self._store.save(goal)
            except Exception as exc:  # noqa: BLE001 - job remains recoverable by name
                report.errors.append(f"bind {goal.goal_id}: {exc}")
                return
            report.adopted.append(goal.goal_id)

    async def _retire(self, goal: GoalRecord, report: ReconcileReport) -> None:
        jobs = self._named_jobs(goal_job_name(goal.goal_id))
        all_removed = True
        for job in jobs:
            all_removed = await self._remove(job, report) and all_removed
        if not all_removed:
            return
        if goal.owner_session != OwnerSession():
            goal.owner_session = OwnerSession()
            try:
                self._store.save(goal)
            except Exception as exc:  # noqa: BLE001 - retry clear next pass
                report.errors.append(f"clear {goal.goal_id}: {exc}")

    async def reconcile(self) -> ReconcileReport:
        """Reconcile runnable goals and recover interrupted owner bindings."""
        snapshot = self._store.snapshot()
        report = ReconcileReport(errors=list(snapshot.errors))
        known_jobs: set[str] = set()

        for goal in snapshot.goals:
            runnable = goal.status is GoalStatus.RUNNING and not goal.validate()
            if runnable:
                known_jobs.add(goal_job_name(goal.goal_id))
                await self._ensure(goal, report)
            else:
                await self._retire(goal, report)

        # Empty storage is a valid state; an uncertain storage read is not proof
        # that old jobs are orphaned. Prune only with a complete snapshot.
        if snapshot.complete:
            try:
                for job in self._jobs():
                    name = getattr(job, "name", "")
                    if name.startswith(JOB_NAME_PREFIX) and name not in known_jobs:
                        await self._remove(job, report)
            except Exception as exc:  # noqa: BLE001 - leave jobs for next pass
                report.errors.append(f"orphan scan: {exc}")

        return report


__all__ = [
    "DEFAULT_OWNER_AGENT",
    "DEFAULT_WAKE_INTERVAL_SECS",
    "GoalScheduler",
    "JOB_NAME_PREFIX",
    "ReconcileReport",
    "goal_job_name",
]
