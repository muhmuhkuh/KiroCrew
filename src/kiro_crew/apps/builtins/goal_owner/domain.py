"""Domain model for the autonomous Goal / Project Owner.

This module is deliberately app-local. It defines the durable record shape and
its invariants without owning persistence, cron, sessions, or work-ledger
storage. Later stages can attach those adapters without creating a second
execution ledger.

A ``GoalRecord`` is a product/lifecycle record. Work items remain references to
``work_ledger`` entries; :func:`project_tasks` reads those entries and derives
its task lanes instead of copying worker state into this record.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1

MAX_GOAL_CHARS = 2_000
MAX_DOD_ITEMS = 64
MAX_DOD_CHARS = 500
MAX_PLAN_ITEMS = 64
MAX_PLAN_CHARS = 500
MAX_TASK_REFS = 64
MAX_TASK_REF_CHARS = 128
MAX_HISTORY_ITEMS = 64
MAX_SUMMARY_CHARS = 1_000
MAX_REASON_CHARS = 1_000
MAX_FINGERPRINT_CHARS = 128
MAX_EVIDENCE_ITEMS = 128
MAX_REFERENCE_CHARS = 500
MAX_AGENT_CHARS = 128
MAX_ID_CHARS = 128
MAX_DISPATCH_REQUEST_CHARS = 512
MAX_DISPATCH_RESERVATIONS = 64

DISPATCH_RESERVED = "reserved"
DISPATCH_BOUND = "bound"
DISPATCH_SEEDED = "seeded"
VALID_DISPATCH_STATES = frozenset({DISPATCH_RESERVED, DISPATCH_BOUND, DISPATCH_SEEDED})

DEFAULT_MAX_CYCLES = 24
DEFAULT_MAX_WORKER_DISPATCHES = 64
DEFAULT_MAX_REPLANS = 24

COMPLETION_PASS = "pass"
COMPLETION_FAIL = "fail"
COMPLETION_UNKNOWN = "unknown"
VALID_COMPLETION_RESULTS = frozenset({COMPLETION_PASS, COMPLETION_FAIL, COMPLETION_UNKNOWN})

BLOCKER_HUMAN = "human"
BLOCKER_APPROVAL = "approval"
BLOCKER_AUTHORIZATION = "authorization"
BLOCKER_EXTERNAL = "external"
BLOCKER_AMBIGUITY = "ambiguity"
VALID_BLOCKER_KINDS = frozenset(
    {
        BLOCKER_HUMAN,
        BLOCKER_APPROVAL,
        BLOCKER_AUTHORIZATION,
        BLOCKER_EXTERNAL,
        BLOCKER_AMBIGUITY,
    }
)

# Work-ledger worker status values. Keep this vocabulary in one place rather than
# teaching the goal projection several spellings of the same worker report.
WORKER_BLOCKED_STATUSES = frozenset({"blocked", "question"})
WORK_ITEM_COMPLETED_STATES = frozenset({"accepted", "completed"})


class GoalStatus(str, Enum):
    """Closed lifecycle vocabulary for one goal."""

    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"


VALID_STATUSES = frozenset(GoalStatus)
TERMINAL_STATUSES = frozenset({GoalStatus.FAILED, GoalStatus.COMPLETED})

# Deliberately no implicit terminal escape. Resurrecting a failed/completed goal
# is a new goal or an explicit human-created record, not an owner-cycle action.
LEGAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.RUNNING: frozenset(
        {
            GoalStatus.WAITING,
            GoalStatus.BLOCKED,
            GoalStatus.PAUSED,
            GoalStatus.FAILED,
            GoalStatus.COMPLETED,
        }
    ),
    GoalStatus.WAITING: frozenset(
        {
            GoalStatus.RUNNING,
            GoalStatus.BLOCKED,
            GoalStatus.PAUSED,
            GoalStatus.FAILED,
            GoalStatus.COMPLETED,
        }
    ),
    GoalStatus.BLOCKED: frozenset({GoalStatus.RUNNING, GoalStatus.PAUSED, GoalStatus.FAILED}),
    GoalStatus.PAUSED: frozenset({GoalStatus.RUNNING}),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.COMPLETED: frozenset(),
}


class GoalDomainError(ValueError):
    """Base error for malformed records or rejected domain operations."""

    def __init__(self, message: str, *, code: str = "invalid_goal") -> None:
        super().__init__(message)
        self.code = code


class InvalidGoalTransition(GoalDomainError):
    """Raised when a status transition or terminal invariant is refused."""


class InvalidGoalRecord(GoalDomainError):
    """Raised when a newly-created record cannot satisfy its invariants."""


# Conservative id shape: safe for a route segment and a future storage key. The
# domain module does not perform filesystem I/O, but keeping this boundary here
# means later adapters do not need to trust model-supplied ids.
_GOAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TASK_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any, limit: int, default: str = "") -> str:
    return value[:limit] if isinstance(value, str) else default


def _required_text(value: Any, limit: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidGoalRecord(f"{name} must be a non-empty string", code="invalid_value")
    if len(value) > limit:
        raise InvalidGoalRecord(f"{name} exceeds {limit} characters", code="field_too_long")
    return value.strip()


def _text_list(value: Any, *, item_limit: int, max_items: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [
        item.strip()
        for item in value[:max_items]
        if isinstance(item, str) and item.strip() and len(item) <= item_limit
    ]


def _required_text_list(value: Any, *, item_limit: int, max_items: int, name: str) -> list[str]:
    values = _text_list(value, item_limit=item_limit, max_items=max_items)
    if not values:
        raise InvalidGoalRecord(f"{name} must contain one or more items", code="invalid_value")
    if isinstance(value, (list, tuple)) and len(values) != len(
        [item for item in value if isinstance(item, str) and item.strip()]
    ):
        raise InvalidGoalRecord(f"{name} contains an overlong item", code="field_too_long")
    return values


def _non_negative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _status(value: Any, default: GoalStatus = GoalStatus.PAUSED) -> GoalStatus:
    if isinstance(value, GoalStatus):
        return value
    if isinstance(value, str):
        try:
            return GoalStatus(value.lower())
        except ValueError:
            pass
    return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fingerprint(value: Any) -> str:
    return _text(value, MAX_FINGERPRINT_CHARS).strip()


def blocker_fingerprint(kind: str, summary: str) -> str:
    """Return stable identity for one blocker class and normalized summary."""
    material = f"{kind.strip().lower()}\n{' '.join(summary.split())}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class GoalAssessment:
    """Latest bounded observation of progress against the goal."""

    summary: str = ""
    gap: str = ""
    confidence: str = "unknown"
    observed_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "summary": _text(self.summary, MAX_SUMMARY_CHARS),
            "gap": _text(self.gap, MAX_SUMMARY_CHARS),
            "confidence": _text(self.confidence, 64),
            "observed_at": _text(self.observed_at, 64),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GoalAssessment:
        if isinstance(raw, str):
            return cls(summary=_text(raw, MAX_SUMMARY_CHARS))
        data = _mapping(raw)
        return cls(
            summary=_text(data.get("summary"), MAX_SUMMARY_CHARS),
            gap=_text(data.get("gap"), MAX_SUMMARY_CHARS),
            confidence=_text(data.get("confidence"), 64, "unknown") or "unknown",
            observed_at=_text(data.get("observed_at"), 64),
        )


@dataclass
class NextAction:
    """One bounded action the next owner cycle should begin with."""

    kind: str = "observe"
    summary: str = ""
    task_ref: str | None = None
    due_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": _text(self.kind, 64, "observe") or "observe",
            "summary": _text(self.summary, MAX_SUMMARY_CHARS),
            "task_ref": _text(self.task_ref, MAX_TASK_REF_CHARS) or None,
            "due_at": _text(self.due_at, 64),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> NextAction:
        if isinstance(raw, str):
            return cls(summary=_text(raw, MAX_SUMMARY_CHARS))
        data = _mapping(raw)
        task_ref = _text(data.get("task_ref"), MAX_TASK_REF_CHARS).strip() or None
        return cls(
            kind=_text(data.get("kind"), 64, "observe") or "observe",
            summary=_text(data.get("summary"), MAX_SUMMARY_CHARS),
            task_ref=task_ref,
            due_at=_text(data.get("due_at"), 64),
        )


@dataclass
class GoalCounters:
    """Bounded owner-cycle counters; derived task counts are not copied here."""

    cycles: int = 0
    worker_dispatches: int = 0
    replans: int = 0
    retries: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cycles": max(self.cycles, 0),
            "worker_dispatches": max(self.worker_dispatches, 0),
            "replans": max(self.replans, 0),
            "retries": max(self.retries, 0),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GoalCounters:
        data = _mapping(raw)
        return cls(
            cycles=_non_negative_int(data.get("cycles")),
            worker_dispatches=_non_negative_int(data.get("worker_dispatches")),
            replans=_non_negative_int(data.get("replans")),
            retries=_non_negative_int(data.get("retries")),
        )


@dataclass
class GoalBudgets:
    """Human-owned ceilings. Owner cycles may consume, never increase, them."""

    max_cycles: int = DEFAULT_MAX_CYCLES
    max_worker_dispatches: int = DEFAULT_MAX_WORKER_DISPATCHES
    max_replans: int = DEFAULT_MAX_REPLANS

    def to_dict(self) -> dict[str, int]:
        return {
            "max_cycles": max(self.max_cycles, 1),
            "max_worker_dispatches": max(self.max_worker_dispatches, 1),
            "max_replans": max(self.max_replans, 1),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GoalBudgets:
        data = _mapping(raw)
        return cls(
            max_cycles=_positive_int(data.get("max_cycles"), DEFAULT_MAX_CYCLES),
            max_worker_dispatches=_positive_int(
                data.get("max_worker_dispatches"), DEFAULT_MAX_WORKER_DISPATCHES
            ),
            max_replans=_positive_int(data.get("max_replans"), DEFAULT_MAX_REPLANS),
        )


@dataclass
class GoalTimestamps:
    """Wall-clock stamps used for inspection and restart recovery."""

    created_at: str = ""
    updated_at: str = ""
    last_wake_at: str = ""
    last_cycle_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "created_at": _text(self.created_at, 64),
            "updated_at": _text(self.updated_at, 64),
            "last_wake_at": _text(self.last_wake_at, 64),
            "last_cycle_at": _text(self.last_cycle_at, 64),
            "completed_at": _text(self.completed_at, 64),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GoalTimestamps:
        data = _mapping(raw)
        return cls(
            created_at=_text(data.get("created_at"), 64),
            updated_at=_text(data.get("updated_at"), 64),
            last_wake_at=_text(data.get("last_wake_at"), 64),
            last_cycle_at=_text(data.get("last_cycle_at"), 64),
            completed_at=_text(data.get("completed_at"), 64),
        )


@dataclass
class OwnerSession:
    """Reference to the persistent owner session and its scheduler job."""

    session_key: str = ""
    job_id: str = ""
    agent: str = ""
    attached_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "session_key": _text(self.session_key, MAX_AGENT_CHARS),
            "job_id": _text(self.job_id, MAX_AGENT_CHARS),
            "agent": _text(self.agent, MAX_AGENT_CHARS),
            "attached_at": _text(self.attached_at, 64),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> OwnerSession:
        if isinstance(raw, str):
            return cls(session_key=_text(raw, MAX_AGENT_CHARS))
        data = _mapping(raw)
        return cls(
            session_key=_text(data.get("session_key"), MAX_AGENT_CHARS),
            job_id=_text(data.get("job_id"), MAX_AGENT_CHARS),
            agent=_text(data.get("agent"), MAX_AGENT_CHARS),
            attached_at=_text(data.get("attached_at"), 64),
        )


@dataclass
class DispatchReservation:
    """Durable idempotency record for one owner-to-worker dispatch."""

    item_id: str = ""
    request_id: str = ""
    state: str = DISPATCH_RESERVED
    session_key: str = ""
    reserved_at: str = ""

    def __post_init__(self) -> None:
        self.item_id = _text(self.item_id, MAX_TASK_REF_CHARS).strip()
        self.request_id = _text(self.request_id, MAX_DISPATCH_REQUEST_CHARS).strip()
        self.state = _text(self.state, 32, DISPATCH_RESERVED).strip().lower()
        if self.state not in VALID_DISPATCH_STATES:
            self.state = DISPATCH_RESERVED
        self.session_key = _text(self.session_key, MAX_AGENT_CHARS).strip()
        self.reserved_at = _text(self.reserved_at, 64)

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "request_id": self.request_id,
            "state": self.state,
            "session_key": self.session_key,
            "reserved_at": self.reserved_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DispatchReservation:
        data = _mapping(raw)
        return cls(
            item_id=_text(data.get("item_id"), MAX_TASK_REF_CHARS),
            request_id=_text(data.get("request_id"), MAX_DISPATCH_REQUEST_CHARS),
            state=_text(data.get("state"), 32, DISPATCH_RESERVED),
            session_key=_text(data.get("session_key"), MAX_AGENT_CHARS),
            reserved_at=_text(data.get("reserved_at"), 64),
        )


@dataclass
class DecisionRecord:
    """Structured owner decision, kept as bounded history."""

    kind: str = ""
    reason: str = ""
    at: str = ""
    task_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": _text(self.kind, 64),
            "reason": _text(self.reason, MAX_REASON_CHARS),
            "at": _text(self.at, 64),
            "task_refs": _text_list(
                self.task_refs, item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
            ),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DecisionRecord:
        data = _mapping(raw)
        return cls(
            kind=_text(data.get("kind"), 64),
            reason=_text(data.get("reason"), MAX_REASON_CHARS),
            at=_text(data.get("at"), 64),
            task_refs=_text_list(
                data.get("task_refs"), item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
            ),
        )


@dataclass
class ReplanRecord:
    """Why current plan was replaced."""

    code: str = ""
    reason: str = ""
    at: str = ""
    task_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": _text(self.code, 64),
            "reason": _text(self.reason, MAX_REASON_CHARS),
            "at": _text(self.at, 64),
            "task_refs": _text_list(
                self.task_refs, item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
            ),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ReplanRecord:
        data = _mapping(raw)
        return cls(
            code=_text(data.get("code"), 64),
            reason=_text(data.get("reason"), MAX_REASON_CHARS),
            at=_text(data.get("at"), 64),
            task_refs=_text_list(
                data.get("task_refs"), item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
            ),
        )


@dataclass
class BlockerRecord:
    """Structured blocker history; ``fingerprint`` identifies repeats."""

    fingerprint: str = ""
    kind: str = BLOCKER_HUMAN
    summary: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": _text(self.fingerprint, MAX_FINGERPRINT_CHARS),
            "kind": _text(self.kind, 64, BLOCKER_HUMAN) or BLOCKER_HUMAN,
            "summary": _text(self.summary, MAX_SUMMARY_CHARS),
            "first_seen_at": _text(self.first_seen_at, 64),
            "last_seen_at": _text(self.last_seen_at, 64),
            "attempts": max(self.attempts, 0),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> BlockerRecord:
        data = _mapping(raw)
        return cls(
            fingerprint=_fingerprint(data.get("fingerprint")),
            kind=_text(data.get("kind"), 64, BLOCKER_HUMAN) or BLOCKER_HUMAN,
            summary=_text(data.get("summary"), MAX_SUMMARY_CHARS),
            first_seen_at=_text(data.get("first_seen_at"), 64),
            last_seen_at=_text(data.get("last_seen_at"), 64),
            attempts=_non_negative_int(data.get("attempts")),
        )


@dataclass
class CompletionCheck:
    """One result for one DoD criterion."""

    criterion: str = ""
    result: str = COMPLETION_UNKNOWN
    reference: str = ""
    observed_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "criterion": _text(self.criterion, MAX_DOD_CHARS),
            "result": (
                self.result if self.result in VALID_COMPLETION_RESULTS else COMPLETION_UNKNOWN
            ),
            "reference": _text(self.reference, MAX_REFERENCE_CHARS),
            "observed_at": _text(self.observed_at, 64),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CompletionCheck:
        data = _mapping(raw)
        result = _text(data.get("result"), 32, COMPLETION_UNKNOWN).lower()
        return cls(
            criterion=_text(data.get("criterion"), MAX_DOD_CHARS),
            result=result if result in VALID_COMPLETION_RESULTS else COMPLETION_UNKNOWN,
            reference=_text(data.get("reference"), MAX_REFERENCE_CHARS),
            observed_at=_text(data.get("observed_at"), 64),
        )


@dataclass
class CompletionEvidence:
    """Independent evidence that every current DoD criterion passed."""

    checks: list[CompletionCheck] = field(default_factory=list)
    verified_at: str = ""
    evaluator: str = ""

    def satisfies(self, definition_of_done: Iterable[str]) -> bool:
        required = {
            item.strip() for item in definition_of_done if isinstance(item, str) and item.strip()
        }
        passed = {
            check.criterion.strip()
            for check in self.checks
            if check.result == COMPLETION_PASS and check.criterion.strip()
        }
        return bool(required) and required.issubset(passed) and bool(self.verified_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.to_dict() for check in self.checks[:MAX_EVIDENCE_ITEMS]],
            "verified_at": _text(self.verified_at, 64),
            "evaluator": _text(self.evaluator, MAX_AGENT_CHARS),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CompletionEvidence | None:
        if not isinstance(raw, Mapping):
            return None
        checks_raw = raw.get("checks")
        checks: list[CompletionCheck] = []
        if isinstance(checks_raw, list):
            checks = [
                CompletionCheck.from_dict(item)
                for item in checks_raw[:MAX_EVIDENCE_ITEMS]
                if isinstance(item, Mapping)
            ]
        return cls(
            checks=checks,
            verified_at=_text(raw.get("verified_at"), 64),
            evaluator=_text(raw.get("evaluator"), MAX_AGENT_CHARS),
        )


@dataclass
class TaskProjection:
    """Derived task lanes; no worker state is persisted in ``GoalRecord``."""

    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "completed": list(self.completed),
            "pending": list(self.pending),
            "blocked": list(self.blocked),
            "discovered": list(self.discovered),
        }


def _item_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def project_tasks(
    goal_or_refs: GoalRecord | Iterable[str], work_items: Iterable[Any]
) -> TaskProjection:
    """Project work-ledger entries into completed/pending/blocked/discovered lanes.

    ``task_refs`` identify the entries this goal planned. Entries present in the
    ledger but absent from that set are ``discovered``. A worker ``done`` report
    is still ``pending`` until the work ledger's conductor-owned state becomes
    ``accepted``; worker claims never become completion evidence by themselves.
    """
    refs = (
        list(goal_or_refs.task_refs)
        if isinstance(goal_or_refs, GoalRecord)
        else [ref for ref in goal_or_refs if isinstance(ref, str)]
    )
    ref_set = set(refs)
    seen: set[str] = set()
    projected = TaskProjection()

    for item in work_items:
        item_id = _item_value(item, "item_id", _item_value(item, "id", ""))
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            continue
        seen.add(item_id)
        state = _item_value(item, "state", "")
        status = _item_value(item, "status", "")
        if item_id not in ref_set:
            projected.discovered.append(item_id)
        elif state in WORK_ITEM_COMPLETED_STATES:
            projected.completed.append(item_id)
        elif status in WORKER_BLOCKED_STATUSES:
            projected.blocked.append(item_id)
        else:
            projected.pending.append(item_id)

    # Preserve planned reference order and keep a missing reference visible as
    # pending rather than silently dropping work from the owner's view.
    completed = set(projected.completed)
    blocked = set(projected.blocked)
    projected.pending = [ref for ref in refs if ref not in completed and ref not in blocked]
    return projected


def migrate_goal_record(raw: Any) -> dict[str, Any]:
    """Map known pre-v1 spellings onto the v1 shape without raising.

    Missing/legacy fields are deliberately mapped to safe defaults. A future
    schema is returned as an empty payload: a down-level reader must not pretend
    it can safely rewrite fields introduced by a newer owner.
    """
    if not isinstance(raw, Mapping):
        return {}

    raw_version = raw.get("schema_version", raw.get("schema", 0))
    version = (
        raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else 0
    )
    if version > SCHEMA_VERSION:
        return {}

    data = dict(raw)
    aliases = {
        "goal": ("goal", "objective", "description"),
        "definition_of_done": ("definition_of_done", "dod", "acceptance"),
        "next_action": ("next_action", "next"),
        "task_refs": ("task_refs", "work_item_refs", "tasks"),
        "owner_session": ("owner_session", "owner", "owner_session_key"),
        "counters": ("counters", "counts"),
        "budgets": ("budgets", "budget"),
        "timestamps": ("timestamps", "time"),
    }
    for canonical, names in aliases.items():
        if canonical not in data:
            for name in names:
                if name in raw:
                    data[canonical] = raw[name]
                    break

    status = data.get("status")
    legacy_statuses = {
        "active": GoalStatus.RUNNING.value,
        "idle": GoalStatus.WAITING.value,
        "waiting_for_human": GoalStatus.BLOCKED.value,
        "blocked_on_human": GoalStatus.BLOCKED.value,
        "paused": GoalStatus.PAUSED.value,
        "error": GoalStatus.FAILED.value,
        "done": GoalStatus.COMPLETED.value,
    }
    if isinstance(status, str):
        data["status"] = legacy_statuses.get(status.lower(), status.lower())

    if "owner_session_key" in raw and not isinstance(data.get("owner_session"), Mapping):
        data["owner_session"] = {"session_key": raw.get("owner_session_key")}
    if "created_at" in raw and not isinstance(data.get("timestamps"), Mapping):
        data["timestamps"] = {
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at", raw.get("created_at")),
        }

    data["schema_version"] = SCHEMA_VERSION
    return data


@dataclass
class GoalRecord:
    """Durable goal lifecycle record and its bounded state-machine boundary."""

    goal_id: str = ""
    goal: str = ""
    definition_of_done: list[str] = field(default_factory=list)
    status: GoalStatus = GoalStatus.PAUSED
    plan: list[str] = field(default_factory=list)
    task_refs: list[str] = field(default_factory=list)
    assessment: GoalAssessment = field(default_factory=GoalAssessment)
    next_action: NextAction = field(default_factory=NextAction)
    counters: GoalCounters = field(default_factory=GoalCounters)
    budgets: GoalBudgets = field(default_factory=GoalBudgets)
    timestamps: GoalTimestamps = field(default_factory=GoalTimestamps)
    owner_session: OwnerSession = field(default_factory=OwnerSession)
    dispatch_reservations: list[DispatchReservation] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    replan_reasons: list[ReplanRecord] = field(default_factory=list)
    blockers: list[BlockerRecord] = field(default_factory=list)
    blocker_fingerprint: str = ""
    last_signal_fingerprint: str = ""
    completion_evidence: CompletionEvidence | None = None
    failure_reason: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.goal_id = _text(self.goal_id, MAX_ID_CHARS)
        self.goal = _text(self.goal, MAX_GOAL_CHARS)
        self.definition_of_done = _text_list(
            self.definition_of_done, item_limit=MAX_DOD_CHARS, max_items=MAX_DOD_ITEMS
        )
        self.status = _status(self.status)
        self.plan = _text_list(self.plan, item_limit=MAX_PLAN_CHARS, max_items=MAX_PLAN_ITEMS)
        self.task_refs = _text_list(
            self.task_refs, item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
        )
        if not isinstance(self.assessment, GoalAssessment):
            self.assessment = GoalAssessment.from_dict(self.assessment)
        if not isinstance(self.next_action, NextAction):
            self.next_action = NextAction.from_dict(self.next_action)
        if not isinstance(self.counters, GoalCounters):
            self.counters = GoalCounters.from_dict(self.counters)
        if not isinstance(self.budgets, GoalBudgets):
            self.budgets = GoalBudgets.from_dict(self.budgets)
        if not isinstance(self.timestamps, GoalTimestamps):
            self.timestamps = GoalTimestamps.from_dict(self.timestamps)
        if not isinstance(self.owner_session, OwnerSession):
            self.owner_session = OwnerSession.from_dict(self.owner_session)
        self.dispatch_reservations = [
            item if isinstance(item, DispatchReservation) else DispatchReservation.from_dict(item)
            for item in self.dispatch_reservations[:MAX_DISPATCH_RESERVATIONS]
            if isinstance(item, (DispatchReservation, Mapping))
        ]
        self.decisions = [
            item if isinstance(item, DecisionRecord) else DecisionRecord.from_dict(item)
            for item in self.decisions[:MAX_HISTORY_ITEMS]
            if isinstance(item, (DecisionRecord, Mapping))
        ]
        self.replan_reasons = [
            item if isinstance(item, ReplanRecord) else ReplanRecord.from_dict(item)
            for item in self.replan_reasons[:MAX_HISTORY_ITEMS]
            if isinstance(item, (ReplanRecord, Mapping))
        ]
        self.blockers = [
            item if isinstance(item, BlockerRecord) else BlockerRecord.from_dict(item)
            for item in self.blockers[:MAX_HISTORY_ITEMS]
            if isinstance(item, (BlockerRecord, Mapping))
        ]
        self.blocker_fingerprint = _fingerprint(self.blocker_fingerprint)
        self.last_signal_fingerprint = _fingerprint(self.last_signal_fingerprint)
        if self.completion_evidence is not None and not isinstance(
            self.completion_evidence, CompletionEvidence
        ):
            self.completion_evidence = CompletionEvidence.from_dict(self.completion_evidence)
        self.failure_reason = _text(self.failure_reason, MAX_REASON_CHARS)
        self.schema_version = SCHEMA_VERSION

    @classmethod
    def new(
        cls,
        goal: str,
        definition_of_done: str | Iterable[str],
        *,
        goal_id: str | None = None,
        budgets: GoalBudgets | Mapping[str, Any] | None = None,
        owner_session: OwnerSession | Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> GoalRecord:
        """Create a valid, immediately runnable goal."""
        clean_goal = _required_text(goal, MAX_GOAL_CHARS, "goal")
        dod = _required_text_list(
            definition_of_done,
            item_limit=MAX_DOD_CHARS,
            max_items=MAX_DOD_ITEMS,
            name="definition_of_done",
        )
        resolved_id = goal_id or f"goal_{uuid.uuid4().hex[:16]}"
        if not _GOAL_ID_RE.fullmatch(resolved_id):
            raise InvalidGoalRecord("goal_id has unsafe shape", code="invalid_id")
        stamp = now or _now_iso()
        timestamps = GoalTimestamps(created_at=stamp, updated_at=stamp)
        resolved_budgets = (
            budgets if isinstance(budgets, GoalBudgets) else GoalBudgets.from_dict(budgets)
        )
        resolved_owner_session = (
            owner_session
            if isinstance(owner_session, OwnerSession)
            else OwnerSession.from_dict(owner_session)
        )
        return cls(
            goal_id=resolved_id,
            goal=clean_goal,
            definition_of_done=dod,
            status=GoalStatus.RUNNING,
            budgets=resolved_budgets,
            owner_session=resolved_owner_session,
            timestamps=timestamps,
        )

    create = new

    @classmethod
    def safe_default(cls) -> GoalRecord:
        """Return non-runnable default for absent, corrupt, or newer records."""
        return cls(status=GoalStatus.PAUSED)

    @classmethod
    def from_dict(cls, raw: Any) -> GoalRecord:
        """Read a record tolerantly; never raises for malformed stored input."""
        data = migrate_goal_record(raw)
        if not data:
            return cls.safe_default()
        record = cls(
            goal_id=_text(data.get("goal_id"), MAX_ID_CHARS),
            goal=_text(data.get("goal"), MAX_GOAL_CHARS),
            definition_of_done=_text_list(
                data.get("definition_of_done"), item_limit=MAX_DOD_CHARS, max_items=MAX_DOD_ITEMS
            ),
            status=_status(data.get("status")),
            plan=_text_list(data.get("plan"), item_limit=MAX_PLAN_CHARS, max_items=MAX_PLAN_ITEMS),
            task_refs=_text_list(
                data.get("task_refs"), item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
            ),
            assessment=GoalAssessment.from_dict(data.get("assessment")),
            next_action=NextAction.from_dict(data.get("next_action")),
            counters=GoalCounters.from_dict(data.get("counters")),
            budgets=GoalBudgets.from_dict(data.get("budgets")),
            timestamps=GoalTimestamps.from_dict(data.get("timestamps")),
            owner_session=OwnerSession.from_dict(data.get("owner_session")),
            dispatch_reservations=(
                [
                    DispatchReservation.from_dict(item)
                    for item in data.get("dispatch_reservations", [])
                ]
                if isinstance(data.get("dispatch_reservations"), list)
                else []
            ),
            decisions=(
                [DecisionRecord.from_dict(item) for item in data.get("decisions", [])]
                if isinstance(data.get("decisions"), list)
                else []
            ),
            replan_reasons=(
                [ReplanRecord.from_dict(item) for item in data.get("replan_reasons", [])]
                if isinstance(data.get("replan_reasons"), list)
                else []
            ),
            blockers=(
                [BlockerRecord.from_dict(item) for item in data.get("blockers", [])]
                if isinstance(data.get("blockers"), list)
                else []
            ),
            blocker_fingerprint=_fingerprint(data.get("blocker_fingerprint")),
            last_signal_fingerprint=_fingerprint(data.get("last_signal_fingerprint")),
            completion_evidence=CompletionEvidence.from_dict(data.get("completion_evidence")),
            failure_reason=_text(data.get("failure_reason"), MAX_REASON_CHARS),
        )
        # Any invariant violation must never survive a tolerant read as a
        # runnable goal. PAUSED is the safe, human-repairable state. This also
        # covers malformed RUNNING records with a missing goal or DoD.
        if record.validate():
            record.status = GoalStatus.PAUSED
        return record

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def may_run(self) -> bool:
        return self.status is GoalStatus.RUNNING

    @property
    def has_valid_completion_evidence(self) -> bool:
        return bool(
            self.completion_evidence and self.completion_evidence.satisfies(self.definition_of_done)
        )

    def can_retry(self, signal_fingerprint: str | None = None) -> bool:
        """Whether another owner action is legal under current lifecycle."""
        if self.status in {GoalStatus.PAUSED, *TERMINAL_STATUSES}:
            return False
        if self.status is not GoalStatus.BLOCKED:
            return self.status in {GoalStatus.RUNNING, GoalStatus.WAITING}
        signal = _fingerprint(signal_fingerprint)
        return bool(
            signal and signal != self.blocker_fingerprint and signal != self.last_signal_fingerprint
        )

    def validate(self) -> list[str]:
        """Return invariant violations without mutating the record."""
        errors: list[str] = []
        if not self.goal_id or not _GOAL_ID_RE.fullmatch(self.goal_id):
            errors.append("goal_id must be a safe non-empty identifier")
        if not self.goal.strip():
            errors.append("goal must be non-empty")
        if not self.definition_of_done:
            errors.append("definition_of_done must be non-empty")
        if len(set(self.task_refs)) != len(self.task_refs):
            errors.append("task_refs must be unique")
        if any(not _TASK_REF_RE.fullmatch(ref) for ref in self.task_refs):
            errors.append("task_refs contain an unsafe identifier")
        reservation_ids = [item.item_id for item in self.dispatch_reservations]
        if len(set(reservation_ids)) != len(reservation_ids):
            errors.append("dispatch_reservations must be unique by item_id")
        for reservation in self.dispatch_reservations:
            if not _TASK_REF_RE.fullmatch(reservation.item_id):
                errors.append("dispatch reservation has unsafe item_id")
            if not reservation.request_id:
                errors.append("dispatch reservation needs request_id")
            if reservation.state not in VALID_DISPATCH_STATES:
                errors.append("dispatch reservation has invalid state")
        if self.status is GoalStatus.BLOCKED and not self.blocker_fingerprint:
            errors.append("blocked goal requires blocker_fingerprint")
        if self.status is GoalStatus.COMPLETED and not self.has_valid_completion_evidence:
            errors.append("completed goal requires passing DoD evidence")
        if self.is_terminal and self.status is not GoalStatus.COMPLETED and not self.failure_reason:
            errors.append("failed goal requires failure_reason")
        for name, value in (
            ("cycles", self.counters.cycles),
            ("worker_dispatches", self.counters.worker_dispatches),
            ("replans", self.counters.replans),
            ("retries", self.counters.retries),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"counters.{name} must be non-negative")
        return errors

    def transition(
        self,
        target: GoalStatus | str,
        *,
        now: str | None = None,
        signal_fingerprint: str | None = None,
        blocker: BlockerRecord | Mapping[str, Any] | None = None,
        completion_evidence: CompletionEvidence | Mapping[str, Any] | None = None,
        failure_reason: str = "",
    ) -> GoalRecord:
        """Apply one legal transition, enforcing blocked/completed gates."""
        target_status = _status(target, default=GoalStatus.PAUSED)
        if not isinstance(target, (GoalStatus, str)) or (
            isinstance(target, str) and target.lower() not in {item.value for item in GoalStatus}
        ):
            raise InvalidGoalTransition("unknown goal status", code="invalid_status")
        if target_status is self.status:
            raise InvalidGoalTransition(
                "same-state transition is not an owner action", code="same_state"
            )
        if self.is_terminal:
            raise InvalidGoalTransition("terminal goal cannot run or transition", code="terminal")
        if target_status not in LEGAL_TRANSITIONS[self.status]:
            raise InvalidGoalTransition(
                f"{self.status.value} -> {target_status.value} is not allowed",
                code="illegal_transition",
            )

        stamp = now or _now_iso()
        if target_status is GoalStatus.BLOCKED:
            candidate_blocker = blocker
            if isinstance(candidate_blocker, BlockerRecord):
                blocked = candidate_blocker
            else:
                blocked = BlockerRecord.from_dict(candidate_blocker)
            fp = _fingerprint(blocked.fingerprint or signal_fingerprint)
            if not fp:
                raise InvalidGoalTransition(
                    "blocked goal requires blocker fingerprint", code="blocker_required"
                )
            blocked.fingerprint = fp
            blocked.first_seen_at = blocked.first_seen_at or stamp
            blocked.last_seen_at = stamp
            blocked.attempts = max(blocked.attempts, 0) + 1
            self.blocker_fingerprint = fp
            self._append_blocker(blocked)
        elif self.status is GoalStatus.BLOCKED and target_status is GoalStatus.RUNNING:
            signal = _fingerprint(signal_fingerprint)
            if not signal:
                raise InvalidGoalTransition(
                    "blocked goal requires a new signal before retry", code="new_signal_required"
                )
            if signal in {self.blocker_fingerprint, self.last_signal_fingerprint}:
                raise InvalidGoalTransition(
                    "blocked goal received no new signal", code="duplicate_signal"
                )
            self.last_signal_fingerprint = signal
            self.blocker_fingerprint = ""
        elif target_status is GoalStatus.COMPLETED:
            candidate_evidence = completion_evidence
            if candidate_evidence is None:
                evidence = self.completion_evidence
            elif isinstance(candidate_evidence, CompletionEvidence):
                evidence = candidate_evidence
            else:
                evidence = CompletionEvidence.from_dict(candidate_evidence)
            if evidence is None or not evidence.satisfies(self.definition_of_done):
                raise InvalidGoalTransition(
                    "completed goal requires passing evidence for every DoD criterion",
                    code="dod_evidence_required",
                )
            self.completion_evidence = evidence
        elif target_status is GoalStatus.FAILED:
            self.failure_reason = _text(failure_reason, MAX_REASON_CHARS).strip()
            if not self.failure_reason:
                raise InvalidGoalTransition(
                    "failed goal requires a failure reason", code="failure_reason_required"
                )

        self.status = target_status
        self.timestamps.updated_at = stamp
        if target_status is GoalStatus.COMPLETED:
            self.timestamps.completed_at = stamp
        return self

    def record_cycle(
        self,
        *,
        assessment: GoalAssessment | Mapping[str, Any] | None = None,
        next_action: NextAction | Mapping[str, Any] | str | None = None,
        now: str | None = None,
    ) -> GoalRecord:
        """Record one bounded owner wake; terminal goals cannot be advanced."""
        if not self.may_run:
            raise InvalidGoalTransition("only running goals may record a cycle", code="not_running")
        if self.counters.cycles >= self.budgets.max_cycles:
            raise InvalidGoalTransition("cycle budget exhausted", code="budget_exhausted")
        stamp = now or _now_iso()
        self.counters.cycles += 1
        self.timestamps.last_wake_at = stamp
        self.timestamps.last_cycle_at = stamp
        self.timestamps.updated_at = stamp
        if assessment is not None:
            self.assessment = (
                assessment
                if isinstance(assessment, GoalAssessment)
                else GoalAssessment.from_dict(assessment)
            )
            self.assessment.observed_at = self.assessment.observed_at or stamp
        if next_action is not None:
            self.next_action = (
                next_action
                if isinstance(next_action, NextAction)
                else NextAction.from_dict(next_action)
            )
        return self

    def record_decision(
        self,
        kind: str,
        reason: str,
        *,
        task_refs: Iterable[str] = (),
        now: str | None = None,
    ) -> GoalRecord:
        if self.is_terminal:
            raise InvalidGoalTransition("terminal goal cannot record a decision", code="terminal")
        self.decisions.append(
            DecisionRecord(
                kind=_text(kind, 64),
                reason=_text(reason, MAX_REASON_CHARS),
                at=now or _now_iso(),
                task_refs=_text_list(
                    list(task_refs), item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
                ),
            )
        )
        self.decisions = self.decisions[-MAX_HISTORY_ITEMS:]
        self.timestamps.updated_at = now or _now_iso()
        return self

    def record_replan(
        self,
        code: str,
        reason: str,
        *,
        task_refs: Iterable[str] = (),
        now: str | None = None,
    ) -> GoalRecord:
        if self.is_terminal:
            raise InvalidGoalTransition("terminal goal cannot replan", code="terminal")
        if self.counters.replans >= self.budgets.max_replans:
            raise InvalidGoalTransition("replan budget exhausted", code="budget_exhausted")
        stamp = now or _now_iso()
        self.counters.replans += 1
        self.replan_reasons.append(
            ReplanRecord(
                code=_text(code, 64),
                reason=_text(reason, MAX_REASON_CHARS),
                at=stamp,
                task_refs=_text_list(
                    list(task_refs), item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
                ),
            )
        )
        self.replan_reasons = self.replan_reasons[-MAX_HISTORY_ITEMS:]
        self.timestamps.updated_at = stamp
        return self

    def _append_blocker(self, blocker: BlockerRecord) -> None:
        for existing in self.blockers:
            if existing.fingerprint == blocker.fingerprint:
                existing.last_seen_at = blocker.last_seen_at
                existing.attempts = max(existing.attempts, blocker.attempts)
                if blocker.summary:
                    existing.summary = blocker.summary
                return
        self.blockers.append(blocker)
        self.blockers = self.blockers[-MAX_HISTORY_ITEMS:]

    def to_dict(self) -> dict[str, Any]:
        """Serialize only bounded, JSON-compatible v1 fields."""
        return {
            "schema_version": SCHEMA_VERSION,
            "goal_id": _text(self.goal_id, MAX_ID_CHARS),
            "goal": _text(self.goal, MAX_GOAL_CHARS),
            "definition_of_done": _text_list(
                self.definition_of_done, item_limit=MAX_DOD_CHARS, max_items=MAX_DOD_ITEMS
            ),
            "status": self.status.value,
            "plan": _text_list(self.plan, item_limit=MAX_PLAN_CHARS, max_items=MAX_PLAN_ITEMS),
            "task_refs": _text_list(
                self.task_refs, item_limit=MAX_TASK_REF_CHARS, max_items=MAX_TASK_REFS
            ),
            "assessment": self.assessment.to_dict(),
            "next_action": self.next_action.to_dict(),
            "counters": self.counters.to_dict(),
            "budgets": self.budgets.to_dict(),
            "timestamps": self.timestamps.to_dict(),
            "owner_session": self.owner_session.to_dict(),
            "dispatch_reservations": [
                item.to_dict() for item in self.dispatch_reservations[-MAX_DISPATCH_RESERVATIONS:]
            ],
            "decisions": [item.to_dict() for item in self.decisions[-MAX_HISTORY_ITEMS:]],
            "replan_reasons": [item.to_dict() for item in self.replan_reasons[-MAX_HISTORY_ITEMS:]],
            "blockers": [item.to_dict() for item in self.blockers[-MAX_HISTORY_ITEMS:]],
            "blocker_fingerprint": _text(self.blocker_fingerprint, MAX_FINGERPRINT_CHARS),
            "last_signal_fingerprint": _text(self.last_signal_fingerprint, MAX_FINGERPRINT_CHARS),
            "completion_evidence": (
                self.completion_evidence.to_dict() if self.completion_evidence else None
            ),
            "failure_reason": _text(self.failure_reason, MAX_REASON_CHARS),
        }


__all__ = [
    "BLOCKER_AMBIGUITY",
    "BLOCKER_APPROVAL",
    "BLOCKER_AUTHORIZATION",
    "BLOCKER_EXTERNAL",
    "BLOCKER_HUMAN",
    "COMPLETION_FAIL",
    "COMPLETION_PASS",
    "COMPLETION_UNKNOWN",
    "DISPATCH_BOUND",
    "DISPATCH_RESERVED",
    "DISPATCH_SEEDED",
    "CompletionCheck",
    "CompletionEvidence",
    "DecisionRecord",
    "DispatchReservation",
    "GoalAssessment",
    "GoalBudgets",
    "GoalCounters",
    "GoalDomainError",
    "GoalRecord",
    "GoalStatus",
    "GoalTimestamps",
    "InvalidGoalRecord",
    "InvalidGoalTransition",
    "LEGAL_TRANSITIONS",
    "NextAction",
    "OwnerSession",
    "ReplanRecord",
    "TaskProjection",
    "TERMINAL_STATUSES",
    "blocker_fingerprint",
    "migrate_goal_record",
    "project_tasks",
]
