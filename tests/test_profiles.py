"""
Unit tests for the profile system.

Pure stdlib `unittest` and no Qt/XInput imports, so this runs anywhere:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from combo_trainer.games import (
    LEVERLESS_16_ID,
    TOKON_ID,
    leverless_16,
    marvel_tokon,
    seed_builtins,
    tokon_inputs,
)
from combo_trainer.models import Button, Motion
from combo_trainer.motion_parser import SOCD_NEUTRAL, SOCD_UP_PRIORITY, clean_socd
from combo_trainer.profiles import (
    Direction,
    InputCategory,
    InputProfile,
    LogicalInput,
    PhysicalButton,
    ProfileError,
    ProfileManager,
    ResolvedProfile,
    validate_layout,
)


def fresh_manager() -> ProfileManager:
    mgr = ProfileManager(Path(tempfile.mkdtemp()) / "profiles.json")
    return seed_builtins(mgr)


# --------------------------------------------------------------------------
# Device + vocabulary
# --------------------------------------------------------------------------


class TestDevice(unittest.TestCase):
    def test_sixteen_buttons_with_unique_sources(self):
        device = leverless_16()
        self.assertEqual(len(device), 16)
        self.assertEqual(len(set(device.ids)), 16)
        self.assertEqual(len({b.source for b in device}), 16)

    def test_groups_preserve_silkscreen_order(self):
        groups = [name for name, _ in leverless_16().groups()]
        self.assertEqual(groups, ["Movement", "Top row", "Bottom row", "Aux"])

    def test_set_source_rejects_a_collision(self):
        """16 switches saturate XInput's 16 sources, so every re-point collides."""
        device = leverless_16()
        with self.assertRaises(ProfileError):
            device.set_source("T1", "DPAD_UP")  # already owned by UP
        self.assertEqual(device.require("T1").source, "X")  # unchanged

    def test_swap_sources_rewires_without_touching_labels(self):
        """The fix for a box that reports its cluster in a different order."""
        device = leverless_16()
        device.swap_sources("T1", "T2")
        self.assertEqual(device.require("T1").source, "Y")
        self.assertEqual(device.require("T2").source, "X")
        self.assertEqual(device.require("T1").label, "Top 1 (index)")
        self.assertEqual(len({b.source for b in device}), 16)

    def test_swapping_sources_moves_the_binding_with_the_wire(self):
        game, device = marvel_tokon(), leverless_16()
        layout = game.require_layout("default")
        device.swap_sources("T1", "T2")  # LIGHT is on T1, MEDIUM on T2
        r = ResolvedProfile.resolve(game, layout, device)
        self.assertEqual(r.actions["Y"][0].button, Button.LIGHT)
        self.assertEqual(r.actions["X"][0].button, Button.MEDIUM)

    def test_unknown_xinput_source_rejected(self):
        with self.assertRaises(ProfileError):
            PhysicalButton("T9", "Nope", "NOT_A_SOURCE")


class TestVocabulary(unittest.TestCase):
    def test_tokon_declares_thirteen_inputs(self):
        inputs = tokon_inputs()
        self.assertEqual(len(inputs), 13)
        by_cat = {c: [i.id for i in inputs if i.category is c] for c in InputCategory}
        self.assertEqual(by_cat[InputCategory.DIRECTION], ["UP", "DOWN", "LEFT", "RIGHT"])
        self.assertEqual(
            by_cat[InputCategory.ATTACK], ["LIGHT", "MEDIUM", "HEAVY", "UNIQUE"]
        )
        self.assertEqual(
            by_cat[InputCategory.MECHANIC],
            ["ASSEMBLE", "QUICK_SKILL", "QUICK_ASSEMBLE", "QUICK_DASH", "THROW"],
        )

    def test_quick_inputs_do_not_absorb_motions(self):
        game = marvel_tokon()
        self.assertFalse(game.require_input("QUICK_SKILL").parse_motions)
        self.assertFalse(game.require_input("QUICK_ASSEMBLE").parse_motions)
        self.assertFalse(game.require_input("QUICK_DASH").parse_motions)
        # Attacks still record 236+H as QCF.
        self.assertTrue(game.require_input("HEAVY").parse_motions)
        self.assertTrue(game.require_input("ASSEMBLE").parse_motions)

    def test_directional_may_not_carry_a_button(self):
        with self.assertRaises(ProfileError):
            LogicalInput(
                "UP", "Up", InputCategory.DIRECTION,
                direction=Direction.UP, button=Button.LIGHT,
            )

    def test_action_needs_a_button(self):
        with self.assertRaises(ProfileError):
            LogicalInput("LIGHT", "Light", InputCategory.ATTACK)


# --------------------------------------------------------------------------
# The Marvel Tōkon default layout: 13 inputs onto 16 switches
# --------------------------------------------------------------------------


class TestTokonDefaultLayout(unittest.TestCase):
    def setUp(self):
        self.game = marvel_tokon()
        self.device = leverless_16()
        self.layout = self.game.require_layout("default")
        self.validation = validate_layout(self.game, self.layout, self.device)

    def test_every_logical_input_is_reachable(self):
        self.assertEqual(self.validation.unmapped_inputs, ())
        self.assertEqual(self.validation.unknown_bindings, {})
        self.assertTrue(self.validation.ok)

    def test_three_leftovers_put_to_three_different_uses(self):
        # duplicate
        self.assertEqual(self.layout.logical_for("AUX2"), ("UP",))
        self.assertEqual(
            set(self.validation.duplicate_inputs["UP"]), {"UP", "AUX2"}
        )
        # action macro
        self.assertEqual(self.layout.logical_for("AUX3"), ("LIGHT", "MEDIUM"))
        self.assertTrue(self.layout.is_macro("AUX3"))
        # Command Normal: direction + attack, one switch
        self.assertEqual(self.layout.logical_for("AUX4"), ("DOWN", "HEAVY"))
        self.assertTrue(self.layout.is_macro("AUX4"))  # still a multi-input binding
        self.assertEqual(self.validation.unbound_buttons, ())  # nothing left inert

    def test_resolution_splits_directions_from_actions(self):
        r = ResolvedProfile.resolve(self.game, self.layout, self.device)
        # Both Up switches feed the direction pipeline, neither is an action.
        self.assertEqual(set(r.up_sources), {"DPAD_UP", "RIGHT_THUMB"})
        self.assertNotIn("DPAD_UP", r.actions)
        self.assertNotIn("RIGHT_THUMB", r.actions)
        # Top row -> the four attacks.
        self.assertEqual(r.actions["X"][0].button, Button.LIGHT)
        self.assertEqual(r.actions["Y"][0].button, Button.MEDIUM)
        self.assertEqual(r.actions["RIGHT_SHOULDER"][0].button, Button.HEAVY)
        self.assertEqual(r.actions["LEFT_SHOULDER"][0].button, Button.UNIQUE)
        # Trigger sources work like any other switch.
        self.assertEqual(r.actions["RIGHT_TRIGGER"][0].button, Button.QUICK_SKILL)
        self.assertEqual(r.actions["LEFT_TRIGGER"][0].button, Button.QUICK_ASSEMBLE)

    def test_macro_resolves_to_two_actions_on_one_source(self):
        r = ResolvedProfile.resolve(self.game, self.layout, self.device)
        actions = r.actions["BACK"]  # AUX3
        self.assertEqual([a.button for a in actions], [Button.LIGHT, Button.MEDIUM])
        self.assertEqual(r.label_for("BACK"), "Light + Medium")

    def test_unbound_switch_resolves_to_nothing(self):
        """A switch with no binding at all contributes nothing, regardless of
        which specific switch that happens to be on the shipped layout."""
        layout = InputProfile(id="mostly_default", name="x", bindings=dict(self.layout.bindings))
        layout.clear_binding("AUX4")  # the shipped layout's only remaining free slot to test with
        r = ResolvedProfile.resolve(self.game, layout, self.device)
        self.assertNotIn("START", r.actions)
        self.assertNotIn("START", r.sources)
        self.assertEqual(r.label_for("START"), "")

    def test_duplicate_up_buttons_or_together_through_socd(self):
        """Either Up switch must produce numpad 8 — and both at once, too."""
        r = ResolvedProfile.resolve(self.game, self.layout, self.device)

        def numpad(*held: str) -> int:
            down = set(held)
            return clean_socd(
                up=any(s in down for s in r.up_sources),
                down=any(s in down for s in r.down_sources),
                left=any(s in down for s in r.left_sources),
                right=any(s in down for s in r.right_sources),
                mode=r.socd_mode,
            )

        self.assertEqual(numpad("DPAD_UP"), 8)
        self.assertEqual(numpad("RIGHT_THUMB"), 8)
        self.assertEqual(numpad("DPAD_UP", "RIGHT_THUMB"), 8)
        self.assertEqual(numpad("DPAD_DOWN", "DPAD_RIGHT"), 3)
        self.assertEqual(numpad("DPAD_LEFT", "DPAD_RIGHT"), 5)  # SOCD neutral
        self.assertEqual(numpad("RIGHT_THUMB", "DPAD_DOWN"), 5)  # U+D -> neutral

    def test_button_map_backcompat_view(self):
        r = ResolvedProfile.resolve(self.game, self.layout, self.device)
        self.assertEqual(r.button_map()["X"], Button.LIGHT)
        self.assertEqual(r.button_map()["BACK"], Button.LIGHT)  # macro: first only


class TestCommandNormal(unittest.TestCase):
    """AUX4's Down+Heavy binding: a single switch pairing a direction with an
    attack, resolved instantly (same frame, no ring-buffer lookback)."""

    def setUp(self):
        self.game = marvel_tokon()
        self.device = leverless_16()
        self.layout = self.game.require_layout("default")
        self.r = ResolvedProfile.resolve(self.game, self.layout, self.device)

    def test_action_is_flagged_command_normal(self):
        heavy = self.r.actions["START"][0]  # AUX4
        self.assertEqual(heavy.button, Button.HEAVY)
        self.assertTrue(heavy.command_normal)

    def test_plain_binding_of_the_same_button_is_unaffected(self):
        """T3 is plain Heavy, no direction — must NOT pick up the flag just
        because AUX4 also fires Button.HEAVY elsewhere."""
        heavy = self.r.actions["RIGHT_SHOULDER"][0]  # T3
        self.assertEqual(heavy.button, Button.HEAVY)
        self.assertFalse(heavy.command_normal)

    def test_direction_half_still_joins_the_ambient_socd_pool(self):
        """The Down half isn't swallowed by being paired with an action — it
        behaves exactly like a second physical Down button."""
        self.assertIn("START", self.r.down_sources)

    def test_release_of_the_macro_button_does_not_drop_a_real_held_down(self):
        """Regression for Task 3: releasing AUX4 while the real d-pad Down is
        also held must leave Down asserted — and vice versa. Each source is
        independently OR-ed every poll tick, so there is no shared state to
        corrupt on release."""

        def numpad(*held: str) -> int:
            down = set(held)
            return clean_socd(
                up=any(s in down for s in self.r.up_sources),
                down=any(s in down for s in self.r.down_sources),
                left=any(s in down for s in self.r.left_sources),
                right=any(s in down for s in self.r.right_sources),
                mode=self.r.socd_mode,
            )

        self.assertEqual(numpad("DPAD_DOWN", "START"), 2)  # both held
        self.assertEqual(numpad("DPAD_DOWN"), 2)  # AUX4 released, real Down stays
        self.assertEqual(numpad("START"), 2)  # real Down released, AUX4 stays
        self.assertEqual(numpad(), 5)  # both released -> neutral, no leftover state

    def test_label_shows_the_paired_direction(self):
        self.assertEqual(self.r.label_for("START"), "Down + Heavy")

    def test_is_still_a_macro_for_validation_bookkeeping(self):
        """Two logical ids on one switch is a macro either way; command_normal
        is an orthogonal, more specific flag layered on top."""
        v = validate_layout(self.game, self.layout, self.device)
        self.assertIn("AUX4", v.macros)
        self.assertEqual(v.macros["AUX4"], ("DOWN", "HEAVY"))

    def test_all_three_shipped_layouts_stay_playable(self):
        for layout in self.game.layouts.values():
            with self.subTest(layout=layout.id):
                self.assertTrue(validate_layout(self.game, layout, self.device).ok)


class TestBuiltinLayouts(unittest.TestCase):
    def test_all_shipped_layouts_are_playable(self):
        game, device = marvel_tokon(), leverless_16()
        self.assertEqual(len(game.layouts), 3)
        for layout in game.layouts.values():
            with self.subTest(layout=layout.id):
                self.assertTrue(validate_layout(game, layout, device).ok)

    def test_character_layout_lookup_and_socd_override(self):
        game = marvel_tokon()
        layout = game.layout_for_character("iron man")  # case-insensitive
        self.assertIsNotNone(layout)
        self.assertEqual(layout.id, "iron_man")
        self.assertEqual(layout.socd_mode, SOCD_UP_PRIORITY)
        self.assertEqual(game.require_layout("default").socd_mode, SOCD_NEUTRAL)
        self.assertIsNone(game.layout_for_character("Nobody"))


# --------------------------------------------------------------------------
# ProfileManager: creating, switching, rebinding
# --------------------------------------------------------------------------


class TestRebinding(unittest.TestCase):
    def setUp(self):
        self.mgr = fresh_manager()

    def test_seeded_active_selection(self):
        self.assertEqual(self.mgr.active_game_id, TOKON_ID)
        self.assertEqual(self.mgr.active_layout_id, "default")
        self.assertEqual(self.mgr.active_device.id, LEVERLESS_16_ID)
        self.assertTrue(self.mgr.validate().ok)

    def test_bind_replaces_and_takes_effect_on_resolve(self):
        self.mgr.bind("T1", "HEAVY")
        self.assertEqual(self.mgr.active_layout.logical_for("T1"), ("HEAVY",))
        self.assertEqual(self.mgr.resolved().actions["X"][0].button, Button.HEAVY)
        # LIGHT is now only on the AUX3 macro, so still reachable.
        self.assertNotIn("LIGHT", self.mgr.validate().unmapped_inputs)

    def test_unbind_makes_a_switch_inert(self):
        self.mgr.unbind("B2")  # THROW
        self.assertEqual(self.mgr.active_layout.logical_for("B2"), ())
        self.assertNotIn("B", self.mgr.resolved().actions)
        self.assertIn("THROW", self.mgr.validate().unmapped_inputs)
        self.assertFalse(self.mgr.validate().ok)

    def test_macro_build_up_and_tear_down(self):
        self.mgr.bind("AUX4", "LIGHT")
        self.mgr.add_to_macro("AUX4", "MEDIUM")
        self.mgr.add_to_macro("AUX4", "HEAVY")
        self.assertEqual(
            self.mgr.active_layout.logical_for("AUX4"), ("LIGHT", "MEDIUM", "HEAVY")
        )
        self.assertEqual(len(self.mgr.resolved().actions["START"]), 3)
        self.mgr.add_to_macro("AUX4", "HEAVY")  # idempotent
        self.assertEqual(len(self.mgr.active_layout.logical_for("AUX4")), 3)
        self.mgr.rebind("AUX4", ())
        self.assertNotIn("START", self.mgr.resolved().actions)

    def test_rebind_dedupes_within_one_binding(self):
        self.assertEqual(
            self.mgr.rebind("AUX4", ("LIGHT", "LIGHT", "MEDIUM")), ("LIGHT", "MEDIUM")
        )

    def test_swap_exchanges_two_switches(self):
        self.mgr.swap("T1", "T3")  # LIGHT <-> HEAVY
        self.assertEqual(self.mgr.active_layout.logical_for("T1"), ("HEAVY",))
        self.assertEqual(self.mgr.active_layout.logical_for("T3"), ("LIGHT",))
        self.assertTrue(self.mgr.validate().ok)

    def test_directional_rebinding_moves_the_socd_source(self):
        """Bind Up to a face button: it must leave the action table entirely."""
        self.mgr.unbind("UP")
        self.mgr.bind("T4", "UP")
        r = self.mgr.resolved()
        self.assertEqual(set(r.up_sources), {"LEFT_SHOULDER", "RIGHT_THUMB"})
        self.assertNotIn("LEFT_SHOULDER", r.actions)  # no longer UNIQUE
        self.assertIn("UNIQUE", self.mgr.validate().unmapped_inputs)

    def test_rejects_unknown_ids(self):
        with self.assertRaises(ProfileError):
            self.mgr.bind("T1", "SUPER_PUNCH")
        with self.assertRaises(ProfileError):
            self.mgr.bind("T99", "LIGHT")

    def test_socd_mode_is_per_layout(self):
        self.mgr.set_socd_mode(SOCD_UP_PRIORITY)
        self.assertEqual(self.mgr.resolved().socd_mode, SOCD_UP_PRIORITY)
        with self.assertRaises(ProfileError):
            self.mgr.set_socd_mode("sideways")

    def test_edits_do_not_leak_between_layouts(self):
        self.mgr.bind("T1", "THROW")
        other = self.mgr.active_game.require_layout("quick_focus")
        self.assertEqual(other.logical_for("T1"), ("LIGHT",))

    def test_listeners_fire_on_mutation(self):
        seen: list[str] = []
        self.mgr.add_listener(lambda m: seen.append(m.active_layout_id))
        self.mgr.bind("T1", "HEAVY")
        self.mgr.set_active_layout("quick_focus")
        self.assertEqual(seen, ["default", "quick_focus"])


class TestProfileLifecycle(unittest.TestCase):
    def setUp(self):
        self.mgr = fresh_manager()

    def test_create_layout_from_scratch(self):
        layout = self.mgr.create_layout("Empty Slate")
        self.assertEqual(layout.id, "empty_slate")
        self.assertEqual(layout.bindings, {})
        self.mgr.set_active_layout("empty_slate")
        self.assertEqual(len(self.mgr.validate().unmapped_inputs), 13)

    def test_create_layout_by_cloning_then_diverging(self):
        layout = self.mgr.create_layout(
            "Doctor Doom", copy_from="default", character="Doctor Doom"
        )
        self.mgr.set_active_layout(layout.id)
        self.mgr.bind("AUX4", "QUICK_DASH")
        self.assertEqual(layout.logical_for("T1"), ("LIGHT",))  # inherited
        self.assertEqual(
            self.mgr.active_game.require_layout("default").logical_for("AUX4"),
            ("DOWN", "HEAVY"),
        )

    def test_duplicate_layout_id_rejected(self):
        with self.assertRaises(ProfileError):
            self.mgr.create_layout("Default")  # slugs to the existing 'default'

    def test_delete_layout_falls_back_to_default(self):
        self.mgr.set_active_layout("quick_focus")
        self.mgr.delete_layout("quick_focus")
        self.assertEqual(self.mgr.active_layout_id, "default")
        self.assertNotIn("quick_focus", self.mgr.active_game.layouts)

    def test_cannot_delete_the_last_layout(self):
        for lid in ("quick_focus", "iron_man"):
            self.mgr.delete_layout(lid)
        with self.assertRaises(ProfileError):
            self.mgr.delete_layout("default")

    def test_activate_character_switches_layout(self):
        self.assertIsNotNone(self.mgr.activate_character("Iron Man"))
        self.assertEqual(self.mgr.active_layout_id, "iron_man")
        self.assertIsNone(self.mgr.activate_character("Iron Man"))  # already there
        self.assertIsNone(self.mgr.activate_character("Nobody"))
        self.assertEqual(self.mgr.active_layout_id, "iron_man")

    def test_second_game_keeps_its_own_vocabulary_and_layouts(self):
        self.mgr.create_game(
            "Test Fighter",
            inputs=[
                LogicalInput("UP", "Up", InputCategory.DIRECTION, direction=Direction.UP),
                LogicalInput("PUNCH", "Punch", InputCategory.ATTACK, button=Button.LIGHT),
            ],
        )
        self.mgr.activate("test_fighter")
        self.mgr.bind("T1", "PUNCH")
        self.assertEqual(self.mgr.resolved().actions["X"][0].logical_id, "PUNCH")
        with self.assertRaises(ProfileError):
            self.mgr.bind("T2", "QUICK_SKILL")  # Tōkon's vocabulary, not this game's
        # Switching back restores Tōkon untouched.
        self.mgr.activate(TOKON_ID)
        self.assertEqual(self.mgr.resolved().actions["X"][0].logical_id, "LIGHT")

    def test_switching_games_resets_to_that_games_default_layout(self):
        self.mgr.set_active_layout("iron_man")
        self.mgr.create_game("Other", inputs=tokon_inputs(), game_id="other")
        self.mgr.activate("other")
        self.assertEqual(self.mgr.active_layout_id, "default")
        self.mgr.activate(TOKON_ID)
        self.assertEqual(self.mgr.active_layout_id, "default")


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class TestPersistence(unittest.TestCase):
    def test_round_trip_preserves_everything(self):
        mgr = fresh_manager()
        mgr.bind("AUX4", "QUICK_DASH")
        mgr.add_to_macro("AUX4", "UNIQUE")
        mgr.set_active_layout("iron_man")
        path = mgr.save()
        self.assertTrue(path.exists())
        self.assertFalse(mgr.is_dirty)

        reloaded = ProfileManager(path).load()
        self.assertEqual(reloaded.active_game_id, TOKON_ID)
        self.assertEqual(reloaded.active_layout_id, "iron_man")
        self.assertEqual(
            reloaded.active_game.require_layout("default").logical_for("AUX4"),
            ("QUICK_DASH", "UNIQUE"),
        )
        self.assertEqual(reloaded.resolved(), mgr.resolved())
        self.assertEqual(len(reloaded.active_device), 16)

    def test_load_or_seed_creates_builtins_when_absent(self):
        path = Path(tempfile.mkdtemp()) / "nested" / "profiles.json"
        mgr = ProfileManager.load_or_seed(path)
        self.assertIn(TOKON_ID, mgr.games)
        self.assertTrue(mgr.validate().ok)

    def test_corrupt_file_is_moved_aside_not_lost(self):
        path = Path(tempfile.mkdtemp()) / "profiles.json"
        path.write_text("{not json at all", encoding="utf-8")
        mgr = ProfileManager.load_or_seed(path)
        self.assertIn(TOKON_ID, mgr.games)
        self.assertTrue(path.with_suffix(".json.bak").exists())

    def test_future_schema_version_refused(self):
        mgr = fresh_manager()
        payload = mgr.to_dict()
        payload["schema_version"] = 99
        with self.assertRaises(ProfileError):
            ProfileManager().load_dict(payload)

    def test_saved_document_is_human_editable_json(self):
        mgr = fresh_manager()
        data = json.loads(mgr.save().read_text(encoding="utf-8"))
        self.assertEqual(data["active"], {"game": TOKON_ID, "layout": "default"})
        game = next(g for g in data["games"] if g["id"] == TOKON_ID)
        default = next(l for l in game["layouts"] if l["id"] == "default")
        self.assertEqual(default["bindings"]["AUX3"], ["LIGHT", "MEDIUM"])


# --------------------------------------------------------------------------
# Stale-binding tolerance
# --------------------------------------------------------------------------


class TestStaleBindings(unittest.TestCase):
    def test_resolution_skips_bindings_the_hardware_lost(self):
        """A profile written for a bigger box must still boot on this one."""
        game, device = marvel_tokon(), leverless_16()
        layout = InputProfile(
            id="ported",
            name="Ported",
            bindings={"T1": ("LIGHT",), "T99": ("HEAVY",), "T2": ("DOES_NOT_EXIST",)},
        )
        r = ResolvedProfile.resolve(game, layout, device)
        self.assertEqual(r.actions["X"][0].button, Button.LIGHT)
        self.assertNotIn("Y", r.actions)  # unknown logical id dropped

        v = validate_layout(game, layout, device)
        self.assertFalse(v.ok)
        self.assertIn("T99", v.unknown_bindings)
        self.assertEqual(v.unknown_bindings["T2"], ("DOES_NOT_EXIST",))
        self.assertIn("unmapped:", v.summary())


if __name__ == "__main__":
    unittest.main()
