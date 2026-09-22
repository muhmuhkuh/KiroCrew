"""Durable GoalRecord storage for the goal-owner app.

The store owns only GoalRecord lifecycle data. Worker reports stay in the
existing work ledger and owner recovery state stays in the session ledger.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.app_storage import AppStorage

from .domain import _GOAL_ID_RE, GoalBudgets, GoalRecord, OwnerSession

_GOAL_KEY_PREFIX = "goal-"


class GoalStoreError(ValueError):
    """Raised when a goal cannot be safely persisted."""


def _goal_key(goal_id: str) -> str:
    """Return a path-safe AppStorage key for *goal_id*."""
    if not isinstance(goal_id, str) or not _GOAL_ID_RE.fullmatch(goal_id) or ".." in goal_id:
        raise GoalStoreError("goal_id has unsafe shape")
    return f"{_GOAL_KEY_PREFIX}{goal_id}"


@dataclass(frozen=True)
class GoalStoreSnapshot:
    """Goals read plus whether the complete key set was trustworthy."""

    goals: tuple[GoalRecord, ...] = ()
    complete: bool = True
    errors: tuple[str, ...] = ()


class GoalStore:
    """Persist and read bounded GoalRecords through app-scoped storage."""

    def __init__(self, storage: AppStorage) -> None:
        self._storage = storage

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> GoalStore:
        """Build a store for the builtin app's data directory."""
        return cls(AppStorage("goal-owner", data_dir))

    def get(self, goal_id: str) -> GoalRecord | None:
        """Read one goal; malformed content degrades to a paused record."""
        raw = self._storage.get(_goal_key(goal_id))
        if raw is None:
            return None
        if not isinstance(raw, dict):
            return GoalRecord.safe_default()
        record = GoalRecord.from_dict(raw)
        # A key and its payload are one identity. Never return data stored under
        # a different id, and never let a tampered record become runnable.
        if record.goal_id != goal_id:
            return GoalRecord.safe_default()
        return record

    def save(self, record: GoalRecord) -> GoalRecord:
        """Validate then atomically persist one GoalRecord."""
        if not isinstance(record, GoalRecord):
            raise GoalStoreError("record must be a GoalRecord")
        errors = record.validate()
        if errors:
            raise GoalStoreError("invalid GoalRecord: " + "; ".join(errors))
        self._storage.set(_goal_key(record.goal_id), record.to_dict())
        return record

    def create(
        self,
        goal: str,
        definition_of_done: str | Iterable[str],
        *,
        goal_id: str | None = None,
        budgets: GoalBudgets | dict[str, Any] | None = None,
        owner_session: OwnerSession | dict[str, Any] | None = None,
        now: str | None = None,
    ) -> GoalRecord:
        """Create and persist one validated, runnable goal."""
        return self.save(
            GoalRecord.new(
                goal,
                definition_of_done,
                goal_id=goal_id,
                budgets=budgets,
                owner_session=owner_session,
                now=now,
            )
        )

    def delete(self, goal_id: str) -> bool:
        """Delete one goal record; scheduler cleanup remains separate."""
        return self._storage.delete(_goal_key(goal_id))

    def snapshot(self) -> GoalStoreSnapshot:
        """Read all records and mark any uncertain read as incomplete.

        Reconciliation must not delete cron jobs from an empty-looking snapshot
        when storage was corrupt, unreadable, or changed during enumeration.
        """
        try:
            keys = self._storage.list_keys()
        except Exception as exc:  # noqa: BLE001 - fail closed for reconciliation
            return GoalStoreSnapshot(complete=False, errors=(f"list: {exc}",))

        goals: list[GoalRecord] = []
        errors: list[str] = []
        complete = True
        for key in keys:
            if not key.startswith(_GOAL_KEY_PREFIX):
                continue
            goal_id = key[len(_GOAL_KEY_PREFIX) :]
            try:
                _goal_key(goal_id)
            except GoalStoreError as exc:
                complete = False
                errors.append(f"{key}: {exc}")
                continue
            raw = self._storage.get(key)
            if not isinstance(raw, dict):
                complete = False
                errors.append(f"{key}: record unreadable")
                continue
            record = GoalRecord.from_dict(raw)
            if record.goal_id != goal_id:
                complete = False
                errors.append(f"{key}: record identity mismatch")
                continue
            goals.append(record)
            if record.validate():
                complete = False
                errors.append(f"{key}: record invariant failed")

        goals.sort(key=lambda item: item.goal_id)
        return GoalStoreSnapshot(tuple(goals), complete, tuple(errors))

    def list(self) -> list[GoalRecord]:
        """Return readable records, including paused malformed records."""
        return list(self.snapshot().goals)


__all__ = ["GoalStore", "GoalStoreError", "GoalStoreSnapshot"]
