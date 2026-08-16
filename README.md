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
- Leverless/d-pad only for motions. SOCD: L+R→neutral, U+D→neutral
  (configurable in `input_engine.py`). "Facing Left" in the Mode menu
  mirrors motion detection. **Ctrl+T** opens the Controller Test modal: pick
  the right XInput slot, watch live d-pad/button/trigger state, and confirm
  motions parse (e.g. QCF) before recording. The `dir: N` readout (numpad notation, top-right
  of the lane) live-verifies your box's output.

## Modules

- `models.py` — JSON schema v1, ComboFile/ComboInput, ComboManager, timing windows
- `frame_clock.py` — thread-safe extrapolated video-frame clock (the canonical time source)
- `motion_parser.py` — SOCD cleaning, numpad mapping, ring buffer, motion recognition
- `input_engine.py` — 250 Hz XInput QThread, rising-edge detection, InputEvent stream
- `controllers.py` — Record sink + Practice matcher/judge with stats and seek resync
- `timeline.py` — scrolling icon lane, hit zone, sparks, judgement popups
- `audio.py` — procedurally generated hit-confirm WAV + QSoundEffect
- `state.py` — IDLE/RECORD/PRACTICE state machine with guarded transitions
- `main_window.py` — PyQt6 shell + embedded mpv, wires everything together
- `app.py` — entry point, Windows libmpv DLL discovery

## Config knobs

- Button map / trigger threshold: `input_engine.DEFAULT_BUTTON_MAP`
- Timing windows: `TimingWindows(perfect_frames=3, good_frames=5)` in `controllers`/`models`
- Motion window & charge time: `motion_parser.ParserConfig`
- Scroll density: `timeline.PX_PER_FRAME`
- Strict motion matching (require QCF vs. accept Quick Special): `PracticeController(strict_motions=...)`
