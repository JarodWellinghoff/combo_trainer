"""
controllers.py — Record and Practice sinks for the InputEvent stream.

Both controllers are thin QObjects around pure logic so the matching rules
stay unit-testable. MainWindow routes InputEvents to whichever controller is
active for the current AppMode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from PyQt6.QtCore import QObject, pyqtSignal

from .input_engine import InputEvent
from .models import ComboFile, ComboInput, ComboManager, Judgement, Motion, TimingWindows


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------

class RecordController(QObject):
    input_recorded = pyqtSignal(object)  # ComboInput

    def __init__(self, combo_manager: ComboManager, parent: QObject | None = None):
        super().__init__(parent)
        self._cm = combo_manager

    def on_input(self, ev: InputEvent) -> None:
        entry = self._cm.add_input(frame=ev.frame, button=ev.button, motion=ev.motion)
        self.input_recorded.emit(entry)


# --------------------------------------------------------------------------
# Practice — pure matching core
# --------------------------------------------------------------------------

class NoteStatus(str, Enum):
    PENDING = "PENDING"
    PERFECT = "PERFECT"
    GOOD = "GOOD"
    MISS = "MISS"
    SKIPPED = "SKIPPED"  # jumped past via seek; excluded from stats

_JUDGED = {NoteStatus.PERFECT, NoteStatus.GOOD, NoteStatus.MISS}
_FROM_JUDGEMENT = {
    Judgement.PERFECT: NoteStatus.PERFECT,
    Judgement.GOOD: NoteStatus.GOOD,
    Judgement.MISS: NoteStatus.MISS,
}


def find_match(
    inputs: list[ComboInput],
    statuses: list[NoteStatus],
    ev_frame: int,
    ev_button,
    ev_motion: Motion,
    windows: TimingWindows,
    strict_motions: bool = True,
) -> int | None:
    """
    Return the index of the best pending note this press should judge,
    or None (stray press). Best = same button, within the GOOD window,
    minimal |frame delta|. If the note requires a motion and strict mode
    is on, the parsed motion must match (Quick-Special notes are motion
    NONE and match any bare press of the right button).
    """
    best_i: int | None = None
    best_d = 10 ** 9
    for i, note in enumerate(inputs):
        if note.frame - ev_frame > windows.good_frames:
            break  # inputs are sorted; everything further is too far ahead
        if statuses[i] is not NoteStatus.PENDING:
            continue
        d = abs(ev_frame - note.frame)
        if d > windows.good_frames:
            continue
        if note.button is not ev_button:
            continue
        if strict_motions and note.motion is not Motion.NONE and ev_motion is not note.motion:
            continue
        if d < best_d:
            best_i, best_d = i, d
    return best_i


@dataclass
class PracticeStats:
    perfect: int = 0
    good: int = 0
    miss: int = 0
    stray: int = 0

    @property
    def judged(self) -> int:
        return self.perfect + self.good + self.miss

    @property
    def accuracy(self) -> float:
        if not self.judged:
            return 0.0
        return (self.perfect + 0.5 * self.good) / self.judged


# --------------------------------------------------------------------------
# Practice controller
# --------------------------------------------------------------------------

class PracticeController(QObject):
    note_judged = pyqtSignal(int, object, int)   # note index, NoteStatus, delta frames
    stray_input = pyqtSignal(object)             # InputEvent
    stats_changed = pyqtSignal(object)           # PracticeStats
    run_finished = pyqtSignal(object)            # PracticeStats

    def __init__(
        self,
        windows: TimingWindows = TimingWindows(),
        strict_motions: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.windows = windows
        self.strict_motions = strict_motions
        self._combo: ComboFile | None = None
        self._statuses: list[NoteStatus] = []
        self._stats = PracticeStats()
        self._finished = False

    # -- session lifecycle ------------------------------------------------------

    def start(self, combo: ComboFile) -> None:
        self._combo = combo
        self._statuses = [NoteStatus.PENDING] * len(combo.inputs)
        self._stats = PracticeStats()
        self._finished = False
        self.stats_changed.emit(self._stats)

    def stop(self) -> None:
        self._combo = None
        self._statuses = []

    @property
    def active(self) -> bool:
        return self._combo is not None

    @property
    def stats(self) -> PracticeStats:
        return self._stats

    def status_at(self, index: int) -> NoteStatus:
        return self._statuses[index] if index < len(self._statuses) else NoteStatus.PENDING

    # -- event sinks ---------------------------------------------------------------

    def on_input(self, ev: InputEvent) -> None:
        if self._combo is None:
            return
        idx = find_match(
            self._combo.inputs, self._statuses,
            ev.frame, ev.button, ev.motion,
            self.windows, self.strict_motions,
        )
        if idx is None:
            self._stats.stray += 1
            self.stray_input.emit(ev)
            self.stats_changed.emit(self._stats)
            return
        delta = ev.frame - self._combo.inputs[idx].frame
        status = _FROM_JUDGEMENT[self.windows.classify(delta)]
        self._set_status(idx, status, delta)

    def on_frame(self, current_frame: int) -> None:
        """Called every UI tick during playback: expire unhit notes to MISS."""
        if self._combo is None:
            return
        deadline = current_frame - self.windows.good_frames
        for i, note in enumerate(self._combo.inputs):
            if note.frame >= deadline:
                break
            if self._statuses[i] is NoteStatus.PENDING:
                self._set_status(i, NoteStatus.MISS, delta=None)
        self._check_finished()

    def resync(self, current_frame: int) -> None:
        """
        After a seek: notes at/after the playhead become attemptable again;
        pending notes now behind the playhead are excluded (SKIPPED), so
        seeking never manufactures misses.
        """
        if self._combo is None:
            return
        for i, note in enumerate(self._combo.inputs):
            if note.frame >= current_frame - self.windows.good_frames:
                self._statuses[i] = NoteStatus.PENDING
            elif self._statuses[i] is NoteStatus.PENDING:
                self._statuses[i] = NoteStatus.SKIPPED
        self._recount()
        self._finished = False

    # -- internals ---------------------------------------------------------------------

    def _set_status(self, idx: int, status: NoteStatus, delta: int | None) -> None:
        self._statuses[idx] = status
        if status is NoteStatus.PERFECT:
            self._stats.perfect += 1
        elif status is NoteStatus.GOOD:
            self._stats.good += 1
        elif status is NoteStatus.MISS:
            self._stats.miss += 1
        self.note_judged.emit(idx, status, delta if delta is not None else 0)
        self.stats_changed.emit(self._stats)

    def _recount(self) -> None:
        self._stats = PracticeStats(
            perfect=sum(s is NoteStatus.PERFECT for s in self._statuses),
            good=sum(s is NoteStatus.GOOD for s in self._statuses),
            miss=sum(s is NoteStatus.MISS for s in self._statuses),
            stray=self._stats.stray,
        )
        self.stats_changed.emit(self._stats)

    def _check_finished(self) -> None:
        if self._finished or self._combo is None or not self._statuses:
            return
        if all(s is not NoteStatus.PENDING for s in self._statuses):
            self._finished = True
            self.run_finished.emit(self._stats)
