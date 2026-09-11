"""Native modes are explicit resources and must not finish model turns."""

from __future__ import annotations

import pytest

from kiro_crew.pi_support import _PiModeCompletion, resolve_pi_mode_resources


def test_mode_resources_require_both_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="pi install npm:"):
        resolve_pi_mode_resources()
    root = tmp_path / "npm" / "node_modules"
    ponytail = root / "@dietrichgebert" / "ponytail"
    files = (
        ponytail / "pi-extension" / "index.js",
        root / "pi-caveman" / "extensions" / "caveman.ts",
    )
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError):
        resolve_pi_mode_resources()
    skills = ponytail / "skills"
    skills.mkdir()
    assert resolve_pi_mode_resources() == (*files, skills)


@pytest.mark.parametrize(
    "command", ["/ponytail full", "/ponytail off", "/caveman ultra", "/caveman stop"]
)
def test_native_command_settles_exactly_once(command):
    tracker = _PiModeCompletion()
    tracker.request({"type": "prompt", "id": "one", "message": command})
    response = {"type": "response", "command": "prompt", "id": "one", "success": True}
    assert not tracker.response({**response, "id": "other"})
    assert tracker.response(response)
    assert not tracker.response(response)


@pytest.mark.parametrize(
    "command",
    [
        "hello",
        "/skill:ponytail-review",
        "/ponytail-review",
        "/caveman-like",
        " /ponytail full",
        "/ponytail\tfull",
        "/caveman\nlite",
    ],
)
def test_model_and_alias_prompts_never_synthesize_completion(command):
    tracker = _PiModeCompletion()
    tracker.request({"type": "prompt", "id": "one", "message": command})
    assert not tracker.response(
        {"type": "response", "command": "prompt", "id": "one", "success": True}
    )


@pytest.mark.parametrize(
    "interruption", ["abort", "agent_start", "agent_settled", "failure", "overlap"]
)
def test_interrupted_command_cannot_settle_later_turn(interruption):
    tracker = _PiModeCompletion()
    tracker.request({"type": "prompt", "id": "one", "message": "/caveman off"})
    response = {"type": "response", "command": "prompt", "id": "one", "success": True}
    if interruption == "abort":
        tracker.request({"type": "abort"})
    elif interruption == "overlap":
        tracker.request({"type": "prompt", "id": "two", "message": "real model task"})
    elif interruption == "failure":
        assert not tracker.response({**response, "success": False})
    else:
        assert not tracker.response({"type": interruption})
    assert not tracker.response(response)


def test_rejected_model_prompt_does_not_block_later_native_command():
    tracker = _PiModeCompletion()
    tracker.request({"type": "prompt", "id": "model", "message": "hello"})
    assert not tracker.response(
        {"type": "response", "command": "prompt", "id": "model", "success": False}
    )
    tracker.request({"type": "prompt", "id": "mode", "message": "/ponytail off"})
    assert tracker.response(
        {"type": "response", "command": "prompt", "id": "mode", "success": True}
    )


def test_pi_command_classification_keeps_kiro_unchanged():
    from kiro_crew.dashboard.chat_utils import is_harness_slash_command

    for command in ("/ponytail", "/caveman", "/skill:ponytail-audit"):
        assert is_harness_slash_command(command, cc_provider=False, pi_backend=True)
        assert not is_harness_slash_command(command, cc_provider=False)
    assert not is_harness_slash_command("/skill:untrusted", cc_provider=False, pi_backend=True)
