"""Tests for the native Ponytail coding-mode policy."""

from __future__ import annotations

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.ponytail import (
    PONYTAIL_DEFAULT,
    PONYTAIL_LEVELS,
    normalize_ponytail,
    render_ponytail_mode,
    resolve_ponytail,
)
from kiro_crew.skills import SkillsLoader


@pytest.mark.parametrize("mode", PONYTAIL_LEVELS)
def test_concrete_modes_are_preserved(mode: str) -> None:
    assert normalize_ponytail(mode) == mode
    assert resolve_ponytail(mode, default="off") == mode


def test_invalid_values_fail_closed_to_full() -> None:
    assert normalize_ponytail("invalid") == PONYTAIL_DEFAULT
    assert resolve_ponytail("invalid", default="invalid") == PONYTAIL_DEFAULT


def test_rendering_keeps_off_silent_and_other_modes_marked() -> None:
    assert render_ponytail_mode("off") == ""
    rendered = render_ponytail_mode("ultra")
    assert "[PONYTAIL MODE]" in rendered
    assert "Ponytail coding mode: ultra" in rendered
    assert "[END PONYTAIL MODE]" in rendered


def _builder(tmp_path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def test_build_message_injects_and_remembers_resolved_mode(tmp_path) -> None:
    builder = _builder(tmp_path)

    message, _ = builder.build_message(
        "hello",
        is_new_session=False,
        session_key="dashboard:ponytail-test",
        ponytail_mode="ultra",
        interactive=False,
    )

    assert "Ponytail coding mode: ultra" in message
    assert builder.ponytail_mode_for_session("dashboard:ponytail-test") == "ultra"


def test_off_override_does_not_inject_guidance(tmp_path) -> None:
    builder = _builder(tmp_path)

    message, _ = builder.build_message(
        "hello",
        is_new_session=False,
        session_key="dashboard:ponytail-off",
        ponytail_mode="off",
        interactive=False,
    )

    assert "[PONYTAIL MODE]" not in message
    assert builder.ponytail_mode_for_session("dashboard:ponytail-off") == "off"
