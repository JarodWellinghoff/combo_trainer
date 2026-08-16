"""
games.py — Built-in device + game profiles shipped with the trainer.

Everything here is plain data assembled from `profiles.py` primitives, so a
new game (or a new stick) is a function in this file, not a code change
anywhere else. On first run `seed_builtins()` writes these into the user's
profiles.json; after that the user's edited copy wins and this module is only
consulted for `Restore defaults`.

The 16-button leverless
-----------------------
A 16-button box saturates XInput's digital sources exactly: 4 d-pad + 8 main
cluster + 4 aux. The default source assignment follows the standard leverless
convention (top row X/Y/RB/LB, bottom row A/B/RT/LT) so a stock Hitbox-style
box works with no setup; `DeviceLayout.set_source()` re-points any switch
whose firmware disagrees.

    Movement    [LEFT] [DOWN] [RIGHT]            [UP](thumb)
    Top row     [T1]  [T2]  [T3]  [T4]
    Bottom row  [B1]  [B2]  [B3]  [B4]
    Aux         [AUX1] [AUX2] [AUX3] [AUX4]

Marvel Tōkon: 13 logical inputs onto 16 switches
------------------------------------------------
4 directions + 4 attacks + 5 mechanics = 13, leaving 3 switches spare. The
default layout demonstrates all three ways of handling the leftovers, which
is the whole point of the binding-as-a-tuple design:

    AUX2  duplicate  -> a second UP for the right thumb
    AUX3  macro      -> LIGHT + MEDIUM fired from one switch
    AUX4  unmapped   -> menu/Start button, deliberately inert in-game
"""

from __future__ import annotations

from .models import Button
from .motion_parser import SOCD_NEUTRAL, SOCD_UP_PRIORITY
from .profiles import (
    Direction,
    DeviceLayout,
    GameProfile,
    InputCategory,
    InputProfile,
    LogicalInput,
    PhysicalButton,
    ProfileManager,
)

LEVERLESS_16_ID = "leverless_16"
TOKON_ID = "marvel_tokon"


# --------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------


def leverless_16() -> DeviceLayout:
    """The 16-button leverless box, in silkscreen order."""
    return DeviceLayout(
        id=LEVERLESS_16_ID,
        name="16-Button Leverless",
        buttons=[
            # Movement cluster (left hand + left thumb).
            PhysicalButton("LEFT", "Left", "DPAD_LEFT", group="Movement"),
            PhysicalButton("DOWN", "Down", "DPAD_DOWN", group="Movement"),
            PhysicalButton("RIGHT", "Right", "DPAD_RIGHT", group="Movement"),
            PhysicalButton("UP", "Up (thumb)", "DPAD_UP", group="Movement"),
            # Main cluster, top row: index → pinky.
            PhysicalButton("T1", "Top 1 (index)", "X", group="Top row"),
            PhysicalButton("T2", "Top 2 (middle)", "Y", group="Top row"),
            PhysicalButton("T3", "Top 3 (ring)", "RIGHT_SHOULDER", group="Top row"),
            PhysicalButton("T4", "Top 4 (pinky)", "LEFT_SHOULDER", group="Top row"),
            # Main cluster, bottom row: index → pinky.
            PhysicalButton("B1", "Bottom 1 (index)", "A", group="Bottom row"),
            PhysicalButton("B2", "Bottom 2 (middle)", "B", group="Bottom row"),
            PhysicalButton("B3", "Bottom 3 (ring)", "RIGHT_TRIGGER", group="Bottom row"),
            PhysicalButton("B4", "Bottom 4 (pinky)", "LEFT_TRIGGER", group="Bottom row"),
            # Aux switches.
            PhysicalButton("AUX1", "Aux 1 (left thumb)", "LEFT_THUMB", group="Aux"),
            PhysicalButton("AUX2", "Aux 2 (right thumb)", "RIGHT_THUMB", group="Aux"),
            PhysicalButton("AUX3", "Aux 3 (side)", "BACK", group="Aux"),
            PhysicalButton("AUX4", "Aux 4 (menu)", "START", group="Aux"),
        ],
    )


# --------------------------------------------------------------------------
# Marvel Tōkon: logical inputs
# --------------------------------------------------------------------------


def tokon_inputs() -> list[LogicalInput]:
    """The 13 logical inputs Marvel Tōkon: Fighting Souls understands.

    `parse_motions=False` on the three Quick* mechanics: the motion is
    implied by the button, so a QCF sitting in the ring buffer must not be
    stamped onto the note. Attacks keep it True — 236+H still records as QCF.
    """
    return [
        # -- Directionals: routed to SOCD cleaning + the motion parser ------
        LogicalInput("UP", "Up", InputCategory.DIRECTION, direction=Direction.UP, short="8"),
        LogicalInput("DOWN", "Down", InputCategory.DIRECTION, direction=Direction.DOWN, short="2"),
        LogicalInput("LEFT", "Left", InputCategory.DIRECTION, direction=Direction.LEFT, short="4"),
        LogicalInput("RIGHT", "Right", InputCategory.DIRECTION, direction=Direction.RIGHT, short="6"),
        # -- Attacks --------------------------------------------------------
        LogicalInput("LIGHT", "Light", InputCategory.ATTACK, button=Button.LIGHT, short="L"),
        LogicalInput("MEDIUM", "Medium", InputCategory.ATTACK, button=Button.MEDIUM, short="M"),
        LogicalInput("HEAVY", "Heavy", InputCategory.ATTACK, button=Button.HEAVY, short="H"),
        LogicalInput("UNIQUE", "Unique", InputCategory.ATTACK, button=Button.UNIQUE, short="U"),
        # -- Game mechanics --------------------------------------------------
        LogicalInput("ASSEMBLE", "Assemble", InputCategory.MECHANIC, button=Button.ASSEMBLE, short="AS"),
        LogicalInput(
            "QUICK_SKILL", "Quick Skill", InputCategory.MECHANIC,
            button=Button.QUICK_SKILL, parse_motions=False, short="QS",
        ),
        LogicalInput(
            "QUICK_ASSEMBLE", "Quick Assemble", InputCategory.MECHANIC,
            button=Button.QUICK_ASSEMBLE, parse_motions=False, short="QA",
        ),
        LogicalInput(
            "QUICK_DASH", "Quick Dash", InputCategory.MECHANIC,
            button=Button.QUICK_DASH, parse_motions=False, short="QD",
        ),
        LogicalInput("THROW", "Throw", InputCategory.MECHANIC, button=Button.THROW, short="TH"),
    ]


# --------------------------------------------------------------------------
# Marvel Tōkon: layouts
# --------------------------------------------------------------------------


def _tokon_default_layout() -> InputProfile:
    """Standard leverless: 13 inputs, 3 leftovers handled three different ways."""
    return InputProfile(
        id="default",
        name="Standard Leverless",
        socd_mode=SOCD_NEUTRAL,
        notes=(
            "Attacks on the top row, team/skill mechanics on the bottom row. "
            "AUX2 duplicates Up, AUX3 is an L+M macro, AUX4 is left free."
        ),
        bindings={
            "LEFT": ("LEFT",),
            "DOWN": ("DOWN",),
            "RIGHT": ("RIGHT",),
            "UP": ("UP",),
            "T1": ("LIGHT",),
            "T2": ("MEDIUM",),
            "T3": ("HEAVY",),
            "T4": ("UNIQUE",),
            "B1": ("ASSEMBLE",),
            "B2": ("THROW",),
            "B3": ("QUICK_SKILL",),
            "B4": ("QUICK_ASSEMBLE",),
            "AUX1": ("QUICK_DASH",),
            # -- the 3 leftover switches ----------------------------------
            "AUX2": ("UP",),                 # duplicate: second Up, right thumb
            "AUX3": ("LIGHT", "MEDIUM"),     # macro: one press fires both
            # AUX4 absent from the dict == unmapped (menu button, inert)
        },
    )


def _tokon_quick_layout() -> InputProfile:
    """For players who lean on the single-button motion inputs."""
    layout = _tokon_default_layout().copy("quick_focus", "Quick-Skill Focus")
    layout.notes = (
        "Quick Skill and Quick Assemble duplicated onto the aux row so both "
        "hands can reach them; the L+M macro is dropped."
    )
    layout.set_binding("AUX3", ("QUICK_SKILL",))     # duplicate instead of macro
    layout.set_binding("AUX4", ("QUICK_ASSEMBLE",))  # leftover put to work
    return layout


def _tokon_character_layout() -> InputProfile:
    """
    Per-character example: a charge/zoning character wants Up-priority SOCD
    for instant air-dashes and Assemble under the other thumb.

    `character` matches ComboFile.character, so loading an Iron Man combo
    JSON auto-selects this layout (see MainWindow._on_combo_loaded).
    """
    layout = _tokon_default_layout().copy("iron_man", "Iron Man — Air Zoning")
    layout.character = "Iron Man"
    layout.socd_mode = SOCD_UP_PRIORITY  # U+D -> Up: cleaner instant air-dashes
    layout.notes = (
        "Up-priority SOCD for instant air-dashes; Assemble duplicated to the "
        "right thumb so assists stay callable while holding a direction."
    )
    layout.set_binding("AUX2", ("ASSEMBLE",))
    layout.set_binding("AUX3", ("UNIQUE", "QUICK_SKILL"))
    return layout


def marvel_tokon(device_id: str = LEVERLESS_16_ID) -> GameProfile:
    """The Marvel Tōkon game profile with its three built-in layouts."""
    game = GameProfile(
        id=TOKON_ID,
        name="Marvel Tōkon: Fighting Souls",
        inputs=tokon_inputs(),
        device_id=device_id,
    )
    game.add_layout(_tokon_default_layout(), make_default=True)
    game.add_layout(_tokon_quick_layout())
    game.add_layout(_tokon_character_layout())
    return game


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

BUILTIN_DEVICES = (leverless_16,)
BUILTIN_GAMES = (marvel_tokon,)


def seed_builtins(manager: ProfileManager, overwrite: bool = False) -> ProfileManager:
    """Install the shipped device + game profiles into a manager.

    Called on first run and by `Restore Defaults`. With `overwrite=False`
    (the default) anything the user already has is left untouched.
    """
    for factory in BUILTIN_DEVICES:
        device = factory()
        if overwrite or device.id not in manager.devices:
            manager.add_device(device, replace_existing=True)
    for factory in BUILTIN_GAMES:
        game = factory()
        if overwrite or game.id not in manager.games:
            manager.add_game(game, replace_existing=True)
    if not manager.active_game_id and manager.games:
        manager.activate(next(iter(manager.games)))
    return manager
