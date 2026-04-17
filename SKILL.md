---
name: macos-background-cua
description: Control and automate macOS applications (click, type, scroll, screenshot) without activating the window or stealing focus — the target app stays in the background. Use whenever the user wants to script a macOS app, drive UI from the command line or Python, send clicks/keys to a specific window, capture a specific window's screenshot (even if it's off-screen or occluded), build a computer-use agent targeting macOS, or automate a background app while they keep working in the foreground. Triggers on phrases like "control a Mac app from Python / shell", "click in this app without focusing it", "screenshot a window", "background CUA on macOS", "automate Messages / Mail / Xcode / any macOS app", "send events to a window", or references to Accessibility API + CGEventPostToPid automation.
---

# macOS Background CUA

A CLI tool (also importable as a Python module) that drives a macOS app — click, type, scroll, screenshot, drag — while the target window stays in the background. Every **input** operation is addressed by window id (`wid`); `x, y` are window pixel offsets, the same as screenshot pixels.

## Why background (not just pyautogui)

`pyautogui`, `osascript tell app … activate`, and raw `CGEventPost` all require the target window to be frontmost. This tool:

- Uses **AX APIs** (AXPress, AXValue, AXScrollDownByPage, AXSelectedRows) for semantic actions that work on backgrounded apps.
- Falls back to **`CGEventPostToPid`** for raw clicks/keys/wheel targeted at the app's PID — lands without activation.
- Captures window screenshots via `CGWindowListCreateImage` — works even when occluded or off-screen.

Prereqs: Accessibility + Screen Recording permission for the Python interpreter (System Settings → Privacy & Security). Dependencies: `pyobjc` (`pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`, `pyobjc-framework-ApplicationServices`).

## The script

Single file: [scripts/macos_bg_cua.py](scripts/macos_bg_cua.py). Run it directly:

```bash
S=~/.claude/skills/macos-background-cua/scripts/macos_bg_cua.py
```

All commands print JSON to stdout (one line). All input commands take `wid` as the first positional argument.

## Commands

### `list-windows` — enumerate windows

```bash
python $S list-windows
```

Each window: `{"pid", "wid", "width", "height", "owner", "name"}`. Filter with jq/grep to find the one you want.

Restricted to `kCGWindowLayer == 0` — where apps like Finder, Safari, Messages, Xcode put their main document windows. This skips menubar items, Control Center icons, wallpaper, notification popovers, and other system UI surfaces that `CGWindowListCopyWindowInfo` returns mixed in.

### `screenshot <wid>` — capture a window

```bash
python $S screenshot 12345                     # → prints /tmp/win-12345.jpg
python $S screenshot 12345 -o /tmp/out.png --png
```

Works on backgrounded, occluded, and off-screen windows. `x, y` in every other command are pixel offsets into this image.

### `click <wid> <x> <y>`

Routes AX-first: a button → `AXPress`, a text field → focus it, a sidebar row → single-select, a pressable cell → press. Unroutable elements (canvas apps, empty space) → raw CG click targeted at the PID. Returns `{"plan": "...", "role": "...", "ok": true}` so you can see which path was taken.

### `right-click <wid> <x> <y>` · `double-click <wid> <x> <y>`

CG right-click (AX has no general right-click action) and a pair of clicks within the OS double-click threshold.

### `drag <wid> <x1> <y1> <x2> <y2> [--duration 0.3] [--steps 20]`

Interpolated CG drag. Good for sliders, resizing, drag-and-drop.

### `scroll <wid> <x> <y> <dx> <dy>`

Scroll at `(x, y)` by `(dx, dy)` pixels — positive `dy` scrolls down, positive `dx` scrolls right. Tries AX page-scroll first (Catalyst apps silently drop CG wheel events); falls back to CG wheel. Returns `{"via": "ax"}` or `{"via": "cg"}`.

### `type <wid> <text> [--at X Y]`

Write text into the app. The `--at` form is the most reliable: it clicks `(X, Y)` to focus a text field, then writes the whole string atomically via AX — handles Unicode, bypasses Catalyst focus quirks. Without `--at`, it uses whatever is currently focused (AX if a text field, CG keystrokes otherwise).

### `press <wid> <key> [--mod cmd]...` · `hotkey <wid> <mod>... <key>`

```bash
python $S press 12345 Enter
python $S press 12345 ArrowDown
python $S press 12345 F5
python $S hotkey 12345 cmd c                  # Cmd+C
python $S hotkey 12345 cmd shift p            # Cmd+Shift+P
```

Key names: `Enter Tab Space Escape Backspace Delete Arrow{Up,Down,Left,Right} Home End PageUp PageDown F1..F12`, plus single chars `a-z 0-9` and punctuation. Modifiers: `cmd shift alt ctrl fn`.

## Typical agent loop

```bash
# Find the target wid (filter `list-windows` output however you like)
WID=$(python $S list-windows | python -c "import sys,json; print(next(w['wid'] for w in json.load(sys.stdin) if 'Messages' in w['owner']))")

# Screenshot and inspect
python $S screenshot $WID -o /tmp/m.png --png

# Act on what you see — coords are pixel offsets into the screenshot
python $S click $WID 240 180
python $S type $WID "Hi there!" --at 500 780
python $S press $WID Enter
```

## Coordinate system

`x, y` default to **window pixel offsets, top-left origin** — matches screenshot pixels exactly. Other modes via `--coord`:

- `--coord pixel` (default) — window pixels
- `--coord normalized` — 0..1 fractions of window size (resolution-independent)
- `--coord global` — screen-global coords (pass through)

**Viewer downscaling — the #1 source of wrong clicks.** The ground truth is the window's real size from `list-windows` (`width` × `height`, same as the screenshot's pixel dimensions). Image viewers (including the Read tool) often render the screenshot at a smaller display size, so absolute pixel eyeballing from the rendered image will be wrong.

Convert proportionally: if a target looks 30% down and 20% across the visible image, and `list-windows` says the window is 921×546, the pixel coord is `(0.20 × 921, 0.30 × 546) = (184, 164)` — regardless of how your viewer rescaled it. Always ground estimates in the real `width`/`height` from `list-windows`, not the on-screen render.

## Python API

Same functions, same signatures. Add the scripts dir to `sys.path`:

```python
import sys
sys.path.insert(0, '/Users/USER/.claude/skills/macos-background-cua/scripts')
import macos_bg_cua as m

m.list_windows()
m.screenshot(wid, path='/tmp/w.jpg')
m.click(wid, x, y)
m.right_click(wid, x, y); m.double_click(wid, x, y)
m.drag(wid, x1, y1, x2, y2)
m.scroll(wid, x, y, dx, dy)
m.type_text(wid, 'hello', at=(120, 300))    # at=(x,y) clicks to focus first
m.press_key(wid, 'Enter')
m.hotkey(wid, 'cmd', 'c')
```

## Implementation notes worth knowing

These are the nonobvious bits baked in — useful for explaining behavior and debugging.

**App-scoped hit-test.** `AXUIElementCopyElementAtPosition(app_ax, x, y)` hit-tests within one app's AX tree. A system-wide hit-test would return the visually topmost element — wrong when the target is obscured by another window.

**Focus-steal guard.** Some CG paths can momentarily raise the app. After CG events, if the frontmost PID became the target, the previous frontmost is reactivated before the command returns (≈120ms).

**Catalyst flags.** On attach, `AXEnhancedUserInterface` and `AXManualAccessibility` are set to `True`. AppKit apps accept them and become more event-responsive while backgrounded; Catalyst rejects them harmlessly. Flags persist across CLI invocations.

**Catalyst row selection.** Catalyst sidebars (Messages, Maps) treat `AXPress` as a toggle and allow multi-select. The tool clears selected siblings first so AXPress ends up as a single-select — what users expect from a click.

**AppKit row selection.** Native AppKit tables (Mail, Finder sidebar) don't expose `AXPress` on rows — selection is via `AXSelectedRows` on the enclosing table. The tool detects and dispatches that.

**AX scroll vs CG wheel.** Catalyst apps silently drop CG scroll wheel events. AX `AXScroll*ByPage` is coarser (one page per event) but actually scrolls, so the tool tries AX first.

**Canvas apps.** Bambu Studio, Blender, and similar wxWidgets/Qt/OpenGL apps expose no useful AX tree. CG events sent to their PID may be dropped because their event loops want key-window state — without activation, there's no reliable fix.

**Text input via AXValue.** `type` writes directly to the AXValue/AXSelectedText of the clicked text element, even when `AXFocusedUIElement` reports something else (common on Catalyst). That's why `--at X Y` is more reliable than relying on prior focus.

## Permissions checklist

If clicks/keys silently don't land and screenshots come back black:

1. System Settings → Privacy & Security → **Accessibility**: add the Python interpreter (or Terminal / your app).
2. System Settings → Privacy & Security → **Screen Recording**: same.
3. Restart the Python process after granting — permissions are checked at process start.

A uniformly black screenshot = Screen Recording denied. If clicks silently don't land, Accessibility is likely denied.
