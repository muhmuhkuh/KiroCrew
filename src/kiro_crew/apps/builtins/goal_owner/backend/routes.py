"""AppKit routes for the Goal Owner builtin.

Routes are registered only while the app is enabled by the lifecycle dispatcher.
Handlers still re-check enablement because disable and request dispatch can race.
The route surface owns GoalRecord lifecycle only; bounded owner execution arrives
in a later stage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from aiohttp import web

from kiro_crew.apps.context import AppContext
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.apps.route_registry import AppRoute

from ..domain import GoalRecord, GoalStatus
from ..scheduler import GoalScheduler, ReconcileReport, goal_job_name
from ..store import GoalStore

logger = logging.getLogger(__name__)

APP_NAME = "goal-owner"
Handler = Callable[[web.Request, AppContext], Awaitable[web.Response]]


def _bad_request(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _not_found(message: str = "goal not found") -> web.Response:
    return web.json_response({"error": message, "code": "goal_not_found"}, status=404)


def _conflict(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=409)


def _unavailable(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=503)


def _require_enabled(handler: Handler) -> Handler:
    """Gate enabled state and required app capabilities."""

    @wraps(handler)
    async def _wrapped(request: web.Request, ctx: AppContext) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": "goal-owner is disabled", "code": "app_disabled"}, status=403
            )
        if ctx.storage is None:
            return _unavailable("goal-owner storage is unavailable", "storage_unavailable")
        return await handler(request, ctx)

    return _wrapped


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        parsed = await request.json()
    except (LookupError, RecursionError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _store(ctx: AppContext) -> GoalStore:
    assert ctx.storage is not None
    return GoalStore(ctx.storage)


def _goal_id(request: web.Request) -> str | None:
    value = request.match_info.get("goal_id", "")
    if not isinstance(value, str) or not value:
        return None
    try:
        goal_job_name(value)
    except ValueError:
        return None
    return value


def _record_payload(record: GoalRecord) -> dict[str, Any]:
    return record.to_dict()


def _report_payload(report: ReconcileReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "created": len(report.created),
        "adopted": len(report.adopted),
        "removed": len(report.removed),
        "errors": len(report.errors),
    }


async def _reconcile(ctx: AppContext, store: GoalStore) -> ReconcileReport:
    if ctx.cron is None:
        return ReconcileReport(errors=["cron runtime unavailable"])
    try:
        return await GoalScheduler(store, ctx.cron).reconcile()
    except Exception:  # noqa: BLE001 - writes stay visible and retryable
        logger.exception("goal-owner route reconciliation failed")
        return ReconcileReport(errors=["reconciliation failed"])


def _load_goal(store: GoalStore, goal_id: str) -> tuple[GoalRecord | None, web.Response | None]:
    try:
        record = store.get(goal_id)
    except ValueError:
        return None, _bad_request("goal_id has unsafe shape", "invalid_goal_id")
    if record is None:
        return None, _not_found()
    errors = record.validate()
    if record.goal_id != goal_id or errors:
        return None, _conflict("stored goal is not runnable", "goal_record_invalid")
    return record, None


async def _handle_status(request: web.Request, ctx: AppContext) -> web.Response:
    del request
    store = _store(ctx)
    snapshot = await asyncio.to_thread(store.snapshot)

    jobs: list[Any] = []
    scheduler_readable = ctx.cron is not None
    if ctx.cron is not None:
        try:
            jobs = await asyncio.to_thread(ctx.cron.list_jobs)
        except Exception:  # noqa: BLE001 - status must remain readable
            scheduler_readable = False
            logger.exception("goal-owner status could not read cron jobs")

    goal_job_names: set[str] = set()
    for goal in snapshot.goals:
        try:
            goal_job_names.add(goal_job_name(goal.goal_id))
        except ValueError:
            continue

    return web.json_response(
        {
            "app": APP_NAME,
            "runtime": "ready",
            "storage": {
                "complete": snapshot.complete,
                "goals": len(snapshot.goals),
                "errors": len(snapshot.errors),
            },
            "scheduler": {
                "available": ctx.cron is not None,
                "readable": scheduler_readable,
                "jobs": len(jobs),
                "goal_jobs": sum(1 for job in jobs if getattr(job, "name", "") in goal_job_names),
            },
            "goals": [
                {
                    "goal_id": goal.goal_id,
                    "status": goal.status.value,
                    "cycles": goal.counters.cycles,
                    "max_cycles": goal.budgets.max_cycles,
                    "next_action": goal.next_action.to_dict(),
                }
                for goal in snapshot.goals
            ],
        }
    )


async def _handle_goals(request: web.Request, ctx: AppContext) -> web.Response:
    del request
    snapshot = await asyncio.to_thread(_store(ctx).snapshot)
    return web.json_response(
        {
            "goals": [_record_payload(goal) for goal in snapshot.goals],
            "complete": snapshot.complete,
            "errors": len(snapshot.errors),
        }
    )


async def _handle_goal(request: web.Request, ctx: AppContext) -> web.Response:
    goal_id = _goal_id(request)
    if goal_id is None:
        return _bad_request("goal_id has unsafe shape", "invalid_goal_id")
    record, response = _load_goal(_store(ctx), goal_id)
    if response is not None:
        return response
    assert record is not None
    return web.json_response(_record_payload(record))


async def _handle_create(request: web.Request, ctx: AppContext) -> web.Response:
    if ctx.cron is None:
        return _unavailable("goal-owner scheduler is unavailable", "scheduler_unavailable")

    body = await _body(request)
    goal = body.get("goal")
    definition_of_done = body.get("definition_of_done")
    if not isinstance(goal, str) or not goal.strip():
        return _bad_request("goal is required", "goal_required")
    if not isinstance(definition_of_done, (str, list, tuple)):
        return _bad_request(
            "definition_of_done must be a string or list", "definition_of_done_required"
        )
    budgets = body.get("budgets")
    if budgets is not None and not isinstance(budgets, dict):
        return _bad_request("budgets must be an object", "invalid_budgets")

    try:
        record = await asyncio.to_thread(
            _store(ctx).create,
            goal,
            definition_of_done,
            budgets=budgets,
        )
    except (TypeError, ValueError) as exc:
        return _bad_request(str(exc), "invalid_goal")

    report = await _reconcile(ctx, _store(ctx))
    payload = {"goal": _record_payload(record), "scheduler": _report_payload(report)}
    if not report.ok:
        return web.json_response(
            {
                "error": "goal saved but scheduler reconciliation was incomplete",
                "code": "scheduler_reconcile_failed",
                **payload,
            },
            status=503,
        )
    return web.json_response(payload, status=201)


async def _handle_status_transition(
    request: web.Request, ctx: AppContext, target: GoalStatus
) -> web.Response:
    goal_id = _goal_id(request)
    if goal_id is None:
        return _bad_request("goal_id has unsafe shape", "invalid_goal_id")

    store = _store(ctx)
    record, response = _load_goal(store, goal_id)
    if response is not None:
        return response
    assert record is not None
    try:
        await asyncio.to_thread(record.transition, target)
        await asyncio.to_thread(store.save, record)
    except ValueError as exc:
        return _conflict(str(exc), "invalid_goal_transition")

    report = await _reconcile(ctx, store)
    payload = {"goal": _record_payload(record), "scheduler": _report_payload(report)}
    if not report.ok:
        return web.json_response(
            {
                "error": "goal status saved but scheduler reconciliation was incomplete",
                "code": "scheduler_reconcile_failed",
                **payload,
            },
            status=503,
        )
    return web.json_response(payload)


async def _handle_pause(request: web.Request, ctx: AppContext) -> web.Response:
    return await _handle_status_transition(request, ctx, GoalStatus.PAUSED)


async def _handle_resume(request: web.Request, ctx: AppContext) -> web.Response:
    return await _handle_status_transition(request, ctx, GoalStatus.RUNNING)


def register_routes(ctx: AppContext) -> list[AppRoute]:
    """Return app-local routes for the gateway's dynamic AppKit registry."""
    del ctx
    return [
        AppRoute("GET", "/status", _require_enabled(_handle_status)),
        AppRoute("GET", "/goals", _require_enabled(_handle_goals)),
        AppRoute("GET", "/goals/{goal_id}", _require_enabled(_handle_goal)),
        AppRoute("POST", "/goals", _require_enabled(_handle_create)),
        AppRoute("POST", "/goals/{goal_id}/pause", _require_enabled(_handle_pause)),
        AppRoute("POST", "/goals/{goal_id}/resume", _require_enabled(_handle_resume)),
    ]


__all__ = ["APP_NAME", "register_routes"]
