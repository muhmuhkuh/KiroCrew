"""Mistral Vibe, launched one process per session by ``AcpClient``.

Mistral Vibe serves ACP from its own binary (``vibe-acp``), a Python entry point
shipped by the ``mistral-vibe`` package. There is no npm adapter to resolve and no
Node floor to satisfy -- the published package ships the executable -- so its binary
comes off the shared self-served ladder
(:func:`kiro_crew.acp.launch.resolve_self_served_launch`), from its
``ACP_BACKEND_LAUNCH`` row.

What makes the launch this host's own is the permission routing. Vibe has built-in
agent profiles (``ask``, ``plan``, ``accept-edits``, ``auto-approve``) that decide
tool approval, but the ACP surface's ``session/set_mode`` support and the exact
mechanism to force the ``ask`` profile over ACP are not yet verified. Until routing
is established, sessions are refused (``Routing.UNVERIFIED``); see
``docs/system-specs/modules/harness-onboarding.md`` for the verification bar.

The API key reaches the child through the ``MISTRAL_API_KEY`` environment variable,
which Vibe reads from the inherited process environment above its own ``~/.vibe/.env``
file. Crew's host auth supplies this from its own vault.
"""

from __future__ import annotations

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import ACP_BACKEND_VIBE

__all__ = ["VibeLaunch"]

# The environment variable Vibe reads its Mistral API key from. Vibe resolves
# credentials from the inherited process environment first, above its own
# ``~/.vibe/.env`` file, so handing the key to the harness process is authoritative.
_ENV_MISTRAL_API_KEY = "MISTRAL_API_KEY"

# The environment variable that overrides the vibe-acp binary path.
_ENV_VIBE_ACP_BIN = "VIBE_ACP_BIN"

# The default agent profile Vibe starts with when no ``--agent`` is given. The
# ``ask`` profile requires approval for all tool executions, which is the posture
# Crew needs for its PreToolUse gate to run. This is not yet wired through the ACP
# spawn -- see the module docstring for why.
_VIBE_DEFAULT_AGENT = "ask"


class VibeLaunch(ProcessAdapter):
    """Mistral Vibe: a self-served binary launched one process per session."""

    backend = ACP_BACKEND_VIBE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, then mask."""
        session = ctx.session
        assert session is not None, "vibe is launched for one session"
        _vibe_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # Translate the agent spec into this session's MCP array HERE, for the
        # reason the claude and opencode adapters do it in their own launches: the
        # translation reads disk, and doing it at the shared session/new call site
        # would put an executor hop and a new failure mode on EVERY backend's
        # construction path, kiro-cli included (harness-parity H13).
        await session._prepare_session_mcp()
        # The same refuse-then-mask preflight the other enforced hosts run, keyed on
        # the same routing question rather than on this harness's identity. Vibe's
        # routing is UNVERIFIED today, so the preflight refuses the spawn rather
        # than masking -- which is the correct behaviour until routing is established.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Nothing beyond what the generic path already sets.

        The Mistral API key reaches the child through the inherited environment
        (``MISTRAL_API_KEY``), which Vibe resolves above its own credential files.
        Crew's host auth supplies this from its vault; no file write is needed.
        """
