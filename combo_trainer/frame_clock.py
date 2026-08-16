"""
frame_clock.py — The shared, thread-safe "what video frame is it RIGHT NOW?" object.

Written to by mpv's property-observer thread (highest accuracy source),
read by:
  * the XInput polling thread, to stamp button presses, and
  * the GUI thread, to position timeline icons with sub-frame smoothness.

Between mpv's per-frame `time-pos` notifications we extrapolate:
    t = last_time_pos + (monotonic_now - update_stamp) * playback_speed
so a press landing 9 ms after the last notification still resolves to the
correct frame, even at 0.25x speed. While paused, no extrapolation occurs —
frame-stepping in Record mode is therefore exact.
"""

from __future__ import annotations

import threading
import time


class FrameClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._time_pos: float = 0.0     # seconds, last reported by mpv
        self._stamp: float = time.monotonic()
        self._speed: float = 1.0
        self._paused: bool = True
        self._fps: float = 60.0

    # -- writers (mpv observer thread / GUI thread) ---------------------------

    def update_time_pos(self, time_pos: float) -> None:
        with self._lock:
            self._time_pos = time_pos
            self._stamp = time.monotonic()

    def update_speed(self, speed: float) -> None:
        with self._lock:
            # Re-anchor so the speed change doesn't warp extrapolation.
            self._reanchor_locked()
            self._speed = speed

    def update_paused(self, paused: bool) -> None:
        with self._lock:
            self._reanchor_locked()
            self._paused = paused

    def update_fps(self, fps: float) -> None:
        if fps > 0:
            with self._lock:
                self._fps = fps

    def _reanchor_locked(self) -> None:
        if not self._paused:
            elapsed = time.monotonic() - self._stamp
            self._time_pos += elapsed * self._speed
        self._stamp = time.monotonic()

    # -- readers (any thread) ---------------------------------------------------

    @property
    def fps(self) -> float:
        with self._lock:
            return self._fps

    def current_time(self) -> float:
        """Extrapolated playback position in seconds."""
        with self._lock:
            if self._paused:
                return self._time_pos
            return self._time_pos + (time.monotonic() - self._stamp) * self._speed

    def current_frame_f(self) -> float:
        """Float frame number — used by the renderer for smooth scrolling."""
        with self._lock:
            t = self._time_pos
            if not self._paused:
                t += (time.monotonic() - self._stamp) * self._speed
            return t * self._fps

    def current_frame(self) -> int:
        """Integer frame number — used to stamp recorded/judged inputs."""
        return round(self.current_frame_f())
