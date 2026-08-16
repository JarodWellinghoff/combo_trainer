"""
Unit tests for the live input-history parsing/aggregation logic. Pure
stdlib `unittest`, no PyQt6/XInput required — see `input_history.py`'s
module docstring for why this stays Qt-free.
"""

from __future__ import annotations

import unittest

from combo_trainer.input_history import HistoryRow, InputHistory, numpad_to_cardinals
from combo_trainer.input_types import FrameState, InputEvent
from combo_trainer.models import Button, Motion
from combo_trainer.profiles import Direction


def make_event(button: Button, frame: int = 10, **kw) -> InputEvent:
    return InputEvent(
        frame=frame, button=button, motion=Motion.NONE, direction=5,
        t_monotonic=1.0, logical_name=button.value, **kw,
    )


class TestNumpadToCardinals(unittest.TestCase):
    def test_neutral_is_empty(self):
        self.assertEqual(numpad_to_cardinals(5), ())

    def test_cardinals(self):
        self.assertEqual(numpad_to_cardinals(8), (Direction.UP,))
        self.assertEqual(numpad_to_cardinals(2), (Direction.DOWN,))
        self.assertEqual(numpad_to_cardinals(4), (Direction.LEFT,))
        self.assertEqual(numpad_to_cardinals(6), (Direction.RIGHT,))

    def test_diagonals_decompose_to_exactly_two(self):
        self.assertEqual(numpad_to_cardinals(3), (Direction.DOWN, Direction.RIGHT))
        self.assertEqual(numpad_to_cardinals(7), (Direction.UP, Direction.LEFT))
        for d in (1, 3, 7, 9):
            self.assertEqual(len(numpad_to_cardinals(d)), 2)

    def test_unknown_value_is_empty_not_a_crash(self):
        self.assertEqual(numpad_to_cardinals(0), ())
        self.assertEqual(numpad_to_cardinals(99), ())


class TestHistoryRowParsing(unittest.TestCase):
    def test_motion_only_row_has_no_actions(self):
        state = FrameState(frame=10, t_monotonic=1.0, direction=2, events=())
        row = HistoryRow.from_frame_state(state)
        self.assertEqual(row.motions, (Direction.DOWN,))
        self.assertEqual(row.actions, ())
        self.assertFalse(row.is_empty)

    def test_action_only_row_has_no_motions(self):
        ev = make_event(Button.HEAVY)
        state = FrameState(frame=10, t_monotonic=1.0, direction=5, events=(ev,))
        row = HistoryRow.from_frame_state(state)
        self.assertEqual(row.motions, ())
        self.assertEqual(row.actions, (ev,))
        self.assertFalse(row.is_empty)

    def test_command_normal_row_has_both(self):
        """Down+Heavy fired from one switch: both columns populated from the
        SAME tick's payload -- this is the whole point of bundling."""
        ev = make_event(Button.HEAVY, command_normal=True)
        state = FrameState(frame=10, t_monotonic=1.0, direction=2, events=(ev,))
        row = HistoryRow.from_frame_state(state)
        self.assertEqual(row.motions, (Direction.DOWN,))
        self.assertEqual(row.actions, (ev,))

    def test_macro_row_carries_every_fired_action(self):
        l, m = make_event(Button.LIGHT), make_event(Button.MEDIUM)
        state = FrameState(frame=10, t_monotonic=1.0, direction=5, events=(l, m))
        row = HistoryRow.from_frame_state(state)
        self.assertEqual(row.actions, (l, m))

    def test_no_direction_and_no_action_is_flagged_empty(self):
        state = FrameState(frame=10, t_monotonic=1.0, direction=5, events=())
        self.assertTrue(HistoryRow.from_frame_state(state).is_empty)


class TestInputHistoryBuffer(unittest.TestCase):
    def test_push_returns_and_appends_the_row(self):
        history = InputHistory(maxlen=10)
        state = FrameState(frame=1, t_monotonic=1.0, direction=8, events=())
        row = history.push(state)
        self.assertEqual(history.rows, [row])
        self.assertEqual(row.motions, (Direction.UP,))

    def test_bounded_length_drops_oldest_first(self):
        history = InputHistory(maxlen=3)
        for i in range(5):
            history.push(FrameState(frame=i, t_monotonic=float(i), direction=5, events=()))
        self.assertEqual([r.frame for r in history.rows], [2, 3, 4])

    def test_newest_first_view_reverses_without_mutating_storage(self):
        history = InputHistory(maxlen=10)
        for i in range(3):
            history.push(FrameState(frame=i, t_monotonic=float(i), direction=5, events=()))
        self.assertEqual([r.frame for r in history.newest_first], [2, 1, 0])
        self.assertEqual([r.frame for r in history.rows], [0, 1, 2])  # storage untouched

    def test_clear_empties_the_buffer(self):
        history = InputHistory()
        history.push(FrameState(frame=1, t_monotonic=1.0, direction=5, events=()))
        history.clear()
        self.assertEqual(history.rows, [])


if __name__ == "__main__":
    unittest.main()
