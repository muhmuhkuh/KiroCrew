"""The Mistral Vibe backend: vocabulary, the launch record, and the routing state.

Vibe is onboarded with ``Routing.UNVERIFIED``: its ACP surface's ``session/set_mode``
support and the exact mechanism to force the ``ask`` agent profile over ACP are not
yet verified. Until routing is established, ``register_selectable_backend`` refuses
the backend, which is the correct fail-closed behaviour -- a selectable harness
starts sessions, and for an UNVERIFIED one nothing establishes that its tool calls
reach the host permission gate.

This file pins the vocabulary that IS established: the backend id, its membership in
``ACP_BACKENDS_KNOWN`` and ``ACP_BACKENDS_INDEPENDENT_SETUP``, its
``SelfServedLaunch`` record (binary ``vibe-acp``, no adapter package), and its
absence from the capability sets it has no evidence for.
"""

from __future__ import annotations

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp.harness import VibeLaunch, process_adapter_for
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import tool_gate as gate

VIBE = acp_backends.ACP_BACKEND_VIBE

#: This harness's launch record. The override variable, the binary and the argv
#: tail are read from here rather than from module constants, because the record is
#: where they are declared and a second spelling could drift from it.
_VIBE_LAUNCH = sdk_backends.launch_for(sdk_backends.ACP_BACKEND_VIBE)


# ── Vocabulary ──


def test_vibe_is_a_known_backend() -> None:
    """Known gates the kwarg: an unrecognized value would silently spawn kiro-cli."""
    assert VIBE == "vibe"
    assert VIBE in sdk_backends.ACP_BACKENDS_KNOWN


def test_vibe_is_not_yet_selectable_because_routing_is_unverified() -> None:
    """UNVERIFIED refuses registration; this is the fail-closed state.

    ``register_selectable_backend`` refuses UNVERIFIED with no opt-out, so Vibe is
    NOT in ``BASELINE_SELECTABLE_BACKENDS`` until routing is established. This test
    pins that absence as a decision, not an oversight.
    """
    assert sdk_backends.routing_for(VIBE) is sdk_backends.Routing.UNVERIFIED
    assert VIBE not in sdk_backends.BASELINE_SELECTABLE_BACKENDS


def test_register_selectable_backend_refuses_vibe() -> None:
    """The one gate that makes a backend selectable refuses UNVERIFIED."""
    with pytest.raises(ValueError, match="unverified"):
        sdk_backends.register_selectable_backend(VIBE)


def test_vibe_is_in_independent_setup() -> None:
    """First-run setup can complete without Kiro CLI for harnesses that launch independently."""
    assert VIBE in sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP


def test_vibe_carries_its_mcp_servers_on_the_session_array() -> None:
    """Membership here is what gives a Vibe session Crew's own tools.

    Vibe supports MCP servers (stdio, http, streamable-http) configured via
    ``mcp_servers`` in ``config.toml`` or added via ``vibe mcp add``. The ACP
    ``session/new`` array is the channel Crew has to mount its own servers on the
    session.
    """
    assert VIBE in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY


def test_vibe_is_acp_client_spawnable() -> None:
    """Vibe is launched one process per session by ``AcpClient``."""
    assert VIBE in sdk_backends.ACP_BACKENDS_ACP_CLIENT_SPAWNABLE


def test_vibe_is_not_in_the_capability_sets_it_has_no_evidence_for() -> None:
    """Absence is a decision here, not an oversight, so it is pinned as one.

    Each of these would make a claim the wire does not support: a shared process,
    a steer method, an internal sandbox that would displace the credential mask,
    session sharing, or an effort channel.
    """
    assert VIBE not in sdk_backends.ACP_BACKENDS_ACP_RUNTIME
    assert VIBE not in sdk_backends.ACP_BACKENDS_STEER
    assert VIBE not in sdk_backends.ACP_BACKENDS_STEERING_REQUEST
    assert VIBE not in sdk_backends.ACP_BACKENDS_INTERNAL_SANDBOX
    assert VIBE not in sdk_backends.ACP_BACKENDS_SESSION_SHARING
    assert VIBE not in sdk_backends.ACP_BACKENDS_SESSION_EVICTION
    assert VIBE not in sdk_backends.ACP_BACKENDS_COMPACT
    assert VIBE not in sdk_backends.ACP_BACKENDS_INLINE_COMPACTION
    assert VIBE not in sdk_backends.ACP_BACKENDS_HARNESS_MANAGED_COMPACTION
    assert VIBE not in sdk_backends.ACP_BACKENDS_CONTEXT_RECYCLE
    assert VIBE not in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert VIBE not in sdk_backends.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert VIBE not in sdk_backends.ACP_BACKENDS_KIRO_SLASH_COMMANDS
    assert VIBE not in sdk_backends.ACP_BACKENDS_MARKDOWN_AGENT_SPECS
    assert VIBE not in sdk_backends.ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY
    assert VIBE not in sdk_backends.ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE
    assert VIBE not in sdk_backends.ACP_BACKENDS_MEMBER_CAPABILITIES
    assert VIBE not in sdk_backends.ACP_BACKENDS_MEMBER_DISPATCH
    assert VIBE not in sdk_backends.ACP_BACKENDS_MEMBER_PANEL
    assert VIBE not in sdk_backends.ACP_BACKENDS_META_IDENTITY


# ── The launch record ──


def test_vibe_launch_record_names_the_binary_and_no_adapter() -> None:
    """``vibe-acp`` is the ACP server; no npm adapter is needed.

    The published Python package ``mistral-vibe`` ships the ``vibe-acp`` executable,
    which serves ACP (JSON-RPC 2.0 over stdin/stdout) directly.
    """
    assert _VIBE_LAUNCH.binary == "vibe-acp"
    assert _VIBE_LAUNCH.acp_args == ()
    assert _VIBE_LAUNCH.bin_env_var == "VIBE_ACP_BIN"
    assert _VIBE_LAUNCH.protocol_version == 1
    assert _VIBE_LAUNCH.label == "Mistral Vibe"


def test_vibe_install_command_is_pip_not_npm() -> None:
    """Vibe is a Python package; the install command reflects that."""
    assert "pip" in _VIBE_LAUNCH.install_command or "uv" in _VIBE_LAUNCH.install_command
    assert "npm" not in _VIBE_LAUNCH.install_command


def test_vibe_missing_hint_names_the_python_install() -> None:
    """The hint tells the operator what to install: a Python package, not npm.

    The hint says "not an npm adapter" to steer the operator away from the wrong
    installer; the assertion checks the positive signal (pip/uv) rather than the
    absence of the word "npm", which appears in the corrective context.
    """
    assert "pip" in _VIBE_LAUNCH.missing_hint or "uv" in _VIBE_LAUNCH.missing_hint
    assert "python" in _VIBE_LAUNCH.missing_hint.lower()


def test_vibe_is_self_served_acp() -> None:
    """Vibe serves ACP from its own binary; its whole launch is in the table."""
    assert VIBE in sdk_backends.ACP_BACKENDS_SELF_SERVED_ACP


def test_vibe_process_name_is_the_binary() -> None:
    """The PID-file reclaim recognises a vibe-acp child by its argv0 basename."""
    assert sdk_backends.ACP_BACKEND_PROCESS_NAMES[VIBE] == "vibe-acp"


def test_vibe_is_not_a_node_adapter() -> None:
    """Vibe is a Python binary, not a Node entry script."""
    assert VIBE not in sdk_backends.ACP_BACKEND_NODE_ADAPTER_PACKAGES


# ── The harness adapter ──


def test_vibe_has_a_process_adapter() -> None:
    """``AcpClient`` launches one vibe-acp process per session through this adapter."""
    adapter = process_adapter_for(VIBE)
    assert adapter is not None
    assert isinstance(adapter, VibeLaunch)
    assert adapter.backend == VIBE


def test_vibe_launch_backend_id_matches() -> None:
    """The adapter's backend id is the vocabulary constant, not a literal."""
    assert VibeLaunch.backend == sdk_backends.ACP_BACKEND_VIBE


# ── The routing state ──


def test_vibe_routing_is_unverified_not_absent() -> None:
    """UNVERIFIED is a declared state, not a missing row.

    A ``.get(backend, Routing.UNVERIFIED)`` read is deliberate: an id the table
    does not name fails closed rather than inheriting a neighbour's mechanism.
    Vibe IS named, with UNVERIFIED, so the refusal message names the routing state
    rather than reading as "unknown backend".
    """
    assert VIBE in sdk_backends.ACP_BACKEND_ROUTING
    assert sdk_backends.ACP_BACKEND_ROUTING[VIBE] is sdk_backends.Routing.UNVERIFIED


def test_vibe_has_no_permission_setting_or_config() -> None:
    """No seeded setting or config option is declared for Vibe.

    Until routing is verified, there is no ``(setting_key, value)`` pair or
    ``(option_id, value)`` pair to seed. Declaring one would be a claim the wire
    does not support.
    """
    assert sdk_backends.permission_setting_for(VIBE) == ("", "")
    assert sdk_backends.permission_config_for(VIBE) == ("", "")


def test_vibe_is_not_in_enforced_routings() -> None:
    """UNVERIFIED is not in ENFORCED_ROUTINGS; the tool gate does not enforce it."""
    assert sdk_backends.routing_for(VIBE) not in gate.ENFORCED_ROUTINGS


# ── Capability set containment ──


def test_every_capability_set_is_a_subset_of_known_backends() -> None:
    """H8: every capability set is a subset of ACP_BACKENDS_KNOWN."""
    known = sdk_backends.ACP_BACKENDS_KNOWN
    for name in dir(sdk_backends):
        if not name.startswith("ACP_BACKENDS_"):
            continue
        value = getattr(sdk_backends, name)
        if isinstance(value, frozenset):
            assert value <= known, f"{name} has members outside ACP_BACKENDS_KNOWN: {value - known}"


def test_vibe_in_session_mcp_array_is_a_subset_of_known() -> None:
    """The session MCP array set contains Vibe and stays within known backends."""
    assert VIBE in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY <= sdk_backends.ACP_BACKENDS_KNOWN


def test_vibe_in_acp_client_spawnable_is_a_subset_of_known() -> None:
    """The client-spawnable set contains Vibe and stays within known backends."""
    assert VIBE in sdk_backends.ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
    assert sdk_backends.ACP_BACKENDS_ACP_CLIENT_SPAWNABLE <= sdk_backends.ACP_BACKENDS_KNOWN


def test_vibe_in_independent_setup_is_a_subset_of_known() -> None:
    """The independent-setup set contains Vibe and stays within known backends."""
    assert VIBE in sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP
    assert sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP <= sdk_backends.ACP_BACKENDS_KNOWN


# ── Re-exports ──


def test_vibe_is_reexported_from_acp_backends_shim() -> None:
    """The shim re-exports the vocabulary so existing call sites keep working."""
    assert acp_backends.ACP_BACKEND_VIBE == "vibe"


def test_vibe_is_reexported_from_acp_types() -> None:
    """``acp.types`` re-exports the vocabulary for existing importers."""
    from kiro_crew.acp.types import ACP_BACKEND_VIBE as VIBE_FROM_TYPES

    assert VIBE_FROM_TYPES == "vibe"
