"""
state.py — Application mode state machine.

Record and Practice are not separate windows; they are routing states for
the same input-event stream and the same video player. This controller is
the single source of truth for the current mode, with guarded transitions
and a Qt signal that everything else (menus, overlay, input router) hooks.

Phase 2 wiring plan:
    InputThread ──InputEvent──▶ ModeController.route_input()
        RECORD   -> RecordController.on_input()   (ComboManager.add_input)
        PRACTICE -> PracticeController.on_input() (match + judge + feedback)
        IDLE     -> dropped
"""

from __future__ import annotations

from enum import Enum, auto

from PyQt6.QtCore import QObject, pyqtSignal


class AppMode(Enum):
    IDLE = auto()      # browsing / editing, inputs ignored
    RECORD = auto()    # presses appended to ComboManager at current frame
    PRACTICE = auto()  # presses judged against loaded combo


class ModeController(QObject):
    """Guards and broadcasts mode transitions."""

    mode_changed = pyqtSignal(AppMode, AppMode)  # (old, new)
    transition_rejected = pyqtSignal(str)        # human-readable reason

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode = AppMode.IDLE
        # Preconditions, set by MainWindow as things load:
        self.video_loaded = False
        self.combo_loaded = False
        self.controller_connected = False  # set by input thread in Phase 2

    @property
    def mode(self) -> AppMode:
        return self._mode

    # -- transition API -------------------------------------------------------

    def request_mode(self, target: AppMode) -> bool:
        if target is self._mode:
            return True

        reason = self._check_guard(target)
        if reason is not None:
            self.transition_rejected.emit(reason)
            return False

        old, self._mode = self._mode, target
        self.mode_changed.emit(old, target)
        return True

    def to_idle(self) -> bool:
        return self.request_mode(AppMode.IDLE)

    # -- guards ----------------------------------------------------------------

    def _check_guard(self, target: AppMode) -> str | None:
        """Return a rejection reason, or None if the transition is allowed."""
        if target is AppMode.IDLE:
            return None

        if not self.video_loaded:
            return "Load a combo video before entering Record or Practice mode."

        if target is AppMode.PRACTICE and not self.combo_loaded:
            return "Load or record a combo file before entering Practice mode."

        # Soft-fail on no controller: allow the mode, MainWindow shows a
        # status-bar warning instead. (Keyboard fallback may exist later.)
        return None
