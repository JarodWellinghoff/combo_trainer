"""
Unit tests for the cluster-grouping/layout math behind the timeline's
simultaneous-input rendering. Pure stdlib `unittest`, no PyQt6 required.
"""

from __future__ import annotations

import itertools
import math
import unittest

from combo_trainer.cluster_layout import (
    CLUSTER_MAX_SPAN,
    CLUSTER_MIN_RADIUS,
    ClusterCell,
    cluster_layout,
    group_by_key,
)

BASE_RADIUS = 24.0


class TestClusterLayoutSingleAndEmpty(unittest.TestCase):
    def test_empty_group_is_empty(self):
        self.assertEqual(cluster_layout(0, BASE_RADIUS), [])

    def test_single_press_is_unchanged_from_the_old_single_icon_rendering(self):
        cells = cluster_layout(1, BASE_RADIUS)
        self.assertEqual(cells, [ClusterCell(0, 0.0, 0.0, BASE_RADIUS)])


class TestClusterLayoutGrouping(unittest.TestCase):
    def test_every_count_from_two_to_sixteen_produces_exactly_n_cells(self):
        for n in range(2, 17):
            with self.subTest(n=n):
                cells = cluster_layout(n, BASE_RADIUS)
                self.assertEqual(len(cells), n)
                self.assertEqual([c.index for c in cells], list(range(n)))

    def test_full_sixteen_button_mash_does_not_overflow_the_bounding_box(self):
        """The headline requirement: 16 at once must not break or spill out."""
        cells = cluster_layout(16, BASE_RADIUS)
        for c in cells:
            with self.subTest(cell=c.index):
                self.assertLessEqual(abs(c.dx) + c.radius, CLUSTER_MAX_SPAN / 2 + 1e-9)
                self.assertLessEqual(abs(c.dy) + c.radius, CLUSTER_MAX_SPAN / 2 + 1e-9)

    def test_radius_never_exceeds_base_or_drops_below_the_hard_floor(self):
        for n in range(1, 33):  # defensively past 16 too
            cells = cluster_layout(n, BASE_RADIUS)
            for c in cells:
                self.assertLessEqual(c.radius, BASE_RADIUS)
                self.assertGreaterEqual(c.radius, CLUSTER_MIN_RADIUS)

    def test_radius_shrinks_monotonically_as_the_group_grows(self):
        radii = [cluster_layout(n, BASE_RADIUS)[0].radius for n in range(1, 17)]
        for a, b in zip(radii, radii[1:]):
            self.assertLessEqual(b, a + 1e-9)

    def test_no_two_icons_in_a_cluster_overlap(self):
        for n in (2, 3, 5, 7, 9, 13, 16):
            cells = cluster_layout(n, BASE_RADIUS)
            for a, b in itertools.combinations(cells, 2):
                with self.subTest(n=n, a=a.index, b=b.index):
                    dist = math.hypot(a.dx - b.dx, a.dy - b.dy)
                    self.assertGreaterEqual(dist, a.radius + b.radius - 1e-9)

    def test_short_last_row_is_horizontally_centered_not_edge_hugging(self):
        """3 items -> a 2x2 grid with one empty slot; the lone item on row 2
        should sit at dx=0 (centered), like flex-wrap + justify-content:center,
        not flush against whichever edge it happened to land on."""
        cells = cluster_layout(3, BASE_RADIUS)
        last = cells[-1]
        self.assertAlmostEqual(last.dx, 0.0, places=6)
        # And it should be a full row below the first two.
        self.assertGreater(last.dy, cells[0].dy)

    def test_grid_is_roughly_square_not_a_single_long_row(self):
        """A 16-wide macro should read as a grid, not one unreadable strip."""
        cells = cluster_layout(16, BASE_RADIUS)
        xs = {round(c.dx, 3) for c in cells}
        ys = {round(c.dy, 3) for c in cells}
        self.assertEqual(len(xs), 4)  # 4 distinct columns
        self.assertEqual(len(ys), 4)  # 4 distinct rows


class TestGroupByKey(unittest.TestCase):
    def test_empty_input_yields_nothing(self):
        self.assertEqual(list(group_by_key([], key=lambda x: x)), [])

    def test_groups_adjacent_equal_keys_and_keeps_original_indices(self):
        # Mirrors ComboFile.inputs: frame-sorted, ties from a simultaneous
        # 3-button press landing at the same frame.
        frames = [10, 10, 10, 40, 40, 90]
        groups = list(group_by_key(frames, key=lambda f: f))
        self.assertEqual(
            groups, [(10, [0, 1, 2]), (40, [3, 4]), (90, [5])]
        )

    def test_all_distinct_keys_each_form_a_singleton_group(self):
        frames = [1, 2, 3, 4]
        groups = list(group_by_key(frames, key=lambda f: f))
        self.assertEqual(groups, [(1, [0]), (2, [1]), (3, [2]), (4, [3])])

    def test_non_adjacent_equal_keys_stay_separate_groups(self):
        """Documents the function's contract: it groups RUNS, not all
        matches — callers are responsible for pre-sorting (which
        ComboManager already guarantees for `frame`)."""
        values = ["a", "b", "a"]
        groups = list(group_by_key(values, key=lambda v: v))
        self.assertEqual(groups, [("a", [0]), ("b", [1]), ("a", [2])])


if __name__ == "__main__":
    unittest.main()
