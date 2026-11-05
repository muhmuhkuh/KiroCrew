"""Consolidation snapshots traverse the semantic store in bounded keyset pages."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from kiro_crew.history_consolidation_memory import _semantic_snapshot_in_pages
from kiro_crew.vector_memory import VectorMemoryStore


def test_snapshot_pages_cover_each_active_row_once(tmp_path: Path, opened):
    store = opened(VectorMemoryStore(db_path=tmp_path / "memory.db"))
    store.init()
    for key in ("pref.echo", "pref.delta", "pref.charlie", "pref.bravo", "pref.alpha"):
        assert store.set_semantic(key, key, 0.9, "user_explicit") is None
    assert store.delete_semantic("pref.charlie", "user_explicit")

    snapshot = _semantic_snapshot_in_pages(store, page=2)

    assert snapshot == store.get_all_semantic()
    assert [row["key"] for row in snapshot] == [
        "pref.alpha",
        "pref.bravo",
        "pref.delta",
        "pref.echo",
    ]
    assert [row["key"] for row in store.get_all_semantic(limit=2, after_key="pref.bravo")] == [
        "pref.delta",
        "pref.echo",
    ]


def test_snapshot_stops_when_a_store_does_not_advance_the_cursor():
    rows = [{"key": "pref.alpha"}, {"key": "pref.bravo"}]
    store = Mock(spec=VectorMemoryStore)
    store.get_all_semantic.return_value = rows

    assert _semantic_snapshot_in_pages(store, page=2) == rows
    assert store.get_all_semantic.call_count == 2
