"""
motion_parser.py — SOCD cleaning, numpad direction mapping, and motion
recognition over a direction ring buffer.

Pure Python (no Qt, no XInput) so it is fully unit-testable off-Windows.

Numpad notation (Player 1 facing RIGHT):
        7 8 9
        4 5 6        5 = neutral
        1 2 3

Ring buffer
-----------
The input thread appends a (monotonic_seconds, direction) sample ONLY when
the direction changes (edge-compressed at the source). When an attack button
is pressed, `parse_motion()` inspects the samples inside a wall-clock window
(default 300 ms — hand speed is real-time regardless of video playback
speed) and returns the highest-priority motion that matches, or Motion.NONE,
which is exactly Tōkon's Quick-Special case.

Matching strategy
-----------------
Each motion is a set of acceptable direction SUBSEQUENCES. Subsequence
matching (other directions may appear in between) makes the parser tolerant
of stray neutral frames and skipped diagonals on leverless. Priority order
resolves containment ambiguities (DP is checked before QCF because a DP
input contains a QCF-ish tail; half-circles before both).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import Motion

# --------------------------------------------------------------------------
# SOCD cleaning + numpad mapping
# --------------------------------------------------------------------------

SOCD_NEUTRAL = "neutral"        # L+R -> 5, U+D -> 5  (tournament standard)
SOCD_UP_PRIORITY = "up"         # L+R -> 5, U+D -> up (common leverless FW)

_MIRROR = {1: 3, 3: 1, 4: 6, 6: 4, 7: 9, 9: 7}


def clean_socd(up: bool, down: bool, left: bool, right: bool,
               mode: str = SOCD_NEUTRAL) -> int:
    """Resolve simultaneous opposing cardinals, return numpad direction 1-9."""
    if left and right:
        left = right = False
    if up and down:
        if mode == SOCD_UP_PRIORITY:
            down = False
        else:
            up = down = False
    x = -1 if left else (1 if right else 0)
    y = 1 if up else (-1 if down else 0)
    return 5 + x + 3 * y


def mirror_direction(direction: int) -> int:
    """Flip horizontally for a Player-2-facing (facing-left) session."""
    return _MIRROR.get(direction, direction)


# --------------------------------------------------------------------------
# Motion patterns
# --------------------------------------------------------------------------

_DOWNS = frozenset({1, 2, 3})
_BACKS = frozenset({1, 4, 7})
_FORWARDS = frozenset({3, 6, 9})
_UPS = frozenset({7, 8, 9})

# Priority-ordered: first match wins.
_SUBSEQUENCE_MOTIONS: tuple[tuple[Motion, tuple[tuple[int, ...], ...]], ...] = (
    (Motion.HCF, ((4, 2, 6),)),                     # 41236 lenient
    (Motion.HCB, ((6, 2, 4),)),                     # 63214 lenient
    (Motion.DP,  ((6, 2, 3), (6, 3, 2, 3))),        # 623 + common 6323 slide
    (Motion.RDP, ((4, 2, 1), (4, 1, 2, 1))),        # 421 mirror
    (Motion.QCF, ((2, 3, 6), (2, 6))),              # 236, tolerate skipped 3
    (Motion.QCB, ((2, 1, 4), (2, 4))),              # 214, tolerate skipped 1
)


@dataclass(frozen=True)
class ParserConfig:
    window_s: float = 0.30       # motion look-back window (wall clock)
    charge_s: float = 0.60       # ~36f at 60 fps; tune per game data
    detect_charge: bool = True
    detect_double_down: bool = True


@dataclass
class DirectionSample:
    t: float          # time.monotonic() seconds
    direction: int    # numpad 1-9


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def _is_subsequence(pattern: Sequence[int], seq: Sequence[int]) -> bool:
    it = iter(seq)
    return all(p in it for p in pattern)


def _groups_in_window(samples: Sequence[DirectionSample],
                      t_press: float,
                      window_s: float) -> list[tuple[int, float, float]]:
    """
    Return (direction, start_t, end_t) runs overlapping [t_press-window, t_press].
    `samples` is already edge-compressed (one entry per direction change), so
    each entry IS a run; its end is the next entry's start (or t_press).
    A run that began before the window still counts (charge holds!).
    """
    if not samples:
        return []
    w_start = t_press - window_s
    out: list[tuple[int, float, float]] = []
    times = [s.t for s in samples]
    # First run that is still active at w_start:
    i = max(bisect.bisect_right(times, w_start) - 1, 0)
    for j in range(i, len(samples)):
        start = samples[j].t
        if start > t_press:
            break
        end = samples[j + 1].t if j + 1 < len(samples) else t_press
        end = min(end, t_press)
        out.append((samples[j].direction, start, end))
    return out


def parse_motion(samples: Sequence[DirectionSample],
                 t_press: float,
                 config: ParserConfig = ParserConfig()) -> Motion:
    """Classify the motion (if any) completed at `t_press`."""
    groups = _groups_in_window(samples, t_press, config.window_s)
    if not groups:
        return Motion.NONE
    dirs = [g[0] for g in groups]

    # 1) Sequence motions, priority order.
    for motion, patterns in _SUBSEQUENCE_MOTIONS:
        if any(_is_subsequence(p, dirs) for p in patterns):
            return motion

    # 2) Double-down (22): two separate down-runs split by a non-down run.
    if config.detect_double_down and _has_double_tap(dirs, _DOWNS):
        return Motion.DD

    # 3) Charge motions: long hold of back/down (may predate the window),
    #    then the opposite direction at/near the press.
    if config.detect_charge:
        charge = _detect_charge(samples, groups, t_press, config.charge_s)
        if charge is not Motion.NONE:
            return charge

    return Motion.NONE


def _has_double_tap(dirs: Iterable[int], zone: frozenset[int]) -> bool:
    state = 0  # 0: seeking 1st tap, 1: seeking gap, 2: seeking 2nd tap
    for d in dirs:
        in_zone = d in zone
        if state == 0 and in_zone:
            state = 1
        elif state == 1 and not in_zone:
            state = 2
        elif state == 2 and in_zone:
            return True
    return False


def _detect_charge(samples: Sequence[DirectionSample],
                   window_groups: list[tuple[int, float, float]],
                   t_press: float,
                   charge_s: float) -> Motion:
    # The release direction is whatever run the press landed in (or just before).
    tail = window_groups[-1][0]
    if tail in _FORWARDS:
        hold_zone, motion = _BACKS, Motion.CHARGE_B_F
    elif tail in _UPS:
        hold_zone, motion = _DOWNS, Motion.CHARGE_D_U
    else:
        return Motion.NONE

    # Walk the FULL sample history backwards from the tail run, accumulating
    # contiguous time spent in the hold zone (allowing the diagonal overlap
    # runs, e.g. 1 counts for both back- and down-charge).
    idx = len(samples) - 1
    while idx >= 0 and samples[idx].t > t_press:
        idx -= 1
    # Skip the release tail run(s):
    while idx >= 0 and samples[idx].direction not in hold_zone:
        idx -= 1
    if idx < 0:
        return Motion.NONE
    held = 0.0
    end_t = samples[idx + 1].t if idx + 1 < len(samples) else t_press
    while idx >= 0 and samples[idx].direction in hold_zone:
        held += end_t - samples[idx].t
        end_t = samples[idx].t
        idx -= 1
    return motion if held >= charge_s else Motion.NONE


# --------------------------------------------------------------------------
# Ring buffer helper used by the input thread
# --------------------------------------------------------------------------

@dataclass
class DirectionRingBuffer:
    """Edge-compressed direction history with a hard size cap."""
    maxlen: int = 64
    _samples: list[DirectionSample] = field(default_factory=list)

    def push(self, t: float, direction: int) -> bool:
        """Append only if direction changed. Returns True if appended."""
        if self._samples and self._samples[-1].direction == direction:
            return False
        self._samples.append(DirectionSample(t, direction))
        if len(self._samples) > self.maxlen:
            del self._samples[: len(self._samples) - self.maxlen]
        return True

    @property
    def samples(self) -> list[DirectionSample]:
        return self._samples

    @property
    def current_direction(self) -> int:
        return self._samples[-1].direction if self._samples else 5

    def clear(self) -> None:
        self._samples.clear()
