from kiro_crew import agent_capabilities


def test_member_platform_server_refresh_and_retraction(monkeypatch):
    server = "kirocrew-mate"
    monkeypatch.setattr(agent_capabilities, "_parent_is_owned", lambda snap: False)
    monkeypatch.setattr(
        agent_capabilities,
        "_member_platform_transport",
        lambda name: {"command": "current", "args": ["serve"], "disabled": False},
    )
    snap = {"parent_spec": {"mcpServers": {}}}
    spec = {
        "mcpServers": {
            server: {"command": "stale", "disabled": False, "disabledTools": ["update_self"]}
        }
    }

    agent_capabilities._maintain_owned(snap, spec)

    assert spec["mcpServers"][server] == {
        "command": "current",
        "args": ["serve"],
        "disabledTools": ["update_self"],
    }
    assert "disabled" not in spec["mcpServers"][server]

    spec["mcpServers"][server]["disabled"] = True
    agent_capabilities._maintain_owned(snap, spec)
    assert spec["mcpServers"][server]["disabled"] is True

    monkeypatch.setattr(agent_capabilities, "_member_platform_transport", lambda name: None)
    agent_capabilities._maintain_owned(snap, spec)

    assert server not in spec["mcpServers"]
