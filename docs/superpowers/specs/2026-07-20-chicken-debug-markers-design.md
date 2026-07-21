# Chicken Count Debug Markers — Design

## Problem

Nighttime roost photos are captured with infrared, and the OpenAI vision call
(`openai_roost_count()` in `coop_control.py`) sometimes undercounts chickens —
in particular ones sitting below the front roost bar, which a human reviewing
the same photo can spot but the model misses. There's currently no way to see
*what* the model identified as a chicken versus what it missed; the API only
returns a bare integer, so debugging a miscount means squinting at the raw
photo yourself and guessing why the number came out low.

## Goal

Add an on-demand debug mode that asks the model to locate each chicken it
identifies (not just count them), draws numbered markers on the photo at
those locations, and delivers the annotated image to Telegram — so a
mismatch between the reported count and `TOTAL_CHICKENS` can be visually
diagnosed (is the model missing a chicken entirely, or double-counting one).

## Non-goals

- Not changing the production `--chicken_count` / default nightly behavior.
  That path keeps its existing bare-integer prompt and two-pass logic
  untouched.
- Not changing `OPENAI_MODEL` or its default. This spec only adds a new
  debug prompt/parsing path used by the new flag; model choice is a separate
  decision left for later.
- Not building precise object detection (bounding-box IoU accuracy, etc.).
  The markers are a debugging aid for a human to eyeball, not a measurement
  tool.

## CLI

A new standalone mode flag, consistent with the existing pattern:

```
python coop_control.py --chicken_debug
```

- Implies a chicken-only run (same as `--chicken_count`): `RUN_CHICKEN = True`,
  `RUN_DOOR = False`.
- Requires the same env vars as `--chicken_count` (`ROOST_IP`, `ROOST_USER`,
  `ROOST_PASS`, `ROOST_PRESET`, `TOTAL_CHICKENS`, `OPENAI_API_KEY`), validated
  by the existing `validate_env()` path — no new required env vars.
- Mutually exclusive with `--chicken_count`: passing both is a validation
  error at startup, same style as the existing `--auto_door_close` /
  `--auto_door_open` exclusivity check.
- Respects `--telegram_off` like every other mode.
- Combinable with `--auto_door_close` / `--auto_door_open`, same as
  `--chicken_count` is today (each mode flag just turns on its own
  `RUN_CHICKEN` / `RUN_DOOR`; `--chicken_debug` slots into the `any_mode` /
  `RUN_CHICKEN` logic exactly where `--chicken_count` does today).

## Coordinate format

The debug prompt asks the model for each chicken's location as an **absolute
pixel bounding box** (`[x1, y1, x2, y2]`), with the actual image width/height
told to the model in the prompt text (read from the captured JPG via
`cv2.imread` before the API call). Absolute pixel coordinates are used
instead of percentage/normalized coordinates because vision models are
generally more reliable at returning coordinates when given the real pixel
dimensions to anchor against, rather than estimating a fraction of an image
whose true resolution they can't otherwise infer.

For drawing, the marker is placed at the center of each returned box:
`((x1+x2)/2, (y1+y2)/2)`. The box itself is not drawn — just a numbered
circle at its center — to keep the overlay readable on a small/dense roost
image (see "Marker style" below).

Expected response shape (JSON):

```json
{"count": 6, "chickens": [{"box": [120, 340, 210, 430]}, ...]}
```

## Prompt design

Debug mode reuses the same two-pass structure as production counting
(pass1, then pass2 recount if pass1's count doesn't match `TOTAL_CHICKENS`),
but with a debug-specific prompt that carries over the existing counting
guidance (count one-by-one, check underneath/overlapping chickens, roost
bar note, etc.) and additionally asks for the JSON box-list format above,
including the image's pixel width/height in the prompt text.

Each pass that runs gets annotated independently — if pass2 runs, you get
two annotated images, one per pass, so you can visually compare whether the
recount found a chicken the first pass missed.

Parsing: extract the JSON object from the response text (same tolerance for
surrounding prose as today's regex-based integer extraction), decode it, and
validate that `count` matches `len(chickens)`. If parsing fails after
retries, that pass falls back to the same "analysis failed" error path as
today (see Error handling).

## Drawing

New helper `annotate_chicken_image(image_path, chickens) -> str`:

- Opens `image_path` with `cv2.imread`.
- For each chicken (in order), draws a filled circle at the box's center and
  a number label (1, 2, 3, ...) next to it via `cv2.putText`.
- Saves to `logs/roost_debug_pass{1,2}_<timestamp>.jpg` (reusing the
  timestamp already generated for the underlying capture). These are not
  overwritten as a rotating "latest" file — each debug run's annotated
  images persist in `logs/` for later comparison, same lifecycle as the
  existing timestamped capture files.

## Output

For each pass that runs, one Telegram photo message is sent: the annotated
image for that pass, with the same caption format already produced by
`format_roost_message()` (e.g. "🔴 PROBLEM: Only 5 out of 6 chickens
found."). No separate plain-photo message — the annotated image replaces it
for debug runs. This follows the existing `send_telegram()` /
`make_telegram_image_copy()` path unchanged (the annotated JPG is just
another file path passed in).

## Error handling

Follows the existing `run_*_check()` shape:

- Camera unreachable → `format_camera_unreachable_message("Roost")`, no
  photo, same as today.
- OpenAI call or JSON parsing fails after retries → send the existing
  "🔴 PROBLEM: Roost analysis failed (...)" message with the **plain**
  captured photo (not annotated, since there's nothing to annotate) as the
  attachment — same fallback pattern used elsewhere in the file.

## New/changed functions in `coop_control.py`

- `--chicken_debug` argparse flag + mode-flag wiring (`RUN_CHICKEN`,
  validation exclusivity with `--chicken_count`).
- `_openai_run_debug_prompt(data_url, prompt, width, height)` — debug
  counterpart to `_openai_run_count_prompt()`, returns `(count, chickens)`
  instead of a bare int; reuses `with_retries()`.
- `openai_roost_count_debug(image_path)` — debug counterpart to
  `openai_roost_count()`, same two-pass control flow, returns a list of
  `(count, chickens, image_path)` per pass that ran.
- `annotate_chicken_image(image_path, chickens) -> str` — new drawing
  helper described above.
- `run_chicken_check_debug()` — new top-level flow function, same shape as
  `run_chicken_check()`, calling the above and sending one Telegram message
  per pass.
- `main` dispatch: `if args.chicken_debug: run_chicken_check_debug()`
  alongside the existing `RUN_CHICKEN` / `RUN_DOOR` dispatch.

## Testing

No automated test suite exists in this repo (per CLAUDE.md). Verification
is manual, same pattern as other changes here:

- Run `python coop_control.py --chicken_debug --telegram_off` against the
  real roost camera (or a saved test frame, if one is substituted in
  manually) and confirm:
  - `logs/coop_monitor.log` shows pass1 (and pass2 if triggered) with
    parsed count and chicken list logged.
  - Annotated JPG(s) appear in `logs/` with numbered circles roughly on top
    of visible chickens.
- Run once more without `--telegram_off` against a test chat to confirm the
  annotated image (not the plain one) arrives with the expected caption.
- Confirm `--chicken_debug --chicken_count` together exits with a validation
  error, matching the existing door-flag exclusivity behavior.
