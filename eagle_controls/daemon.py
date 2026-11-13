"""Eagle-style mouse and touchpad controls for KiCad (Windows): the helper process.

The plugin (__init__.py) starts this next to KiCad, with KiCad's own Python:

    pythonw.exe -I daemon.py <kicad pid> [path to kicad_common.json]

It watches the mouse with a low-level hook and only touches input aimed at
that KiCad process. What clicks do is set in the settings window (dialog.py);
by default:

  * right click while holding a part        -> rotate; while routing: switch the bend direction
    (also a pen's side button pressed with the tip down, which Windows doesn't make a right click of)
  * middle click while holding a part       -> flip (PCB) or mirror (schematic)
  * middle click otherwise                  -> the other side's layer (F.Silkscreen <-> B.Silkscreen...),
                                               next copper layer on copper; while routing: via layer chooser
  * mouse wheel                             -> zoom
  * touchpad two-finger swipe / pinch       -> pan in any direction / zoom
  * tap with the pen's eraser end           -> delete what's there (off by default; a drag does nothing)

While panning or zooming by dragging with the middle or right button, KiCad
wraps the cursor round to the canvas' other side when it reaches an edge.
That only works for a mouse: a pen or touchscreen puts the cursor back where
it points, and the view jumps. For those the cursor is kept on the canvas
instead, so it stops at the edge.

The wheel part relies on KiCad's "trackpad" scroll layout (plain scroll pans,
Ctrl+scroll zooms). With it a real mouse wheel would pan too, so wheel events
from a mouse are re-sent with Ctrl held to make them zoom.

The pieces, each on its own thread:
  Hook (here)          the mouse hook; Mouse decides what each message does
  raw_input_thread     raw input: touchpad fingers for pan.py, pen buttons and ends for pen.py, which wheel is a
                       mouse's, keys for Holding
  Pan.run (pan.py)     sends the touchpad pan to KiCad
  LayerChooser.run     the via layer chooser (via_chooser.py)
  Switcher.run         switches to the other side's layer (layers.py)
  Holding.run          whether a new part from a chooser is on the cursor (kicad.py)
  EraserTap.run        KiCad's selection menu after a tap with the pen's eraser end (eraser.py)
  refresher            re-reads the settings files, and reinstalls the hook if Windows dropped it
  input_sender, logfile.writer
                       inject input and write the log, which the hook itself must never wait on
The main thread waits for KiCad to exit.
"""

import collections
import ctypes
import faulthandler
import os
import sys
import threading
import time
from ctypes import wintypes as w

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script's folder off sys.path
import kicad  # noqa: E402
import logfile  # noqa: E402
import settings  # noqa: E402
from eraser import EraserTap  # noqa: E402
from layers import Switcher  # noqa: E402
from logfile import DEBUG, log  # noqa: E402
from pan import Pan  # noqa: E402
from pen import Pen  # noqa: E402
from via_chooser import LayerChooser, OPEN, PICK, STEP  # noqa: E402
from winapi import (  # noqa: E402
    ABOVE_NORMAL_PRIORITY_CLASS, ERROR_ALREADY_EXISTS, HOOKPROC, HWND_MESSAGE, INFINITE, INJECTED_TAG, LLMHF_INJECTED,
    MI_WP_SIGNATURE, MI_WP_SIGNATURE_MASK, MI_WP_TOUCH, MOUSE_MOVE_ABSOLUTE, MOUSEEVENTF_RIGHTDOWN, MSLLHOOKSTRUCT, RAWINPUTDEVICE, RAWINPUTHEADER, RI_MOUSE_WHEEL, RID_INPUT, RIDEV_INPUTSINK,
    RI_KEY_BREAK, RIM_TYPEHID, RIM_TYPEKEYBOARD, RIM_TYPEMOUSE, SM_CXDOUBLECLK, SM_CYDOUBLECLK, SYNCHRONIZE, THREAD_PRIORITY_TIME_CRITICAL, VK_CONTROL,
    VK_LBUTTON, WH_MOUSE_LL, WM_APP, WM_INPUT, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP,
    WM_MOUSEHWHEEL, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_RBUTTONDOWN, WM_RBUTTONUP, WNDPROC, create_window, input_sender,
    class_name, cursor_pos, kernel32, key_down, key_input, message_loop, modifiers_held, mouse_input,
    move_input, send_input_later, user32, window_rect,
    wheel_input, window_pid)

# --- timings ---------------------------------------------------------------------

CLICK_SLOP = 10            # a click may move this many (96-dpi) pixels; scaled to the display in main()...
DRAG_MIN_S = 0.15          # ...and a press only becomes a drag once held this long
DRAG_SLOP = 3              # KiCad drags a part once the left button moves it this many (96-dpi) pixels
CLICK_MAX_S = 0.5          # a middle click must be released within this time
EDGE_INSET = 2             # an absolute pointer is held this many pixels inside the canvas while panning
PEN_TWIN_S = 0.3           # a right click and a pen side button this close together are the same press
WHEEL_STICKY_S = 0.35      # a scroll gesture keeps its mouse/touchpad verdict this long
HOOK_SILENT_S = 0.5        # mouse moved but the hook wasn't called for this long: Windows dropped it
REFRESH_S = 0.25           # how often refresher() looks at the files and the hook
EXIT_DUPLICATE = 3         # exit code: another helper already serves this KiCad

dpi_scale = 1.0            # physical pixels per 96-dpi pixel, set in main()

# --- telling a mouse wheel from a touchpad ------------------------------------
# A precision touchpad's scrolling is synthesized by Windows: it shows up as
# wheel messages but not as raw wheel input from a mouse device, and usually
# in steps smaller than one notch (120). A real mouse produces raw wheel input.


class WheelSource:
    def __init__(self):
        self.last_raw_wheel = 0.0    # when a mouse last sent raw wheel input (raw input thread)
        self.verdict, self.verdict_t = None, 0.0

    def classify(self, delta, injected):
        """"mouse" or "touchpad" for a vertical wheel message."""
        now = time.perf_counter()
        if injected:
            verdict = "touchpad"     # touchpad drivers inject their scrolling
        elif now - self.last_raw_wheel < WHEEL_STICKY_S:
            verdict = "mouse"
        elif self.verdict and now - self.verdict_t < WHEEL_STICKY_S:
            verdict = self.verdict   # keep the verdict for the rest of a gesture
        else:
            verdict = "mouse" if delta % 120 == 0 else "touchpad"
        self.verdict, self.verdict_t = verdict, now
        return verdict


# --- what the mouse does ------------------------------------------------------

Press = collections.namedtuple("Press", "x y t action")


def is_drag(press, pt):
    """Whether a press has turned into a drag now that the mouse is at pt."""
    slop = CLICK_SLOP * dpi_scale
    moved = abs(pt.x - press.x) > slop or abs(pt.y - press.y) > slop
    return moved and time.perf_counter() - press.t >= DRAG_MIN_S


def second_click(last, now, pt):
    """Whether a press at pt is the second of a double-click after the last one, (time, x, y) or None."""
    # Windows' double-click metrics are in 96-dpi pixels, the hook's in physical ones.
    return (last is not None and now - last[0] <= user32.GetDoubleClickTime() / 1000
            and abs(pt.x - last[1]) <= user32.GetSystemMetrics(SM_CXDOUBLECLK) * dpi_scale
            and abs(pt.y - last[2]) <= user32.GetSystemMetrics(SM_CYDOUBLECLK) * dpi_scale)


class Mouse:
    """Decides what the hook does with each mouse message. handle() returns True to hide it from KiCad."""

    def __init__(self):
        self.left_down = False   # tracked here: GetAsyncKeyState can miss it from another process
        self.left_from = None    # where the left button went down
        self.left_dragged = False  # and the mouse has since moved far enough for KiCad to drag
        self.erase_from = None   # (x, y) where the pen's eraser end touched, hidden from KiCad
        self.right = None        # Press held back from KiCad: its action runs on release, or KiCad pans if it moves
        self.middle = None       # Press whose action runs on release, unless it moves (a pan)
        self.last_middle = None  # (time, x, y) of the previous middle press, to spot double-clicks
        self.last_left = None    # the same for the left button
        # When the right button / the pen's side button last acted while dragging. Some pen
        # drivers make a right click of the side button themselves: then both arrive, once.
        self.right_drag_t = self.pen_drag_t = -1.0
        self.hidden = set()      # button-up messages to hide because their button-down was hidden
        # A middle or right drag that KiCad may pan or zoom by: the canvas it started on
        # (left, top, right, bottom, edges included), its button-up message, and
        # where we last held the cursor at the edge.
        self.pan_area = self.pan_end = self.edge_point = None
        self.absolute = False    # the mouse device moving the cursor reports positions, e.g. a tablet (raw input)
        self.wheel = WheelSource()
        self.handlers = {WM_LBUTTONDOWN: self.left_press, WM_LBUTTONUP: self.left_release,
                         WM_RBUTTONDOWN: self.right_press, WM_RBUTTONUP: self.right_release,
                         WM_MBUTTONDOWN: self.middle_press, WM_MBUTTONUP: self.middle_release,
                         WM_MOUSEMOVE: self.move, WM_MOUSEWHEEL: self.scroll, WM_MOUSEHWHEEL: self.scroll}

    def handle(self, msg, info):
        if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            self.hidden.discard(msg + 1)  # a new press: whatever became of the last release, it's over
        if msg != WM_MOUSEMOVE:
            eraser_tap.button(msg, info.pt)
        if msg == self.pan_end:
            self.pan_area = self.pan_end = None
        # While our highlight is over KiCad's via layer chooser, a left click
        # picks the highlighted layer and a middle click moves the highlight.
        if chooser.shown and msg in (WM_LBUTTONDOWN, WM_MBUTTONDOWN):
            chooser.request(PICK if msg == WM_LBUTTONDOWN else STEP)
            return self._hide_press(msg)
        handler = self.handlers.get(msg)
        hide = handler(msg, info) if handler else False
        if msg in (WM_MBUTTONDOWN, WM_RBUTTONDOWN) and msg + 1 not in self.hidden:
            self._pan_may_start(msg, info.pt)  # KiCad gets the press, now or once it moves
        return hide

    def _pan_may_start(self, msg, pt):
        canvas = kicad.canvas_window_at(pt)
        r = canvas and window_rect(canvas)
        if r:
            self.pan_area = (r.left + EDGE_INSET, r.top + EDGE_INSET, r.right - 1 - EDGE_INSET, r.bottom - 1 - EDGE_INSET)
            self.pan_end, self.edge_point = msg + 1, None

    def _absolute_pointer(self, info):
        """Whether this mouse move comes from something that points (pen, touch, tablet) rather than a mouse."""
        return (info.dwExtraInfo & MI_WP_SIGNATURE_MASK) == MI_WP_SIGNATURE or self.absolute

    def _keep_on_canvas(self, pt):
        """A move during a pan: hold the cursor at the canvas' edge rather than let it off,
        where KiCad would wrap it round. Returns True to hide the move from KiCad."""
        left, top, right, bottom = self.pan_area
        edge = (min(max(pt.x, left), right), min(max(pt.y, top), bottom))
        if edge == (pt.x, pt.y):
            self.edge_point = None
            return False
        if edge != self.edge_point:
            self.edge_point = edge
            send_input_later([move_input(*edge)])
        return True

    def _hide_press(self, msg):
        self.hidden.add(msg + 1)  # the matching button-up
        return True

    def _hide_release(self, msg):
        if msg in self.hidden:
            self.hidden.discard(msg)
            return True
        return False

    def dragging(self):
        return self.left_down or key_down(VK_LBUTTON)

    def held(self, kind):
        """Whether a part is in hand on a canvas of that kind: if the mouse moves, the part moves with it.

        That's while dragging one with the left button (KiCad shows its move
        cursor then; the selection box has its own), and from when a key picks
        one up (M, G, Ctrl+D, Ctrl+V...; while the move cursor shows) or a
        chooser hands over a new one, until a left click puts it down or Esc
        (kicad.Holding). A part that's
        only selected isn't held, even with the left button down on it.
        """
        if self.dragging() and self.left_dragged and kicad.move_cursor():
            return True
        return kicad.holding.holds(kind)

    def click_action(self, button, kind):
        """The action (a settings.KICAD_ACTIONS key) for a "right_click" or "middle_click"
        on a canvas of that kind just now, or None to leave the click to KiCad."""
        if kind not in ("board", "schematic"):
            return None
        prefs = settings.prefs.value
        prefix = "sch_" if kind == "schematic" else ""  # the schematic has its own settings
        held = self.held(kind)
        if held:  # before routing: placing a footprint shows the pencil too
            situation = "_drag"
        elif kicad.routing():  # the router's or the wire tool's pencil cursor
            situation = "_route"
        else:
            situation = ""
        action = prefix + prefs[prefix + button + situation]
        if action not in settings.KICAD_ACTIONS:
            return None
        if action in settings.PART_ACTIONS and prefs["parts_only_while_moving"] and not held:
            return None  # a part is merely selected: leave the click to KiCad
        return action

    def _canvas(self, button, pt):
        kind = kicad.canvas_at(pt)
        if DEBUG:
            under = user32.WindowFromPoint(pt)
            log("%s click on %s (%r, pid %d): left button=%s dragged=%s, part held by key/chooser in %s, %s" % (
                button, kind, class_name(under), window_pid(under), self.dragging(), self.left_dragged,
                kicad.holding.kind, kicad.describe_cursor()))
        return kind

    def _eraser_press(self, info):
        """Whether a left press is the pen's eraser end touching a canvas, to delete with."""
        if not settings.prefs.value["pen_eraser_deletes"] or not pen.eraser:
            return False
        extra = info.dwExtraInfo
        if (extra & MI_WP_SIGNATURE_MASK) != MI_WP_SIGNATURE or extra & MI_WP_TOUCH:
            return False  # a mouse or a finger: pen.eraser is only about the pen
        kind = kicad.canvas_at(info.pt)
        return kind in ("board", "schematic") and not modifiers_held() and not self.held(kind)

    def left_press(self, msg, info):
        """A press of the pen's eraser end is hidden from KiCad, so dragging it does nothing;
        a tap is acted on at release."""
        self.erase_from = None
        if self._eraser_press(info):
            self.erase_from = (info.pt.x, info.pt.y)
            return self._hide_press(msg)
        if settings.prefs.value["pen_eraser_deletes"]:  # whether this starts or ends a track or a wire
            now = time.perf_counter()
            second = second_click(self.last_left, now, info.pt)
            self.last_left = None if second else (now, info.pt.x, info.pt.y)
            kind = kicad.canvas_at(info.pt)
            on_canvas = kind in ("board", "schematic")
            eraser_tap.left_press(on_canvas, on_canvas and kicad.routing() and not self.held(kind), second)
        self.left_down = True
        self.left_from, self.left_dragged = (info.pt.x, info.pt.y), False
        kicad.holding.left_press(info.pt)
        return False

    def left_release(self, msg, info):
        if self.erase_from:
            (x, y), self.erase_from = self.erase_from, None
            slop = CLICK_SLOP * dpi_scale
            if abs(info.pt.x - x) <= slop and abs(info.pt.y - y) <= slop:  # a tap, not a drag
                eraser_tap.tap()
        if self._hide_release(msg):
            return True
        self.left_down = False
        return False

    def move(self, msg, info):
        if self.left_down and not self.left_dragged:
            slop = DRAG_SLOP * dpi_scale
            x, y = self.left_from
            self.left_dragged = abs(info.pt.x - x) > slop or abs(info.pt.y - y) > slop
        if self.middle and is_drag(self.middle, info.pt):
            self.middle = None  # a pan drag: leave it to KiCad
        if self.right and is_drag(self.right, info.pt):
            self.right = None   # a right drag: give KiCad the press after all, so it pans
            send_input_later([mouse_input(MOUSEEVENTF_RIGHTDOWN)])
        if self.pan_area and self._absolute_pointer(info):
            return self._keep_on_canvas(info.pt)
        return False

    def right_press(self, msg, info):
        action = self.click_action("right_click", self._canvas("right", info.pt))
        if not action or modifiers_held():
            return False  # e.g. Shift+right click: KiCad's menu
        if self.dragging():
            now = time.perf_counter()
            if now - self.pen_drag_t > PEN_TWIN_S:  # else the pen's side button did it already
                self.right_drag_t = now
                perform(action, "right click while dragging", info.pt)
            return self._hide_press(msg)
        self.right = Press(info.pt.x, info.pt.y, time.perf_counter(), action)  # decided on release or movement
        return True

    def pen_side_button(self):
        """A pen's side button went down with its tip on the screen (raw input thread).

        The tip is the left button, so this is a right click while the left
        button is held, except that Windows makes no mouse input of it. (Some
        pen drivers do: then the right click acts and this doesn't, or the
        other way round, whichever comes first.)
        """
        pt = cursor_pos()
        now = time.perf_counter()
        if not self.dragging() or now - self.right_drag_t <= PEN_TWIN_S:
            return
        action = self.click_action("right_click", self._canvas("pen side button", pt))
        if action:
            self.pen_drag_t = now
            perform(action, "pen side button while dragging", pt)

    def right_release(self, msg, info):
        if self._hide_release(msg):
            return True
        if self.right:
            perform(self.right.action, "right click", info.pt)
            self.right = None
            return True
        return False

    def middle_press(self, msg, info):
        kind = self._canvas("middle", info.pt)
        action = self.click_action("middle_click", kind)
        now = time.perf_counter()
        self.middle = Press(info.pt.x, info.pt.y, now, action) if action else None
        # The second press of a double-click: KiCad would zoom to fit. Hide it
        # from KiCad if asked to; our own click action still runs on release.
        second = second_click(self.last_middle, now, info.pt)
        self.last_middle = None if second else (now, info.pt.x, info.pt.y)
        if second and kind and settings.prefs.value["block_middle_double_click"]:
            return self._hide_press(msg)
        return False

    def middle_release(self, msg, info):
        if self.middle and time.perf_counter() - self.middle.t < CLICK_MAX_S:
            perform(self.middle.action, "middle click", info.pt)
        self.middle = None
        return self._hide_release(msg)

    def scroll(self, msg, info):
        if kicad.canvas_at(info.pt) is None or not kicad.trackpad_layout.value:
            return False
        prefs = settings.prefs.value
        delta = ctypes.c_short(info.mouseData >> 16).value
        horizontal = msg == WM_MOUSEHWHEEL
        fingers = pan.fingers
        if fingers >= 2 or time.perf_counter() < pan.swallow_until:  # a touchpad swipe
            return prefs["touchpad_free_pan"] and pan.windows_wheel(horizontal, delta, fingers)
        if horizontal or not prefs["wheel_zoom"]:
            return False
        source = self.wheel.classify(delta, bool(info.flags & LLMHF_INJECTED))
        if DEBUG:
            log("wheel delta=%d flags=%x -> %s" % (delta, info.flags, source))
        if source == "mouse" and not modifiers_held():
            send_input_later([key_input(VK_CONTROL), wheel_input(delta), key_input(VK_CONTROL, up=True)])  # zoom
            return True
        return False


def perform(action, trigger, pt):
    """Carry out a click at pt: run the via layer chooser, switch layers, or press the action's hotkey."""
    if action == "via_chooser":
        chooser.request(OPEN)
    elif action == "other_layer":
        switcher.request(pt, trigger)
    else:
        kicad.press(action, trigger)


# --- the hook ------------------------------------------------------------------

WM_REHOOK = WM_APP + 1


class Hook:
    """The low-level mouse hook, on a thread of its own.

    Windows silently removes a hook that is slow to return, and doesn't call
    hooks while an elevated (admin) window has focus. check() notices the
    mouse moving without the hook being called, and reinstalls it.
    """

    def __init__(self, handle):
        self.handle = handle
        self.proc = HOOKPROC(self._proc)  # keep a reference
        self.thread_id = None
        self.last_call = self.last_mouse = time.perf_counter()  # last_mouse: set by raw input
        self.last_report = 0.0

    def _proc(self, code, wparam, lparam):
        self.last_call = time.perf_counter()
        if code == 0:
            info = MSLLHOOKSTRUCT.from_address(lparam)
            if info.dwExtraInfo != INJECTED_TAG:
                try:
                    if self.handle(wparam, info):
                        return 1
                except Exception as e:
                    log("hook error: %r" % e)
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _install(self):
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self.proc, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            log("SetWindowsHookExW failed: %d" % ctypes.get_last_error())
            logfile.flush()
            os._exit(1)
        self.last_call = time.perf_counter()
        return hook

    def run(self):
        """Thread: install the hook and run the message loop that calls it."""
        # Get scheduled promptly even when KiCad keeps the CPU busy.
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL)
        self.thread_id = kernel32.GetCurrentThreadId()
        hook = self._install()

        def on_message(msg):
            nonlocal hook
            if msg.message == WM_REHOOK:
                user32.UnhookWindowsHookEx(hook)
                hook = self._install()

        message_loop(on_message)

    def check(self):
        silent = self.last_mouse - self.last_call
        if silent > HOOK_SILENT_S and self.thread_id:
            now = time.perf_counter()
            if now - self.last_report > 30:  # reinstalling is harmless; just don't flood the log
                log("mouse hook went silent for %.1f s while the mouse moved; reinstalling" % silent)
                self.last_report = now
            self.last_call = now
            user32.PostThreadMessageW(self.thread_id, WM_REHOOK, 0, 0)


# --- raw input -----------------------------------------------------------------

def on_raw_input(lparam, age_s):
    hdr = ctypes.sizeof(RAWINPUTHEADER)
    size = w.UINT(0)
    user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), hdr)
    if size.value == 0:
        return
    buf = ctypes.create_string_buffer(size.value)
    if user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), hdr) != size.value:
        return
    header = RAWINPUTHEADER.from_buffer(buf)
    if header.dwType == RIM_TYPEMOUSE:
        # RAWMOUSE: usFlags, 2 bytes padding, usButtonFlags, usButtonData, ulRawButtons, lLastX, lLastY,
        # ulExtraInformation. Before the check for injected input: some tablet drivers inject their moves.
        raw = buf.raw[hdr:size.value]
        if len(raw) >= 24 and int.from_bytes(raw[20:24], "little") != INJECTED_TAG:
            if int.from_bytes(raw[0:2], "little") & MOUSE_MOVE_ABSOLUTE:
                mouse.absolute = True
            elif raw[12:20] != bytes(8):  # it moved
                mouse.absolute = False
    if not header.hDevice:
        return  # injected input
    if header.dwType == RIM_TYPEHID:
        if pen.is_pen(header.hDevice):
            pen.feed(header.hDevice, buf.raw[hdr:size.value])
        else:
            pan.feed(header.hDevice, buf.raw[hdr:size.value], age_s)
    elif header.dwType == RIM_TYPEKEYBOARD:
        # RAWKEYBOARD: MakeCode, Flags, Reserved, VKey
        flags = int.from_bytes(buf.raw[hdr + 2:hdr + 4], "little")
        vkey = int.from_bytes(buf.raw[hdr + 6:hdr + 8], "little")
        if not flags & RI_KEY_BREAK:  # a key going down
            kicad.holding.key_pressed(vkey)
            eraser_tap.key_pressed(vkey)
    elif header.dwType == RIM_TYPEMOUSE:
        # RAWMOUSE: usFlags, 2 bytes padding, usButtonFlags, usButtonData
        flags = int.from_bytes(buf.raw[hdr + 4:hdr + 6], "little")
        delta = int.from_bytes(buf.raw[hdr + 6:hdr + 8], "little", signed=True)
        now = time.perf_counter()
        if flags & RI_MOUSE_WHEEL and delta % 120 == 0:
            mouse.wheel.last_raw_wheel = now
        hook.last_mouse = now


def raw_input_thread():
    """Thread: a message-only window that gets raw input from mice, touchpads, pens and keyboards
    (for kicad.Holding), whichever window has focus."""

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            age_s = ((kernel32.GetTickCount() - user32.GetMessageTime()) & 0xFFFFFFFF) / 1000
            try:
                on_raw_input(lparam, age_s)
            except Exception as e:  # never let an exception escape a WNDPROC
                log("raw input error: %r" % e)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    raw_input_thread.wndproc = WNDPROC(wndproc)  # keep a reference
    hwnd = create_window("KiCadEagleControlsRaw", raw_input_thread.wndproc, parent=HWND_MESSAGE)
    devices = (RAWINPUTDEVICE * 5)(RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),  # mice
                                   RAWINPUTDEVICE(0x0D, 0x05, RIDEV_INPUTSINK, hwnd),  # precision touchpads
                                   RAWINPUTDEVICE(0x0D, 0x01, RIDEV_INPUTSINK, hwnd),  # pens (digitizers)
                                   RAWINPUTDEVICE(0x0D, 0x02, RIDEV_INPUTSINK, hwnd),  # pens
                                   RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd))  # keyboards
    if not user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RAWINPUTDEVICE)):
        log("RegisterRawInputDevices failed: %d" % ctypes.get_last_error())
    message_loop()


# --- running -------------------------------------------------------------------

pan = Pan()
chooser = LayerChooser()
switcher = Switcher()
chooser.fallback = switcher.request
mouse = Mouse()
eraser_tap = EraserTap()
pen = Pen(mouse.pen_side_button, log)
hook = Hook(mouse.handle)


def refresher():
    """Thread: re-read the settings files when they change, and keep the hook installed."""
    while True:
        settings.prefs.refresh()
        kicad.refresh()
        hook.check()
        time.sleep(REFRESH_S)


def make_dpi_aware():
    """Work in physical pixels, like the mouse hook does.

    Without this, on a scaled display (125 %, 150 %...) Windows hands this
    process scaled window coordinates while the hook reports physical ones,
    so clicks would be looked up in the wrong place.
    """
    try:
        user32.SetProcessDpiAwarenessContext.restype = w.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # per-monitor v2 (Windows 10 1703+)
            return
    except AttributeError:
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:     # per-monitor (Windows 8.1+)
            return
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()


def main():
    global dpi_scale
    kicad_pid = int(sys.argv[1])
    common_json = sys.argv[2] if len(sys.argv) > 2 else None

    # One helper per KiCad process, even if the plugin is loaded twice.
    kernel32.CreateMutexW(None, False, settings.MUTEX_NAME % kicad_pid)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        sys.exit(EXIT_DUPLICATE)

    # Record crashes: pythonw has no console to print them to.
    crash_log = open(logfile.PATH, "a", encoding="utf-8")
    faulthandler.enable(crash_log)
    threading.excepthook = lambda a: (log("thread %s died: %r" % (a.thread.name, a.exc_value)), logfile.flush())

    kicad_process = kernel32.OpenProcess(SYNCHRONIZE, False, kicad_pid)
    if not kicad_process:
        log("KiCad process %d not found" % kicad_pid)
        logfile.flush()
        return

    make_dpi_aware()
    try:
        dpi_scale = user32.GetDpiForSystem() / 96
    except AttributeError:  # before Windows 10 1607
        pass
    # The hook blocks all mouse input until it returns, so it must get the GIL
    # quickly; the default 5 ms switch interval lets lag pile up.
    sys.setswitchinterval(0.0002)
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
    settings.prefs.refresh()
    kicad.init(kicad_pid, common_json)
    for run in (logfile.writer, input_sender, pan.run, raw_input_thread, hook.run, refresher, chooser.run,
                switcher.run, kicad.holding.run, eraser_tap.run):
        threading.Thread(target=run, name=run.__qualname__, daemon=True).start()
    log("started for KiCad pid %d" % kicad_pid)
    if DEBUG:
        log("settings from %s: %s" % (settings.PATH, {k: v for k, v in settings.prefs.value.items() if "click" in k}))

    kernel32.WaitForSingleObject(w.HANDLE(kicad_process), w.DWORD(INFINITE))  # exit together with KiCad
    log("KiCad exited, stopping")
    logfile.flush()


if __name__ == "__main__":
    main()
