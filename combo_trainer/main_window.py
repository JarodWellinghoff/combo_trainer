"""
main_window.py — PyQt6 shell wiring all subsystems together.

Signal flow (arrows are queued Qt connections = thread-safe):

  mpv observer thread ──▶ FrameClock (direct, locked writes)
                      └─▶ MpvPlayerWidget signals ──▶ GUI slots

  InputThread (250 Hz) ──InputEvent──▶ MainWindow._route_input
        AppMode.RECORD   ──▶ RecordController  ──▶ ComboManager + overlay flash
        AppMode.PRACTICE ──▶ PracticeController ──▶ judge ──▶ overlay + SFX
        AppMode.IDLE     ──▶ dropped

  QTimer (16 ms) ──▶ label refresh, overlay repaint, PracticeController.on_frame
"""

from __future__ import annotations

# --- mpv import preamble (order matters!) -----------------------------------
import locale

locale.setlocale(locale.LC_NUMERIC, "C")
import os
import sys

os.environ["PATH"] = os.path.dirname(__file__) + os.pathsep + os.environ["PATH"]
import mpv  # noqa: E402

from pathlib import Path  # noqa: E402

from PyQt6.QtCore import Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QAction, QActionGroup  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .audio import SfxPlayer  # noqa: E402
from .controller_dialog import ControllerTestDialog  # noqa: E402
from .controllers import NoteStatus, PracticeController, RecordController  # noqa: E402
from .frame_clock import FrameClock  # noqa: E402
from .input_engine import InputThread  # noqa: E402
from .models import ComboManager  # noqa: E402
from .state import AppMode, ModeController  # noqa: E402
from .timeline import TimelineOverlay  # noqa: E402

PLAYBACK_SPEEDS = [0.25, 0.50, 0.75, 1.00]


# ---------------------------------------------------------------------------
# Video widget
# ---------------------------------------------------------------------------


class MpvPlayerWidget(QWidget):
    """Hosts libmpv via `wid` embedding; republishes state as Qt signals and
    feeds the FrameClock directly from the observer thread (lowest latency)."""

    time_pos_changed = pyqtSignal(float)
    fps_detected = pyqtSignal(float)
    duration_changed = pyqtSignal(float)
    pause_changed = pyqtSignal(bool)
    speed_changed = pyqtSignal(float)
    file_loaded = pyqtSignal(str)
    end_of_file = pyqtSignal()

    def __init__(self, frame_clock: FrameClock, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background-color: black;")

        self._clock = frame_clock
        self._fps = 60.0
        self._time_pos = 0.0
        self._loaded_path: str | None = None

        self.mpv = mpv.MPV(
            wid=str(int(self.winId())),
            vo="gpu",
            hwdec="auto-safe",
            keep_open="yes",
            pause=True,
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            hr_seek="yes",
            audio_pitch_correction="yes",
        )
        self._install_observers()

    def _install_observers(self) -> None:
        @self.mpv.property_observer("time-pos")
        def _time(_n, value):
            if value is not None:
                self._time_pos = float(value)
                self._clock.update_time_pos(self._time_pos)
                self.time_pos_changed.emit(self._time_pos)

        @self.mpv.property_observer("container-fps")
        def _fps(_n, value):
            if value:
                self._fps = float(value)
                self._clock.update_fps(self._fps)
                self.fps_detected.emit(self._fps)

        @self.mpv.property_observer("duration")
        def _dur(_n, value):
            if value:
                self.duration_changed.emit(float(value))

        @self.mpv.property_observer("pause")
        def _pause(_n, value):
            if value is not None:
                self._clock.update_paused(bool(value))
                self.pause_changed.emit(bool(value))

        @self.mpv.property_observer("speed")
        def _speed(_n, value):
            if value:
                self._clock.update_speed(float(value))
                self.speed_changed.emit(float(value))

        @self.mpv.property_observer("eof-reached")
        def _eof(_n, value):
            if value:
                self.end_of_file.emit()

    # -- accessors ----------------------------------------------------------------

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def time_pos(self) -> float:
        return self._time_pos

    @property
    def loaded_path(self) -> str | None:
        return self._loaded_path

    # -- transport ------------------------------------------------------------------

    def load(self, path: str) -> None:
        self._loaded_path = path
        self.mpv.loadfile(path)
        self.mpv.pause = True
        self.file_loaded.emit(path)

    def toggle_pause(self) -> None:
        self.mpv.pause = not self.mpv.pause

    def set_speed(self, speed: float) -> None:
        self.mpv.speed = speed

    def seek_seconds(self, seconds: float) -> None:
        self.mpv.command("seek", seconds, "absolute", "exact")

    def frame_step(self, forward: bool = True) -> None:
        if self._loaded_path is not None:
            self.mpv.command("frame-step" if forward else "frame-back-step")

    def shutdown(self) -> None:
        try:
            self.mpv.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Tōkon Combo Trainer")
        self.resize(1280, 800)

        # Core subsystems -----------------------------------------------------
        self.frame_clock = FrameClock()
        self.combo_manager = ComboManager()
        self.mode_controller = ModeController(self)
        self.record_controller = RecordController(self.combo_manager, self)
        self.practice_controller = PracticeController(parent=self)
        self.sfx = SfxPlayer()
        self.input_thread = InputThread(self.frame_clock)

        self._duration_s = 0.0
        self._slider_dragging = False

        self._build_ui()
        self._build_menus()
        self._wire_signals()

        self._ui_clock = QTimer(self)
        self._ui_clock.setInterval(16)
        self._ui_clock.timeout.connect(self._on_ui_tick)
        self._ui_clock.start()

        self.input_thread.start()

    # -- UI construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.video_container = QWidget(central)
        vc = QVBoxLayout(self.video_container)
        vc.setContentsMargins(0, 0, 0, 0)

        self.player = MpvPlayerWidget(self.frame_clock, self.video_container)
        vc.addWidget(self.player)

        self.overlay = TimelineOverlay(self.video_container)
        self.overlay.bind(
            self.frame_clock,
            get_combo=lambda: self.combo_manager.combo,
            practice=self.practice_controller,
        )
        # self.overlay.raise_()

        root.addWidget(self.video_container, stretch=1)
        root.addWidget(self._build_transport_bar(central))
        self.setCentralWidget(central)

        self.stats_label = QLabel("")
        self.pad_label = QLabel("🎮 waiting…")
        self.statusBar().addPermanentWidget(self.stats_label)
        self.statusBar().addPermanentWidget(self.pad_label)
        self.statusBar().showMessage("Open a video to begin.  Mode: IDLE")

    def _build_transport_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)

        self.play_btn = QPushButton("▶ Play")
        self.play_btn.setFixedWidth(90)
        self.step_back_btn = QPushButton("⏮ Frame")
        self.step_fwd_btn = QPushButton("Frame ⏭")
        lay.addWidget(self.play_btn)
        lay.addWidget(self.step_back_btn)
        lay.addWidget(self.step_fwd_btn)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        lay.addWidget(self.seek_slider, stretch=1)

        self.frame_label = QLabel("Frame 0  ·  00:00.000")
        self.frame_label.setMinimumWidth(190)
        lay.addWidget(self.frame_label)

        lay.addWidget(QLabel("Speed:"))
        self.speed_box = QComboBox()
        for s in PLAYBACK_SPEEDS:
            self.speed_box.addItem(f"{s:g}x", userData=s)
        self.speed_box.setCurrentIndex(PLAYBACK_SPEEDS.index(1.00))
        lay.addWidget(self.speed_box)
        return bar

    def _build_menus(self) -> None:
        m_file = self.menuBar().addMenu("&File")
        for text, key, slot in [
            ("Open &Video…", "Ctrl+O", self._open_video_dialog),
            ("Open &Combo JSON…", "Ctrl+Shift+O", self._open_combo_dialog),
        ]:
            act = QAction(text, self)
            act.setShortcut(key)
            act.triggered.connect(slot)
            m_file.addAction(act)

        self.act_save_combo = QAction("&Save Combo", self)
        self.act_save_combo.setShortcut("Ctrl+S")
        self.act_save_combo.triggered.connect(self._save_combo_dialog)
        self.act_save_combo.setEnabled(False)
        m_file.addAction(self.act_save_combo)
        m_file.addSeparator()
        act_quit = QAction("E&xit", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_mode = self.menuBar().addMenu("&Mode")
        group = QActionGroup(self)
        group.setExclusive(True)
        self.mode_actions: dict[AppMode, QAction] = {}
        for mode, text, key in [
            (AppMode.IDLE, "&Idle", "F1"),
            (AppMode.RECORD, "&Record", "F2"),
            (AppMode.PRACTICE, "&Practice", "F3"),
        ]:
            act = QAction(text, self, checkable=True)
            act.setShortcut(key)
            act.triggered.connect(
                lambda _c, m=mode: self.mode_controller.request_mode(m)
            )
            group.addAction(act)
            m_mode.addAction(act)
            self.mode_actions[mode] = act
        self.mode_actions[AppMode.IDLE].setChecked(True)

        m_mode.addSeparator()
        self.act_facing_left = QAction(
            "Facing &Left (mirror motions)", self, checkable=True
        )
        self.act_facing_left.toggled.connect(
            lambda on: self.input_thread.set_facing_right(not on)
        )
        m_mode.addAction(self.act_facing_left)

        m_input = self.menuBar().addMenu("&Input")
        act_test = QAction("Controller &Test…", self)
        act_test.setShortcut("Ctrl+T")
        act_test.triggered.connect(self._open_controller_test)
        m_input.addAction(act_test)

    # -- wiring --------------------------------------------------------------------

    def _wire_signals(self) -> None:
        p = self.player
        p.fps_detected.connect(
            lambda f: self.statusBar().showMessage(f"Video FPS: {f:.3f}", 5000)
        )
        p.duration_changed.connect(lambda s: setattr(self, "_duration_s", s))
        p.pause_changed.connect(
            lambda paused: self.play_btn.setText("▶ Play" if paused else "⏸ Pause")
        )
        p.file_loaded.connect(self._on_file_loaded)

        self.play_btn.clicked.connect(p.toggle_pause)
        self.step_back_btn.clicked.connect(lambda: p.frame_step(forward=False))
        self.step_fwd_btn.clicked.connect(lambda: p.frame_step(forward=True))
        self.speed_box.currentIndexChanged.connect(
            lambda i: p.set_speed(self.speed_box.itemData(i))
        )
        self.seek_slider.sliderPressed.connect(
            lambda: setattr(self, "_slider_dragging", True)
        )
        self.seek_slider.sliderReleased.connect(self._on_slider_released)

        mc = self.mode_controller
        mc.mode_changed.connect(self._on_mode_changed)
        mc.transition_rejected.connect(
            lambda reason: QMessageBox.warning(self, "Cannot switch mode", reason)
        )

        it = self.input_thread
        it.input_event.connect(self._route_input)
        it.direction_changed.connect(self.overlay.set_direction)
        it.connected_changed.connect(
            lambda ok: self.pad_label.setText(
                "🎮 connected" if ok else "🎮 disconnected"
            )
        )
        it.error.connect(lambda msg: self.statusBar().showMessage(msg, 8000))

        self.record_controller.input_recorded.connect(self._on_input_recorded)

        pc = self.practice_controller
        pc.note_judged.connect(self._on_note_judged)
        pc.stray_input.connect(lambda _ev: self.overlay.spawn_stray())
        pc.stats_changed.connect(self._on_stats_changed)
        pc.run_finished.connect(self._on_run_finished)

    # -- input routing (the mode switchboard) ------------------------------------------

    def _route_input(self, ev) -> None:
        mode = self.mode_controller.mode
        if mode is AppMode.RECORD:
            self.record_controller.on_input(ev)
        elif mode is AppMode.PRACTICE:
            self.practice_controller.on_input(ev)
        # IDLE: dropped.

    # -- tick ----------------------------------------------------------------------------

    def _on_ui_tick(self) -> None:
        frame_f = self.frame_clock.current_frame_f()
        t = self.frame_clock.current_time()
        mins, secs = divmod(max(t, 0.0), 60.0)
        self.frame_label.setText(
            f"Frame {int(frame_f)}  ·  {int(mins):02d}:{secs:06.3f}"
        )

        if self._duration_s > 0 and not self._slider_dragging:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(int(1000 * min(t / self._duration_s, 1.0)))
            self.seek_slider.blockSignals(False)

        if self.mode_controller.mode is AppMode.PRACTICE:
            self.practice_controller.on_frame(round(frame_f))

        self.overlay.update()

    # -- feedback slots ---------------------------------------------------------------------

    def _on_input_recorded(self, entry) -> None:
        self.overlay.spawn_record_flash(entry)
        n = len(self.combo_manager.combo.inputs) if self.combo_manager.combo else 0
        self.stats_label.setText(f"REC · {n} inputs")

    def _on_note_judged(self, _idx: int, status: NoteStatus, _delta: int) -> None:
        self.overlay.spawn_judgement(status)
        if status is NoteStatus.PERFECT:
            self.sfx.play_perfect()

    def _on_stats_changed(self, s) -> None:
        self.stats_label.setText(
            f"P:{s.perfect}  G:{s.good}  M:{s.miss}  stray:{s.stray}  "
            f"acc:{s.accuracy * 100:.0f}%"
        )

    def _on_run_finished(self, s) -> None:
        self.statusBar().showMessage(
            f"Run complete — {s.perfect} Perfect / {s.good} Good / {s.miss} Miss "
            f"({s.accuracy * 100:.0f}%)",
            10000,
        )

    # -- mode transitions -----------------------------------------------------------------------

    def _on_mode_changed(self, old: AppMode, new: AppMode) -> None:
        self.mode_actions[new].setChecked(True)
        self.statusBar().showMessage(f"Mode: {new.name}")

        if old is AppMode.PRACTICE:
            self.practice_controller.stop()
            self.overlay.set_practice_active(False)

        if new is AppMode.RECORD:
            if not self.combo_manager.is_loaded:
                self.combo_manager.new_combo(
                    video_path=self.player.loaded_path or "",
                    video_fps=self.player.fps,
                )
            self.mode_controller.combo_loaded = True
            self.act_save_combo.setEnabled(True)
            self.stats_label.setText("REC · armed")

        elif new is AppMode.PRACTICE:
            combo = self.combo_manager.combo
            if combo is not None:
                self.practice_controller.start(combo)
                self.overlay.set_practice_active(True)
                self.player.seek_seconds(0.0)  # restart from the top, paused
                self.statusBar().showMessage("Practice armed — press Play when ready.")

        else:
            self.stats_label.setText("")

    # -- transport / file slots ---------------------------------------------------------------------

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        if self._duration_s > 0:
            target_s = self.seek_slider.value() / 1000 * self._duration_s
            self.player.seek_seconds(target_s)
            if self.mode_controller.mode is AppMode.PRACTICE:
                self.practice_controller.resync(round(target_s * self.frame_clock.fps))

    def _on_file_loaded(self, path: str) -> None:
        self.mode_controller.video_loaded = True
        self.statusBar().showMessage(f"Loaded video: {Path(path).name}", 5000)

    def _open_controller_test(self) -> None:
        ControllerTestDialog(self.input_thread, self).exec()

    def _open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open combo video",
            "",
            "Video files (*.mp4 *.mkv *.webm *.mov);;All files (*)",
        )
        if path:
            self.player.load(path)

    def _open_combo_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open combo file", "", "Combo JSON (*.json)"
        )
        if not path:
            return
        try:
            combo = self.combo_manager.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Failed to load combo", str(exc))
            return
        self.mode_controller.combo_loaded = True
        self.act_save_combo.setEnabled(True)
        self.player.load(combo.video_path)
        self.statusBar().showMessage(
            f"Loaded combo '{combo.combo_name}' ({len(combo.inputs)} inputs)", 5000
        )

    def _save_combo_dialog(self) -> None:
        if not self.combo_manager.is_loaded:
            return
        target = self.combo_manager.json_path
        if target is None:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save combo file", "combo.json", "Combo JSON (*.json)"
            )
            if not path:
                return
            target = path
        saved = self.combo_manager.save(target)
        self.statusBar().showMessage(f"Saved {saved.name}", 5000)

    # -- geometry / lifecycle -----------------------------------------------------------------------------
    def _sync_overlay(self) -> None:
        """Keep the floating overlay perfectly aligned with the video container's global position."""
        if hasattr(self, "overlay") and hasattr(self, "video_container"):
            # Map the local top-left corner to global screen coordinates
            top_left = self.video_container.mapToGlobal(
                self.video_container.rect().topLeft()
            )
            self.overlay.setGeometry(
                top_left.x(),
                top_left.y(),
                self.video_container.width(),
                self.video_container.height(),
            )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_overlay()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._sync_overlay()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_overlay()
        self.overlay.show()  # Explicitly show the floating window

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.combo_manager.is_dirty:
            choice = QMessageBox.question(
                self,
                "Unsaved combo",
                "The current combo has unsaved changes. Quit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice is QMessageBox.StandardButton.No:
                event.ignore()
                return
        self.input_thread.stop()
        self.player.shutdown()
        event.accept()
