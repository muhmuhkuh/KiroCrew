"""Focused HTTP contract tests for Goal Owner's app-local surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from aiohttp import web

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.builtins.goal_owner.backend import routes
from kiro_crew.apps.context import AppContext
from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.route_registry import AppRoute, RouteRegistry

pytestmark = pytest.mark.asyncio


class _Cron:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    def list_jobs(self) -> list[SimpleNamespace]:
        return list(self.jobs)

    async def add_job_if_absent_async(self, *, name: str, **kwargs: Any) -> SimpleNamespace | None:
        del kwargs
        for job in self.jobs:
            if job.name == name:
                return None
        job = SimpleNamespace(
            id=f"job-{len(self.jobs) + 1}",
            name=name,
            persistent_session=True,
        )
        self.jobs.append(job)
        return job

    async def remove_job_async(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs[:] = [job for job in self.jobs if job.id != job_id]
        return len(self.jobs) != before


def _ctx(tmp_path: Path, cron: _Cron | None = None) -> AppContext:
    data_dir = tmp_path / "data"
    return cast(
        AppContext,
        SimpleNamespace(
            data_dir=data_dir,
            storage=AppStorage("goal-owner", data_dir),
            cron=cron,
        ),
    )


def _request(body: Any = None, match_info: dict[str, str] | None = None) -> mock.MagicMock:
    request = mock.MagicMock(spec=web.Request)
    request.match_info = match_info or {}

    async def _json():
        if body is None:
            raise ValueError("missing body")
        return body

    request.json = _json
    return request


def _route(registered: list[AppRoute], method: str, path: str) -> AppRoute:
    return next(item for item in registered if item.method == method and item.path == path)


def _payload(response: web.Response) -> dict[str, Any]:
    body = response.body
    assert isinstance(body, (bytes, bytearray))
    return json.loads(body.decode("utf-8"))


async def test_register_routes_returns_only_goal_owner_paths(tmp_path: Path) -> None:
    registered = routes.register_routes(_ctx(tmp_path))
    assert all(isinstance(item, AppRoute) for item in registered)
    assert {(item.method, item.path) for item in registered} == {
        ("GET", "/status"),
        ("GET", "/goals"),
        ("GET", "/goals/{goal_id}"),
        ("POST", "/goals"),
        ("POST", "/goals/{goal_id}/pause"),
        ("POST", "/goals/{goal_id}/resume"),
    }


async def test_route_module_loads_through_appkit_registry(tmp_path: Path) -> None:
    app = web.Application()
    registry = RouteRegistry(app)
    ctx = _ctx(tmp_path)
    root = shipped_builtin_app_root("goal-owner")
    assert root is not None

    registered = await registry.register_app_routes(
        "goal-owner", root, "backend.routes:register_routes", ctx
    )
    assert len(registered) == 6
    assert registry.get_registered_apps() == ["goal-owner"]
    registry.deregister_app_routes("goal-owner")
    assert registry.get_registered_apps() == []


async def test_disabled_app_is_refused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    status = _route(routes.register_routes(ctx), "GET", "/status")
    with mock.patch.object(routes, "is_app_enabled", return_value=False):
        response = await status.handler(_request(), ctx)
    assert response.status == 403
    assert _payload(response)["code"] == "app_disabled"


async def test_create_pause_resume_reconciles_one_job(tmp_path: Path) -> None:
    cron = _Cron()
    ctx = _ctx(tmp_path, cron)
    registered = routes.register_routes(ctx)
    create = _route(registered, "POST", "/goals")
    pause = _route(registered, "POST", "/goals/{goal_id}/pause")
    resume = _route(registered, "POST", "/goals/{goal_id}/resume")

    with mock.patch.object(routes, "is_app_enabled", return_value=True):
        created = await create.handler(
            _request(
                {
                    "goal": "Ship bounded feature",
                    "definition_of_done": ["tests pass", "review complete"],
                }
            ),
            ctx,
        )
        assert created.status == 201
        goal_id = _payload(created)["goal"]["goal_id"]
        assert len(cron.jobs) == 1

        paused = await pause.handler(_request(match_info={"goal_id": goal_id}), ctx)
        assert paused.status == 200
        assert _payload(paused)["goal"]["status"] == "paused"
        assert cron.jobs == []

        resumed = await resume.handler(_request(match_info={"goal_id": goal_id}), ctx)
        assert resumed.status == 200
        assert _payload(resumed)["goal"]["status"] == "running"
        assert len(cron.jobs) == 1


async def test_create_requires_scheduler(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    create = _route(routes.register_routes(ctx), "POST", "/goals")
    with mock.patch.object(routes, "is_app_enabled", return_value=True):
        response = await create.handler(_request({"goal": "x", "definition_of_done": "done"}), ctx)
    assert response.status == 503
    assert _payload(response)["code"] == "scheduler_unavailable"


async def test_malformed_create_is_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _Cron())
    create = _route(routes.register_routes(ctx), "POST", "/goals")
    with mock.patch.object(routes, "is_app_enabled", return_value=True):
        response = await create.handler(_request({"goal": "x"}), ctx)
    assert response.status == 400
    assert _payload(response)["code"] == "definition_of_done_required"
