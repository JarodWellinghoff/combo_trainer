"""
audio.py — Hit-confirm sound effect.

Ships zero binary assets: on first run we synthesize a short, punchy
"hit confirm" blip (falling-pitch sine + bright transient, exponential
decay) into assets/hit_confirm.wav, then play it through QSoundEffect,
which is low-latency enough for rhythm feedback.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from PyQt6.QtCore import QUrl

try:
    from PyQt6.QtMultimedia import QSoundEffect
except Exception:  # pragma: no cover — multimedia plugin missing
    QSoundEffect = None

ASSET_DIR = Path(__file__).resolve().parent / "assets"
HIT_WAV = ASSET_DIR / "hit_confirm.wav"

_SR = 44100


def ensure_assets() -> None:
    if HIT_WAV.exists():
        return
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    n_samples = int(_SR * 0.09)
    frames = bytearray()
    for n in range(n_samples):
        t = n / _SR
        body = 0.62 * math.sin(2 * math.pi * (1500 - 5200 * t) * t) * math.exp(-t * 42)
        snap = 0.30 * math.sin(2 * math.pi * 3400 * t) * math.exp(-t * 110)
        s = max(-1.0, min(1.0, body + snap))
        frames += struct.pack("<h", int(s * 32767))
    with wave.open(str(HIT_WAV), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(bytes(frames))


class SfxPlayer:
    """Preloaded, retrigger-safe hit-confirm player."""

    def __init__(self) -> None:
        ensure_assets()
        self._fx = None
        if QSoundEffect is not None:
            self._fx = QSoundEffect()
            self._fx.setSource(QUrl.fromLocalFile(str(HIT_WAV)))
            self._fx.setVolume(0.85)

    def play_perfect(self) -> None:
        if self._fx is not None:
            self._fx.stop()  # retrigger cleanly on fast note streams
            self._fx.play()
