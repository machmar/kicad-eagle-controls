"""The KiCad process this helper serves, seen from outside.

KiCad doesn't tell other processes what it's doing, so this works it out from
its windows (which canvas is under the mouse), its cursor (whether a tool is
busy, whether the router is active) and its settings files (hotkeys, scroll
layout). It also presses KiCad's hotkeys for our actions.
"""

import ctypes
import time
import zlib

import settings
from logfile import DEBUG, log
from winapi import (BITMAP, CURSORINFO, GA_ROOT, GW_OWNER, GWL_STYLE, ICONINFO, VK_CONTROL, VK_MENU, VK_SHIFT, WM_NULL,
                    class_name, contains, cursor_pos, find_windows, gdi32, has_caption, key_input,
                    send_input_later, send_message_timeout, user32, window_pid, window_rect, window_text)

PID = 0  # set by init()
hotkeys = settings.Watched(settings.kicad_hotkeys)                # {action: key}, see settings.KICAD_ACTIONS
trackpad_layout = settings.Watched(settings.has_trackpad_layout)  # True, False, or None if unknown

BOARD_FRAMES = ("PCB Editor", "Footprint Editor")          # editors with the PCB click settings
SCHEMATIC_FRAMES = ("Schematic Editor", "Symbol Editor")   # editors with the schematic ones
CANVAS_CLASS = "wxGLCanvas"
CATCH_UP_S = 0.2  # wait_until_drawn() waits at most this long


def init(pid, common_json):
    """Serve the KiCad with this process id, whose kicad_common.json is at that path (or None)."""
    global PID
    PID = pid
    trackpad_layout.path = common_json
    hotkeys.path = settings.hotkeys_file(common_json)
    refresh()


def refresh():
    """Re-read KiCad's settings files if they changed. Reads the disk: not from the hook."""
    if hotkeys.refresh():
        log("KiCad hotkeys: %s" % hotkeys.value)
    if trackpad_layout.refresh():
        log("KiCad scroll layout is %s" % ("trackpad" if trackpad_layout.value else "not trackpad, wheel left alone"))


# --- windows -------------------------------------------------------------------

def is_kicad(hwnd):
    return window_pid(hwnd) == PID


def editor(frame):
    """"board" or "schematic" for a KiCad editor's main window (by its title), else None."""
    title = window_text(frame)
    if any(f in title for f in BOARD_FRAMES):
        return "board"
    if any(f in title for f in SCHEMATIC_FRAMES):
        return "schematic"
    return None


def windows():
    """KiCad's visible top-level windows, topmost first."""
    return find_windows(lambda h: is_kicad(h) and user32.IsWindowVisible(h))


def _canvas_kind(canvas):
    """"board", "schematic", or "canvas" (another KiCad canvas, e.g. the 3D viewer)."""
    return editor(user32.GetAncestor(canvas, GA_ROOT)) or "canvas"


def _canvas_in(frame, pt):
    """The visible canvas in frame under pt, or None."""
    return find_windows(lambda h: class_name(h) == CANVAS_CLASS and user32.IsWindowVisible(h)
                        and contains(window_rect(h), pt), parent=frame, first=True)


def canvas_at(pt):
    """The kind of KiCad canvas under pt (see _canvas_kind), or None if it isn't one."""
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd or not is_kicad(hwnd):
        return None
    if class_name(hwnd) == CANVAS_CLASS:
        return _canvas_kind(hwnd)
    # KiCad puts things over the canvas: overlays inside the editor window
    # (e.g. while routing) and little popups such as the hotkey feedback
    # ("Layer: B.Cu") next to the cursor. Look through them to the canvas.
    # Dialogs (captioned), menus and drop-down lists (not wx classes) still
    # count as "not the canvas", as do toolbars and side panels, which lie
    # outside it.
    root = user32.GetAncestor(hwnd, GA_ROOT)
    canvas = _canvas_in(root, pt)  # an overlay inside the editor window
    if not canvas and not has_caption(root) and class_name(root).startswith("wx"):
        # A caption-less popup: look in the topmost KiCad window with a title bar under it.
        frame = find_windows(lambda h: h != root and user32.IsWindowVisible(h) and is_kicad(h)
                             and has_caption(h) and contains(window_rect(h), pt), first=True)
        canvas = frame and _canvas_in(frame, pt)
    if DEBUG:
        log("%r in %r (%r, style %08x) over the canvas area: %s" % (
            class_name(hwnd), class_name(root), window_text(root),
            user32.GetWindowLongW(root, GWL_STYLE) & 0xFFFFFFFF, "yes" if canvas else "no"))
    return _canvas_kind(canvas) if canvas else None


def wait_until_drawn():
    """Wait until KiCad's window under the cursor is back in its message loop.

    When KiCad is slow to redraw (e.g. a throttled CPU) this keeps our pan
    steps from queueing up and panning on after the fingers stop; motion in
    the meantime is merged into the next, bigger step.
    """
    hwnd = user32.WindowFromPoint(cursor_pos())
    if not hwnd or not is_kicad(hwnd):
        return
    # The first ping returns once KiCad has taken the step off its queue;
    # the second only once it has also redrawn and come back for more.
    deadline = time.perf_counter() + CATCH_UP_S
    for _ in range(2):
        left_ms = int((deadline - time.perf_counter()) * 1000)
        if left_ms <= 0 or send_message_timeout(hwnd, WM_NULL, 0, 0, left_ms) is None:
            break


# --- placing a footprint -------------------------------------------------------

FOOTPRINT_CHOOSER = "Footprint Chooser"  # its title goes on: " (15602 items loaded)"


class Placement:
    """Whether a new footprint is on the cursor, waiting to be placed.

    While placing a footprint KiCad shows the plain arrow, so the cursor can't
    tell (moving with M or G, and placing a symbol, show KiCad's own cursors).
    Instead: when KiCad's footprint chooser closes, unless it was cancelled,
    the footprint is in hand until a left click places it or Esc drops it.
    """

    def __init__(self):
        self.in_hand = False
        self.chooser = None      # the open footprint chooser
        self.cancelled = False   # Esc or Cancel while it was open

    def run(self):
        """Thread: notice the chooser opening (within 0.2 s) and closing (within 20 ms)."""
        while True:
            if self.chooser:
                time.sleep(0.02)
                if not (user32.IsWindow(self.chooser) and user32.IsWindowVisible(self.chooser)):
                    self.chooser, self.in_hand = None, not self.cancelled
                    if DEBUG:
                        log("footprint chooser closed: %s" % ("cancelled" if self.cancelled else "footprint in hand"))
            else:
                time.sleep(0.2)
                self.chooser = find_windows(lambda h: is_kicad(h) and user32.IsWindowVisible(h)
                                            and window_text(h).startswith(FOOTPRINT_CHOOSER)
                                            and "PCB Editor" in window_text(user32.GetWindow(h, GW_OWNER)),
                                            first=True)
                if self.chooser:  # opened by the PCB editor to place a footprint
                    self.in_hand = self.cancelled = False

    def left_press(self, pt):
        """A left click places the footprint; one on the chooser's Cancel button cancels it."""
        self.in_hand = False
        if self.chooser:
            button = user32.WindowFromPoint(pt)
            if class_name(button) == "Button" and window_text(button).replace("&", "") == "Cancel":
                self.cancelled = True

    def escape(self):
        self.in_hand = False
        self.cancelled = bool(self.chooser)


placement = Placement()


# --- the cursor ----------------------------------------------------------------
# Windows' own cursors (arrow, I-beam, hand...). KiCad shows the plain arrow
# while nothing is being moved, even with a part selected, and its own
# cursors while a tool is busy: moving (M, or a left-button drag), dragging
# (G), placing a new part, routing.

SYSTEM_CURSORS = {user32.LoadCursorW(None, ctypes.c_void_p(i))
                  for i in (32512, 32513, 32514, 32515, 32516, 32640, 32641, 32642, 32643, 32644,
                            32645, 32646, 32648, 32649, 32650, 32651, 32671, 32672)} - {None}
_shapes = {}  # cursor handle -> (hotspot x, hotspot y, width, height, image checksum if DEBUG)


def _cursor():
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    return ci.hCursor if user32.GetCursorInfo(ctypes.byref(ci)) else None


def _shape(cursor):
    if cursor and cursor not in _shapes:
        ii = ICONINFO()
        if not user32.GetIconInfo(cursor, ctypes.byref(ii)):
            return None
        bm = BITMAP()
        gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(bm), ctypes.byref(bm))
        height = bm.bmHeight if ii.hbmColor else bm.bmHeight // 2  # monochrome: AND and XOR masks stacked
        checksum = None
        if DEBUG:  # tells KiCad's cursors apart in the log
            bits = ctypes.create_string_buffer(bm.bmWidthBytes * bm.bmHeight)
            gdi32.GetBitmapBits(ii.hbmMask, len(bits), bits)
            checksum = "%08x" % zlib.crc32(bits.raw)
        for hbm in (ii.hbmMask, ii.hbmColor):
            if hbm:
                gdi32.DeleteObject(hbm)
        _shapes[cursor] = (ii.xHotspot, ii.yHotspot, bm.bmWidth, height, checksum)
    return _shapes.get(cursor)


def tool_busy():
    """Whether KiCad shows one of its own tool cursors (moving, dragging, placing...)."""
    cursor = _cursor()
    return bool(cursor) and cursor not in SYSTEM_CURSORS


def routing():
    """Whether the router (PCB) or the wire tool (schematic) is active.

    Both show a pencil cursor, the only one whose hotspot (the pencil tip) is
    in the bottom-left corner. Arrows have it top-left, the move and placement
    cursors in the middle.
    """
    shape = _shape(_cursor())
    if not shape or not shape[2] or not shape[3]:
        return False
    x, y, width, height, _ = shape
    return x < 0.3 * width and y > 0.6 * height


def describe_cursor():
    cursor = _cursor()
    return "cursor %s shape %s" % (cursor, _shape(cursor))


# --- hotkeys -------------------------------------------------------------------

_combos = {}  # hotkey text -> ([modifier vks], vk), or None if we can't press it


def _combo(text):
    split = settings.split_hotkey(text)
    if not split:
        return None
    mods, key = split
    vks = [settings.MODIFIER_KEYS[m] for m in mods]
    if key.lower() in settings.NAMED_KEYS:
        return vks, settings.NAMED_KEYS[key.lower()]
    scan = user32.VkKeyScanW(ord(key.lower())) & 0xFFFF  # the key and shift state that type it
    if scan == 0xFFFF:
        return None
    for bit, vk in ((1, VK_SHIFT), (2, VK_CONTROL), (4, VK_MENU)):
        if scan >> 8 & bit and vk not in vks:
            vks.append(vk)
    return vks, scan & 0xFF


def press(action, trigger):
    """Press KiCad's hotkey for action (a settings.KICAD_ACTIONS key); trigger is for the log."""
    text = (hotkeys.value or {}).get(action, "")
    if DEBUG:
        log("%s -> %s (%s)" % (trigger, action, text))
    if not text:
        log("%s has no hotkey in KiCad (Preferences > Hotkeys)" % action)
        return
    if text not in _combos:
        _combos[text] = _combo(text)
        if not _combos[text]:
            log("can't use %r as the %s key" % (text, action))
    if _combos[text]:
        mods, vk = _combos[text]
        send_input_later([key_input(m) for m in mods] + [key_input(vk), key_input(vk, up=True)]
                         + [key_input(m, up=True) for m in reversed(mods)])
