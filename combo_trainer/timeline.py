"""
timeline.py — The horizontal rhythm timeline overlay.

Rendering model
---------------
Icons live in FRAME space. Every repaint (~60 Hz, driven by MainWindow's UI
clock) reads the FrameClock's extrapolated float frame and places each note:

    x = hit_zone_x + (note.frame - current_frame_f) * PX_PER_FRAME

Because current_frame_f advances at (fps * playback_speed) frames per real
second, scroll speed scales with mpv's speed automatically — 0.5x video =
half scroll speed, with the hit zone alignment preserved exactly.

Feedback effects (hit sparks, judgement popups, record flashes) are short
lived dicts pruned each frame.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .controllers import NoteStatus, PracticeController
from .frame_clock import FrameClock
from .models import Button, ComboFile, ComboInput, Motion

PX_PER_FRAME = 8.0
LANE_HEIGHT = 120
HIT_ZONE_X_FRAC = 0.18
ICON_RADIUS = 24

BUTTON_COLORS: dict[Button, QColor] = {
    # Marvel Tōkon
    Button.LIGHT: QColor(90, 170, 255),
    Button.MEDIUM: QColor(250, 200, 60),
    Button.HEAVY: QColor(240, 80, 80),
    Button.UNIQUE: QColor(190, 120, 255),
    Button.ASSEMBLE: QColor(255, 140, 60),
    Button.QUICK_SKILL: QColor(90, 230, 130),
    Button.QUICK_ASSEMBLE: QColor(255, 190, 120),
    Button.QUICK_DASH: QColor(120, 200, 255),
    Button.THROW: QColor(170, 170, 180),
    # Legacy values (pre-profile combo files)
    Button.SPECIAL: QColor(90, 230, 130),
    Button.ASSIST1: QColor(190, 120, 255),
    Button.ASSIST2: QColor(255, 140, 60),
    Button.TAG: QColor(80, 220, 220),
}
BUTTON_LABELS: dict[Button, str] = {
    Button.LIGHT: "L",
    Button.MEDIUM: "M",
    Button.HEAVY: "H",
    Button.UNIQUE: "U",
    Button.ASSEMBLE: "AS",
    Button.QUICK_SKILL: "QS",
    Button.QUICK_ASSEMBLE: "QA",
    Button.QUICK_DASH: "QD",
    Button.THROW: "TH",
    Button.SPECIAL: "S",
    Button.ASSIST1: "A1",
    Button.ASSIST2: "A2",
    Button.TAG: "TAG",
}
MOTION_GLYPHS: dict[Motion, str] = {
    Motion.QCF: "↓↘→",
    Motion.QCB: "↓↙←",
    Motion.DP: "→↓↘",
    Motion.RDP: "←↓↙",
    Motion.HCF: "←↓→",
    Motion.HCB: "→↓←",
    Motion.DD: "↓↓",
    Motion.CHARGE_B_F: "[←]→",
    Motion.CHARGE_D_U: "[↓]↑",
}
STATUS_COLORS: dict[NoteStatus, QColor] = {
    NoteStatus.PERFECT: QColor(255, 215, 60),
    NoteStatus.GOOD: QColor(110, 230, 120),
    NoteStatus.MISS: QColor(235, 70, 70),
    NoteStatus.SKIPPED: QColor(120, 120, 130),
}

_EFFECT_LIFE_S = 0.45


class TimelineOverlay(QWidget):
    """Transparent, mouse-through overlay stacked over the video widget."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        # Detach from Qt's backing store to float over the native mpv window
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._clock: Optional[FrameClock] = None
        self._get_combo: Callable[[], Optional[ComboFile]] = lambda: None
        self._practice: Optional[PracticeController] = None
        self._practice_active = False
        self._effects: list[dict] = []
        self._current_direction = 5  # debug HUD for the leverless box

        self._icon_font = QFont("Segoe UI", 12, QFont.Weight.Bold)
        self._glyph_font = QFont("Segoe UI Symbol", 10, QFont.Weight.Bold)
        self._popup_font = QFont("Segoe UI", 14, QFont.Weight.Black)

    # -- wiring -----------------------------------------------------------------

    def bind(
        self,
        clock: FrameClock,
        get_combo: Callable[[], Optional[ComboFile]],
        practice: PracticeController,
    ) -> None:
        self._clock = clock
        self._get_combo = get_combo
        self._practice = practice

    def set_practice_active(self, active: bool) -> None:
        self._practice_active = active

    def set_direction(self, direction: int) -> None:
        self._current_direction = direction

    # -- effect spawns (GUI thread, via queued signals) ----------------------------

    def spawn_judgement(self, status: NoteStatus) -> None:
        self._effects.append(
            {
                "kind": "popup",
                "t0": time.monotonic(),
                "text": status.value,
                "color": STATUS_COLORS[status],
            }
        )
        if status is NoteStatus.PERFECT:
            self._effects.append({"kind": "spark", "t0": time.monotonic()})

    def spawn_record_flash(self, entry: ComboInput) -> None:
        self._effects.append(
            {
                "kind": "popup",
                "t0": time.monotonic(),
                "text": f"REC {BUTTON_LABELS.get(entry.button, entry.button.value)}",
                "color": QColor(255, 90, 90),
            }
        )

    def spawn_stray(self) -> None:
        self._effects.append(
            {
                "kind": "popup",
                "t0": time.monotonic(),
                "text": "·",
                "color": QColor(150, 150, 160),
            }
        )

    # -- painting ----------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        lane_top = self.height() - LANE_HEIGHT
        lane_mid = lane_top + LANE_HEIGHT // 2
        hit_x = int(self.width() * HIT_ZONE_X_FRAC)

        # Lane background + hit zone.
        p.fillRect(0, lane_top, self.width(), LANE_HEIGHT, QColor(8, 8, 16, 175))
        p.setPen(QPen(QColor(0, 220, 255, 235), 3))
        p.drawLine(hit_x, lane_top + 6, hit_x, self.height() - 6)
        p.drawEllipse(QPointF(hit_x, lane_mid), ICON_RADIUS + 6, ICON_RADIUS + 6)

        combo = self._get_combo()
        if combo is not None and self._clock is not None:
            self._paint_notes(p, combo, hit_x, lane_mid, lane_top)

        self._paint_effects(p, hit_x, lane_mid)
        self._paint_direction_hud(p, lane_top)
        p.end()

    def _paint_notes(
        self, p: QPainter, combo: ComboFile, hit_x: int, lane_mid: int, lane_top: int
    ) -> None:
        cur = self._clock.current_frame_f()
        min_frame = cur - hit_x / PX_PER_FRAME - 4
        max_frame = cur + (self.width() - hit_x) / PX_PER_FRAME + 4

        for idx, note in enumerate(combo.inputs):
            if note.frame < min_frame:
                continue
            if note.frame > max_frame:
                break
            x = hit_x + (note.frame - cur) * PX_PER_FRAME

            status = NoteStatus.PENDING
            if self._practice_active and self._practice is not None:
                status = self._practice.status_at(idx)

            color = BUTTON_COLORS.get(note.button, QColor(200, 200, 200))
            alpha = 255
            if status in (NoteStatus.PERFECT, NoteStatus.GOOD):
                color, alpha = STATUS_COLORS[status], 120  # judged: ghosted
            elif status is NoteStatus.MISS:
                color, alpha = STATUS_COLORS[status], 160
            elif status is NoteStatus.SKIPPED:
                color, alpha = STATUS_COLORS[status], 80

            c = QColor(color)
            c.setAlpha(alpha)
            p.setPen(QPen(QColor(255, 255, 255, alpha), 2))
            p.setBrush(c)
            p.drawEllipse(QPointF(x, lane_mid), ICON_RADIUS, ICON_RADIUS)

            p.setFont(self._icon_font)
            p.setPen(QColor(10, 10, 14, alpha))
            p.drawText(
                QRectF(
                    x - ICON_RADIUS,
                    lane_mid - ICON_RADIUS,
                    ICON_RADIUS * 2,
                    ICON_RADIUS * 2,
                ),
                Qt.AlignmentFlag.AlignCenter,
                BUTTON_LABELS.get(note.button, note.button.value[:2]),
            )

            if note.motion is not Motion.NONE:
                p.setFont(self._glyph_font)
                p.setPen(QColor(255, 255, 255, alpha))
                p.drawText(
                    QRectF(x - 44, lane_top + 4, 88, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    MOTION_GLYPHS.get(note.motion, note.motion.value),
                )
            if status is NoteStatus.MISS:
                p.setPen(QPen(STATUS_COLORS[NoteStatus.MISS], 3))
                r = ICON_RADIUS * 0.7
                p.drawLine(QPointF(x - r, lane_mid - r), QPointF(x + r, lane_mid + r))
                p.drawLine(QPointF(x - r, lane_mid + r), QPointF(x + r, lane_mid - r))

    def _paint_effects(self, p: QPainter, hit_x: int, lane_mid: int) -> None:
        now = time.monotonic()
        self._effects = [e for e in self._effects if now - e["t0"] < _EFFECT_LIFE_S]
        for e in self._effects:
            age = (now - e["t0"]) / _EFFECT_LIFE_S  # 0..1
            fade = int(255 * (1.0 - age))
            if e["kind"] == "spark":
                # Expanding ring + star rays: the "Perfect" confirm flash.
                radius = ICON_RADIUS + 8 + age * 46
                ring = QColor(255, 230, 120, fade)
                p.setPen(QPen(ring, 4 * (1.0 - age) + 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(hit_x, lane_mid), radius, radius)
                ray_in, ray_out = radius * 0.55, radius * 1.15
                for dx, dy in (
                    (1, 0),
                    (-1, 0),
                    (0, 1),
                    (0, -1),
                    (0.7, 0.7),
                    (-0.7, 0.7),
                    (0.7, -0.7),
                    (-0.7, -0.7),
                ):
                    p.drawLine(
                        QPointF(hit_x + dx * ray_in, lane_mid + dy * ray_in),
                        QPointF(hit_x + dx * ray_out, lane_mid + dy * ray_out),
                    )
            elif e["kind"] == "popup":
                color = QColor(e["color"])
                color.setAlpha(fade)
                p.setFont(self._popup_font)
                p.setPen(color)
                y = lane_mid - ICON_RADIUS - 16 - age * 26  # floats upward
                p.drawText(
                    QRectF(hit_x - 90, y - 14, 180, 28),
                    Qt.AlignmentFlag.AlignCenter,
                    e["text"],
                )

    def _paint_direction_hud(self, p: QPainter, lane_top: int) -> None:
        """Tiny numpad readout (top-right of lane) to sanity-check SOCD."""
        p.setFont(self._glyph_font)
        p.setPen(QColor(140, 170, 200, 200))
        p.drawText(
            QRectF(self.width() - 96, lane_top + 4, 88, 20),
            Qt.AlignmentFlag.AlignRight,
            f"dir: {self._current_direction}",
        )
