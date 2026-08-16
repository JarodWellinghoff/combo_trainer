"""
input_engine.py — Dedicated XInput polling thread.

Runs a ~250 Hz loop on a QThread, completely independent of Qt's event loop
and window focus. On every rising edge of a bound action button it:

  1. stamps the press with the current video frame (FrameClock),
  2. runs the motion parser over the direction ring buffer (unless the bound
     logical input is a single-button motion input, OR the switch is a
     Command Normal — see below),
  3. emits a finished InputEvent via a queued Qt signal — one per logical
     input, so a macro binding produces several events from one switch.

Command Normals (instant Direction + Attack)
---------------------------------------------
A switch bound to a direction AND an action together (e.g. Down + Heavy)
resolves as `ResolvedAction.command_normal=True` (`profiles.resolve()`). Two
things follow, both on the SAME poll tick as the switch's rising edge —
no timer, no lookback window:

  * `direction` already reads correctly. The direction pool is a plain
    per-cardinal OR over every source bound to that cardinal (see
    `_direction_from`), computed from `pressed_now` BEFORE the action loop
    below runs. A Command Normal's own switch is simply one more member of
    that OR, exactly like a second physical d-pad button, so it needs no
    special-casing here.
  * `motion` is forced to `Motion.NONE`, skipping `parse_motion()` entirely.
    This is the actual bug a Command Normal exposes: a plain attack button
    parses whatever the 300ms ring-buffer window happens to contain, which
    is exactly the "timers/sequence delays" a same-frame Command Normal must
    NOT depend on — the direction came from this exact press, there is
    nothing to infer.

Release is symmetric and needs no extra bookkeeping: the direction pool is
recomputed from scratch every poll tick, so releasing the switch just drops
it from that tick's OR — any OTHER switch still feeding the same cardinal
(a real d-pad Down held at the same time) keeps it live, and cardinals with
no more sources revert to neutral on the very next tick.

Rebinding
---------
The loop owns no mapping of its own. It reads a `ResolvedProfile`
(`profiles.py`) — a flat, immutable source→action table derived from the
active game profile + layout + device. The GUI thread rebinds by building a
new ResolvedProfile and calling `set_profile()`; that is a single attribute
assignment, atomic in CPython, so no lock is needed and the change takes
effect on the very next poll. The loop notices the swap by identity and
clears its edge state so a held button can't fire a phantom press under its
new binding.

Directions never travel the button path: they are OR-ed from whichever
sources the profile binds to each cardinal, SOCD-cleaned, then pushed to the
motion parser's ring buffer.

Per-tick summary
-----------------
Every tick where a direction edge fired or at least one action fired also
emits a single `FrameState` (frame, direction, and the tick's `InputEvent`s
bundled together) via `frame_state`. This is for consumers that want "what
was active this frame" as one payload rather than correlating two
independently-timed signals — see `input_history.py`, the live input-history
panel's Motions/Actions parser.

Pad selection: by default the thread auto-locks to the first connected
XInput slot; the controller-test dialog can pin a specific slot with
`set_pad_index(0..3)` or return to auto with `set_pad_index(None)`.

Monitor mode: while the controller-test / rebinding dialogs are open they
call `set_monitoring(True)`, and the thread additionally emits ~30 Hz
PadSnapshot lists for ALL four slots so those dialogs can render live state
without touching XInput from a second thread.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QThread, pyqtSignal

from .frame_clock import FrameClock
from .input_types import TRIGGER_THRESHOLD, FrameState, InputEvent, PadSnapshot
from .models import Button, Motion
from .motion_parser import (
    DirectionRingBuffer,
    ParserConfig,
    clean_socd,
    mirror_direction,
    parse_motion,
)
from .profiles import TRIGGER_SOURCES, ResolvedProfile

try:  # XInput-Python is Windows-only; keep the module importable elsewhere.
    import XInput  # type: ignore
except Exception:  # pragma: no cover
    XInput = None


POLL_INTERVAL_S = 0.004  # ~250 Hz
MONITOR_INTERVAL_S = 1 / 30  # snapshot rate for the test dialog
NUM_SLOTS = 4


def default_profile() -> ResolvedProfile:
    """Marvel Tōkon on the stock 16-button leverless — used until the
    ProfileManager pushes the user's own active profile in."""
    from .games import leverless_16, marvel_tokon  # local: avoid import cycle

    game = marvel_tokon()
    return ResolvedProfile.resolve(
        game, game.require_layout(game.default_layout_id), leverless_16()
    )


#: Back-compat view of the shipped default binding, in the old
#: `{XInput source: Button}` shape. Read-only — rebind through ProfileManager.
DEFAULT_BUTTON_MAP: dict[str, Button] = default_profile().button_map()


class InputThread(QThread):
    input_event = pyqtSignal(object)  # InputEvent
    direction_changed = pyqtSignal(int)  # numpad dir (facing-mirrored)
    frame_state = pyqtSignal(object)  # FrameState — bundled per-tick payload
    connected_changed = pyqtSignal(bool)
    active_pad_changed = pyqtSignal(int)  # slot index, -1 = none
    monitor_state = pyqtSignal(object)  # list[PadSnapshot], all 4 slots
    raw_press = pyqtSignal(str)  # XInput source token — drives press-to-bind
    error = pyqtSignal(str)

    def __init__(
        self,
        frame_clock: FrameClock,
        profile: ResolvedProfile | None = None,
        parser_config: ParserConfig = ParserConfig(),
    ) -> None:
        super().__init__()
        self._clock = frame_clock
        self._profile = profile or default_profile()
        self._parser_config = parser_config
        self._buffer = DirectionRingBuffer(maxlen=64)
        self._running = False
        # Flags below are single-word writes from the GUI thread: no lock needed.
        self._facing_right = True
        self._monitoring = False
        self._listening = False
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

    def set_listening(self, enabled: bool) -> None:
        """While listening, every raw source edge is echoed on `raw_press`
        so the rebinding dialog can capture 'press the button you want'."""
        self._listening = enabled

    def set_pad_index(self, index: int | None) -> None:
        """Pin a specific XInput slot (0-3), or None for auto (first found)."""
        self._pad_override = index

    def set_profile(self, profile: ResolvedProfile) -> None:
        """Swap the active binding table. Safe from the GUI thread mid-poll."""
        self._profile = profile

    @property
    def profile(self) -> ResolvedProfile:
        return self._profile

    @property
    def button_map(self) -> dict[str, Button]:
        """Back-compat: `{XInput source: Button}` for the active profile."""
        return self._profile.button_map()

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

    # -- source reads ----------------------------------------------------------

    @staticmethod
    def _read_sources(
        sources: tuple[str, ...], buttons: dict, lt: float, rt: float
    ) -> dict[str, bool]:
        """Sample exactly the sources the active profile cares about."""
        trigger_values = {"LEFT_TRIGGER": lt, "RIGHT_TRIGGER": rt}
        return {
            src: (
                trigger_values[src] >= TRIGGER_THRESHOLD
                if src in TRIGGER_SOURCES
                else bool(buttons.get(src, False))
            )
            for src in sources
        }

    def _direction_from(self, profile: ResolvedProfile, pressed: dict[str, bool]) -> int:
        """OR each cardinal across every source bound to it, then SOCD-clean.
        Duplicate direction keys (two Up buttons) merge here for free."""
        return clean_socd(
            up=any(pressed.get(s, False) for s in profile.up_sources),
            down=any(pressed.get(s, False) for s in profile.down_sources),
            left=any(pressed.get(s, False) for s in profile.left_sources),
            right=any(pressed.get(s, False) for s in profile.right_sources),
            mode=profile.socd_mode,
        )

    # -- monitor snapshots ------------------------------------------------------

    def _maybe_emit_monitor(self, now: float, profile: ResolvedProfile) -> None:
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
                pressed = self._read_sources(
                    profile.direction_sources, buttons, lt, rt
                )
                snaps.append(
                    PadSnapshot(
                        i, True, dict(buttons), lt, rt,
                        self._direction_from(profile, pressed),
                    )
                )
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
        profile = self._profile
        # Sources the profile binds, plus every source while listening for a
        # rebind (an unbound switch has to be capturable to be bindable).
        watched = profile.sources
        prev_raw: dict[str, bool] = {}

        while self._running:
            now = time.monotonic()

            # -- pick up a rebind pushed from the GUI thread -------------------
            current = self._profile
            if current is not profile:
                profile = current
                watched = profile.sources
                # Drop edge state: a switch held across the swap must not fire
                # its new binding until it is physically released and pressed.
                prev_pressed.clear()
                self._buffer.clear()

            if self._monitoring:
                self._maybe_emit_monitor(now, profile)

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
                        prev_raw.clear()
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
            pressed_now = self._read_sources(watched, buttons, lt, rt)

            # -- press-to-bind capture (rebinding dialog only) -----------------
            if self._listening:
                raw = self._read_sources(
                    tuple(buttons) + ("LEFT_TRIGGER", "RIGHT_TRIGGER"), buttons, lt, rt
                )
                for src, down in raw.items():
                    if down and not prev_raw.get(src, False):
                        self.raw_press.emit(src)
                prev_raw = raw
            elif prev_raw:
                prev_raw = {}

            # -- directions (SOCD-cleaned, facing-mirrored) --------------------
            direction = self._direction_from(profile, pressed_now)
            if not self._facing_right:
                direction = mirror_direction(direction)
            direction_edge = self._buffer.push(now, direction)
            if direction_edge:
                self.direction_changed.emit(direction)

            # -- action buttons: rising edges only (no negative edge in Tōkon) -
            # `frame` is read once for the whole tick (not per source) so every
            # event this tick — and the FrameState bundling them below — carry
            # the identical frame number, never a microsecond-apart neighbor.
            frame = self._clock.current_frame()
            motion: Motion | None = None  # parsed at most once per poll
            tick_events: list[InputEvent] = []
            for source, actions in profile.actions.items():
                if not pressed_now.get(source, False) or prev_pressed.get(source, False):
                    continue
                is_macro = len(actions) > 1
                for action in actions:
                    if action.command_normal:
                        # Direction + Attack, same switch: the direction is
                        # this exact press, not a guess from recent history.
                        # No ring-buffer lookback, no timer — deterministic
                        # Motion.NONE every time.
                        resolved_motion = Motion.NONE
                    elif action.parse_motions:
                        if motion is None:
                            motion = parse_motion(
                                self._buffer.samples, now, self._parser_config
                            )
                        resolved_motion = motion
                    else:
                        # Single-button motion input: the button IS the motion.
                        resolved_motion = Motion.NONE
                    ev = InputEvent(
                        frame=frame,
                        button=action.button,
                        motion=resolved_motion,
                        direction=direction,
                        t_monotonic=now,
                        logical_id=action.logical_id,
                        logical_name=action.logical_name,
                        source=source,
                        macro=is_macro,
                        command_normal=action.command_normal,
                    )
                    self.input_event.emit(ev)
                    tick_events.append(ev)
            prev_pressed = pressed_now

            if direction_edge or tick_events:
                self.frame_state.emit(
                    FrameState(
                        frame=frame,
                        t_monotonic=now,
                        direction=direction,
                        events=tuple(tick_events),
                    )
                )

            time.sleep(POLL_INTERVAL_S)
