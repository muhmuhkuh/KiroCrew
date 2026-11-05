"""Bound display labels from a backend model catalog without selecting a model."""

from __future__ import annotations

from collections.abc import Sequence

ADVERTISED_LABEL_MAX_CHARS = 400


def _admit_label_rows(rows: object, kept_ids: Sequence[str]) -> dict[str, dict[str, str]]:
    """Admit ``{id: {"name", "description"}}`` label rows for already-kept ids.

    Labels are advisory: they decorate the id list the picker already trusts, so
    this keeps a label ONLY for an id that passed :func:`_admit_ids` (``kept_ids``)
    and silently drops anything else -- a label for a refused/unknown id, or a
    non-dict row. Each field is coerced to ``str`` and clamped to
    ``ADVERTISED_LABEL_MAX_CHARS``; an empty ``name`` AND empty ``description``
    yields no entry (nothing to show). Never raises.
    """
    if not isinstance(rows, dict):
        return {}
    kept = set(kept_ids)
    out: dict[str, dict[str, str]] = {}
    for model_id, row in rows.items():
        if model_id not in kept or not isinstance(row, dict):
            continue
        name = str(row.get("name", "") or "")[:ADVERTISED_LABEL_MAX_CHARS]
        description = str(row.get("description", "") or "")[:ADVERTISED_LABEL_MAX_CHARS]
        if not name and not description:
            continue
        out[model_id] = {"name": name, "description": description}
    return out


def _payload_labels(rows: Sequence[object]) -> dict[str, dict[str, object]]:
    """``{id: {"name", "description"}}`` from a raw ``availableModels`` payload."""
    raw: dict[str, dict[str, object]] = {}
    for row in rows:
        if isinstance(row, dict):
            model_id = row.get("modelId") or row.get("value") or ""
            if isinstance(model_id, str) and model_id.strip():
                raw[model_id] = {"name": row.get("name"), "description": row.get("description")}
    return raw
