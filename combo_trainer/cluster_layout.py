"""
cluster_layout.py — Pure geometry for rendering N simultaneous inputs as one
compact icon grid instead of N icons stacked on top of each other.

Qt-free by design, matching `motion_parser.py`/`models.py`: no PyQt6 import,
so the layout math is unit-testable without a Qt install, and any renderer
(today: the timeline lane) can consume it without touching a painter.

Why this exists
----------------
The backend already resolves simultaneous presses correctly — a macro
binding fires several logical inputs off one switch, and several switches
pressed in the same poll tick land on the identical video frame (see
`input_engine.InputThread.run`). But `ComboFile.inputs` stays a flat,
frame-sorted list (`ComboManager.add_input` keeps it that way): a 3-button
press becomes 3 separate `ComboInput` rows at the same `frame`. The OLD
timeline renderer drew one full-size icon per row at
`x = f(note.frame)`, so same-frame rows landed on the exact same x and drew
directly on top of each other.

`group_by_key()` recovers the "3 inputs, 1 moment" grouping from that flat
list; `cluster_layout()` turns a group of any size into a small, centered
grid of non-overlapping icon positions.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence, TypeVar

T = TypeVar("T")
K = TypeVar("K")

#: Soft cap on a cluster's bounding box in px — NOT an icon-size floor. Icons
#: shrink and wrap into more rows to stay inside this box as the group grows,
#: so a cluster never overflows the lane regardless of how many switches
#: fire at once (a full 16-button mash still fits a 4x4 grid inside it).
CLUSTER_MAX_SPAN = 96.0
CLUSTER_GAP = 4.0
#: Icons never shrink past this — beyond it we'd rather grow the bounding
#: box than render an unreadable dot. (Not currently reachable at n<=16
#: with the default CLUSTER_MAX_SPAN/ICON_RADIUS, but kept as a hard floor.)
CLUSTER_MIN_RADIUS = 6.0


@dataclass(frozen=True)
class ClusterCell:
    """One icon's position within a cluster, relative to the cluster's own
    center point — callers add their own (x, y) anchor (e.g. the lane's hit
    line) to place the whole group."""

    index: int  # position within the group, 0-based, row-major
    dx: float
    dy: float
    radius: float


def cluster_layout(count: int, base_radius: float) -> list[ClusterCell]:
    """
    Lay out `count` icons in a compact, centered grid of side `ceil(sqrt(n))`.

    Equivalent to CSS `grid-auto-flow: row` + `place-items: center` inside a
    fixed-size container: as the group grows, icons both shrink AND wrap
    into more rows rather than spilling past `CLUSTER_MAX_SPAN`. A short
    final row is centered under the row(s) above it — the same effect as
    `flex-wrap: wrap; justify-content: center` — instead of hugging one edge.

    `count <= 1` returns the icon centered at (0, 0) at `base_radius`
    unchanged, so a single press renders exactly as it always has.
    """
    if count <= 0:
        return []
    if count == 1:
        return [ClusterCell(0, 0.0, 0.0, base_radius)]

    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    cell_w = CLUSTER_MAX_SPAN / cols
    cell_h = CLUSTER_MAX_SPAN / rows
    radius = max(
        CLUSTER_MIN_RADIUS, min(base_radius, (min(cell_w, cell_h) - CLUSTER_GAP) / 2)
    )
    step = radius * 2 + CLUSTER_GAP
    total_h = rows * step - CLUSTER_GAP

    cells: list[ClusterCell] = []
    for i in range(count):
        row, col = divmod(i, cols)
        items_in_row = min(cols, count - row * cols)
        row_w = items_in_row * step - CLUSTER_GAP
        dx = -row_w / 2 + radius + col * step
        dy = -total_h / 2 + radius + row * step
        cells.append(ClusterCell(i, dx, dy, radius))
    return cells


def group_by_key(items: Sequence[T], key: Callable[[T], K]) -> Iterator[tuple[K, list[int]]]:
    """
    Yield `(key_value, [original_indices])` for RUNS of adjacent items
    sharing `key(item)`.

    Assumes `items` is already ordered so equal keys are contiguous — true
    of `ComboFile.inputs`, which `ComboManager` keeps frame-sorted. This
    does NOT sort or bucket non-adjacent matches back together; two notes at
    the same frame separated by a different frame in between stay separate
    groups (which should never happen for `frame`, but the function makes no
    assumption beyond "adjacent").
    """
    for _, run in itertools.groupby(enumerate(items), key=lambda pair: key(pair[1])):
        idxs = [i for i, _ in run]
        yield key(items[idxs[0]]), idxs
