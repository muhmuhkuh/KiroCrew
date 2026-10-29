"""The script_hooks inner events list is enforced at each hook surface."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.platform.governance import parse_policy, resolve


def _doc(**body: object) -> dict:
    return {"version": 1, "boot": {"fail_closed": True}, **body}


def _hook_events(*allowed: str) -> dict:
    return _doc(
        capabilities={
            "script_hooks": {
                "enabled": True,
                "scopes": {"events": {"mode": "allow", "allow": list(allowed)}},
            }
        }
    )


def _set_edition(monkeypatch: pytest.MonkeyPatch, edition: str) -> bool:
    monkeypatch.setattr("kiro_crew.edition.EDITION", edition)
    return edition == "enterprise"


def _install_policy(monkeypatch: pytest.MonkeyPatch, document: dict | None) -> None:
    from kiro_crew.platform import context, governance_profiles

    current = SimpleNamespace(governance=parse_policy(document) if document is not None else None)
    monkeypatch.setattr(context, "current_context", lambda: current)
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)


def test_event_scope_is_case_insensitive_and_does_not_limit_when_unnamed() -> None:
    scope = "capabilities.script_hooks"
    ceiling = parse_policy(_hook_events("PreToolUse"))
    assert resolve(ceiling, None, scope, "events:PreToolUse").permitted
    assert resolve(ceiling, None, scope, "events:preToolUse").permitted
    assert not resolve(ceiling, None, scope, "events:Stop").permitted

    enabled_without_list = parse_policy(_doc(capabilities={"script_hooks": {"enabled": True}}))
    assert resolve(enabled_without_list, None, scope, "events:Stop").permitted


def test_event_refusal_keeps_oss_ungoverned_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.hook_event_governance import event_refusal

    _set_edition(monkeypatch, "oss")
    _install_policy(monkeypatch, None)
    assert event_refusal("session", "Stop") is None
    assert event_refusal("session", "") is None


@pytest.mark.parametrize("edition", ["oss", "enterprise"])
def test_event_evaluation_errors_only_fail_closed_in_enterprise(
    monkeypatch: pytest.MonkeyPatch, edition: str
) -> None:
    from kiro_crew.hook_event_governance import event_refusal
    from kiro_crew.platform import governance_profiles

    enterprise = _set_edition(monkeypatch, edition)

    def fail(*_args, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(governance_profiles, "governance_permits", fail)
    assert (event_refusal("session", "Stop") is not None) is enterprise


@pytest.mark.asyncio
async def test_command_hook_refuses_an_event_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.hooks import HOOK_EVENT_STOP, ScriptHook, run_script_hook

    _set_edition(monkeypatch, "oss")
    _install_policy(monkeypatch, _hook_events("PreToolUse"))
    hook = ScriptHook(id="denied", name="denied", event=HOOK_EVENT_STOP, command="must-not-run")

    result = await run_script_hook(hook)

    assert result.exit_code == 2
    assert "hook event 'Stop' refused" in (result.error or "")
    assert hook.last_status == "blocked"


@pytest.mark.asyncio
async def test_command_hook_judges_the_dispatched_event_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, run_script_hook

    _set_edition(monkeypatch, "oss")
    _install_policy(monkeypatch, _hook_events("PreToolUse"))
    hook = ScriptHook(
        id="snapshot", name="snapshot", event=HOOK_EVENT_PRE_TOOL_USE, command="unused"
    )

    result = await run_script_hook(hook, hook_event={"hook_event_name": "Stop", "cwd": "/"})

    assert result.exit_code == 2
    assert "hook event 'Stop' refused" in (result.error or "")


@pytest.mark.asyncio
async def test_skills_only_hook_obeys_the_same_event_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT, ScriptHook, ScriptHookStore

    _set_edition(monkeypatch, "oss")
    _install_policy(monkeypatch, _hook_events("Stop"))
    store = ScriptHookStore(tmp_path, load=False)
    hook = ScriptHook(
        id="skills", name="skills", event=HOOK_EVENT_USER_PROMPT_SUBMIT, skills=["base/example"]
    )
    store._hooks[hook.id] = hook

    results = await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, session_key="session")

    assert results == []
    assert hook.last_status == "blocked"
    assert "UserPromptSubmit" in hook.last_error


@pytest.mark.parametrize("edition", ["oss", "enterprise"])
def test_user_kiro_hooks_drop_only_policy_refused_events(
    monkeypatch: pytest.MonkeyPatch, edition: str
) -> None:
    from kiro_crew.agent_materialization.kiro_hooks import _apply_user_kiro_hooks

    enterprise = _set_edition(monkeypatch, edition)
    _install_policy(monkeypatch, _hook_events("PreToolUse"))
    # This test is about event governance; use a valid, absolute path so the
    # command-path validator does not reject the fixture before that assertion.
    entry = [{"command": str(Path(__file__).resolve())}]
    config: dict = {"hooks": {}}
    source = {
        "agent": {
            "kiro_hooks": {"preToolUse": entry, "stop": entry},
            "kiro_hooks_autoimport": False,
        }
    }

    _apply_user_kiro_hooks(config, source)

    assert bool(config["hooks"].get("preToolUse"))
    assert bool(config["hooks"].get("stop")) is not enterprise
