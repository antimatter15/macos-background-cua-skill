#!/usr/bin/env python3
"""macOS Background CUA — CLI + library for driving macOS apps without
activating them. Every input operation takes a window id (`wid`); x/y
coords are window pixel offsets (top-left origin), same as screenshot pixels.

CLI:
  list-windows                            JSON list of all windows
  screenshot <wid> [-o path] [--png]      capture; prints path
  click <wid> <x> <y>                     AX-first, CG fallback
  right-click <wid> <x> <y>
  double-click <wid> <x> <y>
  drag <wid> <x1> <y1> <x2> <y2> [--duration 0.3] [--steps 20]
  scroll <wid> <x> <y> <dx> <dy>          AX page-scroll preferred, CG wheel fallback
  type <wid> <text> [--at X Y]            --at X Y focuses a text field first
  press <wid> <key> [--mod cmd]...        Enter, Tab, F5, ArrowUp, 'a', etc.
  hotkey <wid> <mod> [<mod>...] <key>     e.g. `hotkey 1234 cmd shift p`

Python:
  import macos_bg_cua as m
  m.list_windows(); m.screenshot(wid, path)
  m.click(wid, x, y); m.scroll(wid, x, y, dx, dy); m.type_text(wid, 'hi')
  m.press_key(wid, 'Enter'); m.hotkey(wid, 'cmd', 'c')
"""

import argparse
import json
import sys
import time

import AppKit
import ApplicationServices as AS
import Foundation
import Quartz
import objc


# ============================================================
# Window enumeration
# ============================================================

def _window_list(wl):
    for v in wl:
        b = v.valueForKey_('kCGWindowBounds')
        if b is None:
            continue
        yield {
            'pid': int(v.valueForKey_('kCGWindowOwnerPID')),
            'wid': int(v.valueForKey_('kCGWindowNumber')),
            'layer': int(v.valueForKey_('kCGWindowLayer') or 0),
            'bounds': [int(b.valueForKey_(k)) for k in ('X', 'Y', 'Width', 'Height')],
            'owner': str(v.valueForKey_('kCGWindowOwnerName') or ''),
            'name': str(v.valueForKey_('kCGWindowName') or ''),
        }


def list_windows():
    """Return the normal app windows as
    [{pid, wid, width, height, owner, name}, ...].

    Filters to on-screen, `kCGWindowLayer == 0` — the layer where Finder,
    Safari, Xcode, etc. put their main document/app windows. Without this
    filter `CGWindowListCopyWindowInfo` also returns the Dock, Menu Bar,
    Control Center items, wallpaper surfaces, Notification Center, and every
    other window-server surface — rarely what you want.

    Window position on screen is intentionally omitted — callers only need
    width/height (for coord bounds and aspect) and wid (for targeting).
    Internal coord translation uses get_window(wid) which fetches fresh
    bounds at action time (the window may have moved since listing)."""
    with objc.autorelease_pool():
        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        out = []
        for w in _window_list(wl):
            if w['layer'] != 0:
                continue
            _x, _y, width, height = w['bounds']
            out.append({
                'pid': w['pid'],
                'wid': w['wid'],
                'width': width,
                'height': height,
                'owner': w['owner'],
                'name': w['name'],
            })
        return out


def get_window(wid):
    """Look up one window (includes off-screen) by id."""
    arr = Quartz.CGWindowListCreateDescriptionFromArray([int(wid)])
    lst = list(_window_list(arr))
    if not lst:
        raise RuntimeError(f'window {wid} not found')
    return lst[0]


# ============================================================
# AX helpers
# ============================================================

def ax_get(el, attr):
    if el is None:
        return None
    try:
        err, v = AS.AXUIElementCopyAttributeValue(el, attr, None)
        return v if err == 0 else None
    except Exception:
        return None


def ax_set(el, attr, val):
    if el is None:
        return False
    try:
        return AS.AXUIElementSetAttributeValue(el, attr, val) == 0
    except Exception:
        return False


def ax_actions(el):
    if el is None:
        return []
    try:
        err, names = AS.AXUIElementCopyActionNames(el, None)
        return list(names) if err == 0 and names else []
    except Exception:
        return []


def ax_press(el):
    if el is None:
        return False
    try:
        return AS.AXUIElementPerformAction(el, 'AXPress') == 0
    except Exception:
        return False


def ax_parent(el):
    return ax_get(el, 'AXParent')


def _is_settable(el, attr):
    try:
        err, ok = AS.AXUIElementIsAttributeSettable(el, attr, None)
        return err == 0 and bool(ok)
    except Exception:
        return False


def _hit_test_ax(app_ax, gx, gy):
    """App-scoped hit-test. System-wide would return the visually topmost
    element (wrong when target is obscured); app-scoped ignores z-order."""
    if app_ax is None:
        return None
    try:
        err, el = AS.AXUIElementCopyElementAtPosition(app_ax, float(gx), float(gy), None)
        return el if err == 0 else None
    except Exception:
        return None


def find_scrollable_ancestor(el, max_depth=15):
    """Walk up until an element advertises scroll actions or has a scrollbar.
    Catalyst apps expose scroll containers as AXGroup (not AXScrollArea) but
    with AXScroll*ByPage actions + AXVerticalScrollBar — role alone misses them."""
    cur = el
    for _ in range(max_depth):
        if cur is None:
            return None
        acts = ax_actions(cur)
        if any(a in acts for a in ('AXScrollDownByPage', 'AXScrollUpByPage',
                                    'AXScrollLeftByPage', 'AXScrollRightByPage')):
            return cur
        if ax_get(cur, 'AXVerticalScrollBar') or ax_get(cur, 'AXHorizontalScrollBar'):
            return cur
        cur = ax_parent(cur)
    return None


AX_PRESSABLE_ROLES = {
    'AXButton', 'AXMenuItem', 'AXMenuButton', 'AXCheckBox', 'AXRadioButton',
    'AXLink', 'AXPopUpButton', 'AXComboBox', 'AXSegmentedControl',
    'AXDisclosureTriangle', 'AXToolbarButton',
}
AX_SELECTABLE_ROLES = {
    'AXRow', 'AXCell', 'AXStaticText', 'AXOutlineRow', 'AXListItem',
}
AX_TEXT_ROLES = {'AXTextField', 'AXTextArea'}


def _single_select_row(el):
    """Make `el` the sole-selected row. Catalyst sidebars treat AXPress as a
    toggle and allow multi-select; clear siblings first, toggle off if
    already selected, then AXPress so it's the last-pressed and sole-selected."""
    parent = ax_parent(el)
    if parent is not None:
        for sib in (ax_get(parent, 'AXChildren') or []):
            if sib is el:
                continue
            if ax_get(sib, 'AXSelected'):
                ax_press(sib)
                time.sleep(0.03)
    if ax_get(el, 'AXSelected'):
        ax_press(el)
        time.sleep(0.05)
    return ax_press(el)


def _classify(el):
    role = ax_get(el, 'AXRole') or ''
    if role in AX_TEXT_ROLES:
        return ('focus_text', role)
    actions = ax_actions(el)
    if role in AX_PRESSABLE_ROLES and 'AXPress' in actions:
        return ('press', role)
    if role in AX_SELECTABLE_ROLES and 'AXPress' in actions:
        return ('select_press', role)
    # Native AppKit tables (Mail, Finder sidebar) don't advertise AXPress on
    # rows — selection is via AXSelectedRows on the table / AXSelected on row.
    if role == 'AXRow' and _is_settable(el, 'AXSelected'):
        return ('select_row_attr', role)
    return (None, role)


def _opaque_to_ax(el):
    """hit_test returned a raw window/application — no useful element at the
    point. Happens with wxWidgets/Qt/OpenGL canvases. Route to CG."""
    if el is None:
        return True
    return ax_get(el, 'AXRole') in ('AXWindow', 'AXApplication', None)


def _search_descendants(el, max_depth=3):
    queue = [(el, 0)]
    while queue:
        cur, d = queue.pop(0)
        if d > 0:
            plan, role = _classify(cur)
            if plan is not None:
                return (plan, cur, role)
        if d >= max_depth:
            continue
        for c in (ax_get(cur, 'AXChildren') or []):
            queue.append((c, d + 1))
    return None


def _plan_click(el):
    if el is None:
        return ('cg', None, None)
    plan, role = _classify(el)
    if plan is not None:
        return (plan, el, role)
    if _opaque_to_ax(el):
        return ('cg', el, role)
    cur = ax_parent(el)
    for _ in range(5):
        if cur is None:
            break
        p, r = _classify(cur)
        if p is not None:
            return (p, cur, r)
        cur = ax_parent(cur)
    hit = _search_descendants(el, max_depth=3)
    if hit is not None:
        return hit
    return ('cg', el, role)


def _try_ax_scroll(el, dy, dx):
    """Page-scroll via AX on the nearest scrollable ancestor. Coarser than a
    real wheel (one page per event) but works against Catalyst apps where
    CGEvent wheel is silently dropped."""
    scroll_el = find_scrollable_ancestor(el)
    if scroll_el is None:
        return False
    actions = ax_actions(scroll_el)
    did = False
    if dy:
        act = 'AXScrollDownByPage' if dy > 0 else 'AXScrollUpByPage'
        if act in actions and AS.AXUIElementPerformAction(scroll_el, act) == 0:
            did = True
    if dx:
        act = 'AXScrollRightByPage' if dx > 0 else 'AXScrollLeftByPage'
        if act in actions and AS.AXUIElementPerformAction(scroll_el, act) == 0:
            did = True
    return did


# ============================================================
# CGEvent primitives (PID-targeted — no activation needed)
# ============================================================

def _cg_move(pid, gx, gy):
    ev = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved,
        Quartz.CGPointMake(gx, gy), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPostToPid(pid, ev)


def _cg_mouse_down(pid, gx, gy, button='left'):
    kind = (Quartz.kCGEventRightMouseDown if button == 'right'
            else Quartz.kCGEventLeftMouseDown)
    btn = Quartz.kCGMouseButtonRight if button == 'right' else Quartz.kCGMouseButtonLeft
    ev = Quartz.CGEventCreateMouseEvent(None, kind, Quartz.CGPointMake(gx, gy), btn)
    Quartz.CGEventPostToPid(pid, ev)


def _cg_mouse_up(pid, gx, gy, button='left'):
    kind = (Quartz.kCGEventRightMouseUp if button == 'right'
            else Quartz.kCGEventLeftMouseUp)
    btn = Quartz.kCGMouseButtonRight if button == 'right' else Quartz.kCGMouseButtonLeft
    ev = Quartz.CGEventCreateMouseEvent(None, kind, Quartz.CGPointMake(gx, gy), btn)
    Quartz.CGEventPostToPid(pid, ev)


def _cg_scroll(pid, dy, dx=0):
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx))
    Quartz.CGEventPostToPid(pid, ev)


def _cg_key(pid, keycode, down, flags=0):
    ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPostToPid(pid, ev)


# ============================================================
# Focus-steal guard (synchronous — CLI has no event loop)
# ============================================================

RAISE_SETTLE = 0.12


def _frontmost_pid():
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else None


def _guard_and_restore(target_pid, work):
    """Run `work()`, then if target became frontmost, reactivate the previous
    frontmost. Only used for CG paths that can momentarily raise the app."""
    prev = _frontmost_pid()
    steal_possible = prev is not None and int(prev) != int(target_pid)
    result = work()
    if not steal_possible:
        return result
    time.sleep(RAISE_SETTLE)
    cur = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if cur is not None and int(cur.processIdentifier()) == int(target_pid):
        prev_app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(prev))
        if prev_app is not None and not prev_app.isTerminated():
            prev_app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    return result


# ============================================================
# Attach (idempotent — safe to call per-invocation)
# ============================================================

def _attach(wid):
    """Return (pid, app_ax, bounds). Sets AXEnhancedUserInterface and
    AXManualAccessibility — AppKit apps accept and become more event-responsive
    while backgrounded; Catalyst rejects harmlessly. Flags stay set across
    invocations, which is fine."""
    win = get_window(wid)
    pid = int(win['pid'])
    app_ax = AS.AXUIElementCreateApplication(pid)
    try:
        AS.AXUIElementSetMessagingTimeout(app_ax, 2.0)
    except Exception:
        pass
    ax_set(app_ax, 'AXEnhancedUserInterface', True)
    ax_set(app_ax, 'AXManualAccessibility', True)
    return pid, app_ax, win['bounds']


def _to_global(bounds, x, y, coord='pixel'):
    """Translate window-local (x,y) to global screen coords.
    coord='pixel'  → window pixel offset (matches screenshot pixels)
    coord='normalized' → 0..1 fractions of window size
    coord='global' → already global, pass through."""
    if coord == 'global':
        return (float(x), float(y))
    bx, by, bw, bh = bounds
    if coord == 'normalized':
        return (bx + bw * float(x), by + bh * float(y))
    return (bx + float(x), by + float(y))


# ============================================================
# Screenshot
# ============================================================

def screenshot(wid, path=None, fmt='jpeg', jpeg_quality=0.8):
    """Capture one window. Returns image bytes; if `path` given, writes it.
    fmt='jpeg' or 'png'. Works on occluded and off-screen windows."""
    with objc.autorelease_pool():
        win = get_window(wid)
        rect = Quartz.NSMakeRect(*win['bounds'])
        cg = Quartz.CGWindowListCreateImage(
            rect,
            Quartz.kCGWindowListOptionIncludingWindow,
            int(wid),
            Quartz.kCGWindowImageShouldBeOpaque | Quartz.kCGWindowImageNominalResolution,
        )
        rep = Quartz.NSBitmapImageRep.alloc().initWithCGImage_(cg)
        if fmt == 'png':
            data = rep.representationUsingType_properties_(
                Quartz.NSPNGFileType, Foundation.NSDictionary())
        else:
            props = Foundation.NSDictionary({'NSImageCompressionFactor': jpeg_quality})
            data = rep.representationUsingType_properties_(
                Quartz.NSJPEGFileType, props)
        raw = bytes(data.bytes())
    if path:
        with open(path, 'wb') as f:
            f.write(raw)
    return raw


# ============================================================
# Mouse ops
# ============================================================

def click(wid, x, y, coord='pixel', hold=0.05):
    """Click at (x, y) — matches test17's down→up routing.

    Routing (see plan_click for how the element is classified):
      focus_text      → AXFocused=True on the text element
      press           → AXPress on the element
      select_press    → single_select_row (Catalyst sidebar quirk)
      select_row_attr → AXSelectedRows on the table (AppKit native)
      cg              → CGEventPostToPid mouse down/up targeted at app PID

    For `select_press` and `select_row_attr` we re-hit-test at action time
    before acting, mirroring test17. Those two paths modify the AX tree
    (selecting a row can collapse/expand children), so grabbing a fresh
    element right before the write is more robust than reusing the stale
    one from the initial hit-test.

    Returns {'plan', 'role', 'ok'}."""
    pid, app_ax, bounds = _attach(wid)
    gx, gy = _to_global(bounds, x, y, coord)
    el = _hit_test_ax(app_ax, gx, gy)
    plan, target_el, role = _plan_click(el)

    if plan == 'focus_text':
        ok = ax_set(target_el, 'AXFocused', True)
        return {'plan': plan, 'role': role, 'ok': bool(ok)}

    if plan == 'press':
        ok = ax_press(target_el)
        return {'plan': plan, 'role': role, 'ok': bool(ok)}

    if plan == 'select_press':
        fresh = _hit_test_ax(app_ax, gx, gy) or target_el
        ok = _single_select_row(fresh)
        return {'plan': plan, 'role': role, 'ok': bool(ok)}

    if plan == 'select_row_attr':
        # plan_click returned an AXRow, but the re-hit-test might land on a
        # descendant (a cell's AXStaticText etc.) — walk up to the AXRow.
        fresh = _hit_test_ax(app_ax, gx, gy) or target_el
        row = fresh
        while row is not None and ax_get(row, 'AXRole') != 'AXRow':
            row = ax_parent(row)
        if row is None:
            row = target_el
        # Prefer AXSelectedRows on the enclosing table — it replaces the
        # whole selection (so it won't accidentally multi-select).
        table = row
        while (table is not None
               and ax_get(table, 'AXRole') not in ('AXTable', 'AXOutline', 'AXList')):
            table = ax_parent(table)
        ok = False
        if table is not None and _is_settable(table, 'AXSelectedRows'):
            ok = ax_set(table, 'AXSelectedRows', [row])
        if not ok:
            ok = ax_set(row, 'AXSelected', True)
        return {'plan': plan, 'role': role, 'ok': bool(ok)}

    # plan == 'cg': raw click to the app's PID. Guard focus in case the
    # down/up momentarily raises the app.
    def _do():
        _cg_mouse_down(pid, gx, gy)
        if hold:
            time.sleep(hold)
        _cg_mouse_up(pid, gx, gy)
    _guard_and_restore(pid, _do)
    return {'plan': 'cg', 'role': role, 'ok': True}


def right_click(wid, x, y, coord='pixel', hold=0.05):
    """Raw CG right-click targeted at the app's PID. Context menus are almost
    always a CG-level interaction; AX has no general 'right-click' action."""
    pid, _, bounds = _attach(wid)
    gx, gy = _to_global(bounds, x, y, coord)

    def _do():
        _cg_mouse_down(pid, gx, gy, button='right')
        if hold:
            time.sleep(hold)
        _cg_mouse_up(pid, gx, gy, button='right')
    _guard_and_restore(pid, _do)
    return {'plan': 'cg', 'ok': True}


def double_click(wid, x, y, coord='pixel', gap=0.08):
    """Two clicks in quick succession (within the OS double-click threshold)."""
    click(wid, x, y, coord=coord)
    time.sleep(gap)
    click(wid, x, y, coord=coord)
    return {'plan': 'double', 'ok': True}


def drag(wid, x1, y1, x2, y2, coord='pixel', steps=20, duration=0.3, button='left'):
    """Drag from (x1,y1) to (x2,y2) with interpolated mouse moves."""
    pid, _, bounds = _attach(wid)
    gx1, gy1 = _to_global(bounds, x1, y1, coord)
    gx2, gy2 = _to_global(bounds, x2, y2, coord)

    def _do():
        _cg_mouse_down(pid, gx1, gy1, button)
        dt = duration / max(steps, 1)
        for i in range(1, steps + 1):
            t = i / steps
            _cg_move(pid, gx1 + (gx2 - gx1) * t, gy1 + (gy2 - gy1) * t)
            time.sleep(dt)
        _cg_mouse_up(pid, gx2, gy2, button)
    _guard_and_restore(pid, _do)
    return {'ok': True}


# ============================================================
# Scroll
# ============================================================

def scroll(wid, x, y, dx, dy, coord='pixel'):
    """Scroll at (x, y) by (dx, dy) pixels. Positive dy scrolls down,
    positive dx scrolls right. Tries AX page-scroll first (Catalyst-friendly),
    falls back to CG wheel. Returns 'ax' or 'cg'."""
    pid, app_ax, bounds = _attach(wid)
    gx, gy = _to_global(bounds, x, y, coord)
    el = _hit_test_ax(app_ax, gx, gy)
    if _try_ax_scroll(el, dy, dx):
        return 'ax'
    _cg_scroll(pid, dy, dx)
    return 'cg'


# ============================================================
# Keyboard
# ============================================================

US_KEYBOARD = {
    'a': 0, 'b': 11, 'c': 8, 'd': 2, 'e': 14, 'f': 3, 'g': 5, 'h': 4, 'i': 34,
    'j': 38, 'k': 40, 'l': 37, 'm': 46, 'n': 45, 'o': 31, 'p': 35, 'q': 12,
    'r': 15, 's': 1, 't': 17, 'u': 32, 'v': 9, 'w': 13, 'x': 7, 'y': 16, 'z': 6,
    '0': 29, '1': 18, '2': 19, '3': 20, '4': 21, '5': 23, '6': 22, '7': 26,
    '8': 28, '9': 25,
    '-': 27, '=': 24, '`': 50, '[': 33, ']': 30, ';': 41, "'": 39,
    ',': 43, '.': 47, '/': 44, '\\': 42,
    'Tab': 48, ' ': 49, 'Space': 49, 'Enter': 36, 'Return': 36,
    'Backspace': 51, 'Delete': 51, 'ForwardDelete': 117,
    'ArrowUp': 126, 'ArrowDown': 125, 'ArrowLeft': 123, 'ArrowRight': 124,
    'Up': 126, 'Down': 125, 'Left': 123, 'Right': 124,
    'Escape': 53, 'Esc': 53, 'Home': 115, 'End': 119,
    'PageUp': 116, 'PageDown': 121,
    'F1': 122, 'F2': 120, 'F3': 99, 'F4': 118, 'F5': 96, 'F6': 97,
    'F7': 98, 'F8': 100, 'F9': 101, 'F10': 109, 'F11': 103, 'F12': 111,
}

_SHIFTED = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
    '*': '8', '(': '9', ')': '0', '_': '-', '+': '=', '~': '`',
    '{': '[', '}': ']', ':': ';', '"': "'", '<': ',', '>': '.', '?': '/',
    '|': '\\',
}

_MODIFIER_FLAGS = {
    'shift': Quartz.kCGEventFlagMaskShift,
    'cmd': Quartz.kCGEventFlagMaskCommand,
    'command': Quartz.kCGEventFlagMaskCommand,
    'alt': Quartz.kCGEventFlagMaskAlternate,
    'option': Quartz.kCGEventFlagMaskAlternate,
    'opt': Quartz.kCGEventFlagMaskAlternate,
    'ctrl': Quartz.kCGEventFlagMaskControl,
    'control': Quartz.kCGEventFlagMaskControl,
    'fn': Quartz.kCGEventFlagMaskSecondaryFn,
}


def _keycode_for_char(ch):
    """Return (keycode, needs_shift) for a single char, or (None, False)."""
    if ch.isupper():
        code = US_KEYBOARD.get(ch.lower())
        return (code, True) if code is not None else (None, False)
    if ch in _SHIFTED:
        return (US_KEYBOARD[_SHIFTED[ch]], True)
    code = US_KEYBOARD.get(ch)
    return (code, False) if code is not None else (None, False)


def _flags_for(modifiers):
    flags = 0
    for m in modifiers or ():
        flags |= _MODIFIER_FLAGS[m.lower()]
    return flags


def type_text(wid, text, at=None, coord='pixel'):
    """Type `text` into the app. Path selection:

      1. If `at=(x,y)` given → click there first (focuses any text field) and
         AX-insert via AXSelectedText/AXValue. Most reliable.
      2. Else if AXFocusedUIElement is a text field → AX-insert.
      3. Else → per-char CG keystrokes (ASCII + punctuation; shift auto-handled
         for uppercase and symbols). Focus must already be on the right spot.

    Returns 'ax' or 'cg'. AX path writes the whole string atomically and
    handles Unicode; CG path is per-keystroke."""
    pid, app_ax, bounds = _attach(wid)
    target = None

    if at is not None:
        gx, gy = _to_global(bounds, at[0], at[1], coord)
        el = _hit_test_ax(app_ax, gx, gy)
        plan, target_el, _role = _plan_click(el)
        if plan == 'focus_text':
            ax_set(target_el, 'AXFocused', True)
            target = target_el

    if target is None:
        focused = ax_get(app_ax, 'AXFocusedUIElement')
        if focused is not None and ax_get(focused, 'AXRole') in AX_TEXT_ROLES:
            target = focused

    if target is not None:
        before = ax_get(target, 'AXValue') or ''
        # AXSelectedText inserts at the caret (or replaces selection). Preferred.
        if (ax_set(target, 'AXSelectedText', text)
                and (ax_get(target, 'AXValue') or '') != before):
            return 'ax'
        if ax_set(target, 'AXValue', before + text):
            return 'ax'

    for ch in text:
        code, shift = _keycode_for_char(ch)
        if code is None:
            continue
        flags = Quartz.kCGEventFlagMaskShift if shift else 0
        _cg_key(pid, code, True, flags)
        _cg_key(pid, code, False, flags)
    return 'cg'


def press_key(wid, key, modifiers=()):
    """Press a named key ('Enter','Tab','F5','ArrowDown','a', ...) with
    optional modifiers (('cmd',), ('shift','cmd'), ...)."""
    pid, _, _ = _attach(wid)
    code = US_KEYBOARD.get(key)
    if code is None:
        c, add_shift = _keycode_for_char(key)
        if c is None:
            raise ValueError(f'unknown key: {key!r}')
        code = c
        if add_shift:
            modifiers = tuple(modifiers) + ('shift',)
    flags = _flags_for(modifiers)
    _cg_key(pid, code, True, flags)
    _cg_key(pid, code, False, flags)
    return {'ok': True}


def hotkey(wid, *keys):
    """`hotkey(wid, 'cmd', 'c')` == Cmd+C. Last arg is the key, rest modifiers."""
    if len(keys) < 1:
        raise ValueError('need at least one key')
    *mods, key = keys
    return press_key(wid, key, modifiers=mods)


# ============================================================
# CLI
# ============================================================

def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str))


def _add_coord(p):
    p.add_argument('--coord', choices=['pixel', 'normalized', 'global'],
                   default='pixel',
                   help='coordinate system for x/y (default: pixel, matches screenshot)')


def _main(argv=None):
    ap = argparse.ArgumentParser(prog='macos_bg_cua',
                                 description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)

    # list-windows
    sub.add_parser('list-windows', help='list normal app windows as JSON')

    # screenshot
    p = sub.add_parser('screenshot', help='capture window; prints path')
    p.add_argument('wid', type=int)
    p.add_argument('-o', '--out', help='output path (default: /tmp/win-<wid>.<ext>)')
    p.add_argument('--png', action='store_true', help='PNG instead of JPEG')
    p.add_argument('--quality', type=float, default=0.8, help='JPEG quality 0-1')

    # click variants
    for name, help_ in [('click', 'click (AX-first, CG fallback)'),
                        ('right-click', 'right click'),
                        ('double-click', 'double click')]:
        p = sub.add_parser(name, help=help_)
        p.add_argument('wid', type=int)
        p.add_argument('x', type=float); p.add_argument('y', type=float)
        _add_coord(p)

    # drag
    p = sub.add_parser('drag', help='drag from (x1,y1) to (x2,y2)')
    p.add_argument('wid', type=int)
    p.add_argument('x1', type=float); p.add_argument('y1', type=float)
    p.add_argument('x2', type=float); p.add_argument('y2', type=float)
    p.add_argument('--duration', type=float, default=0.3)
    p.add_argument('--steps', type=int, default=20)
    _add_coord(p)

    # scroll
    p = sub.add_parser('scroll', help='scroll at (x,y) by (dx,dy) pixels')
    p.add_argument('wid', type=int)
    p.add_argument('x', type=float); p.add_argument('y', type=float)
    p.add_argument('dx', type=float); p.add_argument('dy', type=float)
    _add_coord(p)

    # type
    p = sub.add_parser('type', help='type text (AX insertion if possible)')
    p.add_argument('wid', type=int)
    p.add_argument('text')
    p.add_argument('--at', type=float, nargs=2, metavar=('X', 'Y'),
                   help='click here first to focus a text field')
    _add_coord(p)

    # press
    p = sub.add_parser('press', help='press a named key')
    p.add_argument('wid', type=int); p.add_argument('key')
    p.add_argument('--mod', action='append', default=[],
                   help='modifier: cmd/shift/alt/ctrl (repeatable)')

    # hotkey
    p = sub.add_parser('hotkey', help='modifiers + key, e.g. `hotkey W cmd c`')
    p.add_argument('wid', type=int); p.add_argument('keys', nargs='+')

    args = ap.parse_args(argv)
    c = args.cmd

    if c == 'list-windows':
        _print_json(list_windows())
    elif c == 'screenshot':
        ext = 'png' if args.png else 'jpg'
        path = args.out or f'/tmp/win-{args.wid}.{ext}'
        screenshot(args.wid, path=path,
                   fmt='png' if args.png else 'jpeg',
                   jpeg_quality=args.quality)
        print(path)
    elif c == 'click':
        _print_json(click(args.wid, args.x, args.y, coord=args.coord))
    elif c == 'right-click':
        _print_json(right_click(args.wid, args.x, args.y, coord=args.coord))
    elif c == 'double-click':
        _print_json(double_click(args.wid, args.x, args.y, coord=args.coord))
    elif c == 'drag':
        _print_json(drag(args.wid, args.x1, args.y1, args.x2, args.y2,
                         coord=args.coord, steps=args.steps, duration=args.duration))
    elif c == 'scroll':
        r = scroll(args.wid, args.x, args.y, args.dx, args.dy, coord=args.coord)
        _print_json({'via': r})
    elif c == 'type':
        at = tuple(args.at) if args.at else None
        r = type_text(args.wid, args.text, at=at, coord=args.coord)
        _print_json({'via': r})
    elif c == 'press':
        _print_json(press_key(args.wid, args.key, modifiers=args.mod))
    elif c == 'hotkey':
        _print_json(hotkey(args.wid, *args.keys))


if __name__ == '__main__':
    try:
        _main()
    except Exception as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)
