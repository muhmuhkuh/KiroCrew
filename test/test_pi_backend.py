"""Focused regression tests for the optional Pi ACP backend."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from kiro_crew.acp.client import PROTOCOL_VERSION_CLAUDE, AcpClient, AcpError
from kiro_crew.acp.types import ACP_BACKEND_PI, JsonRpcMessage
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import (
    _advertised_pi_models,
    _parse_pi_model_list,
)
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG
from kiro_crew.pi_support import (
    _pi_mcp_config,
    _pi_thinking_levels,
    _prompt_path,
    _record_pi_models,
    prepare_pi_environment,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager, _provider_label
from kiro_crew.session_map import SessionMap


@pytest.mark.asyncio
async def test_pi_uses_standard_acp_protocol_version(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._session_id = "session-1"
    sent: dict = {}

    async def send(method, params):
        if method == "initialize":
            sent.update(params)
        return 1

    async def wait(_request_id, timeout=0):
        return {"protocolVersion": PROTOCOL_VERSION_CLAUDE, "agentCapabilities": {}}

    client._send_request = send  # type: ignore[assignment]
    client._wait_for_response = wait  # type: ignore[assignment]
    await client._initialize_session()

    assert sent["protocolVersion"] == 1


def test_pi_startup_prelude_is_not_returned_as_assistant_text(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._store_session_config({"_meta": {"piAcp": {"startupInfo": "adapter startup metadata"}}})
    message = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "adapter startup metadata"},
                }
            },
        }
    )

    text, is_thinking = client._extract_text_chunk(message)
    assert text is None
    assert not is_thinking


def test_pi_permission_is_correlated_to_original_tool_call(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    tool_id = "call-123|fc_opaque"
    tool_message = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_id,
                    "title": "echo safe",
                    "kind": "execute",
                }
            },
        }
    )
    tool_event = client._extract_tool_event(tool_message)
    assert tool_event is not None
    client._observed_tool_calls[tool_id] = (tool_event.title, tool_event.tool_kind)

    permission = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "toolCallId": "pi-ui-confirm-1",
                    "title": "Run bash",
                    "kind": "other",
                    "rawInput": {
                        "message": f"kirocrew-tool-call:{tool_id}",
                    },
                },
                "options": [
                    {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                    {"optionId": "no", "name": "No", "kind": "reject_once"},
                ],
            },
        }
    )

    event = client._build_permission_event(permission)

    assert event.tool_call_id == tool_id
    assert event.tool_input == '{\n  "command": "echo safe"\n}'
    assert event.is_shell
    assert event.title == "echo safe"
    assert event.raw_tool_params == {"command": "echo safe"}

    with pytest.raises(AcpError, match="reused"):
        client._build_permission_event(permission)


def test_pi_permission_without_known_correlation_is_rejected(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    permission = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "toolCallId": "pi-ui-confirm-1",
                    "title": "Run bash",
                    "rawInput": {"message": "kirocrew-tool-call:unknown"},
                }
            },
        }
    )

    with pytest.raises(AcpError, match="unknown or reused"):
        client._build_permission_event(permission)


@pytest.mark.asyncio
async def test_pi_provider_uses_process_per_session_client():
    provider = AcpProvider(acp_backend=ACP_BACKEND_PI)
    provider.client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    provider._start_kiro_runtime = AsyncMock()  # type: ignore[method-assign]

    await provider.start()

    provider.client.ensure_ready.assert_awaited_once()
    provider._start_kiro_runtime.assert_not_awaited()
    assert not provider.is_session_sharing_eligible
    assert not provider.supports_steer


def test_pi_factory_keeps_native_model_id():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    cfg.agent.model = "anthropic/claude-sonnet-4-6"

    with patch("kiro_crew.model_registry.to_acp_id") as to_acp_id:
        provider = cfg.create_provider_factory()(session_key="dash:1")

    assert provider.client.backend == ACP_BACKEND_PI
    assert provider.client._model == "anthropic/claude-sonnet-4-6"
    to_acp_id.assert_not_called()


def test_pi_factory_keeps_effort_for_auto_model():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    cfg.agent.model = "auto"
    cfg.agent.reasoning_effort = "high"

    provider = cfg.create_provider_factory()(session_key="dash:1")

    assert provider._effort_per_model == {"auto": "high"}


def test_pi_disables_warm_pool_until_session_identity_is_known():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    cfg.session.pool_size = 2

    manager = SessionManager(cfg)

    assert manager._pool_size == 0


def test_pi_cli_model_table_is_converted_to_provider_qualified_rows():
    rows = _parse_pi_model_list(
        b"provider model context max-out thinking images\n"
        b"anthropic claude-sonnet-4-6 1M 128K yes yes\n"
        b"openai-codex gpt-5.6-sol 272K 128K yes yes\n"
    )

    assert rows == [
        {
            "model_name": "anthropic/claude-sonnet-4-6",
            "display_name": "anthropic/claude-sonnet-4-6",
            "description": "",
            "context_window_tokens": 1_000_000,
        },
        {
            "model_name": "openai-codex/gpt-5.6-sol",
            "display_name": "openai-codex/gpt-5.6-sol",
            "description": "",
            "context_window_tokens": 272_000,
        },
    ]


def test_pi_model_thinking_levels_follow_model_metadata():
    state = {
        "current_model": "openai-codex/gpt-5.6-luna",
        "models": [
            {
                "provider": "openai-codex",
                "id": "gpt-5.6-luna",
                "reasoning": True,
                "thinkingLevelMap": {
                    "minimal": "low",
                    "xhigh": "xhigh",
                    "max": "max",
                },
            }
        ],
    }

    assert _pi_thinking_levels(state) == [
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_pi_model_metadata_sidecar_preserves_level_map(tmp_path):
    state_path = tmp_path / "pi-state.json"
    _record_pi_models(
        state_path,
        [
            {
                "provider": "openai-codex",
                "id": "gpt-5.6-luna",
                "reasoning": True,
                "thinkingLevelMap": {"xhigh": "xhigh", "max": "max"},
            }
        ],
    )

    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError) as exc:
        pytest.fail(f"Pi metadata sidecar could not be read: {exc}")
    levels = _pi_thinking_levels(state, "openai-codex/gpt-5.6-luna")
    assert levels is not None
    assert levels[-1] == "max"


def test_pi_model_thinking_levels_omit_unadvertised_extensions():
    state = {
        "current_model": "openai-codex/gpt-5.5",
        "models": [
            {
                "provider": "openai-codex",
                "id": "gpt-5.5",
                "reasoning": True,
                "thinkingLevelMap": {"xhigh": "xhigh"},
            }
        ],
    }

    assert _pi_thinking_levels(state) == ["off", "minimal", "low", "medium", "high", "xhigh"]


def test_pi_model_list_ignores_sessions_from_other_backends():
    pi_provider = SimpleNamespace(
        is_pi_backend=True,
        available_models=lambda: [
            {"modelId": "provider/model", "name": "Model", "description": ""}
        ],
    )
    kiro_provider = SimpleNamespace(
        is_pi_backend=False,
        available_models=lambda: [{"modelId": "kiro-model", "name": "Kiro"}],
    )
    sessions = SimpleNamespace(active_providers=lambda: [kiro_provider, pi_provider])
    request = SimpleNamespace(app={"state": SimpleNamespace(sessions=sessions)})

    assert _advertised_pi_models(request) == [  # type: ignore[arg-type]
        {"model_name": "provider/model", "display_name": "Model", "description": ""}
    ]


def test_pi_session_ids_are_opaque_and_survive_pruning(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    session_map = SessionMap()
    session_map.set("dash:1", "opaque-pi-id", provider="pi")

    assert session_map.get("dash:1") == "opaque-pi-id"
    assert session_map.prune() == 0


def test_persisted_pi_session_survives_restart_and_pruning(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro-sessions")
    session_map = SessionMap()
    session_map.set("dashboard:chat-1", "opaque-pi-id", provider="pi", cwd=str(tmp_path))
    session_map.flush()

    restarted = SessionMap()
    assert restarted.prune() == 0
    assert restarted.get("dashboard:chat-1") == "opaque-pi-id"
    assert restarted.get_provider("dashboard:chat-1") == "pi"
    assert restarted.get_cwd("dashboard:chat-1") == str(tmp_path)


def test_pi_background_backend_does_not_inherit_kiro_runtime():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    manager = SessionManager(cfg)

    assert manager._configured_bg_backend_raw() == ACP_BACKEND_PI
    assert not manager._bg_backend_supports_runtime()


def test_provider_label_distinguishes_pi_from_kiro():
    assert _provider_label(AcpProvider(acp_backend=ACP_BACKEND_PI)) == "pi"
    assert _provider_label(AcpProvider()) == "acp"


def test_pi_mcp_config_exposes_only_direct_gated_tools():
    config = _pi_mcp_config(
        {
            "mcpServers": {
                "core": {
                    "command": "python",
                    "args": ["-m", "kiro_crew.mcp_core"],
                    "autoApprove": ["status"],
                }
            }
        }
    )

    assert config["settings"] == {"disableProxyTool": True, "scriptMode": False}
    assert config["mcpServers"]["core"] == {
        "command": "python",
        "args": ["-m", "kiro_crew.mcp_core"],
        "approveTools": False,
        "directTools": True,
    }


def test_pi_mcp_config_translates_disabled_tools():
    server = _pi_mcp_config(
        {
            "mcpServers": {
                "core": {
                    "command": "server",
                    "disabledTools": ["write"],
                    "excludeTools": ["delete", "write"],
                }
            }
        }
    )["mcpServers"]["core"]

    assert server["excludeTools"] == ["delete", "write"]
    assert "disabledTools" not in server


def test_pi_mcp_config_translates_kiro_oauth_shape():
    server = _pi_mcp_config(
        {
            "mcpServers": {
                "sharepoint": {
                    "url": "https://example.test/mcp",
                    "oauth": {
                        "clientId": "public-client",
                        "redirectUri": "http://localhost:7778/oauth/callback",
                    },
                    "oauthScopes": ["tools.readwrite", "offline_access"],
                }
            }
        }
    )["mcpServers"]["sharepoint"]

    assert server["oauth"] == {
        "clientId": "public-client",
        "redirectUri": "http://localhost:7778/oauth/callback",
        "scope": "tools.readwrite offline_access",
    }
    assert "oauthScopes" not in server


@patch("kiro_crew.agent.ensure_agent_materialized")
def test_pi_default_load_merges_assigned_servers_from_materialized_spec(mock_ensure, tmp_path):
    from kiro_crew import pi_support

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kirocrew-core": {"command": "stale-core"},
                    "sharepoint": {"url": "https://example.test/mcp"},
                    "unassigned": {"url": "https://unused.example/mcp"},
                },
                "tools": ["@kirocrew-core", "@sharepoint"],
            }
        )
    )

    with (
        patch("kiro_crew.pi_support.kiro_agents_dir", return_value=agent_dir),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={
                "prompt": "file:///default-prompt.md",
                "mcpServers": {"kirocrew-core": {"command": "fresh-core"}},
            },
        ),
    ):
        spec = pi_support._load_agent_spec("kirocrew")

    mock_ensure.assert_called_once_with("kirocrew")
    assert spec["mcpServers"]["kirocrew-core"]["command"] == "fresh-core"
    assert spec["mcpServers"]["sharepoint"] == {"url": "https://example.test/mcp"}
    assert "unassigned" not in spec["mcpServers"]


@pytest.mark.asyncio
async def test_pi_cleanup_uses_adapter_session_delete():
    provider = AcpProvider(acp_backend=ACP_BACKEND_PI)
    client = provider.client
    assert isinstance(client, AcpClient)

    with patch.object(client, "delete_session", new_callable=AsyncMock) as delete_session:
        await provider.cleanup_session("opaque-pi-id")

    delete_session.assert_awaited_once_with("opaque-pi-id")


def test_pi_client_prefers_model_metadata_over_legacy_adapter_levels(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._model = "openai-codex/gpt-5.6-luna"
    state_path = tmp_path / "pi-state.json"
    client._pi_state_path = state_path
    state_path.write_text(
        json.dumps(
            {
                "current_model": "openai-codex/gpt-5.6-luna",
                "models": [
                    {
                        "provider": "openai-codex",
                        "id": "gpt-5.6-luna",
                        "reasoning": True,
                        "thinkingLevelMap": {"xhigh": "xhigh", "max": "max"},
                    }
                ],
            }
        )
    )
    client._acp_config_options = [
        {"id": "thought_level", "options": [{"value": "xhigh"}]},
    ]

    assert client.get_valid_effort_levels()[-1] == "max"


@pytest.mark.asyncio
async def test_pi_max_effort_uses_adapter_compatibility_value(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._session_id = "pi-session"
    effort_path = tmp_path / "pi-effort"
    client._pi_effort_path = effort_path
    sent: dict = {}

    async def send(_method, params):
        sent.update(params)
        return 1

    client._send_request = send  # type: ignore[assignment]
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.set_config_option("thought_level", "max")

    assert sent["value"] == "xhigh"
    assert effort_path.read_text() == "max\n"


def test_pi_provider_and_qualified_models_are_editable():
    assert _EDITABLE_CONFIG["agent.provider"]["values"] == ["acp"]
    for key in ("agent.model", "agent.role_models.background", "agent.role_models.subagent"):
        assert re.fullmatch(_EDITABLE_CONFIG[key]["pattern"], "provider/model")


@patch("kiro_crew.agent.ensure_agent_materialized")
def test_load_agent_spec_falls_back_to_default_prompt_for_empty_prompt(mock_ensure, tmp_path):
    from kiro_crew import pi_support

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "kirocrew-lite.json").write_text(
        json.dumps({"name": "kirocrew-lite", "model": "auto", "prompt": ""})
    )
    with (
        patch("kiro_crew.pi_support.kiro_agents_dir", return_value=agent_dir),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={"prompt": "file:///default/prompt.md"},
        ),
    ):
        spec = pi_support._load_agent_spec("kirocrew-lite")
    assert spec["prompt"] == "file:///default/prompt.md"


def test_inline_agent_prompt_is_materialized_for_pi(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.pi_support.tempfile.gettempdir", lambda: str(tmp_path))

    path = _prompt_path({"prompt": "inline discovery instructions"})

    assert path.read_text(encoding="utf-8") == "inline discovery instructions"
    assert path.parent == tmp_path / "kirocrew-pi-prompts"


def test_prepare_pi_environment_wires_prompt_mcp_and_launcher(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("You are KiroCrew.")
    adapter = tmp_path / "adapter.ts"
    adapter.write_text("export default () => {}")
    mcp_config = tmp_path / "mcp.json"
    launcher = tmp_path / "kirocrew-pi"
    env: dict[str, str] = {}

    with (
        patch("kiro_crew.pi_support._load_agent_spec", return_value={"prompt": str(prompt)}),
        patch("kiro_crew.pi_support.resolve_pi_mcp_adapter", return_value=adapter),
        patch("kiro_crew.pi_support._write_mcp_config", return_value=mcp_config),
        patch("kiro_crew.pi_support._write_pi_launcher", return_value=launcher),
        patch("kiro_crew.pi_support._resolve_pi_bin", return_value="/bin/pi"),
        patch("kiro_crew.pi_support.resolve_pi_mode_resources"),
    ):
        prepare_pi_environment(env, agent="kirocrew", session_key="dash:1")

    assert env["MCP_UI_VIEWER"] == "none"
    assert env["PI_ACP_PI_COMMAND"] == str(launcher)
    assert env["KIROCREW_PI_BIN"] == "/bin/pi"
    assert env["KIROCREW_PI_PROMPT"] == str(prompt)
    assert env["KIROCREW_PI_MCP_CONFIG"] == str(mcp_config)
    assert env["KIROCREW_PI_MCP_ADAPTER"] == str(adapter)


def test_pi_rpc_proxy_keeps_trusted_extensions(tmp_path, monkeypatch):
    from kiro_crew import pi_support

    prompt = tmp_path / "prompt.md"
    prompt.write_text("You are KiroCrew.")
    adapter = tmp_path / "adapter.ts"
    adapter.write_text("export default () => {}")
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text("{}")
    state = tmp_path / "state.json"
    effort = tmp_path / "effort"
    captured: dict[str, object] = {}

    def fake_proxy(pi_bin, args, state_path, effort_path):
        captured.update(
            pi_bin=pi_bin,
            args=args,
            state_path=state_path,
            effort_path=effort_path,
        )
        return 0

    monkeypatch.setenv("KIROCREW_PI_PROMPT", str(prompt))
    monkeypatch.setenv("KIROCREW_PI_MCP_CONFIG", str(mcp_config))
    monkeypatch.setenv("KIROCREW_PI_MCP_ADAPTER", str(adapter))
    monkeypatch.setenv("KIROCREW_PI_STATE", str(state))
    monkeypatch.setenv("KIROCREW_PI_EFFORT", str(effort))
    monkeypatch.setattr(pi_support, "_resolve_pi_bin", lambda: "/bin/pi")
    monkeypatch.setattr(pi_support, "_run_pi_rpc_proxy", fake_proxy)
    modes = (tmp_path / "ponytail.js", tmp_path / "caveman.ts", tmp_path / "skills")
    monkeypatch.setattr(pi_support, "resolve_pi_mode_resources", lambda: modes)
    monkeypatch.setattr(pi_support.sys, "argv", ["kirocrew-pi", "--mode", "rpc"])

    with pytest.raises(SystemExit) as raised:
        pi_support.main()

    assert raised.value.code == 0
    args = captured["args"]
    assert isinstance(args, list)
    assert "--no-extensions" in args
    assert args[args.index("--extension") + 1].endswith("config/pi-tool-gate.ts")
    assert str(adapter) in args
    assert str(mcp_config) in args
    assert str(prompt) in args
    assert [args[i + 1] for i, value in enumerate(args) if value == "--extension"] == [
        str(pi_support.Path(pi_support.__file__).parent / "config" / "pi-tool-gate.ts"),
        str(adapter),
        str(modes[0]),
        str(modes[1]),
    ]
    assert args[args.index("--skill") + 1] == str(modes[2])
    assert "--no-skills" in args


def test_pi_factory_omits_kiro_tool_search():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    with patch("kiro_crew.providers.acp.AcpProvider") as constructor:
        cfg.create_provider_factory()(session_key="dash:1")
    for key in ("tool_search", "tool_search_min_pct", "tool_search_min_tokens"):
        assert constructor.call_args.kwargs[key] is None


@pytest.mark.asyncio
async def test_pi_reload_keeps_warm_pool_disabled():
    import asyncio

    cfg = KiroCrewConfig()
    cfg.session.pool_size = 2
    manager = SessionManager(cfg)
    selected = KiroCrewConfig()
    selected.agent.acp_backend = ACP_BACKEND_PI
    selected.session.pool_size = 2
    with (
        patch.object(KiroCrewConfig, "load", return_value=selected),
        patch.object(manager, "start_pool", new_callable=AsyncMock),
    ):
        await asyncio.wait_for(manager.reload_provider_factory(), timeout=5)
    assert manager._pool_size == 0


@pytest.mark.asyncio
async def test_pi_catalog_uses_selected_backend():
    from kiro_crew.dashboard.handlers import agents

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    rows = [{"model_name": "example/native-model"}]
    with (
        patch.object(KiroCrewConfig, "load", return_value=cfg),
        patch.object(agents, "_pi_models_from_cli", new_callable=AsyncMock, return_value=rows),
    ):
        response = await agents.api_models(cast(web.Request, SimpleNamespace(app={})))
    assert response.status == 200
    assert response.text is not None
    assert json.loads(response.text) == rows


def test_pi_review_pool_identity_uses_selected_backend():
    from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_pool

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_PI
    with patch.object(KiroCrewConfig, "load", return_value=cfg):
        assert review_pool._configured_provider() == "pi"
