"""Manifest and AppKit declaration contract for Goal Owner."""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

_APP_ROOT = Path(__file__).resolve().parents[1]
_APP_JSON = _APP_ROOT / "app.json"


def _raw() -> dict:
    return json.loads(_APP_JSON.read_text(encoding="utf-8"))


def _manifest() -> AppManifest:
    return AppManifest.from_json_file(_APP_JSON)


def test_manifest_validates_and_is_discovered() -> None:
    assert _manifest().validate(app_root=_APP_ROOT) == []
    assert "goal-owner" in {item["name"] for item in discover_builtin_apps()}


def test_app_is_opt_in_and_declares_own_surface() -> None:
    raw = _raw()
    assert raw["defaultEnabled"] is False
    assert raw["permissions"]["api"] == [
        "/api/apps/goal-owner",
        "/api/apps/goal-owner/*",
    ]
    assert raw["permissions"]["storage"] is True
    assert raw["permissions"]["cron"] is True


def test_manifest_declares_appkit_lifecycle_without_a_frontend_page() -> None:
    hooks = _raw()["backend"]["hooks"]
    assert hooks == {
        "routes": "backend.routes:register_routes",
        "on_startup": "hooks:on_startup",
        "on_shutdown": "hooks:on_shutdown",
    }
    assert "ui" not in _raw()
