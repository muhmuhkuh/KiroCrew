"""Mate state is write-protected for file tools while remaining readable."""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path

_MATE_STATE = (
    "owners.json",
    "kits",
    "revoked-approvals.json",
    "tool-access.json",
    "migrated.json",
)


def _clear_target_cache() -> None:
    security._home_targets_cache.clear()
    security._path_resolve_degraded.clear()


@pytest.fixture(autouse=True)
def _target_cache_isolated():
    _clear_target_cache()
    try:
        yield
    finally:
        _clear_target_cache()


@pytest.mark.parametrize("under_override", [False, True])
def test_mate_state_is_write_only_protected_and_reanchored(
    monkeypatch: pytest.MonkeyPatch, tmp_path, under_override: bool
) -> None:
    home = tmp_path / "home"
    kiro_home = tmp_path / "kiro-home"
    home.mkdir()
    kiro_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))

    kiro_root = kiro_home if under_override else home / ".kiro"
    mates = kiro_root / "mates"
    targets = [mates / name for name in _MATE_STATE]
    targets.append(mates / "kits" / "nested" / "kit.json")
    for target in targets:
        assert is_sensitive_write_path(str(target)), target
        assert not is_sensitive_path(str(target)), target

    neighbour = mates / "unrelated.json"
    assert not is_sensitive_write_path(str(neighbour))
    assert not is_sensitive_path(str(neighbour))
