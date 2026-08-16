# Tōkon Combo Trainer

Rhythm-game style combo trainer for Marvel Tōkon: Fighting Souls.
Record combo inputs against a video, then practice them with a scrolling
timeline, frame-graded timing, and hit-confirm feedback — at any playback speed.

## Setup (Windows)

1. `pip install -r requirements.txt`
2. Download a libmpv build and place `libmpv-2.dll` in the project root,
   or set `MPV_DLL_DIR` to its folder.
3. Run: `python -m combo_trainer.app`

## Usage

- **Ctrl+O** open a combo video → **F2** Record mode → play the video and
  perform the combo on your controller → **Ctrl+S** save the JSON.
- **Ctrl+Shift+O** open a combo JSON (loads its video too) → **F3** Practice
  mode → press Play. Icons scroll into the hit zone; ±3f = Perfect (spark +
  sound), ±5f = Good, else Miss. Speed dropdown (0.25x–1x) rescales
  everything automatically.
- Leverless/d-pad only for motions. SOCD is per-layout (L+R→neutral,
  U+D→neutral by default). "Facing Left" in the Mode menu mirrors motion
  detection. **Ctrl+T** opens the Controller Test modal: pick the right
  XInput slot, watch all 16 switches with their current bindings, and confirm
  motions parse (e.g. QCF) before recording. The `dir: N` readout (numpad
  notation, top-right of the lane) live-verifies your box's output.

## Profiles & rebinding

**Ctrl+B** opens Profiles & Rebinding; **Ctrl+1…9** switch layout instantly.

Three layers, so a rebind never touches code:

- **Device** — the 16-button leverless: each switch has a stable id, a
  silkscreen label, and the XInput source it reports. Box wired differently?
  `DeviceLayout.swap_sources()` re-points it; labels and bindings stay put.
- **Game profile** — the top-level container ("Marvel Tōkon"), owning that
  game's vocabulary of *logical inputs* (Up, Light, Quick Skill…).
- **Input profiles (layouts)** — nested per game: a default, macro variants,
  per-character layouts. Loading a combo JSON whose `character` matches a
  layout selects it automatically.

A binding is a *list* of logical inputs, which is how 16 switches host 13
inputs gracefully: `()` unmapped, `(x,)` simple, `(x, y)` macro — and two
switches may name the same input (a second Up under the other thumb). The
shipped Marvel Tōkon layout demonstrates all three, plus a fourth: AUX4
binds `(DOWN, HEAVY)` as a **Command Normal** — a single-button crouching
Heavy / anti-air. Pairing a direction with an attack on the SAME switch
resolves that attack's `direction` from the press itself and forces
`motion=NONE`, skipping the ring-buffer motion parser entirely — instant and
deterministic, no timing window to race (`ResolvedAction.command_normal`).

Directional inputs are not buttons: they are OR-ed across every switch bound
to that cardinal, SOCD-cleaned, then fed to the motion parser. Quick Skill /
Quick Assemble / Quick Dash are marked `parse_motions=False` — the button IS
the motion, so a QCF sitting in the buffer is never stamped onto the note.

Profiles live in `~/.combo_trainer/profiles.json` (override with
`COMBO_TRAINER_HOME`). Rebinds apply live — the input thread swaps its
lookup table on the next poll, no restart.

## Modules

- `models.py` — JSON schema v1, ComboFile/ComboInput, ComboManager, timing windows
- `profiles.py` — device/game/layout hierarchy, bindings, resolution, ProfileManager
- `cluster_layout.py` — pure grid math for rendering simultaneous inputs as one cluster
- `games.py` — built-in 16-button leverless device + Marvel Tōkon profile
- `frame_clock.py` — thread-safe extrapolated video-frame clock (the canonical time source)
- `motion_parser.py` — SOCD cleaning, numpad mapping, ring buffer, motion recognition
- `input_engine.py` — 250 Hz XInput QThread, rising-edge detection, InputEvent stream
- `controllers.py` — Record sink + Practice matcher/judge with stats and seek resync
- `timeline.py` — scrolling icon lane, hit zone, sparks, judgement popups
- `audio.py` — procedurally generated hit-confirm WAV + QSoundEffect
- `state.py` — IDLE/RECORD/PRACTICE state machine with guarded transitions
- `controller_dialog.py` — controller test modal: slots, live switches, pipeline check
- `profile_dialog.py` — profile picker + rebinding modal (press-to-select)
- `main_window.py` — PyQt6 shell + embedded mpv, wires everything together
- `app.py` — entry point, Windows libmpv DLL discovery

## Tests

`python -m unittest discover -s tests` — the profile system is pure Python
(no Qt/XInput), so the suite runs off-Windows.

## Config knobs

- Bindings / SOCD mode: the profile system (Ctrl+B), or edit `games.py` defaults
- Trigger threshold: `input_engine.TRIGGER_THRESHOLD`
- Timing windows: `TimingWindows(perfect_frames=3, good_frames=5)` in `controllers`/`models`
- Motion window & charge time: `motion_parser.ParserConfig`
- Scroll density: `timeline.PX_PER_FRAME`
- Strict motion matching (require QCF vs. accept Quick Special): `PracticeController(strict_motions=...)`
