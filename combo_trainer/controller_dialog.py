"""
controller_dialog.py — Controller test modal.

Three jobs:
  1. Show all four XInput slots live, so the user can confirm which slot
     their device occupies ("press any button — its slot pulses").
  2. Let them pin that slot (or Auto) for the InputThread.
  3. Verify the FULL pipeline: raw d-pad state, SOCD-cleaned numpad
     direction, lit buttons/triggers with their game-button mapping, and
     the last fully parsed InputEvent (e.g. "SPECIAL (QCF)") — proving
     that motions register off the leverless before entering Record mode.

The dialog never touches XInput itself. It flips the InputThread into
monitor mode and renders the ~30 Hz PadSnapshot stream it emits, so all
controller I/O stays on one thread.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QRadioButton,
    QVBoxLayout,
)

from .input_engine import (
    NUM_SLOTS,
    TRIGGER_THRESHOLD,
    InputEvent,
    InputThread,
    PadSnapshot,
)
from .models import Motion
from .timeline import BUTTON_LABELS, MOTION_GLYPHS

_CHIP_ON = (
    "QLabel { background: #3fae6a; color: #0a0a0e; border-radius: 6px;"
    " padding: 4px 8px; font-weight: bold; }"
)
_CHIP_OFF = (
    "QLabel { background: #26262e; color: #9a9aa4; border-radius: 6px;"
    " padding: 4px 8px; }"
)
_CELL_ON = (
    "QLabel { background: #29b6f6; color: #0a0a0e; border: 1px solid #444;"
    " font-weight: bold; }"
)
_CELL_OFF = "QLabel { background: #1b1b22; color: #55555f; border: 1px solid #333; }"

_PAD_BUTTONS = ("A", "B", "X", "Y", "LEFT_SHOULDER", "RIGHT_SHOULDER", "START", "BACK")
_SHORT = {"LEFT_SHOULDER": "LB", "RIGHT_SHOULDER": "RB", "START": "ST", "BACK": "BK"}

# Numpad layout rows for the 3x3 direction grid (top row first).
_NUMPAD_ROWS = ((7, 8, 9), (4, 5, 6), (1, 2, 3))


class ControllerTestDialog(QDialog):
    def __init__(self, input_thread: InputThread, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Controller Test")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._thread = input_thread
        self._active_pad = -1  # thread's current slot (auto mode display)
        self._snapshots: list[PadSnapshot] = []
        self._last_event_t = 0.0

        self._build_ui()

        # Wire to the thread (queued connections: thread-safe).
        self._thread.monitor_state.connect(self._on_snapshots)
        self._thread.active_pad_changed.connect(self._on_active_pad)
        self._thread.input_event.connect(self._on_input_event)
        self._thread.set_monitoring(True)
        self.finished.connect(self._teardown)

        if not self._thread.available:
            self._hint.setText(
                "⚠ XInput is unavailable on this platform — no controller "
                "input can be read."
            )

        # Fades the "last event" line after a couple of seconds.
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(250)
        self._fade_timer.timeout.connect(self._maybe_fade_event)
        self._fade_timer.start()

    # -- UI ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Slot picker -------------------------------------------------------------
        slot_box = QGroupBox("Device slot")
        slot_lay = QVBoxLayout(slot_box)
        self._hint = QLabel("Press any button on your device — its slot pulses ●")
        self._hint.setStyleSheet("color: #9a9aa4;")
        slot_lay.addWidget(self._hint)

        self._slot_group = QButtonGroup(self)
        self._slot_group.setExclusive(True)

        self._auto_radio = QRadioButton("Auto (first connected)")
        self._auto_radio.setChecked(True)
        self._slot_group.addButton(self._auto_radio, -1)
        slot_lay.addWidget(self._auto_radio)

        self._slot_radios: list[QRadioButton] = []
        for i in range(NUM_SLOTS):
            rb = QRadioButton(f"Slot {i} — scanning…")
            self._slot_group.addButton(rb, i)
            self._slot_radios.append(rb)
            slot_lay.addWidget(rb)
        self._slot_group.idToggled.connect(self._on_slot_toggled)
        root.addWidget(slot_box)

        # Live input ----------------------------------------------------------------
        live_box = QGroupBox("Live input (selected slot)")
        live_lay = QHBoxLayout(live_box)

        dpad_col = QVBoxLayout()
        grid = QGridLayout()
        grid.setSpacing(2)
        self._dir_cells: dict[int, QLabel] = {}
        for r, row in enumerate(_NUMPAD_ROWS):
            for c, d in enumerate(row):
                cell = QLabel(str(d))
                cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setFixedSize(42, 42)
                cell.setStyleSheet(_CELL_OFF)
                grid.addWidget(cell, r, c)
                self._dir_cells[d] = cell
        dpad_col.addLayout(grid)
        self._dir_label = QLabel("D-pad (SOCD-cleaned): 5")
        dpad_col.addWidget(self._dir_label)
        dpad_col.addStretch(1)
        live_lay.addLayout(dpad_col)

        btn_col = QVBoxLayout()
        chip_grid = QGridLayout()
        chip_grid.setSpacing(6)
        self._chips: dict[str, QLabel] = {}
        button_map = self._thread.button_map
        for idx, name in enumerate(_PAD_BUTTONS):
            mapped = button_map.get(name)
            text = _SHORT.get(name, name)
            if mapped is not None:
                text += f" → {BUTTON_LABELS[mapped]}"
            chip = QLabel(text)
            chip.setStyleSheet(_CHIP_OFF)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._chips[name] = chip
            chip_grid.addWidget(chip, idx // 2, idx % 2)
        btn_col.addLayout(chip_grid)

        for trig, label in (("LEFT_TRIGGER", "LT"), ("RIGHT_TRIGGER", "RT")):
            row = QHBoxLayout()
            mapped = button_map.get(trig)
            suffix = f" → {BUTTON_LABELS[mapped]}" if mapped is not None else ""
            row.addWidget(QLabel(f"{label}{suffix}"))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(14)
            row.addWidget(bar, stretch=1)
            setattr(self, f"_bar_{label.lower()}", bar)
            btn_col.addLayout(row)
        live_lay.addLayout(btn_col, stretch=1)
        root.addWidget(live_box)

        # Pipeline check --------------------------------------------------------------
        pipe_box = QGroupBox("Pipeline check (button map + motion parser)")
        pipe_lay = QVBoxLayout(pipe_box)
        self._event_label = QLabel("Waiting for a button press…")
        self._event_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        pipe_lay.addWidget(self._event_label)
        tip = QLabel(
            "Try a QCF + button: you should see e.g.  SPECIAL (↓↘→ QCF).\n"
            "Note: 'Facing Left' mirroring from the Mode menu applies here too."
        )
        tip.setStyleSheet("color: #9a9aa4;")
        pipe_lay.addWidget(tip)
        root.addWidget(pipe_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    # -- slot selection -----------------------------------------------------------------

    def _on_slot_toggled(self, slot_id: int, checked: bool) -> None:
        if checked:
            self._thread.set_pad_index(None if slot_id == -1 else slot_id)

    def _on_active_pad(self, index: int) -> None:
        self._active_pad = index

    def _preview_index(self) -> int:
        checked = self._slot_group.checkedId()
        if checked >= 0:
            return checked
        if self._active_pad >= 0:
            return self._active_pad
        return next((s.index for s in self._snapshots if s.connected), -1)

    # -- rendering ----------------------------------------------------------------------

    def _on_snapshots(self, snaps: list[PadSnapshot]) -> None:
        self._snapshots = snaps
        for snap in snaps:
            rb = self._slot_radios[snap.index]
            if snap.connected:
                pulse = "  ●" if snap.any_pressed else ""
                active = "  (active)" if snap.index == self._active_pad else ""
                rb.setText(f"Slot {snap.index} — connected{active}{pulse}")
                rb.setEnabled(True)
            else:
                rb.setText(f"Slot {snap.index} — empty")
                rb.setEnabled(False)
                if self._slot_group.checkedId() == snap.index:
                    self._auto_radio.setChecked(True)  # pinned pad unplugged

        idx = self._preview_index()
        snap = snaps[idx] if 0 <= idx < len(snaps) else None
        self._render_live(snap)

    def _render_live(self, snap: PadSnapshot | None) -> None:
        direction = snap.direction if snap and snap.connected else 5
        for d, cell in self._dir_cells.items():
            cell.setStyleSheet(_CELL_ON if d == direction else _CELL_OFF)
        self._dir_label.setText(f"D-pad (SOCD-cleaned): {direction}")

        buttons = snap.buttons if snap and snap.connected else {}
        for name, chip in self._chips.items():
            chip.setStyleSheet(_CHIP_ON if buttons.get(name, False) else _CHIP_OFF)

        lt = snap.lt if snap and snap.connected else 0.0
        rt = snap.rt if snap and snap.connected else 0.0
        self._bar_lt.setValue(int(lt * 100))
        self._bar_rt.setValue(int(rt * 100))

    # -- pipeline readout --------------------------------------------------------------------

    def _on_input_event(self, ev: InputEvent) -> None:
        self._last_event_t = time.monotonic()
        if ev.motion is Motion.NONE:
            text = f"{ev.button.value}   (bare press / Quick Special)"
        else:
            glyph = MOTION_GLYPHS.get(ev.motion, "")
            text = f"{ev.button.value}   ({glyph} {ev.motion.value})"
        self._event_label.setText(text)
        self._event_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #6ee87e;"
        )

    def _maybe_fade_event(self) -> None:
        if self._last_event_t and time.monotonic() - self._last_event_t > 2.0:
            self._event_label.setStyleSheet("font-size: 15px; font-weight: bold;")

    # -- lifecycle -------------------------------------------------------------------------------

    def _teardown(self) -> None:
        self._thread.set_monitoring(False)
        try:
            self._thread.monitor_state.disconnect(self._on_snapshots)
            self._thread.active_pad_changed.disconnect(self._on_active_pad)
            self._thread.input_event.disconnect(self._on_input_event)
        except TypeError:
            pass  # already disconnected
