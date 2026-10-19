"""Eagle-style mouse and touchpad controls for KiCad (Windows): the helper process.

The plugin (__init__.py) starts this next to KiCad, with KiCad's own Python:

    pythonw.exe -I daemon.py <kicad pid> [path to kicad_common.json]

It watches the mouse with a low-level hook and only touches input aimed at
that KiCad process. What clicks do is set in the settings window (dialog.py);
by default:

  * right click while moving a part         -> rotate; while routing: switch the bend direction
  * middle click while moving a part        -> flip (PCB) or mirror (schematic)
  * middle click otherwise                  -> next copper layer; while routing: via layer chooser
  * mouse wheel                             -> zoom
  * touchpad two-finger swipe / pinch       -> pan in any direction / zoom

The wheel part relies on KiCad's "trackpad" scroll layout (plain scroll pans,
Ctrl+scroll zooms). With it a real mouse wheel would pan too, so wheel events
from a mouse are re-sent with Ctrl held to make them zoom.

The pieces, each on its own thread:
  Hook (here)          the mouse hook; Mouse decides what each message does
  raw_input_thread     raw input: touchpad fingers for pan.py, and which wheel is a mouse's
  Pan.run (pan.py)     sends the touchpad pan to KiCad
  LayerChooser.run     the via layer chooser (via_chooser.py)
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
from logfile import DEBUG, log  # noqa: E402
from pan import Pan  # noqa: E402
from via_chooser import LayerChooser, OPEN, PICK, STEP  # noqa: E402
from winapi import (  # noqa: E402
    ABOVE_NORMAL_PRIORITY_CLASS, ERROR_ALREADY_EXISTS, HOOKPROC, HWND_MESSAGE, INFINITE, INJECTED_TAG, LLMHF_INJECTED,
    MOUSEEVENTF_RIGHTDOWN, MSLLHOOKSTRUCT, RAWINPUTDEVICE, RAWINPUTHEADER, RI_MOUSE_WHEEL, RID_INPUT, RIDEV_INPUTSINK,
    RIM_TYPEHID, RIM_TYPEMOUSE, SM_CXDOUBLECLK, SM_CYDOUBLECLK, SYNCHRONIZE, THREAD_PRIORITY_TIME_CRITICAL, VK_CONTROL,
    VK_LBUTTON, WH_MOUSE_LL, WM_APP, WM_INPUT, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP,
    WM_MOUSEHWHEEL, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_RBUTTONDOWN, WM_RBUTTONUP, WNDPROC, create_window, input_sender,
    kernel32, key_down, key_input, message_loop, modifiers_held, mouse_input, send_input_later, user32, wheel_input)

# --- timings ---------------------------------------------------------------------

CLICK_SLOP = 10            # a click may move this many (96-dpi) pixels; scaled to the display in main()...
DRAG_MIN_S = 0.15          # ...and a press only becomes a drag once held this long
CLICK_MAX_S = 0.5          # a middle click must be released within this time
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


class Mouse:
    """Decides what the hook does with each mouse message. handle() returns True to hide it from KiCad."""

    def __init__(self):
        self.left_down = False   # tracked here: GetAsyncKeyState can miss it from another process
        self.right = None        # Press held back from KiCad: its action runs on release, or KiCad pans if it moves
        self.middle = None       # Press whose action runs on release, unless it moves (a pan)
        self.last_middle = None  # (time, x, y) of the previous middle press, to spot double-clicks
        self.hidden = set()      # button-up messages to hide because their button-down was hidden
        self.wheel = WheelSource()
        self.handlers = {WM_LBUTTONDOWN: self.left_press, WM_LBUTTONUP: self.left_release,
                         WM_RBUTTONDOWN: self.right_press, WM_RBUTTONUP: self.right_release,
                         WM_MBUTTONDOWN: self.middle_press, WM_MBUTTONUP: self.middle_release,
                         WM_MOUSEMOVE: self.move, WM_MOUSEWHEEL: self.scroll, WM_MOUSEHWHEEL: self.scroll}

    def handle(self, msg, info):
        if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            self.hidden.discard(msg + 1)  # a new press: whatever became of the last release, it's over
        # While our highlight is over KiCad's via layer chooser, a left click
        # picks the highlighted layer and a middle click moves the highlight.
        if chooser.shown and msg in (WM_LBUTTONDOWN, WM_MBUTTONDOWN):
            chooser.request(PICK if msg == WM_LBUTTONDOWN else STEP)
            return self._hide_press(msg)
        handler = self.handlers.get(msg)
        return handler(msg, info) if handler else False

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

    def click_action(self, button, kind):
        """The action (a settings.KICAD_ACTIONS key) for a "right_click" or "middle_click"
        on a canvas of that kind just now, or None to leave the click to KiCad."""
        if kind not in ("board", "schematic"):
            return None
        prefs = settings.prefs.value
        prefix = "sch_" if kind == "schematic" else ""  # the schematic has its own settings
        if self.dragging():
            situation = "_drag"
        elif kicad.routing():  # the router's or the wire tool's pencil cursor
            situation = "_route"
        elif kicad.tool_busy():  # moving with M or G, or placing a new part
            situation = "_drag"
        else:
            situation = ""
        action = prefix + prefs[prefix + button + situation]
        if action not in settings.KICAD_ACTIONS:
            return None
        if (action in settings.PART_ACTIONS and prefs["parts_only_while_moving"]
                and not (self.dragging() or kicad.tool_busy())):
            return None  # a part is merely selected: leave the click to KiCad
        return action

    def _canvas(self, button, pt):
        kind = kicad.canvas_at(pt)
        if DEBUG and kind:
            log("%s click on %s: left held=%s, %s" % (button, kind, self.dragging(), kicad.describe_cursor()))
        return kind

    def left_press(self, msg, info):
        self.left_down = True
        return False

    def left_release(self, msg, info):
        if self._hide_release(msg):
            return True
        self.left_down = False
        return False

    def move(self, msg, info):
        if self.middle and is_drag(self.middle, info.pt):
            self.middle = None  # a pan drag: leave it to KiCad
        if self.right and is_drag(self.right, info.pt):
            self.right = None   # a right drag: give KiCad the press after all, so it pans
            send_input_later([mouse_input(MOUSEEVENTF_RIGHTDOWN)])
        return False

    def right_press(self, msg, info):
        action = self.click_action("right_click", self._canvas("right", info.pt))
        if not action or modifiers_held():
            return False  # e.g. Shift+right click: KiCad's menu
        if self.dragging():
            perform(action, "right click while dragging")
            return self._hide_press(msg)
        self.right = Press(info.pt.x, info.pt.y, time.perf_counter(), action)  # decided on release or movement
        return True

    def right_release(self, msg, info):
        if self._hide_release(msg):
            return True
        if self.right:
            perform(self.right.action, "right click")
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
        # Windows' double-click metrics are in 96-dpi pixels, the hook's in physical ones.
        last = self.last_middle
        second = (last is not None and now - last[0] <= user32.GetDoubleClickTime() / 1000
                  and abs(info.pt.x - last[1]) <= user32.GetSystemMetrics(SM_CXDOUBLECLK) * dpi_scale
                  and abs(info.pt.y - last[2]) <= user32.GetSystemMetrics(SM_CYDOUBLECLK) * dpi_scale)
        self.last_middle = None if second else (now, info.pt.x, info.pt.y)
        if second and kind and settings.prefs.value["block_middle_double_click"]:
            return self._hide_press(msg)
        return False

    def middle_release(self, msg, info):
        if self.middle and time.perf_counter() - self.middle.t < CLICK_MAX_S:
            perform(self.middle.action, "middle click")
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


def perform(action, trigger):
    """Carry out a click action: run the via layer chooser, or press the action's hotkey."""
    if action == "via_chooser":
        chooser.request(OPEN)
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
    if not header.hDevice:
        return  # injected input
    if header.dwType == RIM_TYPEHID:
        pan.feed(header.hDevice, buf.raw[hdr:size.value], age_s)
    elif header.dwType == RIM_TYPEMOUSE:
        # RAWMOUSE: usFlags, 2 bytes padding, usButtonFlags, usButtonData
        flags = int.from_bytes(buf.raw[hdr + 4:hdr + 6], "little")
        delta = int.from_bytes(buf.raw[hdr + 6:hdr + 8], "little", signed=True)
        now = time.perf_counter()
        if flags & RI_MOUSE_WHEEL and delta % 120 == 0:
            mouse.wheel.last_raw_wheel = now
        hook.last_mouse = now


def raw_input_thread():
    """Thread: a message-only window that gets raw input from mice and touchpads, whichever window has focus."""

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
    devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),  # mice
                                   RAWINPUTDEVICE(0x0D, 0x05, RIDEV_INPUTSINK, hwnd))  # precision touchpads
    if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        log("RegisterRawInputDevices failed: %d" % ctypes.get_last_error())
    message_loop()


# --- running -------------------------------------------------------------------

pan = Pan()
chooser = LayerChooser()
mouse = Mouse()
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
    for run in (logfile.writer, input_sender, pan.run, raw_input_thread, hook.run, refresher, chooser.run):
        threading.Thread(target=run, name=run.__qualname__, daemon=True).start()
    log("started for KiCad pid %d" % kicad_pid)
    if DEBUG:
        log("settings from %s: %s" % (settings.PATH, {k: v for k, v in settings.prefs.value.items() if "click" in k}))

    kernel32.WaitForSingleObject(w.HANDLE(kicad_process), w.DWORD(INFINITE))  # exit together with KiCad
    log("KiCad exited, stopping")
    logfile.flush()


if __name__ == "__main__":
    main()
