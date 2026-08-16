"""
input_types.py — Plain data shapes produced by the input thread.

Qt-free, matching `models.py`/`motion_parser.py`/`profiles.py`: none of
`InputEvent`, `FrameState`, or `PadSnapshot` touch PyQt6, XInput, or the
thread itself — they're just dataclasses. They live apart from
`input_engine.py` (which imports PyQt6 for `QThread`) specifically so
consumers that only need the DATA — like `input_history.py`'s parser, or a
future unit test — aren't forced to have PyQt6 installed just to import a
dataclass. `input_engine.py` imports and re-exports all three, so existing
`from .input_engine import InputEvent` call sites elsewhere in the app are
unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Button, Motion

TRIGGER_THRESHOLD = 0.5


@dataclass(frozen=True)
class InputEvent:
    """A confirmed action-button press, fully resolved."""

    frame: int
    button: Button
    motion: Motion  # Motion.NONE == bare button / single-button motion input
    direction: int  # numpad direction held at press time
    t_monotonic: float
    logical_id: str = ""    # e.g. "QUICK_SKILL" — the game's own vocabulary
    logical_name: str = ""  # e.g. "Quick Skill" — display text
    source: str = ""        # raw XInput token that fired it
    macro: bool = False     # True when one switch fired several inputs
    command_normal: bool = False  # True when this switch also bound a direction

    @property
    def display_name(self) -> str:
        return self.logical_name or self.button.value


@dataclass(frozen=True)
class FrameState:
    """
    Everything that changed on ONE poll tick, bundled as a single payload.

    `InputEvent`/`direction_changed` exist for consumers that want a plain
    press stream or an edge-compressed direction feed. `FrameState` is for
    consumers that want "what was active THIS frame" as one object — the
    live input-history panel (`input_history.py`) is the first of these:
    `HistoryRow.from_frame_state()` is the Motions/Actions parser, and it
    parses exactly this shape, not two independently-timed signals it would
    otherwise have to correlate after the fact.

    Emitted only on ticks where something happened (a direction edge or at
    least one fired action) — see `InputThread.run` — so idle polling at
    250 Hz doesn't flood listeners.
    """

    frame: int
    t_monotonic: float
    direction: int  # numpad, SOCD-cleaned, facing-mirrored — same as InputEvent.direction
    events: tuple[InputEvent, ...] = ()  # actions that fired on this exact tick


@dataclass(frozen=True)
class PadSnapshot:
    """Raw live state of one XInput slot, for the controller/rebind dialogs."""

    index: int
    connected: bool
    buttons: dict[str, bool] = field(default_factory=dict)
    lt: float = 0.0
    rt: float = 0.0
    direction: int = 5  # SOCD-cleaned numpad (facing mirror NOT applied)

    def pressed(self, source: str) -> bool:
        """Uniform read for any XInput source, triggers included."""
        if source == "LEFT_TRIGGER":
            return self.lt >= TRIGGER_THRESHOLD
        if source == "RIGHT_TRIGGER":
            return self.rt >= TRIGGER_THRESHOLD
        return bool(self.buttons.get(source, False))

    @property
    def any_pressed(self) -> bool:
        return (
            any(self.buttons.values())
            or self.lt >= TRIGGER_THRESHOLD
            or self.rt >= TRIGGER_THRESHOLD
            or self.direction != 5
        )
