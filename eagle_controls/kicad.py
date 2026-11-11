"""The KiCad process this helper serves, seen from outside.

KiCad doesn't tell other processes what it's doing, so this works it out from
its windows (which canvas is under the mouse), its cursor (whether a tool is
busy, whether the router is active) and its settings files (hotkeys, scroll
layout). It also presses KiCad's hotkeys for our actions and picks layers in
its layer drop-down.
"""

import collections
import ctypes
import time
import zlib

import settings
from logfile import DEBUG, log
from winapi import (BITMAP, CB_GETCOUNT, CB_GETCURSEL, CB_GETITEMDATA, CB_GETLBTEXT, CB_GETLBTEXTLEN, CB_SETCURSEL,
                    CBN_SELENDOK, CURSORINFO, GA_ROOT, GW_OWNER, GWL_STYLE, ICONINFO, VK_CONTROL, VK_MENU, VK_SHIFT,
                    VK_ESCAPE, WM_COMMAND, WM_NULL, class_name, contains, cursor_pos, find_windows,
                    focused_window, gdi32, has_caption, key_down, key_input, send_input_later,
                    send_message_timeout, user32, window_pid, window_rect, window_text)

PID = 0  # set by init()
hotkeys = settings.Watched(settings.kicad_hotkeys)                # {action: key}, see settings.KICAD_ACTIONS
hold_hotkeys = settings.Watched(settings.hold_hotkeys)            # {editor kind: [key]}, see settings.HOLD_ACTIONS
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
    hotkeys.path = hold_hotkeys.path = settings.hotkeys_file(common_json)
    refresh()


def refresh():
    """Re-read KiCad's settings files if they changed. Reads the disk: not from the hook."""
    if hotkeys.refresh():
        log("KiCad hotkeys: %s" % hotkeys.value)
    if hold_hotkeys.refresh():
        log("keys that pick a part up: %s" % hold_hotkeys.value)
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


def focused_canvas():
    """The kind of KiCad canvas (see _canvas_kind) that has the keyboard focus, or None.

    KiCad gives the focus to the panel the canvas fills rather than to the
    canvas itself, so either counts.
    """
    focus = focused_window()
    if not focus or not is_kicad(focus):
        return None
    canvas = focus if class_name(focus) == CANVAS_CLASS else find_windows(
        lambda h: class_name(h) == CANVAS_CLASS and user32.GetParent(h) == focus, parent=focus, first=True)
    return _canvas_kind(canvas) if canvas else None


def board_frame(pt):
    """The PCB or footprint editor window under pt, else the topmost one, or None."""
    frames = [f for f in windows() if editor(f) == "board"]
    return next((f for f in frames if contains(window_rect(f), pt)), frames[0] if frames else None)


# --- the layer drop-down -------------------------------------------------------
# The board editors' toolbar has a drop-down of the board's layers, top to
# bottom, with the active one selected. Each entry keeps KiCad's layer number
# (PCB_LAYER_ID) as its item data, which renamed layers don't change.

LayerBox = collections.namedtuple("LayerBox", "hwnd layers names active")


def layer_box(frame):
    """The layer drop-down in that editor window, or None.

    layers: KiCad's layer numbers, in the list's order; names: the layers'
    names as listed; active: the index of the active layer, or None.
    """
    for combo in find_windows(lambda h: class_name(h) == "ComboBox", parent=frame) if frame else ():
        count = send_message_timeout(combo, CB_GETCOUNT, 0, 0)
        if not count or not 2 <= count <= 200:
            continue
        layers = [send_message_timeout(combo, CB_GETITEMDATA, i, 0) for i in range(count)]
        # The other drop-downs (grid, zoom, track width...) have no item data, or pointers.
        if None in layers or len(set(layers)) != count or 0 not in layers or not all(0 <= n < 256 for n in layers):
            continue
        names = []
        for i in range(count):
            length = send_message_timeout(combo, CB_GETLBTEXTLEN, i, 0)
            buf = ctypes.create_unicode_buffer(max(length or 0, 0) + 2)
            send_message_timeout(combo, CB_GETLBTEXT, i, ctypes.addressof(buf))
            names.append(buf.value.split(" (")[0])  # "F.Cu (PgUp)" -> "F.Cu"
        active = send_message_timeout(combo, CB_GETCURSEL, 0, 0)
        return LayerBox(combo, layers, names, active if active is not None and 0 <= active < count else None)
    return None


def select_layer(box, index):
    """Pick that entry of the layer drop-down, as the user would: KiCad makes it the active layer."""
    if send_message_timeout(box.hwnd, CB_SETCURSEL, index, 0) != index:
        return False
    notify = CBN_SELENDOK << 16 | user32.GetDlgCtrlID(box.hwnd) & 0xFFFF
    return send_message_timeout(user32.GetParent(box.hwnd), WM_COMMAND, notify, box.hwnd) is not None


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


# --- a part in hand ------------------------------------------------------------
# A part is "in hand" while it moves with the mouse: from when it's picked up
# until a left click puts it down or Esc drops it. KiCad's cursor can't tell:
# its move cursor also shows over a part that's only selected, and while
# dragging with G or placing a new footprint it shows the arrow or the pencil.
# So we watch what picks a part up instead. A left-button drag is the mouse
# hook's business (daemon.Mouse.held); the rest is here.

# KiCad's part choosers: (the start of the title, which goes on with
# " (15602 items loaded)"; the editor window that owns it; that editor's kind).
CHOOSERS = (("Footprint Chooser", "PCB Editor", "board"),
            ("Choose Symbol", "Schematic Editor", "schematic"))


def _chooser_kind(hwnd):
    """The editor kind a KiCad part chooser places a part in, or None if hwnd isn't one."""
    title = window_text(hwnd)
    for prefix, owner, kind in CHOOSERS:
        if title.startswith(prefix) and owner in window_text(user32.GetWindow(hwnd, GW_OWNER)):
            return kind
    return None


def _pressed(vk, keys):
    """Whether the key vk going down, with the modifiers held now, is one of keys (KiCad hotkey texts)."""
    held = {m for m in (VK_SHIFT, VK_CONTROL, VK_MENU) if key_down(m)}
    for text in keys:
        if text not in _combos:
            _combos[text] = _combo(text)
        combo = _combos[text]
        if combo and combo[1] == vk and set(combo[0]) == held:
            return True
    return False


class Holding:
    """Whether a part is in hand, picked up by one of KiCad's keys (settings.HOLD_ACTIONS:
    move, drag, duplicate, paste...) or new from a part chooser.

    A chooser closing, unless it was cancelled, puts the new part in hand.
    The keys only count on a canvas that has the keyboard focus, not while
    typing in a text field.
    """

    def __init__(self):
        self.kind = None         # the kind of editor ("board", "schematic") with a part in hand, or None
        self.chooser = None      # the open part chooser
        self.chooser_kind = None
        self.cancelled = False   # Esc or Cancel while it was open

    def _pick_up(self, kind, how):
        self.kind = kind
        if DEBUG:
            log("part in hand (%s, %s)" % (how, kind))

    def drop(self, how):
        if self.kind and DEBUG:
            log("part put down (%s)" % how)
        self.kind = None

    def run(self):
        """Thread: notice a chooser opening (within 0.2 s) and closing (within 20 ms)."""
        while True:
            if self.chooser:
                time.sleep(0.02)
                if not (user32.IsWindow(self.chooser) and user32.IsWindowVisible(self.chooser)):
                    self.chooser = None
                    if not self.cancelled:
                        self._pick_up(self.chooser_kind, "new from the chooser")
                    elif DEBUG:
                        log("part chooser cancelled")
            else:
                time.sleep(0.2)
                for h in windows():
                    kind = _chooser_kind(h)
                    if kind:
                        self.chooser, self.chooser_kind, self.cancelled = h, kind, False
                        self.drop("chooser opened")
                        break

    def key_pressed(self, vk):
        """A key went down (raw input thread)."""
        if vk == VK_ESCAPE:
            self.drop("Esc")
            self.cancelled = bool(self.chooser)
            return
        keys = hold_hotkeys.value or {}
        kind = next((k for k, ks in keys.items() if _pressed(vk, ks)), None)
        if kind:
            canvas = focused_canvas()
            if canvas in keys and _pressed(vk, keys[canvas]):
                self._pick_up(canvas, "key")

    def left_press(self, pt):
        """A left click puts the part down; one on the chooser's Cancel button cancels it."""
        self.drop("left click")
        if self.chooser:
            button = user32.WindowFromPoint(pt)
            if class_name(button) == "Button" and window_text(button).replace("&", "") == "Cancel":
                self.cancelled = True


holding = Holding()


# --- the cursor ----------------------------------------------------------------
# Windows' own cursors (arrow, I-beam, hand...). KiCad 10 shows the plain
# arrow when idle and while dragging with G, and its own cursors over parts
# (one for a selected part, which is also the move cursor), for the selection
# box, while routing or placing a footprint (the pencil), and so on.

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


def move_cursor():
    """Whether KiCad shows its move cursor: an arrow like Windows' own, with the hotspot top-left.

    It shows while a part moves with the mouse, but also over a selected part.
    The selection box's cursors have the hotspot further in, the pencil
    bottom-left.
    """
    cursor = _cursor()
    shape = cursor not in SYSTEM_CURSORS and _shape(cursor)
    if not shape or not shape[2] or not shape[3]:
        return False
    x, y, width, height, _ = shape
    return x < 0.15 * width and y < 0.15 * height


def routing():
    """Whether the router (PCB) or the wire tool (schematic) is active.

    Both show a pencil cursor, the only one whose hotspot (the pencil tip) is
    in the bottom-left corner. Arrows have it top-left (KiCad's move cursor
    too), the selection box's cursors a little further in.

    Placing a footprint shows the pencil too: daemon.Mouse checks for a part
    in hand first.
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
