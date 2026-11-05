"""Shared ACP sessions publish their advertised model catalog to the picker cache."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, model_registry_namespace


@pytest.mark.asyncio
async def test_advertised_models_are_cached_and_persisted_once(monkeypatch):
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._runtime = SimpleNamespace(acp_backend=ACP_BACKEND_CODEX)
    handle._available_models = [{"modelId": "available-test-model", "name": "Available"}]
    handle._advertised_models_changed = False
    persist = Mock()
    monkeypatch.setattr(model_registry, "persist_advertised_models", persist)

    handle._feed_advertised_model_cache()
    assert model_registry.advertised_models(model_registry_namespace(ACP_BACKEND_CODEX)) == [
        "available-test-model"
    ]
    await handle._persist_advertised_models_if_changed()
    await handle._persist_advertised_models_if_changed()
    persist.assert_called_once_with()
