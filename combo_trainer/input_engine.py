"""
input_engine.py — Dedicated XInput polling thread.

Runs a ~250 Hz loop on a QThread, completely independent of Qt's event loop
and window focus. On every attack-button rising edge it:

  1. stamps the press with the current video frame (FrameClock),
  2. runs the motion parser over the direction ring buffer,
  3. emits a finished InputEvent via a queued Qt signal.

Pad selection: by default the thread auto-locks to the first connected
XInput slot; the controller-test dialog can pin a specific slot with
`set_pad_index(0..3)` or return to auto with `set_pad_index(None)`.

Monitor mode: while the controller-test dialog is open it calls
`set_monitoring(True)`, and the thread additionally emits ~30 Hz
PadSnapshot lists for ALL four slots so the dialog can render live state
without touching XInput from a second thread.

D-pad only (leverless-friendly), with configurable SOCD cleaning.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QThread, pyqtSignal

from .frame_clock import FrameClock
from .models import Button, Motion
from .motion_parser import (
    SOCD_NEUTRAL,
    DirectionRingBuffer,
    ParserConfig,
    clean_socd,
    mirror_direction,
    parse_motion,
)

try:  # XInput-Python is Windows-only; keep the module importable elsewhere.
    import XInput  # type: ignore
except Exception:  # pragma: no cover
    XInput = None


DEFAULT_BUTTON_MAP: dict[str, Button] = {
    "X": Button.LIGHT,
    "Y": Button.MEDIUM,
    "B": Button.HEAVY,
    "A": Button.SPECIAL,
    "LEFT_SHOULDER": Button.ASSIST1,
    "RIGHT_SHOULDER": Button.ASSIST2,
    "RIGHT_TRIGGER": Button.TAG,
    "LEFT_TRIGGER": Button.THROW,
}
TRIGGER_THRESHOLD = 0.5
POLL_INTERVAL_S = 0.004  # ~250 Hz
MONITOR_INTERVAL_S = 1 / 30  # snapshot rate for the test dialog
NUM_SLOTS = 4


@dataclass(frozen=True)
class InputEvent:
    """A confirmed attack-button press, fully resolved."""

    frame: int
    button: Button
    motion: Motion  # Motion.NONE == bare button / Quick Special
    direction: int  # numpad direction held at press time
    t_monotonic: float


@dataclass(frozen=True)
class PadSnapshot:
    """Raw live state of one XInput slot, for the controller-test dialog."""

    index: int
    connected: bool
    buttons: dict[str, bool] = field(default_factory=dict)
    lt: float = 0.0
    rt: float = 0.0
    direction: int = 5  # SOCD-cleaned numpad (facing mirror NOT applied)

    @property
    def any_pressed(self) -> bool:
        return (
            any(self.buttons.values())
            or self.lt >= TRIGGER_THRESHOLD
            or self.rt >= TRIGGER_THRESHOLD
            or self.direction != 5
        )


class InputThread(QThread):
    input_event = pyqtSignal(object)  # InputEvent
    direction_changed = pyqtSignal(int)  # numpad dir (facing-mirrored)
    connected_changed = pyqtSignal(bool)
    active_pad_changed = pyqtSignal(int)  # slot index, -1 = none
    monitor_state = pyqtSignal(object)  # list[PadSnapshot], all 4 slots
    error = pyqtSignal(str)

    def __init__(
        self,
        frame_clock: FrameClock,
        button_map: dict[str, Button] | None = None,
        socd_mode: str = SOCD_NEUTRAL,
        parser_config: ParserConfig = ParserConfig(),
    ) -> None:
        super().__init__()
        self._clock = frame_clock
        self._button_map = dict(button_map or DEFAULT_BUTTON_MAP)
        self._socd_mode = socd_mode
        self._parser_config = parser_config
        self._buffer = DirectionRingBuffer(maxlen=64)
        self._running = False
        # Flags below are single-word writes from the GUI thread: no lock needed.
        self._facing_right = True
        self._monitoring = False
        self._pad_override: int | None = None
        self._last_monitor = 0.0

    # -- external control (GUI thread) ---------------------------------------

    def stop(self) -> None:
        self._running = False
        self.wait(1000)

    def set_facing_right(self, facing_right: bool) -> None:
        self._facing_right = facing_right

    def set_monitoring(self, enabled: bool) -> None:
        self._monitoring = enabled

    def set_pad_index(self, index: int | None) -> None:
        """Pin a specific XInput slot (0-3), or None for auto (first found)."""
        self._pad_override = index

    @property
    def button_map(self) -> dict[str, Button]:
        return dict(self._button_map)

    @property
    def available(self) -> bool:
        return XInput is not None

    # -- pad selection --------------------------------------------------------

    @staticmethod
    def _select_pad(connected: tuple, override: int | None) -> int | None:
        if override is not None:
            ok = 0 <= override < len(connected) and connected[override]
            return override if ok else None
        return next((i for i, ok in enumerate(connected) if ok), None)

    # -- monitor snapshots ------------------------------------------------------

    def _maybe_emit_monitor(self, now: float) -> None:
        if now - self._last_monitor < MONITOR_INTERVAL_S:
            return
        self._last_monitor = now
        snaps: list[PadSnapshot] = []
        try:
            connected = XInput.get_connected()
        except Exception:
            connected = (False,) * NUM_SLOTS
        for i in range(NUM_SLOTS):
            if not (i < len(connected) and connected[i]):
                snaps.append(PadSnapshot(index=i, connected=False))
                continue
            try:
                state = XInput.get_state(i)
                buttons = XInput.get_button_values(state)
                lt, rt = XInput.get_trigger_values(state)
                direction = clean_socd(
                    up=buttons.get("DPAD_UP", False),
                    down=buttons.get("DPAD_DOWN", False),
                    left=buttons.get("DPAD_LEFT", False),
                    right=buttons.get("DPAD_RIGHT", False),
                    mode=self._socd_mode,
                )
                snaps.append(PadSnapshot(i, True, dict(buttons), lt, rt, direction))
            except Exception:
                snaps.append(PadSnapshot(index=i, connected=False))
        self.monitor_state.emit(snaps)

    # -- thread body ----------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - hot loop, kept flat on purpose
        if XInput is None:
            self.error.emit(
                "XInput-Python is unavailable on this platform. "
                "Controller input disabled."
            )
            return

        self._running = True
        pad: int | None = None
        last_scan = 0.0
        last_packet = -1
        prev_pressed: dict[str, bool] = {}

        while self._running:
            now = time.monotonic()

            if self._monitoring:
                self._maybe_emit_monitor(now)

            # -- (re)select the active pad -------------------------------------
            override = self._pad_override
            override_mismatch = override is not None and pad != override
            if pad is None or override_mismatch:
                if override_mismatch or now - last_scan >= 0.5:
                    last_scan = now
                    try:
                        connected = XInput.get_connected()
                    except Exception:
                        connected = (False,) * NUM_SLOTS
                    new_pad = self._select_pad(connected, override)
                    if new_pad != pad:
                        pad = new_pad
                        last_packet = -1
                        prev_pressed.clear()
                        self._buffer.clear()
                        self.connected_changed.emit(pad is not None)
                        self.active_pad_changed.emit(pad if pad is not None else -1)
                if pad is None:
                    time.sleep(0.05)
                    continue

            try:
                state = XInput.get_state(pad)
            except Exception:
                pad = None
                self.connected_changed.emit(False)
                self.active_pad_changed.emit(-1)
                continue

            if state.dwPacketNumber == last_packet:
                time.sleep(POLL_INTERVAL_S)
                continue
            last_packet = state.dwPacketNumber

            buttons = XInput.get_button_values(state)
            lt, rt = XInput.get_trigger_values(state)

            # -- directions (d-pad only, SOCD-cleaned, facing-mirrored) --------
            direction = clean_socd(
                up=buttons.get("DPAD_UP", False),
                down=buttons.get("DPAD_DOWN", False),
                left=buttons.get("DPAD_LEFT", False),
                right=buttons.get("DPAD_RIGHT", False),
                mode=self._socd_mode,
            )
            if not self._facing_right:
                direction = mirror_direction(direction)
            if self._buffer.push(now, direction):
                self.direction_changed.emit(direction)

            # -- attack buttons: rising edges only (no negative edge in Tōkon) -
            pressed_now: dict[str, bool] = {
                name: bool(buttons.get(name, False))
                for name in self._button_map
                if not name.endswith("_TRIGGER")
            }
            pressed_now["LEFT_TRIGGER"] = lt >= TRIGGER_THRESHOLD
            pressed_now["RIGHT_TRIGGER"] = rt >= TRIGGER_THRESHOLD

            for name, mapped in self._button_map.items():
                if pressed_now.get(name, False) and not prev_pressed.get(name, False):
                    frame = self._clock.current_frame()
                    motion = parse_motion(
                        self._buffer.samples, now, self._parser_config
                    )
                    self.input_event.emit(
                        InputEvent(
                            frame=frame,
                            button=mapped,
                            motion=motion,
                            direction=direction,
                            t_monotonic=now,
                        )
                    )
            prev_pressed = pressed_now

            time.sleep(POLL_INTERVAL_S)
