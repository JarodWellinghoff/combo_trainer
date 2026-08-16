"""
profiles.py — Hierarchical input-profile system for the leverless box.

Pure Python: no PyQt6, no XInput, no mpv. Fully unit-testable off-Windows
(same contract as `models.py`).

Hierarchy
---------
    DeviceLayout        the physical stick: 16 PhysicalButtons, each with a
                        stable id, a silkscreen label, and the raw XInput
                        source token it reports.

    GameProfile         top-level container (e.g. "Marvel Tokon"). Owns the
                        VOCABULARY of LogicalInputs for that game — the game
                        decides what "Quick Skill" means, not the device.
        └── InputProfile    a named layout nested inside the game (default,
                            per-character, macro variants…). Owns the actual
                            bindings plus its own SOCD mode.

    bindings: physical_id -> (logical_id, ...)
        ()                  unmapped   (button does nothing)
        (x,)                simple
        (x, y, ...)         macro      (one press emits every listed input)
    Two physical buttons may name the same logical input (duplicates) — that
    is a feature on a leverless (second Up under the other thumb), so nothing
    here enforces exclusivity.

Two pipelines, not one
----------------------
A directional logical input is NOT a button press: it has to reach the SOCD
cleaner and the motion parser's direction ring buffer, while an action input
takes the rising-edge path and becomes an `InputEvent`. `ResolvedProfile`
splits the bindings into those two lookup tables once, up front, so the
250 Hz poll loop never walks the profile tree.

`ResolvedProfile` is deliberately immutable: the GUI thread swaps a whole new
one into the InputThread with a single attribute assignment (atomic in
CPython), which is how a rebind takes effect live without a lock.

Persistence
-----------
One JSON document (see `default_profile_path()`) holding every device, game,
layout and the active selection. Saves are atomic (tmp + replace), matching
`ComboManager.save`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .models import Button
from .motion_parser import SOCD_NEUTRAL, SOCD_UP_PRIORITY

PROFILE_SCHEMA_VERSION = 1

TRIGGER_SOURCES = frozenset({"LEFT_TRIGGER", "RIGHT_TRIGGER"})

#: Every digital source an XInput device can report. A 16-button leverless
#: consumes exactly these: 4 d-pad + 8 main cluster + 4 aux.
XINPUT_SOURCES: tuple[str, ...] = (
    "DPAD_UP",
    "DPAD_DOWN",
    "DPAD_LEFT",
    "DPAD_RIGHT",
    "A",
    "B",
    "X",
    "Y",
    "LEFT_SHOULDER",
    "RIGHT_SHOULDER",
    "LEFT_TRIGGER",
    "RIGHT_TRIGGER",
    "LEFT_THUMB",
    "RIGHT_THUMB",
    "BACK",
    "START",
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ProfileError(ValueError):
    """Raised for structurally invalid profile edits (unknown ids, dupes…)."""


def _check_id(kind: str, value: str) -> str:
    value = str(value).strip()
    if not value or not _ID_RE.match(value):
        raise ProfileError(
            f"{kind} id must be non-empty and match [A-Za-z0-9_.-]+, got {value!r}"
        )
    return value


def slugify(name: str) -> str:
    """Derive a stable id from a display name ('Iron Man' -> 'iron_man')."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return slug or "unnamed"


# --------------------------------------------------------------------------
# Logical inputs (the game's vocabulary)
# --------------------------------------------------------------------------


class InputCategory(str, Enum):
    DIRECTION = "DIRECTION"  # routed to SOCD + motion parser
    ATTACK = "ATTACK"        # normals
    MECHANIC = "MECHANIC"    # system buttons (assists, throws, quick actions)
    SYSTEM = "SYSTEM"        # menu/UI only, never sent to the game logic


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"


@dataclass(frozen=True)
class LogicalInput:
    """
    One thing the *game* understands, independent of which key sends it.

    `button` is the bridge into the existing engine domain: an action input
    emits `InputEvent(button=...)`, so combo JSONs, the timeline renderer and
    the practice matcher keep working unchanged.

    `parse_motions=False` marks a single-button motion input (Tokon's Quick
    Skill / Quick Assemble / Quick Dash): the motion is implied BY the button,
    so a QCF that happens to be in the ring buffer must not be stamped onto
    the recorded note. Attack buttons keep it True so 236+H still records QCF.
    """

    id: str
    name: str
    category: InputCategory
    button: Optional[Button] = None
    direction: Optional[Direction] = None
    parse_motions: bool = True
    short: str = ""  # timeline glyph; falls back to the first 2 chars of id

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _check_id("LogicalInput", self.id))
        if isinstance(self.category, str):
            object.__setattr__(self, "category", InputCategory(self.category))
        if isinstance(self.button, str):
            object.__setattr__(self, "button", Button(self.button))
        if isinstance(self.direction, str):
            object.__setattr__(self, "direction", Direction(self.direction))

        if self.category is InputCategory.DIRECTION:
            if self.direction is None:
                raise ProfileError(f"Directional input {self.id!r} needs a `direction`")
            if self.button is not None:
                raise ProfileError(
                    f"Directional input {self.id!r} must not carry a `button`: "
                    "directions travel the SOCD pipeline, not the press pipeline"
                )
        elif self.category is not InputCategory.SYSTEM:
            if self.button is None:
                raise ProfileError(f"Action input {self.id!r} needs a `button`")
            if self.direction is not None:
                raise ProfileError(f"Action input {self.id!r} must not carry a direction")
        if not self.short:
            object.__setattr__(self, "short", self.id[:2].upper())

    @property
    def is_direction(self) -> bool:
        return self.category is InputCategory.DIRECTION

    @property
    def is_action(self) -> bool:
        return self.button is not None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "short": self.short,
        }
        if self.button is not None:
            d["button"] = self.button.value
        if self.direction is not None:
            d["direction"] = self.direction.value
        if not self.parse_motions:
            d["parse_motions"] = False
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LogicalInput":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            category=InputCategory(d.get("category", "MECHANIC")),
            button=Button(d["button"]) if d.get("button") else None,
            direction=Direction(d["direction"]) if d.get("direction") else None,
            parse_motions=bool(d.get("parse_motions", True)),
            short=str(d.get("short", "")),
        )


# --------------------------------------------------------------------------
# Physical device
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PhysicalButton:
    """One switch on the box. `source` is the raw XInput token it reports."""

    id: str
    label: str
    source: str
    group: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _check_id("PhysicalButton", self.id))
        if self.source not in XINPUT_SOURCES:
            raise ProfileError(
                f"Unknown XInput source {self.source!r} for {self.id!r}. "
                f"Expected one of: {', '.join(XINPUT_SOURCES)}"
            )

    @property
    def is_trigger(self) -> bool:
        return self.source in TRIGGER_SOURCES

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "group": self.group,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhysicalButton":
        return cls(
            id=str(d["id"]),
            label=str(d.get("label", d["id"])),
            source=str(d["source"]),
            group=str(d.get("group", "")),
        )


@dataclass
class DeviceLayout:
    """The physical stick: an ordered list of switches and their sources."""

    id: str
    name: str
    buttons: list[PhysicalButton] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = _check_id("DeviceLayout", self.id)
        seen_ids: set[str] = set()
        seen_sources: dict[str, str] = {}
        for btn in self.buttons:
            if btn.id in seen_ids:
                raise ProfileError(f"Duplicate physical button id {btn.id!r}")
            seen_ids.add(btn.id)
            if btn.source in seen_sources:
                raise ProfileError(
                    f"XInput source {btn.source!r} is claimed by both "
                    f"{seen_sources[btn.source]!r} and {btn.id!r}"
                )
            seen_sources[btn.source] = btn.id

    # -- lookup ------------------------------------------------------------

    def __iter__(self) -> Iterator[PhysicalButton]:
        return iter(self.buttons)

    def __len__(self) -> int:
        return len(self.buttons)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(b.id for b in self.buttons)

    def get(self, physical_id: str) -> Optional[PhysicalButton]:
        return next((b for b in self.buttons if b.id == physical_id), None)

    def require(self, physical_id: str) -> PhysicalButton:
        btn = self.get(physical_id)
        if btn is None:
            raise ProfileError(f"Unknown physical button {physical_id!r} on {self.id!r}")
        return btn

    def by_source(self, source: str) -> Optional[PhysicalButton]:
        return next((b for b in self.buttons if b.source == source), None)

    def groups(self) -> list[tuple[str, list[PhysicalButton]]]:
        """Buttons bucketed by silkscreen group, preserving declared order."""
        out: list[tuple[str, list[PhysicalButton]]] = []
        for btn in self.buttons:
            if out and out[-1][0] == btn.group:
                out[-1][1].append(btn)
            else:
                out.append((btn.group, [btn]))
        return out

    # -- editing -----------------------------------------------------------

    def set_source(self, physical_id: str, source: str) -> PhysicalButton:
        """Re-point a switch at an UNUSED XInput token.

        Note a 16-button box saturates XInput's 16 digital sources, so on a
        full device this always raises and `swap_sources()` is the operation
        you actually want.
        """
        btn = self.require(physical_id)
        if source not in XINPUT_SOURCES:
            raise ProfileError(f"Unknown XInput source {source!r}")
        clash = self.by_source(source)
        if clash is not None and clash.id != physical_id:
            raise ProfileError(
                f"Source {source!r} already used by {clash.id!r} — "
                f"use swap_sources({physical_id!r}, {clash.id!r}) instead"
            )
        updated = replace(btn, source=source)
        self.buttons[self.buttons.index(btn)] = updated
        return updated

    def swap_sources(self, physical_a: str, physical_b: str) -> None:
        """Exchange two switches' XInput tokens.

        The fix for a box whose firmware reports the cluster in a different
        order than the silkscreen suggests: the labels and every binding stay
        put, only the wire underneath moves.
        """
        a, b = self.require(physical_a), self.require(physical_b)
        ia, ib = self.buttons.index(a), self.buttons.index(b)
        self.buttons[ia] = replace(a, source=b.source)
        self.buttons[ib] = replace(b, source=a.source)

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "buttons": [b.to_dict() for b in self.buttons],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceLayout":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            buttons=[PhysicalButton.from_dict(b) for b in d.get("buttons", [])],
        )


# --------------------------------------------------------------------------
# Input profile (a layout nested in a game)
# --------------------------------------------------------------------------


@dataclass
class InputProfile:
    """
    One named layout: which logical inputs each physical button fires.

    Bindings are stored as tuples so a binding is naturally 0 (unmapped),
    1 (simple) or N (macro) logical inputs. Ids are validated against the
    owning GameProfile at bind time, never here.
    """

    id: str
    name: str
    bindings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    character: str = ""
    socd_mode: str = SOCD_NEUTRAL
    notes: str = ""

    def __post_init__(self) -> None:
        self.id = _check_id("InputProfile", self.id)
        if self.socd_mode not in (SOCD_NEUTRAL, SOCD_UP_PRIORITY):
            raise ProfileError(f"Unknown SOCD mode {self.socd_mode!r}")
        self.bindings = {
            str(k): tuple(v) if isinstance(v, (list, tuple)) else (str(v),)
            for k, v in self.bindings.items()
        }

    # -- reads -------------------------------------------------------------

    def logical_for(self, physical_id: str) -> tuple[str, ...]:
        return self.bindings.get(physical_id, ())

    def physicals_for(self, logical_id: str) -> tuple[str, ...]:
        return tuple(p for p, ls in self.bindings.items() if logical_id in ls)

    def is_macro(self, physical_id: str) -> bool:
        return len(self.logical_for(physical_id)) > 1

    def bound_logical_ids(self) -> set[str]:
        return {lid for ids in self.bindings.values() for lid in ids}

    # -- writes ------------------------------------------------------------
    # (ProfileManager wraps these to validate against the game vocabulary.)

    def set_binding(self, physical_id: str, logical_ids: Iterable[str]) -> tuple[str, ...]:
        """Replace a button's binding wholesale. Empty iterable = unmapped."""
        ids: tuple[str, ...] = tuple(dict.fromkeys(logical_ids))  # de-dupe, keep order
        if ids:
            self.bindings[physical_id] = ids
        else:
            self.bindings.pop(physical_id, None)
        return ids

    def add_to_binding(self, physical_id: str, logical_id: str) -> tuple[str, ...]:
        """Append one input, turning a simple binding into a macro."""
        current = self.logical_for(physical_id)
        if logical_id in current:
            return current
        return self.set_binding(physical_id, current + (logical_id,))

    def remove_from_binding(self, physical_id: str, logical_id: str) -> tuple[str, ...]:
        current = self.logical_for(physical_id)
        return self.set_binding(physical_id, tuple(x for x in current if x != logical_id))

    def clear_binding(self, physical_id: str) -> None:
        self.bindings.pop(physical_id, None)

    def copy(self, new_id: str, new_name: str = "") -> "InputProfile":
        return InputProfile(
            id=new_id,
            name=new_name or f"{self.name} (copy)",
            bindings={k: tuple(v) for k, v in self.bindings.items()},
            character=self.character,
            socd_mode=self.socd_mode,
            notes=self.notes,
        )

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "character": self.character,
            "socd_mode": self.socd_mode,
            "notes": self.notes,
            "bindings": {k: list(v) for k, v in self.bindings.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InputProfile":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            bindings={k: tuple(v) for k, v in d.get("bindings", {}).items()},
            character=str(d.get("character", "")),
            socd_mode=str(d.get("socd_mode", SOCD_NEUTRAL)),
            notes=str(d.get("notes", "")),
        )


# --------------------------------------------------------------------------
# Game profile (top-level container)
# --------------------------------------------------------------------------


@dataclass
class GameProfile:
    """A game: its logical-input vocabulary plus every layout built for it."""

    id: str
    name: str
    inputs: list[LogicalInput] = field(default_factory=list)
    layouts: dict[str, InputProfile] = field(default_factory=dict)
    default_layout_id: str = ""
    device_id: str = ""

    def __post_init__(self) -> None:
        self.id = _check_id("GameProfile", self.id)
        seen: set[str] = set()
        for li in self.inputs:
            if li.id in seen:
                raise ProfileError(f"Duplicate logical input {li.id!r} in {self.id!r}")
            seen.add(li.id)
        if not self.default_layout_id and self.layouts:
            self.default_layout_id = next(iter(self.layouts))

    # -- vocabulary --------------------------------------------------------

    def input(self, logical_id: str) -> Optional[LogicalInput]:
        return next((li for li in self.inputs if li.id == logical_id), None)

    def require_input(self, logical_id: str) -> LogicalInput:
        li = self.input(logical_id)
        if li is None:
            raise ProfileError(
                f"{logical_id!r} is not a logical input of game {self.id!r}"
            )
        return li

    def inputs_in(self, category: InputCategory) -> list[LogicalInput]:
        return [li for li in self.inputs if li.category is category]

    # -- layouts -----------------------------------------------------------

    def layout(self, layout_id: str) -> Optional[InputProfile]:
        return self.layouts.get(layout_id)

    def require_layout(self, layout_id: str) -> InputProfile:
        lay = self.layouts.get(layout_id)
        if lay is None:
            raise ProfileError(f"Game {self.id!r} has no layout {layout_id!r}")
        return lay

    def add_layout(self, layout: InputProfile, make_default: bool = False) -> InputProfile:
        if layout.id in self.layouts:
            raise ProfileError(f"Layout {layout.id!r} already exists in {self.id!r}")
        unknown = layout.bound_logical_ids() - {li.id for li in self.inputs}
        if unknown:
            raise ProfileError(
                f"Layout {layout.id!r} binds inputs unknown to {self.id!r}: "
                f"{', '.join(sorted(unknown))}"
            )
        self.layouts[layout.id] = layout
        if make_default or not self.default_layout_id:
            self.default_layout_id = layout.id
        return layout

    def remove_layout(self, layout_id: str) -> InputProfile:
        if len(self.layouts) <= 1:
            raise ProfileError("A game must keep at least one layout")
        removed = self.require_layout(layout_id)
        del self.layouts[layout_id]
        if self.default_layout_id == layout_id:
            self.default_layout_id = next(iter(self.layouts))
        return removed

    def layout_for_character(self, character: str) -> Optional[InputProfile]:
        """Match a combo file's `character` field to a per-character layout."""
        if not character:
            return None
        wanted = character.strip().casefold()
        for lay in self.layouts.values():
            if lay.character and lay.character.strip().casefold() == wanted:
                return lay
        return None

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "device_id": self.device_id,
            "default_layout_id": self.default_layout_id,
            "inputs": [i.to_dict() for i in self.inputs],
            "layouts": [l.to_dict() for l in self.layouts.values()],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameProfile":
        layouts = [InputProfile.from_dict(l) for l in d.get("layouts", [])]
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            inputs=[LogicalInput.from_dict(i) for i in d.get("inputs", [])],
            layouts={l.id: l for l in layouts},
            default_layout_id=str(d.get("default_layout_id", "")),
            device_id=str(d.get("device_id", "")),
        )


# --------------------------------------------------------------------------
# Resolution — the flat table the 250 Hz loop reads
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedAction:
    """One `InputEvent` to emit when a source goes from released to pressed."""

    button: Button
    logical_id: str
    logical_name: str
    parse_motions: bool = True


@dataclass(frozen=True)
class ResolvedProfile:
    """
    Immutable, flattened view of (game, layout, device) for the input thread.

    Handed to `InputThread.set_profile()` and swapped atomically; the loop
    never dereferences a GameProfile or walks a bindings dict.
    """

    game_id: str
    game_name: str
    layout_id: str
    layout_name: str
    socd_mode: str = SOCD_NEUTRAL
    #: XInput source -> actions fired on its rising edge (>1 entry == macro).
    actions: dict[str, tuple[ResolvedAction, ...]] = field(default_factory=dict)
    #: XInput sources feeding each cardinal (>1 entry == duplicate keys).
    up_sources: tuple[str, ...] = ()
    down_sources: tuple[str, ...] = ()
    left_sources: tuple[str, ...] = ()
    right_sources: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return f"{self.game_name} · {self.layout_name}"

    @property
    def direction_sources(self) -> tuple[str, ...]:
        return self.up_sources + self.down_sources + self.left_sources + self.right_sources

    @property
    def sources(self) -> tuple[str, ...]:
        """Every source this profile listens to (directions + actions)."""
        return tuple(dict.fromkeys(self.direction_sources + tuple(self.actions)))

    def button_map(self) -> dict[str, Button]:
        """Back-compat view matching the old `DEFAULT_BUTTON_MAP` shape.

        Lossy on purpose: a macro contributes only its first action.
        """
        return {src: acts[0].button for src, acts in self.actions.items() if acts}

    def label_for(self, source: str) -> str:
        acts = self.actions.get(source)
        if acts:
            return " + ".join(a.logical_name for a in acts)
        for dir_name, srcs in (
            ("Up", self.up_sources),
            ("Down", self.down_sources),
            ("Left", self.left_sources),
            ("Right", self.right_sources),
        ):
            if source in srcs:
                return dir_name
        return ""

    @classmethod
    def resolve(
        cls, game: GameProfile, layout: InputProfile, device: DeviceLayout
    ) -> "ResolvedProfile":
        actions: dict[str, list[ResolvedAction]] = {}
        dirs: dict[Direction, list[str]] = {d: [] for d in Direction}

        for physical_id, logical_ids in layout.bindings.items():
            btn = device.get(physical_id)
            if btn is None:
                continue  # binding for a switch this device doesn't have: ignore
            for logical_id in logical_ids:
                li = game.input(logical_id)
                if li is None:
                    continue  # stale binding; validate() surfaces it to the UI
                if li.is_direction:
                    assert li.direction is not None
                    if btn.source not in dirs[li.direction]:
                        dirs[li.direction].append(btn.source)
                elif li.button is not None:
                    actions.setdefault(btn.source, []).append(
                        ResolvedAction(
                            button=li.button,
                            logical_id=li.id,
                            logical_name=li.name,
                            parse_motions=li.parse_motions,
                        )
                    )
                # SYSTEM inputs resolve to nothing by design.

        return cls(
            game_id=game.id,
            game_name=game.name,
            layout_id=layout.id,
            layout_name=layout.name,
            socd_mode=layout.socd_mode,
            actions={src: tuple(acts) for src, acts in actions.items()},
            up_sources=tuple(dirs[Direction.UP]),
            down_sources=tuple(dirs[Direction.DOWN]),
            left_sources=tuple(dirs[Direction.LEFT]),
            right_sources=tuple(dirs[Direction.RIGHT]),
        )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileValidation:
    """A layout's health, rendered by the rebinding dialog."""

    unmapped_inputs: tuple[str, ...] = ()      # logical inputs nothing fires
    unbound_buttons: tuple[str, ...] = ()      # physical switches doing nothing
    duplicate_inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    macros: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unknown_bindings: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Playable: every game input reachable, no stale ids. Leftover
        physical buttons are fine — that's the 16-vs-13 case."""
        return not self.unmapped_inputs and not self.unknown_bindings

    def summary(self) -> str:
        bits = []
        if self.unmapped_inputs:
            bits.append(f"unmapped: {', '.join(self.unmapped_inputs)}")
        if self.unknown_bindings:
            bits.append(f"stale: {', '.join(sorted(self.unknown_bindings))}")
        if self.unbound_buttons:
            bits.append(f"free buttons: {len(self.unbound_buttons)}")
        if self.duplicate_inputs:
            bits.append(f"duplicates: {len(self.duplicate_inputs)}")
        if self.macros:
            bits.append(f"macros: {len(self.macros)}")
        return " · ".join(bits) if bits else "fully mapped"


def validate_layout(
    game: GameProfile, layout: InputProfile, device: DeviceLayout
) -> ProfileValidation:
    known = {li.id for li in game.inputs}
    playable = {li.id for li in game.inputs if li.category is not InputCategory.SYSTEM}

    bound: dict[str, list[str]] = {}
    unknown: dict[str, tuple[str, ...]] = {}
    macros: dict[str, tuple[str, ...]] = {}

    for physical_id, logical_ids in layout.bindings.items():
        if device.get(physical_id) is None:
            unknown[physical_id] = tuple(logical_ids)
            continue
        stale = tuple(l for l in logical_ids if l not in known)
        if stale:
            unknown[physical_id] = stale
        if len(logical_ids) > 1:
            macros[physical_id] = tuple(logical_ids)
        for logical_id in logical_ids:
            bound.setdefault(logical_id, []).append(physical_id)

    return ProfileValidation(
        unmapped_inputs=tuple(li.id for li in game.inputs if li.id in playable and li.id not in bound),
        unbound_buttons=tuple(b.id for b in device if not layout.logical_for(b.id)),
        duplicate_inputs={k: tuple(v) for k, v in bound.items() if len(v) > 1},
        macros=macros,
        unknown_bindings=unknown,
    )


# --------------------------------------------------------------------------
# ProfileManager
# --------------------------------------------------------------------------


def default_profile_path() -> Path:
    """`$COMBO_TRAINER_HOME/profiles.json`, else `~/.combo_trainer/…`."""
    home = os.environ.get("COMBO_TRAINER_HOME")
    base = Path(home) if home else Path.home() / ".combo_trainer"
    return base / "profiles.json"


class ProfileManager:
    """
    Owns every device / game / layout plus the active selection, and is the
    single place mutations happen (so validation and change notification are
    not optional).

    Qt-free on purpose, exactly like `ComboManager`: MainWindow subscribes
    with `add_listener()` and re-pushes `resolved()` into the InputThread.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path = Path(path) if path is not None else default_profile_path()
        self.devices: dict[str, DeviceLayout] = {}
        self.games: dict[str, GameProfile] = {}
        self._active_game_id: str = ""
        self._active_layout_id: str = ""
        self._dirty = False
        self._listeners: list[Callable[["ProfileManager"], None]] = []

    # -- notification -------------------------------------------------------

    def add_listener(self, fn: Callable[["ProfileManager"], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[["ProfileManager"], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _changed(self) -> None:
        self._dirty = True
        for fn in list(self._listeners):
            fn(self)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    # -- registration -------------------------------------------------------

    def add_device(self, device: DeviceLayout, replace_existing: bool = False) -> DeviceLayout:
        if device.id in self.devices and not replace_existing:
            raise ProfileError(f"Device {device.id!r} already registered")
        self.devices[device.id] = device
        self._changed()
        return device

    def add_game(self, game: GameProfile, replace_existing: bool = False) -> GameProfile:
        if game.id in self.games and not replace_existing:
            raise ProfileError(f"Game {game.id!r} already registered")
        if game.device_id and game.device_id not in self.devices:
            raise ProfileError(
                f"Game {game.id!r} references unknown device {game.device_id!r}"
            )
        self.games[game.id] = game
        if not self._active_game_id:
            self.activate(game.id)
        else:
            self._changed()
        return game

    def create_game(
        self,
        name: str,
        inputs: Iterable[LogicalInput],
        device_id: str = "",
        game_id: str = "",
    ) -> GameProfile:
        """Create an empty game plus a blank 'Default' layout to bind into."""
        gid = _check_id("GameProfile", game_id or slugify(name))
        if gid in self.games:
            raise ProfileError(f"Game {gid!r} already exists")
        device_id = device_id or next(iter(self.devices), "")
        game = GameProfile(id=gid, name=name, inputs=list(inputs), device_id=device_id)
        game.add_layout(InputProfile(id="default", name="Default"), make_default=True)
        return self.add_game(game)

    # -- lookup -------------------------------------------------------------

    @property
    def active_game(self) -> Optional[GameProfile]:
        return self.games.get(self._active_game_id)

    @property
    def active_layout(self) -> Optional[InputProfile]:
        game = self.active_game
        return game.layout(self._active_layout_id) if game else None

    @property
    def active_device(self) -> Optional[DeviceLayout]:
        game = self.active_game
        if game is None:
            return None
        return self.devices.get(game.device_id) or next(iter(self.devices.values()), None)

    @property
    def active_game_id(self) -> str:
        return self._active_game_id

    @property
    def active_layout_id(self) -> str:
        return self._active_layout_id

    def require_active(self) -> tuple[GameProfile, InputProfile, DeviceLayout]:
        game, layout, device = self.active_game, self.active_layout, self.active_device
        if game is None or layout is None or device is None:
            raise ProfileError("No active game/layout/device selected")
        return game, layout, device

    # -- activation ---------------------------------------------------------

    def activate(self, game_id: str, layout_id: str = "") -> "ResolvedProfile":
        """Switch the active game (and optionally layout) in one step."""
        game = self.games.get(game_id)
        if game is None:
            raise ProfileError(f"Unknown game {game_id!r}")
        target = layout_id or game.default_layout_id or next(iter(game.layouts), "")
        game.require_layout(target)
        self._active_game_id, self._active_layout_id = game.id, target
        self._changed()
        return self.resolved()

    def set_active_layout(self, layout_id: str) -> "ResolvedProfile":
        """Switch layouts within the active game (character swap mid-session)."""
        game = self.active_game
        if game is None:
            raise ProfileError("No active game")
        game.require_layout(layout_id)
        self._active_layout_id = layout_id
        self._changed()
        return self.resolved()

    def activate_character(self, character: str) -> Optional["ResolvedProfile"]:
        """Auto-select a per-character layout (used when a combo file loads)."""
        game = self.active_game
        if game is None:
            return None
        layout = game.layout_for_character(character)
        if layout is None or layout.id == self._active_layout_id:
            return None
        return self.set_active_layout(layout.id)

    def resolved(self) -> ResolvedProfile:
        game, layout, device = self.require_active()
        return ResolvedProfile.resolve(game, layout, device)

    def validate(self) -> ProfileValidation:
        game, layout, device = self.require_active()
        return validate_layout(game, layout, device)

    # -- layout lifecycle ---------------------------------------------------

    def create_layout(
        self,
        name: str,
        game_id: str = "",
        copy_from: str | None = None,
        character: str = "",
        layout_id: str = "",
    ) -> InputProfile:
        """
        Add a layout to a game. `copy_from` clones an existing layout's
        bindings (the usual way to spin up a character variant); omit it for
        an empty layout.
        """
        game = self.games.get(game_id or self._active_game_id)
        if game is None:
            raise ProfileError(f"Unknown game {game_id!r}")
        lid = _check_id("InputProfile", layout_id or slugify(name))
        if lid in game.layouts:
            raise ProfileError(f"Layout {lid!r} already exists in {game.id!r}")
        if copy_from:
            layout = game.require_layout(copy_from).copy(lid, name)
        else:
            layout = InputProfile(id=lid, name=name)
        layout.character = character
        game.add_layout(layout)
        self._changed()
        return layout

    def delete_layout(self, layout_id: str, game_id: str = "") -> InputProfile:
        game = self.games.get(game_id or self._active_game_id)
        if game is None:
            raise ProfileError(f"Unknown game {game_id!r}")
        removed = game.remove_layout(layout_id)
        if self._active_layout_id == layout_id and game.id == self._active_game_id:
            self._active_layout_id = game.default_layout_id
        self._changed()
        return removed

    def rename_layout(self, layout_id: str, name: str, game_id: str = "") -> InputProfile:
        game = self.games.get(game_id or self._active_game_id)
        if game is None:
            raise ProfileError(f"Unknown game {game_id!r}")
        layout = game.require_layout(layout_id)
        layout.name = name
        self._changed()
        return layout

    # -- rebinding ----------------------------------------------------------

    def rebind(self, physical_id: str, logical_ids: Iterable[str]) -> tuple[str, ...]:
        """
        Point a physical button at zero (unmapped), one, or many (macro)
        logical inputs on the active layout. This is THE rebinding entry
        point — the dialog, hotkeys and any future scripting all land here.
        """
        game, layout, device = self.require_active()
        device.require(physical_id)
        ids = tuple(dict.fromkeys(logical_ids))
        for logical_id in ids:
            game.require_input(logical_id)
        result = layout.set_binding(physical_id, ids)
        self._changed()
        return result

    def bind(self, physical_id: str, logical_id: str) -> tuple[str, ...]:
        """Bind exactly one input, replacing whatever was there."""
        return self.rebind(physical_id, (logical_id,))

    def add_to_macro(self, physical_id: str, logical_id: str) -> tuple[str, ...]:
        _, layout, _ = self.require_active()
        return self.rebind(physical_id, layout.logical_for(physical_id) + (logical_id,))

    def unbind(self, physical_id: str) -> tuple[str, ...]:
        """Make a button do nothing — the graceful 'leftover switch' case."""
        return self.rebind(physical_id, ())

    def swap(self, physical_a: str, physical_b: str) -> None:
        """Exchange two buttons' bindings (the most common rebind by far)."""
        _, layout, device = self.require_active()
        device.require(physical_a)
        device.require(physical_b)
        a, b = layout.logical_for(physical_a), layout.logical_for(physical_b)
        layout.set_binding(physical_a, b)
        layout.set_binding(physical_b, a)
        self._changed()

    def set_socd_mode(self, mode: str) -> None:
        _, layout, _ = self.require_active()
        if mode not in (SOCD_NEUTRAL, SOCD_UP_PRIORITY):
            raise ProfileError(f"Unknown SOCD mode {mode!r}")
        layout.socd_mode = mode
        self._changed()

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "active": {"game": self._active_game_id, "layout": self._active_layout_id},
            "devices": [d.to_dict() for d in self.devices.values()],
            "games": [g.to_dict() for g in self.games.values()],
        }

    def load_dict(self, data: dict) -> None:
        version = int(data.get("schema_version", 0))
        if version > PROFILE_SCHEMA_VERSION:
            raise ProfileError(
                f"Profile file schema v{version} is newer than supported "
                f"v{PROFILE_SCHEMA_VERSION}."
            )
        self.devices = {
            d["id"]: DeviceLayout.from_dict(d) for d in data.get("devices", [])
        }
        self.games = {g["id"]: GameProfile.from_dict(g) for g in data.get("games", [])}
        active = data.get("active", {})
        game_id = str(active.get("game", "")) or next(iter(self.games), "")
        self._active_game_id = game_id if game_id in self.games else ""
        game = self.active_game
        if game is not None:
            layout_id = str(active.get("layout", ""))
            self._active_layout_id = (
                layout_id if layout_id in game.layouts else game.default_layout_id
            )
        self._dirty = False

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(target)  # atomic-ish: never leave a half-written profile
        self.path = target
        self._dirty = False
        return target

    def load(self, path: str | Path | None = None) -> "ProfileManager":
        target = Path(path) if path is not None else self.path
        self.load_dict(json.loads(target.read_text(encoding="utf-8")))
        self.path = target
        return self

    @classmethod
    def load_or_seed(cls, path: str | Path | None = None) -> "ProfileManager":
        """
        Normal startup path: read the user's profiles, or seed the built-ins
        (16-button leverless + Marvel Tokon) on first run. A corrupt file is
        never overwritten silently — it is moved aside as `.bak`.
        """
        from .games import seed_builtins  # local import: games.py imports us

        mgr = cls(path)
        if mgr.path.exists():
            try:
                return mgr.load()
            except Exception:
                backup = mgr.path.with_suffix(mgr.path.suffix + ".bak")
                try:
                    mgr.path.replace(backup)
                except OSError:
                    pass
                mgr = cls(path)
        seed_builtins(mgr)
        return mgr
