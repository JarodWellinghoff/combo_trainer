"""
models.py — Core data layer for the Tōkon Combo Trainer.

Pure Python: no PyQt6, no mpv, no XInput imports. Everything here is
unit-testable in isolation.

Canonical time domain
---------------------
All timing in this application is expressed in **video frame numbers**
(int, 0-based) relative to the combo video, NOT wall-clock time. Playback
speed never touches this layer.

JSON schema (version 1)
-----------------------
{
    "schema_version": 1,
    "game": "Marvel Tokon: Fighting Souls",
    "combo_name": "Iron Man BnB - Midscreen",
    "character": "Iron Man",
    "notes": "Optional free text",
    "created_at": "2026-08-16T12:00:00",
    "video_path": "combos/ironman_bnb.mp4",
    "video_fps": 60.0,
    "inputs": [
        {
            "frame": 128,
            "button": "LIGHT",
            "motion": "NONE",          // bare button (incl. Quick Specials)
            "label": ""                // optional display note
        },
        {
            "frame": 190,
            "button": "SPECIAL",
            "motion": "QCF",           // classic motion input
            "label": "Repulsor Blast"
        }
    ]
}

Notes:
- Tōkon has no negative edge: we store presses only, never releases.
- "motion": "NONE" + any button renders as a standalone button icon.
- "video_path" is stored relative to the JSON file's directory when
  possible, so combo packs are portable; ComboManager resolves it on load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

SCHEMA_VERSION = 1
GAME_NAME = "Marvel Tokon: Fighting Souls"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------

class Motion(str, Enum):
    """Parsed motion inputs (numpad notation in comments, P1 facing right)."""
    NONE = "NONE"   # bare button / Quick Special
    QCF = "QCF"     # 236  quarter circle forward
    QCB = "QCB"     # 214  quarter circle back
    DP = "DP"       # 623  dragon punch
    RDP = "RDP"     # 421  reverse dragon punch
    HCF = "HCF"     # 41236 half circle forward
    HCB = "HCB"     # 63214 half circle back
    DD = "DD"       # 22   double down (down, down)
    CHARGE_B_F = "CHARGE_B_F"  # [4]6 charge back, forward
    CHARGE_D_U = "CHARGE_D_U"  # [2]8 charge down, up


class Button(str, Enum):
    """
    Attack / system buttons — the engine-level domain that combo JSON,
    the timeline renderer and the practice matcher all speak.

    This is NOT the rebinding layer. Which physical switch produces which
    Button is decided by `profiles.InputProfile`; a game's own naming (Tōkon
    calls its assist "Assemble") lives in `profiles.LogicalInput`. Adding a
    game therefore only means adding entries here if it needs a genuinely new
    icon/colour — everything else is data.

    Directions are deliberately absent: they travel the SOCD + motion-parser
    pipeline and never become a Button.
    """
    # -- Marvel Tōkon: Fighting Souls -------------------------------------
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"
    UNIQUE = "UNIQUE"               # 4th attack button
    ASSEMBLE = "ASSEMBLE"           # call partner / assist
    QUICK_SKILL = "QUICK_SKILL"     # single-button motion special
    QUICK_ASSEMBLE = "QUICK_ASSEMBLE"
    QUICK_DASH = "QUICK_DASH"
    THROW = "THROW"

    # -- legacy values -----------------------------------------------------
    # Kept so combo JSONs recorded before the profile system still load.
    SPECIAL = "SPECIAL"     # dedicated special / Quick Special button
    ASSIST1 = "ASSIST1"
    ASSIST2 = "ASSIST2"
    TAG = "TAG"


class Judgement(str, Enum):
    PERFECT = "PERFECT"
    GOOD = "GOOD"
    MISS = "MISS"


# --------------------------------------------------------------------------
# Timing windows
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TimingWindows:
    """
    Judgement windows in video frames, applied symmetrically (|delta|).

    perfect_frames=3, good_frames=5 means:
        |delta| <= 3          -> PERFECT
        3 < |delta| <= 5      -> GOOD
        |delta| > 5           -> MISS
    An expected input with no press within `good_frames` after its frame
    passes is also a MISS (handled by the practice controller in Phase 2).
    """
    perfect_frames: int = 3
    good_frames: int = 5

    def __post_init__(self) -> None:
        if self.perfect_frames < 0 or self.good_frames < self.perfect_frames:
            raise ValueError("Require 0 <= perfect_frames <= good_frames")

    def classify(self, delta_frames: int) -> Judgement:
        d = abs(delta_frames)
        if d <= self.perfect_frames:
            return Judgement.PERFECT
        if d <= self.good_frames:
            return Judgement.GOOD
        return Judgement.MISS


# --------------------------------------------------------------------------
# Core dataclasses
# --------------------------------------------------------------------------

@dataclass
class ComboInput:
    """A single required input, pinned to an exact video frame."""
    frame: int
    button: Button
    motion: Motion = Motion.NONE
    label: str = ""

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError(f"frame must be >= 0, got {self.frame}")
        # Coerce strings coming from JSON into enums.
        if isinstance(self.button, str):
            self.button = Button(self.button)
        if isinstance(self.motion, str):
            self.motion = Motion(self.motion)

    @property
    def is_motion_input(self) -> bool:
        return self.motion is not Motion.NONE

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "button": self.button.value,
            "motion": self.motion.value,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ComboInput":
        return cls(
            frame=int(d["frame"]),
            button=Button(d["button"]),
            motion=Motion(d.get("motion", "NONE")),
            label=str(d.get("label", "")),
        )


@dataclass
class ComboFile:
    """Everything that gets serialized to one combo JSON file."""
    video_path: str
    video_fps: float
    combo_name: str = "Untitled Combo"
    character: str = ""
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    inputs: list[ComboInput] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.video_fps <= 0:
            raise ValueError(f"video_fps must be > 0, got {self.video_fps}")

    # -- time helpers -------------------------------------------------------

    def frame_to_seconds(self, frame: int) -> float:
        return frame / self.video_fps

    def seconds_to_frame(self, seconds: float) -> int:
        return round(seconds * self.video_fps)

    @property
    def duration_frames(self) -> int:
        return self.inputs[-1].frame if self.inputs else 0

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "game": GAME_NAME,
            "combo_name": self.combo_name,
            "character": self.character,
            "notes": self.notes,
            "created_at": self.created_at,
            "video_path": self.video_path,
            "video_fps": self.video_fps,
            "inputs": [i.to_dict() for i in self.inputs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ComboFile":
        version = int(d.get("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"Combo file schema v{version} is newer than supported v{SCHEMA_VERSION}."
            )
        # (Future: migration hooks for older versions go here.)
        return cls(
            video_path=str(d["video_path"]),
            video_fps=float(d["video_fps"]),
            combo_name=str(d.get("combo_name", "Untitled Combo")),
            character=str(d.get("character", "")),
            notes=str(d.get("notes", "")),
            created_at=str(d.get("created_at", "")),
            inputs=[ComboInput.from_dict(i) for i in d.get("inputs", [])],
        )


# --------------------------------------------------------------------------
# ComboManager
# --------------------------------------------------------------------------

class ComboManager:
    """
    Owns the in-memory ComboFile and all mutations to it.

    Responsibilities:
      * create / load / save combo JSON (with schema validation)
      * append inputs during Record mode (kept sorted by frame)
      * edit / delete inputs
      * resolve the video path relative to the JSON file
      * track a dirty flag so the UI can prompt "unsaved changes"

    It deliberately knows nothing about Qt, mpv, or XInput. Record mode
    calls `add_input(...)`; Practice mode reads `combo.inputs`.
    """

    def __init__(self) -> None:
        self._combo: Optional[ComboFile] = None
        self._json_path: Optional[Path] = None
        self._dirty: bool = False

    # -- properties ---------------------------------------------------------

    @property
    def combo(self) -> Optional[ComboFile]:
        return self._combo

    @property
    def is_loaded(self) -> bool:
        return self._combo is not None

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def json_path(self) -> Optional[Path]:
        return self._json_path

    # -- lifecycle ----------------------------------------------------------

    def new_combo(self, video_path: str, video_fps: float, combo_name: str = "Untitled Combo") -> ComboFile:
        """Start a fresh combo (typical entry point when Record mode begins)."""
        self._combo = ComboFile(
            video_path=video_path,
            video_fps=video_fps,
            combo_name=combo_name,
        )
        self._json_path = None
        self._dirty = True
        return self._combo

    def close(self) -> None:
        self._combo = None
        self._json_path = None
        self._dirty = False

    # -- recording / editing -------------------------------------------------

    def add_input(
        self,
        frame: int,
        button: Button,
        motion: Motion = Motion.NONE,
        label: str = "",
    ) -> ComboInput:
        """
        Append an input at `frame`, keeping self.combo.inputs sorted.
        Called by the Record controller on every confirmed press event.
        """
        self._require_loaded()
        entry = ComboInput(frame=frame, button=button, motion=motion, label=label)
        inputs = self._combo.inputs
        # Insert in sorted position (recording is near-append, so scan from end).
        idx = len(inputs)
        while idx > 0 and inputs[idx - 1].frame > entry.frame:
            idx -= 1
        inputs.insert(idx, entry)
        self._dirty = True
        return entry

    def remove_input(self, index: int) -> ComboInput:
        self._require_loaded()
        removed = self._combo.inputs.pop(index)
        self._dirty = True
        return removed

    def update_input(self, index: int, **changes) -> ComboInput:
        """Edit fields of an existing input (frame nudging, relabeling, etc.)."""
        self._require_loaded()
        old = self._combo.inputs[index]
        data = old.to_dict()
        for key, value in changes.items():
            if key not in data:
                raise KeyError(f"Unknown ComboInput field: {key}")
            data[key] = value.value if isinstance(value, Enum) else value
        new = ComboInput.from_dict(data)
        self._combo.inputs[index] = new
        self._combo.inputs.sort(key=lambda i: i.frame)
        self._dirty = True
        return new

    def clear_inputs(self) -> None:
        self._require_loaded()
        self._combo.inputs.clear()
        self._dirty = True

    def inputs_between(self, start_frame: int, end_frame: int) -> Iterator[ComboInput]:
        """Yield inputs whose frame lies in [start_frame, end_frame]. Used by
        the timeline renderer to cull off-screen icons."""
        self._require_loaded()
        for entry in self._combo.inputs:
            if entry.frame > end_frame:
                break
            if entry.frame >= start_frame:
                yield entry

    # -- persistence ---------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        """
        Write the combo JSON. If the video sits alongside/below the JSON,
        store its path relative so combo packs can be zipped and shared.
        """
        self._require_loaded()
        if path is None:
            if self._json_path is None:
                raise ValueError("No save path set; provide one for first save.")
            path = self._json_path
        path = Path(path)

        payload = self._combo.to_dict()
        try:
            video_abs = Path(self._combo.video_path).resolve()
            payload["video_path"] = str(
                video_abs.relative_to(path.resolve().parent)
            )
        except ValueError:
            pass  # video lives elsewhere; keep the absolute path

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic-ish save: never leave a half-written file

        self._json_path = path
        self._dirty = False
        return path

    def load(self, path: str | Path) -> ComboFile:
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        combo = ComboFile.from_dict(data)

        # Resolve relative video path against the JSON's directory.
        video = Path(combo.video_path)
        if not video.is_absolute():
            combo.video_path = str((path.parent / video).resolve())

        self._combo = combo
        self._json_path = path
        self._dirty = False
        return combo

    # -- internals -----------------------------------------------------------

    def _require_loaded(self) -> None:
        if self._combo is None:
            raise RuntimeError("No combo loaded. Call new_combo() or load() first.")
