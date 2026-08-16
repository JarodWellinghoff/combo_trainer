"""
controller_dialog.py — Controller test modal.

Three jobs:
  1. Show all four XInput slots live, so the user can confirm which slot
     their device occupies ("press any button — its slot pulses").
  2. Let them pin that slot (or Auto) for the InputThread.
  3. Verify the FULL pipeline: raw switch state, SOCD-cleaned numpad
     direction, and the last fully parsed InputEvent (e.g. "Quick Skill" or
     "Heavy (↓↘→ QCF)") — proving that the active profile's bindings and the
     motion parser both fire off the leverless before entering Record mode.

The switch grid is driven by the ACTIVE PROFILE, not a hardcoded pad layout:
each of the box's 16 silkscreened switches is listed with the logical input
it currently fires, so this doubles as a printout of the current layout.

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
from .profiles import DeviceLayout
from .timeline import MOTION_GLYPHS

_CHIP_ON = (
    "QLabel { background: #3fae6a; color: #0a0a0e; border-radius: 6px;"
    " padding: 4px 8px; font-weight: bold; }"
)
_CHIP_OFF = (
    "QLabel { background: #26262e; color: #9a9aa4; border-radius: 6px;"
    " padding: 4px 8px; }"
)
_CHIP_UNMAPPED = (
    "QLabel { background: #1b1b22; color: #55555f; border-radius: 6px;"
    " padding: 4px 8px; font-style: italic; }"
)
_CELL_ON = (
    "QLabel { background: #29b6f6; color: #0a0a0e; border: 1px solid #444;"
    " font-weight: bold; }"
)
_CELL_OFF = "QLabel { background: #1b1b22; color: #55555f; border: 1px solid #333; }"

# Numpad layout rows for the 3x3 direction grid (top row first).
_NUMPAD_ROWS = ((7, 8, 9), (4, 5, 6), (1, 2, 3))


class ControllerTestDialog(QDialog):
    def __init__(
        self,
        input_thread: InputThread,
        device: DeviceLayout | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Controller Test")
        self.setModal(True)
        self.setMinimumWidth(620)

        self._thread = input_thread
        self._device = device
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
        profile = self._thread.profile
        live_box = QGroupBox(f"Live input — {profile.title}")
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

        # One chip per physical switch, labelled with what it currently fires.
        btn_col = QVBoxLayout()
        chip_grid = QGridLayout()
        chip_grid.setSpacing(6)
        self._chips: dict[str, QLabel] = {}  # XInput source -> chip
        self._unmapped: set[str] = set()
        for idx, source, text in self._switch_rows():
            chip = QLabel(text)
            chip.setStyleSheet(_CHIP_OFF if source not in self._unmapped else _CHIP_UNMAPPED)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._chips[source] = chip
            chip_grid.addWidget(chip, idx // 2, idx % 2)
        btn_col.addLayout(chip_grid)

        # Analog trigger readout: leverless triggers are digital switches, but
        # a pad's analog travel is worth seeing against TRIGGER_THRESHOLD.
        for source, label in (("LEFT_TRIGGER", "LT"), ("RIGHT_TRIGGER", "RT")):
            row = QHBoxLayout()
            bound = profile.label_for(source)
            row.addWidget(QLabel(f"{label}{f' → {bound}' if bound else ''}"))
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
        pipe_box = QGroupBox("Pipeline check (bindings + motion parser)")
        pipe_lay = QVBoxLayout(pipe_box)
        self._event_label = QLabel("Waiting for a button press…")
        self._event_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        pipe_lay.addWidget(self._event_label)
        tip = QLabel(
            "Try a QCF + Heavy: you should see  Heavy (↓↘→ QCF).\n"
            "Quick Skill / Quick Assemble / Quick Dash always read as bare "
            "presses — the button IS the motion.\n"
            "Note: 'Facing Left' mirroring from the Mode menu applies here too."
        )
        tip.setStyleSheet("color: #9a9aa4;")
        pipe_lay.addWidget(tip)
        root.addWidget(pipe_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _switch_rows(self) -> list[tuple[int, str, str]]:
        """(grid index, XInput source, chip text) for every switch on the box.

        Falls back to the profile's bound sources when no device layout was
        supplied, so the dialog still works standalone.
        """
        profile = self._thread.profile
        rows: list[tuple[int, str, str]] = []
        if self._device is not None:
            for idx, btn in enumerate(self._device):
                bound = profile.label_for(btn.source)
                if not bound:
                    self._unmapped.add(btn.source)
                    bound = "unmapped"
                rows.append((idx, btn.source, f"{btn.label} → {bound}"))
        else:
            for idx, source in enumerate(profile.sources):
                rows.append((idx, source, f"{source} → {profile.label_for(source)}"))
        return rows

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
        live = snap if snap and snap.connected else None

        direction = live.direction if live else 5
        for d, cell in self._dir_cells.items():
            cell.setStyleSheet(_CELL_ON if d == direction else _CELL_OFF)
        self._dir_label.setText(f"D-pad (SOCD-cleaned): {direction}")

        for source, chip in self._chips.items():
            if live is not None and live.pressed(source):
                chip.setStyleSheet(_CHIP_ON)
            elif source in self._unmapped:
                chip.setStyleSheet(_CHIP_UNMAPPED)
            else:
                chip.setStyleSheet(_CHIP_OFF)

        self._bar_lt.setValue(int((live.lt if live else 0.0) * 100))
        self._bar_rt.setValue(int((live.rt if live else 0.0) * 100))

    # -- pipeline readout --------------------------------------------------------------------

    def _on_input_event(self, ev: InputEvent) -> None:
        self._last_event_t = time.monotonic()
        text = ev.display_name
        if ev.motion is not Motion.NONE:
            glyph = MOTION_GLYPHS.get(ev.motion, "")
            text += f"   ({glyph} {ev.motion.value})"
        else:
            text += "   (bare press)"
        if ev.macro:
            text += "   [macro]"
        if ev.source:
            text += f"   ← {ev.source}"
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
