"""Immutable checkpoint witnesses and the result of a resumable load."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.crew_log.prefix_extent import PrefixExtent
    from kiro_crew.crew_log.projection_session import SessionProjections
    from kiro_crew.crew_log.store_results import ResumePoint


class DiskResume(NamedTuple):
    """What :func:`load_resumable` read: the bundle, and how its tail read may start.

    ``points`` maps a checkpoint seq to the byte position its savepoint stored for the
    record at that seq, for the tail read to seek to. ``owes_point`` is true when some
    resumed file stored none, so the next pass should write one even though it has not
    advanced far enough to earn a write on entry count alone -- without that, a session
    resumed from a file written before positions existed decodes its whole log on every
    restart until it grows by :data:`MIN_ADVANCE_ENTRIES`.
    """

    bundle: "SessionProjections | None"
    points: "dict[int, ResumePoint]"
    owes_point: bool


class PrefixWitness(NamedTuple):
    """A digest of the raw records through *seq*, read at one known moment.

    :func:`save` persists this value instead of a digest of its own, so what a
    savepoint certifies are the bytes the fold that produced its state read.
    """

    seq: int
    records: int
    sha: str
    #: Where the digested prefix ENDS on disk, so a resume can settle it without
    #: hashing it again (``prefix_extent.extent_holds``). ``None`` costs that resume the digest.
    extent: "PrefixExtent | None" = None
    #: As :attr:`CutWitness.resume`. On a log with no cut counter the store does not
    #: seek to it today (it has no evidence for the bytes below it); it is stored so a
    #: build that can vouch for that prefix another way has the position to hand.
    resume: "ResumePoint | None" = None


class CutWitness(NamedTuple):
    """The unit's cut count when the records through *seq* were folded.

    What :class:`PrefixWitness` proves, proved by a COUNT instead. A cut is the only
    mutation this store performs on a committed record and the count rises durably
    before each one, so the same count again means those records are the same bytes.

    It costs a few bytes to read where the digest costs a walk of the whole consumed
    prefix -- and it costs them on the WRITE side too, which is where a savepoint's own
    share of the saving is: a digest witness has to resolve a record boundary by
    decoding every record up to it, every time one is written.
    """

    seq: int
    cuts: int
    #: The byte position of the record numbered *seq*, from the pass that folded it, so
    #: the tail read after a resume opens the file there instead of decoding every
    #: record below it. Checked by the store before use (``CrewLog.iter_from``), so a
    #: stale or forged one costs a full walk and never a wrong answer.
    resume: "ResumePoint | None" = None
    #: As :attr:`PrefixWitness.extent`: what a resume checks besides the count, so a
    #: segment replaced or rewritten in place by something other than the store is
    #: refused rather than resumed over.
    extent: "PrefixExtent | None" = None


for _record in (DiskResume, PrefixWitness, CutWitness):
    _record.__module__ = "kiro_crew.crew_log.checkpoint"
