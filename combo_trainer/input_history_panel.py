"""
input_history_panel.py — Live, vertically-scrolling input-history display.

The classic fighting-game training-mode "command history" overlay (SF6 /
Tekken / GGST): a log of the player's own recent presses, newest at the top,
each row split into a fixed-width MOTIONS column (left) and an ACTIONS
column (right) so the two never drift out of alignment down the list.

This is a DIFFERENT thing from `timeline.py`'s `TimelineOverlay`, which
scrolls pre-recorded `ComboFile.inputs` HORIZONTALLY toward a hit-zone,
synced to video playback position, for Record/Practice judgment. This panel
is a live, video-independent readout of "what did I just press" — the two
coexist; this one doesn't replace the other.

All the "which inputs happened together, and how do I split them into two
categories" logic lives in `input_history.py` (Qt-free, unit-tested there).
This module only turns `HistoryRow`s into widgets — no parsing here.

Layout notes (PyQt6 has no CSS, so this is the literal equivalent)
--------------------------------------------------------------------
* "Fixed height, hidden overflow": a `QScrollArea` with `setFixedHeight()`
  clips real overflow — but in the normal newest-at-top flow there is none,
  because the buffer is bounded (`InputHistory.maxlen`) and old row widgets
  are deleted once they age out, not merely scrolled past.
* "Newest at top": each new row is `insertWidget(0, ...)` into a top-anchored
  `QVBoxLayout` (a trailing stretch keeps rows packed at the top), and the
  scrollbar is reset to 0 on every insert — a live feed, not a static list.
* "Grid layout per row, empty column keeps its space": each row is a
  `QGridLayout` with column 0 fixed-width (`setColumnMinimumWidth` AND the
  motions container itself `setFixedWidth`, so it can't collapse to zero
  even with no chips in it) and column 1 stretching — this guarantees every
  row's Actions column starts at the identical x, whether that row has 0, 1,
  or a full macro's worth of motions.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .input_engine import InputThread
from .input_history import HistoryRow, InputHistory
from .input_types import FrameState
from .profiles import Direction
from .timeline import BUTTON_COLORS, BUTTON_LABELS

ROW_HEIGHT = 26
MOTIONS_COL_WIDTH = 56
CHIP_SIZE = 20
ACTION_CHIP_WIDTH = 32
ACTIONS_PER_ROW = 6  # wrap a big macro/mash into a second line rather than overflow

_DIRECTION_GLYPHS: dict[Direction, str] = {
    Direction.UP: "↑",
    Direction.DOWN: "↓",
    Direction.LEFT: "←",
    Direction.RIGHT: "→",
}
_MOTION_CHIP_STYLE = (
    "QLabel { background: #29b6f6; color: #0a0a0e; border-radius: 4px;"
    " font-weight: bold; }"
)
_COMMAND_NORMAL_BORDER = " border: 2px solid #ffd93c;"


class InputHistoryPanel(QWidget):
    """Vertical, dual-column live input log. Call `bind(input_thread)` to
    start receiving ticks; `clear()` to reset."""

    def __init__(
        self, maxlen: int = 64, height: int = 360, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._history = InputHistory(maxlen=maxlen)
        self._row_widgets: list[QWidget] = []
        self._thread: InputThread | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("Input History")
        title.setStyleSheet("font-weight: bold; color: #d0d0d8;")
        header.addWidget(title)
        header.addStretch(1)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedHeight(20)
        clear_btn.clicked.connect(self.clear)
        header.addWidget(clear_btn)
        root.addLayout(header)

        self._scroll = QScrollArea(self)
        self._scroll.setFixedHeight(height)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        list_widget = QWidget()
        self._rows_layout = QVBoxLayout(list_widget)
        self._rows_layout.setContentsMargins(4, 4, 4, 4)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)  # keeps rows packed at the TOP as they insert at 0

        self._scroll.setWidget(list_widget)
        root.addWidget(self._scroll)

    # -- wiring ---------------------------------------------------------------

    def bind(self, input_thread: InputThread) -> None:
        """Start receiving live ticks. Safe to call again to rebind."""
        self.unbind()
        self._thread = input_thread
        input_thread.frame_state.connect(self._on_frame_state)

    def unbind(self) -> None:
        if self._thread is not None:
            try:
                self._thread.frame_state.disconnect(self._on_frame_state)
            except TypeError:
                pass  # already disconnected
            self._thread = None

    def clear(self) -> None:
        self._history.clear()
        for w in self._row_widgets:
            self._rows_layout.removeWidget(w)
            w.deleteLater()
        self._row_widgets.clear()

    # -- live updates -----------------------------------------------------------

    def _on_frame_state(self, state: FrameState) -> None:
        row = self._history.push(state)
        widget = self._build_row_widget(row)
        # Insert after index 0 but before the trailing stretch (which always
        # occupies the layout's last slot) — newest row lands at the top.
        self._rows_layout.insertWidget(0, widget)
        self._row_widgets.insert(0, widget)

        while len(self._row_widgets) > self._history.maxlen:
            oldest = self._row_widgets.pop()
            self._rows_layout.removeWidget(oldest)
            oldest.deleteLater()

        self._scroll.verticalScrollBar().setValue(0)  # keep the new top row in view

    # -- row construction (Task 3: dual-column grid) -----------------------------

    def _build_row_widget(self, row: HistoryRow) -> QWidget:
        w = QWidget()
        w.setFixedHeight(ROW_HEIGHT if not row.actions else max(ROW_HEIGHT, self._actions_rows(row) * (CHIP_SIZE + 2) + 6))

        grid = QGridLayout(w)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(6)
        grid.setColumnMinimumWidth(0, MOTIONS_COL_WIDTH)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)

        motions_box = self._build_motions_column(row)
        grid.addWidget(motions_box, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        actions_box = self._build_actions_column(row)
        grid.addWidget(actions_box, 0, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        return w

    @staticmethod
    def _actions_rows(row: HistoryRow) -> int:
        n = len(row.actions)
        return max(1, -(-n // ACTIONS_PER_ROW))  # ceil div, no import needed

    def _build_motions_column(self, row: HistoryRow) -> QWidget:
        # Fixed width on the CONTAINER itself (not just the grid column) so
        # an empty motions column still reserves its full structural space —
        # the Actions column of every row starts at the identical x either way.
        box = QWidget()
        box.setFixedWidth(MOTIONS_COL_WIDTH)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for direction in row.motions:
            chip = QLabel(_DIRECTION_GLYPHS.get(direction, "?"))
            chip.setFixedSize(CHIP_SIZE, CHIP_SIZE)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setStyleSheet(_MOTION_CHIP_STYLE)
            lay.addWidget(chip)
        lay.addStretch(1)
        return box

    def _build_actions_column(self, row: HistoryRow) -> QWidget:
        box = QWidget()
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for i, ev in enumerate(row.actions):
            chip = QLabel(BUTTON_LABELS.get(ev.button, ev.button.value[:2]))
            chip.setFixedSize(ACTION_CHIP_WIDTH, CHIP_SIZE)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            color = BUTTON_COLORS.get(ev.button)
            hex_color = color.name() if color is not None else "#c8c8c8"
            style = (
                f"QLabel {{ background: {hex_color}; color: #0a0a0e;"
                f" border-radius: 4px; font-weight: bold;"
            )
            if ev.command_normal:
                style += _COMMAND_NORMAL_BORDER
            style += " }"
            chip.setStyleSheet(style)
            chip.setToolTip(
                f"{ev.display_name}"
                + (" (Command Normal)" if ev.command_normal else "")
                + (" [macro]" if ev.macro else "")
            )
            grid.addWidget(chip, i // ACTIONS_PER_ROW, i % ACTIONS_PER_ROW)
        return box
