"""Eagle-style mouse and touchpad controls for KiCad (Windows).

Runs as a small helper process next to KiCad (started by the plugin in
__init__.py) and watches input with a low-level mouse hook:

  * right click while holding the left button (dragging a part) -> rotate (R)
  * middle click without moving the mouse                        -> switch layer (V)
  * mouse wheel                                                  -> zoom
  * touchpad two-finger swipe (any direction) / pinch            -> pan / zoom

The wheel part relies on KiCad's "trackpad" scroll settings (plain scroll
pans, Ctrl+scroll zooms). With those settings a real mouse wheel would pan
too, so wheel events that come from a mouse are re-sent with Ctrl held to make
them zoom. Touchpad swipes are re-done from the raw finger motion so they are
not locked to one axis (see touchpad.py).

Only input aimed at the KiCad process given on the command line is touched.

Usage: pythonw.exe -I daemon.py <kicad pid> [path to kicad_common.json]
"""

import ctypes
import faulthandler
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes as w

# --- settings -------------------------------------------------------------

ROTATE_KEY = ord("R")      # pcbnew.InteractiveEdit.rotateCcw
LAYER_KEY = ord("V")       # pcbnew.Control.layerToggle, and "place via" while routing
CLICK_SLOP_PX = 4          # middle button may move this much and still count as a click
CLICK_MAX_S = 0.5          # ...and must be released within this time
WHEEL_STICKY_S = 0.35      # a scroll gesture keeps its mouse/touchpad verdict this long
EAGLE_FRAMES = ("PCB Editor", "Footprint Editor")  # where the button remaps apply

HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG = os.path.exists(os.path.join(HERE, "debug"))
LOG_PATH = os.path.join(os.environ.get("TEMP", HERE), "kicad_eagle_controls.log")

sys.path.insert(0, HERE)  # -I leaves the script's folder off sys.path
import settings  # noqa: E402
import touchpad  # noqa: E402

# --- Win32 ----------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

WH_MOUSE_LL = 14
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL, WM_INPUT = 0x020A, 0x020E, 0x00FF
LLMHF_INJECTED = 0x01
VK_LBUTTON, VK_SHIFT, VK_CONTROL, VK_MENU = 0x01, 0x10, 0x11, 0x12
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_KEYUP = 0x0002
RID_INPUT, RIM_TYPEMOUSE, RIM_TYPEHID, RIDEV_INPUTSINK = 0x10000003, 0, 2, 0x00000100
RI_MOUSE_WHEEL = 0x0400
GA_ROOT = 2
SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
ERROR_ALREADY_EXISTS = 183
EXIT_DUPLICATE = 3  # another helper already serves this KiCad (checked by __init__.py)

# Tag on everything we inject, so the hook lets our own events through.
MAGIC = 0x4541474C  # "EAGL"


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT),
                ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD), ("hDevice", w.HANDLE), ("wParam", w.WPARAM)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
user32.SetWindowsHookExW.restype = w.HHOOK
user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
user32.CreateWindowExW.restype = w.HWND
user32.GetRawInputData.argtypes = [w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT), w.UINT]
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = w.HWND
user32.GetAncestor.argtypes = [w.HWND, w.UINT]
user32.GetAncestor.restype = w.HWND
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CreateMutexW.restype = w.HANDLE


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s [%d] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))
    except OSError:
        pass


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def window_text(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


# --- sending input --------------------------------------------------------
# Input is injected from a separate thread, never from inside the hook.

outbox = queue.Queue()


def _key(vk, up=False):
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), KEYEVENTF_KEYUP if up else 0, 0, MAGIC)
    return i


def _wheel(delta):
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, MOUSEEVENTF_WHEEL, 0, MAGIC)
    return i


def sender():
    while True:
        events = outbox.get()
        arr = (INPUT * len(events))(*events)
        user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def tap(vk):
    outbox.put([_key(vk), _key(vk, up=True)])


def _hwheel(delta):
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, MOUSEEVENTF_HWHEEL, 0, MAGIC)
    return i


def zoom_wheel(delta):
    outbox.put([_key(VK_CONTROL), _wheel(delta), _key(VK_CONTROL, up=True)])


# --- KiCad's scroll settings ----------------------------------------------
# The wheel remapping assumes KiCad's trackpad layout (plain scroll pans,
# Ctrl+scroll zooms, horizontal scroll pans sideways). With any other layout
# the wheel is left alone; the plugin asks the user to switch.

TRACKPAD_LAYOUT = {"scroll_modifier_zoom": 308, "scroll_modifier_pan_v": 0, "horizontal_pan": True}
COMMON_JSON = None
_layout_ok = False
_layout_checked = 0.0
_layout_mtime = None


def trackpad_layout():
    global _layout_ok, _layout_checked, _layout_mtime
    now = time.perf_counter()
    if now - _layout_checked < 1.0 or not COMMON_JSON:
        return _layout_ok
    _layout_checked = now
    try:
        mtime = os.path.getmtime(COMMON_JSON)
        if mtime != _layout_mtime:
            with open(COMMON_JSON, encoding="utf-8") as f:
                inp = json.load(f).get("input", {})
            _layout_ok = all(inp.get(k) == v for k, v in TRACKPAD_LAYOUT.items())
            _layout_mtime = mtime
            log("KiCad scroll layout is %s" % ("trackpad" if _layout_ok else "not trackpad, wheel left alone"))
    except (OSError, ValueError) as e:
        log("can't read %s: %r" % (COMMON_JSON, e))
    return _layout_ok


# --- touchpad panning -----------------------------------------------------
# Windows turns two-finger swipes into wheel events locked to one axis. Over
# a KiCad canvas we swallow those and pan from the actual finger motion, so
# diagonal swipes work. Whether a gesture is a pan or a pinch is decided from
# the fingers themselves (moving together = pan, spreading = pinch), so the
# pan starts right away and doesn't depend on Windows' axis-locked events;
# a pinch is left to Windows, which sends it as Ctrl+wheel (= zoom in KiCad).
# The wheel-units-per-mm factor, sign included, is learned from the events
# Windows sends, so speed and direction follow the user's touchpad settings.

CALIBRATION = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "kicad_eagle_controls.json")
PAN_START_MM = 1.0         # finger travel before a gesture counts as a pan...
PINCH_RATIO = 0.6          # ...unless the fingers spread/closed by this fraction of it
MOMENTUM_S = 0.3           # keep swallowing Windows' momentum scrolling this long
DEFAULT_GAIN = 25.0        # wheel units per mm until calibrated
PAN_INTERVAL_S = 1 / 60    # send at most one pan update per frame; every injected
                           # event passes through the hook, so flooding it adds lag
ACCEL_REF_MM_S = 30.0      # with acceleration on, finger speed that pans at 1x
ACCEL_RANGE = (0.25, 4.0)  # limits of the acceleration factor


class UserSettings:
    """settings.py values, reloaded when the plugin's settings dialog saves them."""

    def __init__(self):
        self.mtime = None
        self.values = settings.DEFAULTS

    def get(self):
        try:
            mtime = os.path.getmtime(settings.PATH)
        except OSError:
            mtime = None
        if mtime != self.mtime:
            self.mtime, self.values = mtime, settings.load()
        return self.values


user_settings = UserSettings()


def _initial_gain():
    try:
        with open(CALIBRATION, encoding="utf-8") as f:
            return float(json.load(f)["gain"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # No calibration yet: take the direction from Windows' "scrolling
    # direction" setting (0 = the default, "down motion scrolls up").
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad") as k:
            if winreg.QueryValueEx(k, "ScrollDirection")[0] != 0:
                return -DEFAULT_GAIN
    except OSError:
        pass
    return DEFAULT_GAIN


class Pan:
    def __init__(self):
        self.lock = threading.Lock()
        # Fingers moving down by 1 mm = this many units of vertical wheel;
        # horizontal wheel uses the negated value (Windows' sign convention).
        self.gain = _initial_gain()
        self.swallow_until = 0.0
        self.pending = threading.Event()
        self._reset()

    def _reset(self):
        self.state = None            # None (undecided), "pan", "pinch" or "ignore"
        self.travel = self.spread = 0.0
        self.acc_dx = self.acc_dy = self.rem_v = self.rem_h = 0.0
        self.windows_started = False
        self.win_v = self.win_h = 0  # what Windows sent, and the finger travel
        self.dx = self.dy = 0.0      # since then, for calibration
        self.last_t = None
        self.speed = ACCEL_REF_MM_S  # smoothed finger speed, mm/s
        s = user_settings.get()
        self.sensitivity = s["pan_sensitivity"]
        self.acceleration = s["pan_acceleration"]

    def _accel_factor(self, dist):
        now = time.perf_counter()
        if self.last_t is not None:
            dt = min(max(now - self.last_t, 0.004), 0.05)
            self.speed = 0.5 * self.speed + 0.5 * dist / dt
        self.last_t = now
        if self.acceleration <= 0:
            return 1.0
        f = (max(self.speed, 0.1) / ACCEL_REF_MM_S) ** self.acceleration
        return min(max(f, ACCEL_RANGE[0]), ACCEL_RANGE[1])

    def on_motion(self, dx, dy, dspread):
        with self.lock:
            k = self.sensitivity * self._accel_factor((dx * dx + dy * dy) ** 0.5)
            self.acc_dx += dx * k
            self.acc_dy += dy * k
            if self.windows_started:
                self.dx += dx
                self.dy += dy
            if self.state is None:
                self.travel += (dx * dx + dy * dy) ** 0.5
                self.spread += dspread
                if abs(self.spread) > PINCH_RATIO * max(self.travel, PAN_START_MM):
                    self.state = "pinch"
                elif self.travel >= PAN_START_MM:
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    self.state = "pan" if target(pt) and trackpad_layout() else "ignore"
                    if DEBUG:
                        log("touchpad gesture: %s" % self.state)
            if self.state == "pan":
                self._flush()

    def on_fingers_changed(self, n):
        with self.lock:
            self._learn()
            self._reset()

    def windows_wheel(self, horizontal, delta, fingers):
        """Called by the hook for a touchpad wheel event over a canvas. Returns True to swallow it."""
        with self.lock:
            if key_down(VK_CONTROL):         # Windows saw a pinch: let KiCad zoom
                self.state = "pinch"
                return False
            if self.state == "ignore":       # gesture started outside the canvas
                return False
            self.swallow_until = time.perf_counter() + MOMENTUM_S
            if fingers == 2 and self.state != "pinch":
                if self.state is None:
                    self.state = "pan"
                if not self.windows_started:
                    self.windows_started = True
                    self.dx = self.dy = 0.0
                if horizontal:
                    self.win_h += delta
                else:
                    self.win_v += delta
                self._flush()
            return True

    def _flush(self):
        """Hand the accumulated motion to sender()."""
        if self.acc_dx or self.acc_dy:
            self.pending.set()

    def sender(self):
        """Turn accumulated finger motion into wheel events, at most once per PAN_INTERVAL_S."""
        while True:
            self.pending.wait()
            with self.lock:
                self.pending.clear()
                if self.state != "pan":
                    continue
                v = self.gain * self.acc_dy + self.rem_v
                h = -self.gain * self.acc_dx + self.rem_h
                self.acc_dx = self.acc_dy = 0.0
                iv, ih = int(v), int(h)
                self.rem_v, self.rem_h = v - iv, h - ih
            events = ([_wheel(iv)] if iv else []) + ([_hwheel(ih)] if ih else [])
            if events:
                arr = (INPUT * len(events))(*events)
                user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
            time.sleep(PAN_INTERVAL_S)

    def _learn(self):
        """Compare the finger travel with what Windows sent for it."""
        if abs(self.dy) > 5 and abs(self.win_v) >= 120 and abs(self.dy) > 2 * abs(self.dx):
            g = self.win_v / self.dy
        elif abs(self.dx) > 5 and abs(self.win_h) >= 120 and abs(self.dx) > 2 * abs(self.dy):
            g = -self.win_h / self.dx
        else:
            return
        self.gain = g if (g > 0) != (self.gain > 0) else 0.7 * self.gain + 0.3 * g
        if DEBUG:
            log("touchpad gain %.1f (gesture said %.1f)" % (self.gain, g))
        try:
            with open(CALIBRATION, "w", encoding="utf-8") as f:
                json.dump({"gain": self.gain}, f)
        except OSError:
            pass


pan = Pan()
touchpads = touchpad.Touchpads(pan.on_motion, pan.on_fingers_changed, log)


# --- telling a mouse wheel from a touchpad --------------------------------
# A precision touchpad's scrolling is synthesized by Windows: it shows up as
# wheel messages but not as raw wheel input from a mouse device, and usually
# in steps smaller than one notch (120). A real mouse produces raw wheel input.

last_raw_mouse_wheel = 0.0
last_verdict = None
last_verdict_t = 0.0


def _parse_raw(lparam):
    global last_raw_mouse_wheel
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
        touchpads.feed(header.hDevice, buf.raw[hdr:size.value])
        return
    if header.dwType != RIM_TYPEMOUSE:
        return
    # RAWMOUSE: usFlags, 2 bytes padding, usButtonFlags, usButtonData
    flags = int.from_bytes(buf.raw[hdr + 4:hdr + 6], "little")
    delta = int.from_bytes(buf.raw[hdr + 6:hdr + 8], "little", signed=True)
    if flags & RI_MOUSE_WHEEL and delta % 120 == 0:
        last_raw_mouse_wheel = time.perf_counter()


def raw_input_thread():
    hinst = kernel32.GetModuleHandleW(None)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                _parse_raw(lparam)
            except Exception as e:  # never let an exception escape a WNDPROC
                log("raw input error: %r" % e)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    raw_input_thread.wndproc = WNDPROC(wndproc)  # keep a reference
    wc = WNDCLASSW(lpfnWndProc=raw_input_thread.wndproc, hInstance=hinst,
                   lpszClassName="KiCadEagleControlsRaw")
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "", 0, 0, 0, 0, 0,
                                  w.HWND(-3), None, hinst, None)  # message-only window
    rids = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),  # mice
                                RAWINPUTDEVICE(0x0D, 0x05, RIDEV_INPUTSINK, hwnd))  # precision touchpads
    if not user32.RegisterRawInputDevices(rids, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        log("RegisterRawInputDevices failed: %d" % ctypes.get_last_error())
    pump()


def classify_wheel(delta, injected):
    """Return "mouse" or "touchpad" for a vertical wheel event."""
    global last_verdict, last_verdict_t
    now = time.perf_counter()
    if injected:
        verdict = "touchpad"          # touchpad drivers inject their scrolling
    elif now - last_raw_mouse_wheel < WHEEL_STICKY_S:
        verdict = "mouse"
    elif last_verdict and now - last_verdict_t < WHEEL_STICKY_S:
        verdict = last_verdict        # keep the verdict for the rest of a gesture
    else:
        verdict = "mouse" if delta % 120 == 0 else "touchpad"
    last_verdict, last_verdict_t = verdict, now
    return verdict


# --- the hook -------------------------------------------------------------

KICAD_PID = 0
middle = None          # (x, y, time) of a middle press on the board, until it moves
swallow_right_up = False


def target(pt):
    """"board" for a PCB/footprint editor canvas, "canvas" for another KiCad canvas, else None."""
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return None
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value != KICAD_PID or class_name(hwnd) != "wxGLCanvas":
        return None
    title = window_text(user32.GetAncestor(hwnd, GA_ROOT))
    return "board" if any(f in title for f in EAGLE_FRAMES) else "canvas"


def handle(msg, info):
    """Return True to swallow the event."""
    global middle, swallow_right_up

    if msg == WM_MOUSEMOVE:
        if middle and (abs(info.pt.x - middle[0]) > CLICK_SLOP_PX or abs(info.pt.y - middle[1]) > CLICK_SLOP_PX):
            middle = None  # it's a pan drag, leave it to KiCad
        return False

    if msg == WM_RBUTTONDOWN:
        if key_down(VK_LBUTTON) and target(info.pt) == "board":
            tap(ROTATE_KEY)
            swallow_right_up = True
            return True
        return False

    if msg == WM_RBUTTONUP:
        if swallow_right_up:
            swallow_right_up = False
            return True
        return False

    if msg == WM_MBUTTONDOWN:
        middle = (info.pt.x, info.pt.y, time.perf_counter()) if target(info.pt) == "board" else None
        return False

    if msg == WM_MBUTTONUP:
        if middle and time.perf_counter() - middle[2] < CLICK_MAX_S:
            tap(LAYER_KEY)
        middle = None
        return False

    if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
        if target(info.pt) is None or not trackpad_layout():
            return False
        delta = ctypes.c_short(info.mouseData >> 16).value
        horizontal = msg == WM_MOUSEHWHEEL
        fingers = touchpads.fingers
        if fingers >= 2 or time.perf_counter() < pan.swallow_until:
            return pan.windows_wheel(horizontal, delta, fingers)
        if horizontal:
            return False
        kind = classify_wheel(delta, bool(info.flags & LLMHF_INJECTED))
        if DEBUG:
            log("wheel delta=%d flags=%x -> %s" % (delta, info.flags, kind))
        if kind == "mouse" and not (key_down(VK_CONTROL) or key_down(VK_SHIFT) or key_down(VK_MENU)):
            zoom_wheel(delta)
            return True
        return False

    return False


def hook_proc(code, wparam, lparam):
    if code == 0:
        info = MSLLHOOKSTRUCT.from_address(lparam)
        if info.dwExtraInfo != MAGIC:
            try:
                if handle(wparam, info):
                    return 1
            except Exception as e:
                log("hook error: %r" % e)
    return user32.CallNextHookEx(None, code, wparam, lparam)


def hook_thread():
    hook_thread.proc = HOOKPROC(hook_proc)  # keep a reference
    if not user32.SetWindowsHookExW(WH_MOUSE_LL, hook_thread.proc, kernel32.GetModuleHandleW(None), 0):
        log("SetWindowsHookExW failed: %d" % ctypes.get_last_error())
        os._exit(1)
    pump()


def pump():
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def main():
    global KICAD_PID, COMMON_JSON
    KICAD_PID = int(sys.argv[1])
    COMMON_JSON = sys.argv[2] if len(sys.argv) > 2 else None

    # One helper per KiCad process, even if the plugin is loaded twice.
    kernel32.CreateMutexW(None, False, "Local\\KiCadEagleControls_%d" % KICAD_PID)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        sys.exit(EXIT_DUPLICATE)

    # Record crashes: pythonw has no console to print them to.
    crash_log = open(LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(crash_log)
    threading.excepthook = lambda a: log("thread %s died: %r" % (a.thread.name, a.exc_value))

    kicad = kernel32.OpenProcess(SYNCHRONIZE, False, KICAD_PID)
    if not kicad:
        log("KiCad process %d not found" % KICAD_PID)
        return

    # The hook blocks all mouse input until it returns, so it must get the GIL
    # quickly; the default 5 ms switch interval lets lag pile up.
    sys.setswitchinterval(0.0002)
    for fn in (sender, pan.sender, raw_input_thread, hook_thread):
        threading.Thread(target=fn, daemon=True).start()
    log("started for KiCad pid %d" % KICAD_PID)

    kernel32.WaitForSingleObject(w.HANDLE(kicad), w.DWORD(INFINITE))  # exit together with KiCad
    log("KiCad exited, stopping")


if __name__ == "__main__":
    main()
