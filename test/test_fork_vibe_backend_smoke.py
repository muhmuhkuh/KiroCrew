"""Minimal smoke test: ACP_BACKEND_VIBE is importable and correctly defined.

This test exists as a fork-local guard: it verifies the single most important
property of the fork's Vibe backend feature — that the constant can be imported
and has the expected string value — so a future upstream sync that accidentally
removes or renames the constant fails immediately and visibly.

The comprehensive vocabulary and harness tests live in test_acp_vibe_backend.py.
"""

from __future__ import annotations


def test_acp_backend_vibe_is_importable_and_has_correct_value() -> None:
    """ACP_BACKEND_VIBE can be imported from both canonical paths and equals 'vibe'."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_VIBE as VIBE_FROM_SDK
    from kiro_crew.acp_backends import ACP_BACKEND_VIBE as VIBE_FROM_SHIM

    assert VIBE_FROM_SDK == "vibe", f"Expected 'vibe', got {VIBE_FROM_SDK!r}"
    assert VIBE_FROM_SHIM == "vibe", f"Expected 'vibe', got {VIBE_FROM_SHIM!r}"
    assert VIBE_FROM_SDK is VIBE_FROM_SHIM, "Both import paths must refer to the same object"


def test_acp_backend_vibe_is_in_known_backends() -> None:
    """ACP_BACKEND_VIBE is registered in ACP_BACKENDS_KNOWN (the membership gate)."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_VIBE, ACP_BACKENDS_KNOWN

    assert ACP_BACKEND_VIBE in ACP_BACKENDS_KNOWN
