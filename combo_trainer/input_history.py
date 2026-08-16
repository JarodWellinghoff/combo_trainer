"""
input_history.py — Parsing/aggregation for the live input-history panel.

Qt-free by design, matching `motion_parser.py`/`profiles.py`/`cluster_layout.py`:
the actual "split into Motions vs Actions" logic is a pure function you can
unit-test without instantiating any Qt object; `input_history_panel.py` only
renders whatever this module hands it.

Task 1 (data parsing) lives here
---------------------------------
`InputThread` bundles each notable poll tick into one `FrameState` payload
(frame, direction, the tick's fired `InputEvent`s — see `input_engine.py`).
`HistoryRow.from_frame_state()` is the parser: it splits that payload into

  * `motions`  — which of Up/Down/Left/Right are held this tick (0-2 of
    them; opposing pairs cancel via SOCD before this ever runs, so a
    genuine 3-4-way conflict cannot reach here), and
  * `actions`  — the attack/mechanic `InputEvent`s that fired this tick
    (already pre-separated at the source: a direction never produces an
    `InputEvent`, so no filtering is needed here beyond passing them through).

`InputHistory` is the bounded live buffer the panel reads from — same
"bounded ring buffer" shape as `motion_parser.DirectionRingBuffer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .input_types import FrameState, InputEvent
from .profiles import Direction

#: numpad 1-9 -> which cardinals are held. 5 (neutral) and unmapped values -> ().
_NUMPAD_TO_CARDINALS: dict[int, tuple[Direction, ...]] = {
    1: (Direction.DOWN, Direction.LEFT),
    2: (Direction.DOWN,),
    3: (Direction.DOWN, Direction.RIGHT),
    4: (Direction.LEFT,),
    5: (),
    6: (Direction.RIGHT,),
    7: (Direction.UP, Direction.LEFT),
    8: (Direction.UP,),
    9: (Direction.UP, Direction.RIGHT),
}


def numpad_to_cardinals(direction: int) -> tuple[Direction, ...]:
    """Decompose a SOCD-cleaned numpad direction back into 0-2 cardinals."""
    return _NUMPAD_TO_CARDINALS.get(direction, ())


@dataclass(frozen=True)
class HistoryRow:
    """
    One tick's worth of live input, already split for the dual-column panel:
    `motions` is the LEFT column's content, `actions` is the RIGHT column's.

    Both may be empty (e.g. a row that's purely a direction edge has no
    actions; a row that's purely a button press with no direction held has
    no motions) — the panel gives each column fixed structural space
    regardless, so rows stay aligned whichever one is empty.
    """

    frame: int
    t_monotonic: float
    direction: int
    motions: tuple[Direction, ...]
    actions: tuple[InputEvent, ...]

    @property
    def is_empty(self) -> bool:
        return not self.motions and not self.actions

    @classmethod
    def from_frame_state(cls, state: FrameState) -> "HistoryRow":
        """THE parser: split one bundled tick payload into its Motions half
        (directional) and Actions half (attacks/mechanics)."""
        return cls(
            frame=state.frame,
            t_monotonic=state.t_monotonic,
            direction=state.direction,
            motions=numpad_to_cardinals(state.direction),
            actions=state.events,
        )


@dataclass
class InputHistory:
    """Bounded, newest-last buffer of `HistoryRow`s — the panel's data model.

    Qt-free: the widget only ever reads `.rows` after a `push()`/`clear()`
    and repaints; it holds no aggregation logic of its own.
    """

    maxlen: int = 64
    rows: list[HistoryRow] = field(default_factory=list)

    def push(self, state: FrameState) -> HistoryRow:
        row = HistoryRow.from_frame_state(state)
        self.rows.append(row)
        if len(self.rows) > self.maxlen:
            del self.rows[: len(self.rows) - self.maxlen]
        return row

    def clear(self) -> None:
        self.rows.clear()

    @property
    def newest_first(self) -> Sequence[HistoryRow]:
        """Convenience for a "newest at top" renderer."""
        return list(reversed(self.rows))
