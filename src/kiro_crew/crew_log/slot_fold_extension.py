"""Check whether a slot fold can continue over appended units."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.crew_log.projection_slot_memo import _SlotMemo, _UnitMark


def _extended(units: "tuple[str, ...]", marks: "tuple[_UnitMark, ...]", memo: "_SlotMemo") -> bool:
    """Whether *units* is *memo*'s unit list with new units APPENDED, and nothing else moved.

    The shape a slot takes when its session rolls over: a new unit opens after the one
    the cell last folded. The cell's state was folded through every unit it holds, so
    folding the rest of its newest unit and then each new unit from its first record,
    in order, is the same pass a cold fold makes over the longer list -- without
    re-reading the units it already folded.

    The test is :func:`_continuable`'s, applied to the held prefix: every earlier unit
    equal as a whole mark, the held newest unit the same log (it may have grown, or
    not), every unit's identity known. A unit inserted anywhere but the end, a held
    unit dropped or reordered, or an earlier unit that moved is not this shape and
    folds cold. The held newest unit's folded records are proven separately, by
    :func:`_folded_prefix_holds`, exactly as a same-list continuation proves them.
    """
    held_units, held = memo.units, memo.marks
    count = len(held_units)
    return (
        count > 0
        and len(held) == count
        and len(units) > count
        and len(marks) == len(units)
        and units[:count] == held_units
        and all(mark.origin is not None for mark in marks)
        and marks[: count - 1] == held[: count - 1]
        and marks[count - 1].origin == held[-1].origin
        and marks[count - 1].last_seq >= held[-1].last_seq
    )
