# Chicken Count Debug Markers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--chicken_debug` CLI mode to `coop_control.py` that asks OpenAI to locate each chicken it sees (as pixel bounding boxes), draws numbered markers at those locations, and sends the annotated photo to Telegram — so a nightly undercount can be visually diagnosed instead of guessed at.

**Architecture:** One new file (`coop_control.py` only — this repo has no other source modules) grows by ~120 lines across five small, independently-addable pieces: a CLI flag, a JSON-response parser + OpenAI call wrapper, an image-annotation helper, a two-pass counting driver that ties them together, and a top-level check function that follows the existing `run_*_check()` shape. Every new function is a pure addition — no existing function's behavior changes, and the production `--chicken_count` path is untouched.

**Tech Stack:** Python 3.9+, `cv2` (opencv-python-headless, already a dependency) for drawing, stdlib `json` (new import) for parsing, OpenAI Responses API (already in use) for the vision call.

## Global Constraints

- This repo has **no test framework** (no pytest, no test files) — confirmed in `CLAUDE.md`. "Testing" here means either (a) importing `coop_control.py` as a module with dummy env vars and mode-appropriate CLI args set on `sys.argv` before import — this runs all top-level setup code but **not** the `if __name__ == "__main__":` block, so it's safe for exercising pure functions with zero network/hardware calls — or (b) running the script for real against hardware. Every task below uses (a) except the final task, which is (b) and must be run by you, not a subagent.
- Must not change `OPENAI_MODEL` or its default value (explicit instruction from the spec owner).
- Must not modify `openai_roost_count()`, its prompts, or the production `--chicken_count` behavior in any way (spec non-goal).
- Any new network-ish call (OpenAI) must go through the existing `with_retries()` helper — don't add ad-hoc retry loops (`CLAUDE.md`).
- New top-level check functions must follow the existing `run_*_check()` shape: camera-unreachable, analysis-failed, and success are three separate Telegram outcomes (`CLAUDE.md`).
- Keep `README.md` and `CLAUDE.md` updated for this change (`CLAUDE.md`).
- Run all commands from the repo root: `/Users/jegaaravandy/Library/CloudStorage/OneDrive-Valtanix/W/Projects/coop_control`, with the project's venv active (`source venv/bin/activate`) so `cv2`, `openai`, etc. are importable.

---

### Task 1: `--chicken_debug` CLI flag and mode wiring

**Files:**
- Modify: `coop_control.py:36-70` (argparse section + mode-flag derivation + exclusivity checks)
- Modify: `coop_control.py:198-203` (startup log lines)

**Interfaces:**
- Produces: CLI flag `--chicken_debug`; module-level booleans `RUN_CHICKEN` (existing, now also true when `--chicken_debug` is passed) and `CHICKEN_DEBUG` (new) — later tasks read `CHICKEN_DEBUG` to decide which check function to call.

- [ ] **Step 1: Add the new argparse argument**

In `coop_control.py`, immediately after the existing `--chicken_count` argument block (ends at line 40), insert:

```python
parser.add_argument(
    "--chicken_debug",
    action="store_true",
    help="Run the roost chicken count with debug marker annotation "
         "(locates each chicken and draws numbered markers on the photo).",
)
```

- [ ] **Step 2: Wire it into the run-plan derivation and add the exclusivity check**

Replace this block (current lines 56-70):

```python
# Determine run plan:
# - If no mode flags provided -> default nightly: chicken_count + auto_door_close
any_mode = args.chicken_count or args.auto_door_close or args.auto_door_open
RUN_CHICKEN = args.chicken_count or (not any_mode)
RUN_DOOR = args.auto_door_close or args.auto_door_open or (not any_mode)

DOOR_EXPECTED_OVERRIDE = None
if args.auto_door_close:
    DOOR_EXPECTED_OVERRIDE = "CLOSED"
if args.auto_door_open:
    DOOR_EXPECTED_OVERRIDE = "OPEN"

if args.auto_door_close and args.auto_door_open:
    print("❌ Use only one: --auto_door_close or --auto_door_open")
    sys.exit(1)
```

with:

```python
# Determine run plan:
# - If no mode flags provided -> default nightly: chicken_count + auto_door_close
any_mode = (
    args.chicken_count or args.chicken_debug or args.auto_door_close or args.auto_door_open
)
RUN_CHICKEN = args.chicken_count or args.chicken_debug or (not any_mode)
RUN_DOOR = args.auto_door_close or args.auto_door_open or (not any_mode)

CHICKEN_DEBUG = args.chicken_debug

DOOR_EXPECTED_OVERRIDE = None
if args.auto_door_close:
    DOOR_EXPECTED_OVERRIDE = "CLOSED"
if args.auto_door_open:
    DOOR_EXPECTED_OVERRIDE = "OPEN"

if args.auto_door_close and args.auto_door_open:
    print("❌ Use only one: --auto_door_close or --auto_door_open")
    sys.exit(1)

if args.chicken_count and args.chicken_debug:
    print("❌ Use only one: --chicken_count or --chicken_debug")
    sys.exit(1)
```

- [ ] **Step 3: Log the new flag alongside the existing startup log lines**

Replace this line (current line 198):

```python
log.info(f"RUN_CHICKEN={RUN_CHICKEN}, RUN_DOOR={RUN_DOOR}, TELEGRAM_ENABLED={TELEGRAM_ENABLED}")
```

with:

```python
log.info(
    f"RUN_CHICKEN={RUN_CHICKEN}, RUN_DOOR={RUN_DOOR}, "
    f"CHICKEN_DEBUG={CHICKEN_DEBUG}, TELEGRAM_ENABLED={TELEGRAM_ENABLED}"
)
```

- [ ] **Step 4: Verify the flag is registered**

Run:

```bash
python coop_control.py --help
```

Expected: help text includes a `--chicken_debug` line with the help string from Step 1. Exit code 0.

- [ ] **Step 5: Verify the exclusivity check**

Run:

```bash
python coop_control.py --chicken_count --chicken_debug --telegram_off; echo "EXIT:$?"
```

Expected output includes `❌ Use only one: --chicken_count or --chicken_debug` and `EXIT:1`. This must exit before any env var validation, so it works even without a configured `.env`.

- [ ] **Step 6: Commit**

```bash
git add coop_control.py
git commit -m "Add --chicken_debug CLI flag and mode wiring"
```

---

### Task 2: Debug JSON response parsing + OpenAI call wrapper

**Files:**
- Modify: `coop_control.py:12` (add `import json`)
- Modify: `coop_control.py:254-256` (insert new functions between `_openai_run_count_prompt` and `openai_roost_count`)

**Interfaces:**
- Consumes: `with_retries(fn, tries, delay, backoff, label)` (existing, `coop_control.py:214`), `client` / `OPENAI_MODEL` (existing module globals).
- Produces: `_parse_debug_response(text: str) -> tuple[int, list[dict]]`, `_openai_run_debug_prompt(data_url: str, prompt: str) -> tuple[int, list[dict]]`. Each `dict` in the returned list has the shape `{"box": [x1, y1, x2, y2]}`. Task 4 consumes both of these.

- [ ] **Step 1: Add the `json` import**

In `coop_control.py`, change line 12 from:

```python
import re
```

to:

```python
import re
import json
```

- [ ] **Step 2: Write `_parse_debug_response`**

Immediately after `_openai_run_count_prompt` (which ends at line 253, right before `def openai_roost_count`), insert:

```python
def _parse_debug_response(text: str) -> tuple[int, list[dict]]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"Could not find JSON object in debug response: {text!r}")

    data = json.loads(match.group(0))

    if "count" not in data or "chickens" not in data:
        raise RuntimeError(f"Debug response missing 'count' or 'chickens': {data!r}")

    count = data["count"]
    chickens = data["chickens"]

    if not isinstance(chickens, list):
        raise RuntimeError(f"'chickens' is not a list: {data!r}")

    if count != len(chickens):
        raise RuntimeError(
            f"count {count} does not match len(chickens) {len(chickens)}: {data!r}"
        )

    for chicken in chickens:
        box = chicken.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            raise RuntimeError(f"Invalid box format in debug response: {chicken!r}")

    return count, chickens
```

- [ ] **Step 3: Write `_openai_run_debug_prompt`**

Immediately after `_parse_debug_response`, insert:

```python
def _openai_run_debug_prompt(data_url: str, prompt: str) -> tuple[int, list[dict]]:
    def _call():
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }],
        )
        text = resp.output_text.strip()
        return _parse_debug_response(text)

    return with_retries(_call, tries=3, delay=2.0, backoff=2.0, label="OpenAI chicken debug count")
```

- [ ] **Step 4: Verify `_parse_debug_response` with a self-contained smoke test**

Run (from the repo root, venv active):

```bash
ROOST_IP=1.2.3.4 ROOST_USER=u ROOST_PASS=p ROOST_PRESET=roost TOTAL_CHICKENS=6 OPENAI_API_KEY=dummy \
python3 -c "
import sys
sys.argv = ['coop_control.py', '--chicken_debug', '--telegram_off']
import coop_control as cc

count, chickens = cc._parse_debug_response(
    '{\"count\": 2, \"chickens\": [{\"box\": [1,2,3,4]}, {\"box\": [5,6,7,8]}]}'
)
assert count == 2 and len(chickens) == 2, (count, chickens)

count, chickens = cc._parse_debug_response(
    'Here is the result: {\"count\": 1, \"chickens\": [{\"box\": [0,0,1,1]}]} Thanks!'
)
assert count == 1 and len(chickens) == 1, (count, chickens)

try:
    cc._parse_debug_response('{\"count\": 3, \"chickens\": [{\"box\": [1,2,3,4]}]}')
    assert False, 'expected RuntimeError for count mismatch'
except RuntimeError:
    pass

try:
    cc._parse_debug_response('{\"count\": 1, \"chickens\": [{\"box\": [1,2,3]}]}')
    assert False, 'expected RuntimeError for malformed box'
except RuntimeError:
    pass

try:
    cc._parse_debug_response('no json here')
    assert False, 'expected RuntimeError for missing JSON'
except RuntimeError:
    pass

print('OK')
"
```

Expected output: `OK` (exit code 0). Importing `coop_control` here runs its module-level setup (argparse, `validate_env()`, OpenAI client construction) but does **not** hit the network or any camera — `OpenAI(api_key=...)` only constructs a client object, and none of `run_chicken_check()` / `run_door_check()` run because `__name__ != "__main__"` on import.

- [ ] **Step 5: Commit**

```bash
git add coop_control.py
git commit -m "Add debug JSON response parsing and OpenAI debug prompt call"
```

---

### Task 3: Marker-drawing helper

**Files:**
- Modify: `coop_control.py:328-331` (insert new function after `openai_door_state`, before the "Message formatting" section)

**Interfaces:**
- Consumes: `cv2` (existing import).
- Produces: `annotate_chicken_image(image_path: str, chickens: list[dict], out_path: str) -> str`. `chickens` is the list returned by `_parse_debug_response` / `_openai_run_debug_prompt` (Task 2) — each item has `{"box": [x1, y1, x2, y2]}`. Task 4 consumes this function.

- [ ] **Step 1: Write `annotate_chicken_image`**

Immediately after `openai_door_state` (ends at line 328), before the `# Message formatting` section comment, insert:

```python
def annotate_chicken_image(image_path: str, chickens: list[dict], out_path: str) -> str:
    img = cv2.imread(image_path)
    if img is None:
        raise RuntimeError(f"Could not read image for annotation: {image_path}")

    for i, chicken in enumerate(chickens, start=1):
        x1, y1, x2, y2 = chicken["box"]
        center = (int((x1 + x2) / 2), int((y1 + y2) / 2))

        cv2.circle(img, center, 10, (0, 0, 255), -1)
        cv2.putText(
            img, str(i), (center[0] + 14, center[1] + 6),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2,
        )

    if not cv2.imwrite(out_path, img):
        raise RuntimeError(f"Failed to write annotated image: {out_path}")

    return out_path
```

- [ ] **Step 2: Verify with a synthetic image**

Run:

```bash
ROOST_IP=1.2.3.4 ROOST_USER=u ROOST_PASS=p ROOST_PRESET=roost TOTAL_CHICKENS=6 OPENAI_API_KEY=dummy \
python3 -c "
import sys, os, tempfile
sys.argv = ['coop_control.py', '--chicken_debug', '--telegram_off']
import coop_control as cc
import cv2
import numpy as np

tmpdir = tempfile.mkdtemp()
src = os.path.join(tmpdir, 'blank.jpg')
out = os.path.join(tmpdir, 'annotated.jpg')

blank = np.zeros((100, 100, 3), dtype='uint8')
cv2.imwrite(src, blank)

chickens = [{'box': [30, 30, 50, 50]}, {'box': [60, 60, 80, 80]}]
result_path = cc.annotate_chicken_image(src, chickens, out)
assert result_path == out
assert os.path.exists(out)

annotated = cv2.imread(out)
# Chicken 1's box center is (40, 40) -> should now be filled red (BGR).
assert annotated[40, 40].tolist() == [0, 0, 255], annotated[40, 40].tolist()
# Chicken 2's box center is (70, 70) -> should also be filled red.
assert annotated[70, 70].tolist() == [0, 0, 255], annotated[70, 70].tolist()
# A far corner untouched by either marker should still be black.
assert annotated[5, 5].tolist() == [0, 0, 0], annotated[5, 5].tolist()

print('OK')
"
```

Expected output: `OK` (exit code 0).

- [ ] **Step 3: Commit**

```bash
git add coop_control.py
git commit -m "Add annotate_chicken_image marker-drawing helper"
```

---

### Task 4: Two-pass debug counting driver

**Files:**
- Modify: `coop_control.py` (insert new function directly after `annotate_chicken_image`, added in Task 3)

**Interfaces:**
- Consumes: `image_to_data_url` (existing, `coop_control.py:208`), `_openai_run_debug_prompt` (Task 2), `annotate_chicken_image` (Task 3), `TOTAL_CHICKENS` (existing module global), `log` (existing).
- Produces: `openai_roost_count_debug(image_path: str) -> list[tuple[int, list[dict], str]]` — one `(count, chickens, annotated_image_path)` tuple per pass that ran (one tuple if pass1 matched `TOTAL_CHICKENS`, two if pass2 also ran). Task 5 consumes this.

- [ ] **Step 1: Write `openai_roost_count_debug`**

Immediately after `annotate_chicken_image` (added in Task 3), insert:

```python
def openai_roost_count_debug(image_path: str) -> list[tuple[int, list[dict], str]]:
    """
    Debug counterpart to openai_roost_count(): same two-pass structure, but each
    pass asks for chicken bounding boxes and produces an annotated image so a
    miscount can be visually diagnosed.
    """
    data_url = image_to_data_url(image_path)

    img = cv2.imread(image_path)
    if img is None:
        raise RuntimeError(f"Could not read image for debug annotation: {image_path}")
    height, width = img.shape[:2]

    m = re.search(r"(\d{14})", image_path)
    timestamp = m.group(1) if m else datetime.now().strftime("%Y%m%d%H%M%S")

    prompt_pass1 = (
        f"Count the number of chickens visible in this image.\n"
        f"Count the chickens one by one.\n"
        f"Identify chickens by locating heads or eye reflections. If these are not visible then use body shapes.\n"
        f"Some chickens may be partially hidden or overlapping.\n"
        f"Assume no chicken is fully occluded unless proven otherwise.\n"
        f"Carefully check edges, corners, and underneath other chickens.\n"
        f"The amount we are looking for is {TOTAL_CHICKENS}.\n"
        f"If the count is less than {TOTAL_CHICKENS}, do a recount but don't make up numbers.\n"
        f"These chickens are roosting in a coop and so there won't be spaces between them.\n"
        f"Sometimes one of the chicken can be sitting underneath the chickens sitting in the front roost.\n"
        f"The image is {width} pixels wide and {height} pixels tall.\n"
        f"For EVERY chicken you count, report its location as a bounding box in absolute pixel "
        f"coordinates [x1, y1, x2, y2], where (x1,y1) is the top-left corner and (x2,y2) is the "
        f"bottom-right corner, using the image's actual pixel dimensions given above.\n"
        f'Return ONLY a JSON object of the form: {{"count": <int>, "chickens": [{{"box": [x1, y1, x2, y2]}}, ...]}}\n'
        f"No words outside the JSON."
    )

    count1, chickens1 = _openai_run_debug_prompt(data_url, prompt_pass1)
    log.info(f"OpenAI debug pass1 chicken count = {count1}, chickens = {chickens1}")

    annotated_path1 = f"logs/roost_debug_pass1_{timestamp}.jpg"
    annotate_chicken_image(image_path, chickens1, annotated_path1)
    results = [(count1, chickens1, annotated_path1)]

    if count1 == TOTAL_CHICKENS:
        return results

    prompt_pass2 = (
        f"You previously counted {count1} chickens, but the expected total is {TOTAL_CHICKENS}.\n"
        f"Do a FULL recount carefully.\n"
        f"Count one-by-one and avoid double counting.\n"
        f"Look for hidden chickens underneath or overlapping.\n"
        f"Be conservative. Do not invent chickens.\n"
        f"The image is {width} pixels wide and {height} pixels tall.\n"
        f"For EVERY chicken you count, report its location as a bounding box in absolute pixel "
        f"coordinates [x1, y1, x2, y2], where (x1,y1) is the top-left corner and (x2,y2) is the "
        f"bottom-right corner, using the image's actual pixel dimensions given above.\n"
        f'Return ONLY a JSON object of the form: {{"count": <int>, "chickens": [{{"box": [x1, y1, x2, y2]}}, ...]}}\n'
        f"No words outside the JSON."
    )

    count2, chickens2 = _openai_run_debug_prompt(data_url, prompt_pass2)
    log.info(f"OpenAI debug pass2 chicken count = {count2}, chickens = {chickens2}")

    annotated_path2 = f"logs/roost_debug_pass2_{timestamp}.jpg"
    annotate_chicken_image(image_path, chickens2, annotated_path2)
    results.append((count2, chickens2, annotated_path2))

    return results
```

- [ ] **Step 2: Verify the single-pass case with a fake OpenAI call**

This exercises the full function without real network access by monkeypatching `_openai_run_debug_prompt` before calling it — a reasonable substitute given this codebase has no mocking framework and the goal is to verify the two-pass control flow and file-naming logic, not the OpenAI integration itself (that's covered by Task 6's hardware run).

```bash
ROOST_IP=1.2.3.4 ROOST_USER=u ROOST_PASS=p ROOST_PRESET=roost TOTAL_CHICKENS=2 OPENAI_API_KEY=dummy \
python3 -c "
import sys, os, tempfile
sys.argv = ['coop_control.py', '--chicken_debug', '--telegram_off']
import coop_control as cc
import cv2
import numpy as np

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)
os.makedirs('logs', exist_ok=True)

img_path = 'logs/roost_20260101120000.jpg'
cv2.imwrite(img_path, np.zeros((50, 50, 3), dtype='uint8'))

# Pass1 matches TOTAL_CHICKENS=2 -> only one pass should run.
cc._openai_run_debug_prompt = lambda data_url, prompt: (2, [{'box': [1,1,5,5]}, {'box': [10,10,15,15]}])

results = cc.openai_roost_count_debug(img_path)
assert len(results) == 1, results
count, chickens, annotated_path = results[0]
assert count == 2 and len(chickens) == 2
assert annotated_path == 'logs/roost_debug_pass1_20260101120000.jpg', annotated_path
assert os.path.exists(annotated_path)

print('OK')
"
```

Expected output: `OK` (exit code 0).

- [ ] **Step 3: Verify the two-pass case**

```bash
ROOST_IP=1.2.3.4 ROOST_USER=u ROOST_PASS=p ROOST_PRESET=roost TOTAL_CHICKENS=3 OPENAI_API_KEY=dummy \
python3 -c "
import sys, os, tempfile
sys.argv = ['coop_control.py', '--chicken_debug', '--telegram_off']
import coop_control as cc
import cv2
import numpy as np

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)
os.makedirs('logs', exist_ok=True)

img_path = 'logs/roost_20260101120000.jpg'
cv2.imwrite(img_path, np.zeros((50, 50, 3), dtype='uint8'))

# Pass1 (2) does not match TOTAL_CHICKENS=3 -> pass2 must run and return 3.
responses = iter([
    (2, [{'box': [1,1,5,5]}, {'box': [10,10,15,15]}]),
    (3, [{'box': [1,1,5,5]}, {'box': [10,10,15,15]}, {'box': [20,20,25,25]}]),
])
cc._openai_run_debug_prompt = lambda data_url, prompt: next(responses)

results = cc.openai_roost_count_debug(img_path)
assert len(results) == 2, results
assert results[0][0] == 2 and results[0][2] == 'logs/roost_debug_pass1_20260101120000.jpg'
assert results[1][0] == 3 and results[1][2] == 'logs/roost_debug_pass2_20260101120000.jpg'
assert os.path.exists(results[0][2]) and os.path.exists(results[1][2])

print('OK')
"
```

Expected output: `OK` (exit code 0).

- [ ] **Step 4: Commit**

```bash
git add coop_control.py
git commit -m "Add openai_roost_count_debug two-pass driver"
```

---

### Task 5: `run_chicken_check_debug()`, main dispatch, and docs

**Files:**
- Modify: `coop_control.py:558-559` (insert new function before `run_door_check`)
- Modify: `coop_control.py:577-582` (main dispatch)
- Modify: `README.md:83-107` (Usage section)
- Modify: `CLAUDE.md` (Architecture notes bullet list)

**Interfaces:**
- Consumes: `move_then_capture_roost()` (existing), `openai_roost_count_debug()` (Task 4), `format_roost_message()` / `format_camera_unreachable_message()` (existing), `send_telegram()` (existing), `CHICKEN_DEBUG` (Task 1).

- [ ] **Step 1: Write `run_chicken_check_debug`**

In `coop_control.py`, immediately before `def run_door_check():` (currently line 560), insert:

```python
def run_chicken_check_debug():
    try:
        img = move_then_capture_roost()
    except Exception as e:
        log.warning(f"Roost camera not accessible: {e}")
        send_telegram(format_camera_unreachable_message("Roost"))
        return

    try:
        passes = openai_roost_count_debug(img)
    except Exception as e:
        msg = f"🔴 PROBLEM: Roost analysis failed ({e})"
        log.warning(msg)
        send_telegram(msg, img)
        return

    for count, chickens, annotated_path in passes:
        msg = format_roost_message(count)
        send_telegram(msg, annotated_path)


```

- [ ] **Step 2: Wire it into the main dispatch**

Replace the current bottom block (lines 577-582):

```python
if __name__ == "__main__":
    if RUN_CHICKEN:
        run_chicken_check()

    if RUN_DOOR:
        run_door_check()
```

with:

```python
if __name__ == "__main__":
    if RUN_CHICKEN:
        if CHICKEN_DEBUG:
            run_chicken_check_debug()
        else:
            run_chicken_check()

    if RUN_DOOR:
        run_door_check()
```

- [ ] **Step 3: Verify the module still imports cleanly and the new function is dispatchable**

```bash
ROOST_IP=1.2.3.4 ROOST_USER=u ROOST_PASS=p ROOST_PRESET=roost TOTAL_CHICKENS=6 OPENAI_API_KEY=dummy \
python3 -c "
import sys
sys.argv = ['coop_control.py', '--chicken_debug', '--telegram_off']
import coop_control as cc
assert cc.CHICKEN_DEBUG is True
assert callable(cc.run_chicken_check_debug)
print('OK')
"
```

Expected output: `OK` (exit code 0).

- [ ] **Step 4: Update README.md**

In `README.md`, replace the Usage code block and bullet list (current lines 85-107):

```markdown
### coop_control.py

```bash
# Default: chicken count + door check (uses DOOR_EXPECTED_STATE from .env)
python coop_control.py

# Only chicken count
python coop_control.py --chicken_count

# Only door check, expect CLOSED
python coop_control.py --auto_door_close

# Only door check, expect OPEN
python coop_control.py --auto_door_open

# Disable sending to Telegram (still logs what would be sent)
python coop_control.py --telegram_off
```

- **Default (no mode flags):** Runs both chicken check and door check. Door expected state comes from `DOOR_EXPECTED_STATE` (typical nightly: CLOSED).
- **`--chicken_count`:** Roost only.
- **`--auto_door_close`:** Door check only, expect CLOSED.
- **`--auto_door_open`:** Door check only, expect OPEN.

Images and logs go to `logs/` (timestamped JPGs plus `roost.jpg` / `auto_door.jpg`, and `coop_monitor.log`).
```

with:

```markdown
### coop_control.py

```bash
# Default: chicken count + door check (uses DOOR_EXPECTED_STATE from .env)
python coop_control.py

# Only chicken count
python coop_control.py --chicken_count

# Chicken count with debug markers (see below)
python coop_control.py --chicken_debug

# Only door check, expect CLOSED
python coop_control.py --auto_door_close

# Only door check, expect OPEN
python coop_control.py --auto_door_open

# Disable sending to Telegram (still logs what would be sent)
python coop_control.py --telegram_off
```

- **Default (no mode flags):** Runs both chicken check and door check. Door expected state comes from `DOOR_EXPECTED_STATE` (typical nightly: CLOSED).
- **`--chicken_count`:** Roost only.
- **`--chicken_debug`:** Roost only, same as `--chicken_count`, but instead of a bare count OpenAI is asked to locate each chicken. The photo is annotated with a numbered marker per detected chicken and sent to Telegram, so you can see exactly what the model found (and what it missed) instead of just a number. Mutually exclusive with `--chicken_count`.
- **`--auto_door_close`:** Door check only, expect CLOSED.
- **`--auto_door_open`:** Door check only, expect OPEN.

Images and logs go to `logs/` (timestamped JPGs plus `roost.jpg` / `auto_door.jpg`, `coop_monitor.log`, and — for `--chicken_debug` runs — `roost_debug_pass1_<timestamp>.jpg` / `roost_debug_pass2_<timestamp>.jpg`).
```

- [ ] **Step 5: Update CLAUDE.md**

In `CLAUDE.md`, in the "Architecture notes worth knowing before editing" section, after the bullet that starts with "**Chicken counting is two-pass**", add a new bullet:

```markdown
- **`--chicken_debug`** is a separate on-demand mode for diagnosing miscounts: it asks OpenAI for each chicken's pixel bounding box instead of a bare count, draws a numbered marker per detection onto the photo (`annotate_chicken_image()`), and sends that annotated image to Telegram instead of the plain one. It shares the two-pass structure with `openai_roost_count()` but uses its own prompt/parsing path (`openai_roost_count_debug()`) — the production `--chicken_count` path is unaffected.
```

- [ ] **Step 6: Commit**

```bash
git add coop_control.py README.md CLAUDE.md
git commit -m "Add run_chicken_check_debug, wire up dispatch, update docs"
```

---

### Task 6: Manual end-to-end hardware verification

This task requires a real roost camera and OpenAI API key, so it must be run by you, not a subagent. It exercises the full pipeline (ONVIF preset move, RTSP capture, real OpenAI vision call, real Telegram send) that no earlier task can safely simulate.

- [ ] **Step 1: Run without Telegram**

```bash
python coop_control.py --chicken_debug --telegram_off
```

Check `logs/coop_monitor.log` for:
- A line like `OpenAI debug pass1 chicken count = N, chickens = [...]`.
- If `N != TOTAL_CHICKENS`, a second line for pass2.

Check `logs/` for `roost_debug_pass1_<timestamp>.jpg` (and `roost_debug_pass2_<timestamp>.jpg` if a recount ran). Open the image(s) and confirm numbered red markers appear roughly on top of visible chickens.

- [ ] **Step 2: Run with Telegram enabled**

```bash
python coop_control.py --chicken_debug
```

Confirm the annotated image (not the plain photo) arrives in your Telegram chat, with a caption matching `format_roost_message()`'s style (e.g. "🐔 All 6 out of 6 chickens found." or "🔴 PROBLEM: Only 5 out of 6 chickens found."). If a recount ran, confirm you receive two separate photo messages, one per pass.

- [ ] **Step 3: Confirm existing checks still work unmodified**

```bash
python coop_control.py --chicken_count --telegram_off
python coop_control.py --auto_door_close --telegram_off
```

Confirm both behave exactly as before this change (bare-integer count message for the first, door-state message for the second) — this checks that nothing in the new code path leaked into the production paths.
