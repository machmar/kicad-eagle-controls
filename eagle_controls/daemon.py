"""Eagle-style mouse and touchpad controls for KiCad (Windows).

Runs as a small helper process next to KiCad (started by the plugin in
__init__.py) and watches input with a low-level mouse hook:

  * right click on the board (Shift+right click: KiCad's menu)  -> rotate (R), or bend (/) while routing
  * middle click without moving the mouse                        -> switch layer (V; a via while routing),
                                                                    or flip (F) while dragging
  * mouse wheel                                                  -> zoom
  * touchpad two-finger swipe (any direction) / pinch            -> pan / zoom

Each of these can be changed or turned off in the settings (settings.py).

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
import zlib
from ctypes import wintypes as w

# --- settings -------------------------------------------------------------

CLICK_SLOP = 10            # a click may move this many (96-dpi) pixels; scaled to the display in main()
DRAG_MIN_S = 0.15          # ...and a press only becomes a drag once held this long
CLICK_SLOP_PX = CLICK_SLOP
CLICK_MAX_S = 0.5          # ...and must be released within this time
WHEEL_STICKY_S = 0.35      # a scroll gesture keeps its mouse/touchpad verdict this long
BOARD_FRAMES = ("PCB Editor", "Footprint Editor")       # editors with the board click settings
SCHEMATIC_FRAMES = ("Schematic Editor", "Symbol Editor")  # editors with the schematic ones

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
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0008, 0x0800, 0x1000
KEYEVENTF_KEYUP = 0x0002
RID_INPUT, RIM_TYPEMOUSE, RIM_TYPEHID, RIDEV_INPUTSINK = 0x10000003, 0, 2, 0x00000100
RI_MOUSE_WHEEL = 0x0400
GA_ROOT = 2
SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
ERROR_ALREADY_EXISTS = 183
EXIT_DUPLICATE = 3  # another helper already serves this KiCad

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


# Windows silently removes a low-level hook that is slow to return, so nothing
# the hook calls may wait on the disk: log lines are written by a thread.
_log_queue = queue.Queue()


def log(msg):
    _log_queue.put("%s [%d] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))


def flush_log():
    lines = []
    while not _log_queue.empty():
        lines.append(_log_queue.get())
    if lines:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass


def log_writer():
    while True:
        _log_queue.put(_log_queue.get())  # wait for a line, then write everything queued
        flush_log()


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


NAMED_KEYS = {"pgup": 0x21, "pgdn": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
              "right": 0x27, "down": 0x28, "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
              "tab": 0x09, "space": 0x20, "back": 0x08, "backspace": 0x08, "esc": 0x1B,
              "escape": 0x1B, "return": 0x0D, "enter": 0x0D}
NAMED_KEYS.update({"f%d" % n: 0x6F + n for n in range(1, 25)})
MODIFIER_KEYS = {"shift": VK_SHIFT, "ctrl": VK_CONTROL, "alt": VK_MENU}
_parsed_keys = {}


def parse_key(text):
    """KiCad hotkey notation ("R", "Shift+R", "Ctrl++", "PgUp") -> ([modifier vks], vk), or None."""
    text = text.strip()
    if text.endswith("++"):
        mods, key = text[:-2].split("+") if len(text) > 2 else [], "+"
    elif "+" in text[:-1]:
        *mods, key = text.split("+")
    else:
        mods, key = [], text
    try:
        vks = [MODIFIER_KEYS[m.strip().lower()] for m in mods if m.strip()]
    except KeyError:
        return None
    key = key.strip()
    if key.lower() in NAMED_KEYS:
        return vks, NAMED_KEYS[key.lower()]
    if len(key) != 1:
        return None
    scan = user32.VkKeyScanW(ord(key.lower())) & 0xFFFF
    if scan == 0xFFFF:
        return None
    shift_state = scan >> 8
    for bit, vk in ((1, VK_SHIFT), (2, VK_CONTROL), (4, VK_MENU)):
        if shift_state & bit and vk not in vks:
            vks.append(vk)
    return vks, scan & 0xFF


def do_action(action, trigger):
    """Press KiCad's hotkey for one of settings.KICAD_ACTIONS."""
    text = kicad_keys.get(action, "")
    if DEBUG:
        log("%s -> %s (%s)" % (trigger, action, text))
    if not text:
        log("%s has no hotkey in KiCad (Preferences > Hotkeys)" % action)
        return
    if text not in _parsed_keys:
        _parsed_keys[text] = parse_key(text)
        if _parsed_keys[text] is None:
            log("can't use %r as the %s key" % (text, action))
    combo = _parsed_keys[text]
    if combo:
        mods, vk = combo
        outbox.put([_key(m) for m in mods] + [_key(vk), _key(vk, up=True)]
                   + [_key(m, up=True) for m in reversed(mods)])


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
_layout_mtime = None


def trackpad_layout():
    return _layout_ok


# The keys KiCad has bound for our actions, kept current by refresher().
kicad_keys = settings.kicad_hotkeys(None)
_hotkeys_mtime = None


def _refresh_hotkeys():
    global kicad_keys, _hotkeys_mtime
    if not COMMON_JSON:
        return
    folder = os.path.dirname(COMMON_JSON)
    try:
        mtime = os.path.getmtime(settings.hotkeys_path(folder))
    except OSError:
        mtime = None
    if mtime != _hotkeys_mtime:
        _hotkeys_mtime = mtime
        kicad_keys = settings.kicad_hotkeys(folder)
        log("KiCad hotkeys: %s" % kicad_keys)


def _refresh_layout():
    global _layout_ok, _layout_mtime
    if not COMMON_JSON:
        return
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
LEARN_MIN_MM = 15.0        # calibrate only from swipes at least this long
STALE_INPUT_S = 0.1        # touchpad reports that waited this long only update the finger count
KICAD_CATCH_UP_S = 0.2     # wait at most this long for KiCad to catch up after a pan step
PAN_INTERVAL_S = 1 / 60    # send at most one pan update per frame; every injected
                           # event passes through the hook, so flooding it adds lag
PAN_SPEED = 0.3            # at sensitivity 1x, pan this fraction of Windows' own scroll speed;
                           # matching it fully pans far too fast for board work
ACCEL_REF_MM_S = 30.0      # with acceleration on, finger speed that pans at 1x
ACCEL_RANGE = (0.25, 4.0)  # limits of the acceleration factor


class UserSettings:
    """settings.py values, reloaded by refresher() when the settings dialog saves them."""

    def __init__(self):
        self.mtime = None
        self.values = settings.load()

    def get(self):
        return self.values

    def refresh(self):
        try:
            mtime = os.path.getmtime(settings.PATH)
        except OSError:
            mtime = None
        if mtime != self.mtime:
            self.mtime, self.values = mtime, settings.load()


user_settings = UserSettings()


def windows_scroll_sign():
    """+1 or -1 from Windows' touchpad "scrolling direction" setting, or None if unknown.

    0 is Windows' default, "down motion scrolls up" (content follows the fingers).
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad") as k:
            return 1 if winreg.QueryValueEx(k, "ScrollDirection")[0] == 0 else -1
    except OSError:
        return None


def _initial_gain():
    gain = DEFAULT_GAIN
    try:
        with open(CALIBRATION, encoding="utf-8") as f:
            gain = float(json.load(f)["gain"]) or DEFAULT_GAIN
    except (OSError, ValueError, KeyError, TypeError):
        pass
    sign = windows_scroll_sign()
    return abs(gain) * sign if sign else gain


class Pan:
    def __init__(self):
        self.lock = threading.Lock()
        # Fingers moving down by 1 mm = this many units of vertical wheel;
        # horizontal wheel uses the negated value (Windows' sign convention).
        self.gain = _initial_gain()
        self.flip_votes = 0          # swipes in a row that suggested the other direction
        self.momentum_ours = False   # the last gesture was ours, so swallow its momentum scrolling
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
        self.stats = {"steps": 0, "wait": [], "stale": 0, "last_motion": None, "last_send": None,
                      "max_fingers": 0, "motion_mm": 0.0, "win_events": 0, "passed": 0,
                      "start": time.perf_counter()}
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
        if input_age_s > STALE_INPUT_S:
            self.stats["stale"] += 1
            return  # we fell behind (e.g. a throttled CPU): don't replay old movement
        self.stats["last_motion"] = time.perf_counter()
        self.stats["motion_mm"] += (dx * dx + dy * dy) ** 0.5
        with self.lock:
            k = PAN_SPEED * self.sensitivity * self._accel_factor((dx * dx + dy * dy) ** 0.5)
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
                    self.state = ("pan" if target(pt) and trackpad_layout()
                                  and user_settings.get()["touchpad_free_pan"] else "ignore")
                    if DEBUG:
                        log("touchpad gesture: %s" % self.state)
            if self.state == "pan":
                self._flush()

    def on_fingers_changed(self, n):
        self.stats["max_fingers"] = max(self.stats["max_fingers"], n)
        if n:
            # Fingers landing or lifting one at a time, or the touchpad briefly
            # losing one: the gesture goes on (restarting it caused stalls).
            self.momentum_ours = False  # fingers are down: no momentum scrolling now
            return
        with self.lock:
            st = self.stats
            if DEBUG and self.state != "pan" and (st["motion_mm"] > 0 or st["win_events"]):
                log("swipe without pan: state %s, most fingers %d, two-finger motion %.1f mm, "
                    "%d Windows scroll events (%d passed to KiCad), %.0f ms long" % (
                        self.state, st["max_fingers"], st["motion_mm"], st["win_events"], st["passed"],
                        1000 * (time.perf_counter() - st["start"])))
            if DEBUG and self.state == "pan":
                st, now = self.stats, time.perf_counter()
                waits = st["wait"] or [0]
                log("pan: %d steps, KiCad wait avg %.0f / max %.0f ms, %d stale reports dropped, "
                    "last step %.0f ms after last motion, fingers up %.0f ms after last step" % (
                        st["steps"], 1000 * sum(waits) / len(waits), 1000 * max(waits), st["stale"],
                        1000 * ((st["last_send"] or now) - (st["last_motion"] or now)),
                        1000 * (now - (st["last_send"] or now))))
            self._learn()
            self.momentum_ours = self.state == "pan"
            self._reset()

    def windows_wheel(self, horizontal, delta, fingers):
        """Called by the hook for a touchpad wheel event over a canvas. Returns True to swallow it."""
        with self.lock:
            self.stats["win_events"] += 1
            if key_down(VK_CONTROL):         # Windows saw a pinch: let KiCad zoom
                self.state = "pinch"
                self.stats["passed"] += 1
                return False
            if self.state == "ignore":       # gesture started outside the canvas
                self.stats["passed"] += 1
                return False
            if fingers != 2 and self.state != "pan" and not self.momentum_ours:
                # We can't see two fingers (the touchpad lost one), so we can't
                # pan this ourselves: let Windows' own scrolling through.
                self.stats["passed"] += 1
                return False
            self.swallow_until = time.perf_counter() + MOMENTUM_S
            if self.state == "pinch" and fingers == 2:
                # Our spread test guessed pinch (fingers settling as they land
                # look like one), but Windows sees a scroll: trust Windows.
                if DEBUG:
                    log("touchpad gesture: pan (Windows scrolls, not a pinch)")
                self.state = "pan"
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
                started = time.perf_counter()
                wait_for_kicad()
                self.stats["steps"] += 1
                self.stats["wait"].append(time.perf_counter() - started)
                self.stats["last_send"] = started
            time.sleep(PAN_INTERVAL_S)

    def _learn(self):
        """Compare the finger travel with what Windows sent for it."""
        # Only longer, mostly straight swipes: in short ones Windows' first events
        # also cover the travel before it recognised the gesture.
        if abs(self.dy) > LEARN_MIN_MM and abs(self.win_v) >= 120 and abs(self.dy) > 2 * abs(self.dx):
            g = self.win_v / self.dy
        elif abs(self.dx) > LEARN_MIN_MM and abs(self.win_h) >= 120 and abs(self.dx) > 2 * abs(self.dy):
            g = -self.win_h / self.dx
        else:
            return
        # The direction comes from Windows' setting; a swipe only tunes the speed.
        # Without that setting, flip only after two swipes in a row agree.
        sign = windows_scroll_sign()
        if sign and (g > 0) != (sign > 0):
            return
        if (g > 0) != (self.gain > 0):
            self.flip_votes += 1
            if self.flip_votes < 2:
                return
            self.gain = -self.gain
        self.flip_votes = 0
        # Windows speeds fast swipes up, so one gesture may only nudge the gain.
        g = min(max(g, self.gain / 1.5), self.gain * 1.5)
        self.gain = 0.85 * self.gain + 0.15 * g
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


def wait_for_kicad():
    """Wait until KiCad's window under the cursor gets back to its message loop.

    When KiCad is slow to redraw (e.g. a throttled CPU) this keeps our pan
    steps from queueing up and panning on after the fingers stop; motion in
    the meantime is merged into the next, bigger step.
    """
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hwnd = user32.WindowFromPoint(pt)
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if hwnd and pid.value == KICAD_PID:
        # The first ping returns once KiCad has taken the step off its queue;
        # the second only once it has also redrawn and come back for more.
        deadline = time.perf_counter() + KICAD_CATCH_UP_S
        for _ in range(2):
            left = int((deadline - time.perf_counter()) * 1000)
            result = ULONG_PTR()
            if left <= 0 or not user32.SendMessageTimeoutW(hwnd, 0, 0, 0, SMTO_ABORTIFHUNG,  # WM_NULL
                                                           left, ctypes.byref(result)):
                break


input_age_s = 0.0  # how long the WM_INPUT being handled waited in our queue
user32.GetMessageTime.restype = ctypes.c_long


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
    hook_watch.last_mouse = time.perf_counter()


def raw_input_thread():
    hinst = kernel32.GetModuleHandleW(None)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            global input_age_s
            input_age_s = ((kernel32.GetTickCount() - user32.GetMessageTime()) & 0xFFFFFFFF) / 1000
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
middle = None          # (x, y, time, action) of a middle press on the board, until it moves
swallow_right_up = False
right_pending = None   # (x, y, time, action) of a held-back right press: act on release, or pan if it moves
left_down = False      # tracked here: GetAsyncKeyState can miss it from another process
swallow_up = None      # button-up message to swallow because we took its button-down
last_middle_down = None  # (time, x, y) of the previous middle press, to spot double-clicks
hide_middle_up = False   # we hid this middle press from KiCad, so hide its release too
SM_CXDOUBLECLK, SM_CYDOUBLECLK = 36, 37


GWL_STYLE, WS_CAPTION = -16, 0x00C00000
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
EnumChildProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.EnumChildWindows.argtypes = [w.HWND, EnumChildProc, w.LPARAM]
user32.IsWindowVisible.argtypes = [w.HWND]


def _canvas_kind(canvas):
    """"board", "schematic", or "canvas" (another KiCad canvas, e.g. the 3D viewer)."""
    title = window_text(user32.GetAncestor(canvas, GA_ROOT))
    if any(f in title for f in BOARD_FRAMES):
        return "board"
    if any(f in title for f in SCHEMATIC_FRAMES):
        return "schematic"
    return "canvas"


def _canvas_below(frame, pt):
    """The visible KiCad canvas of `frame` under pt, if any."""
    found = []

    def check(child, _):
        r = w.RECT()
        if (class_name(child) == "wxGLCanvas" and user32.IsWindowVisible(child)
                and user32.GetWindowRect(child, ctypes.byref(r))
                and r.left <= pt.x < r.right and r.top <= pt.y < r.bottom):
            found.append(child)
            return False
        return True

    user32.EnumChildWindows(frame, EnumChildProc(check), 0)
    return found[0] if found else None


def target(pt):
    """The kind of KiCad canvas under pt (see _canvas_kind), or None if it isn't one."""
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return None
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value != KICAD_PID:
        return None
    if class_name(hwnd) == "wxGLCanvas":
        return _canvas_kind(hwnd)
    # KiCad puts things over the canvas: overlays inside the editor window
    # (e.g. while routing) and little popups such as the hotkey feedback
    # ("Layer: B.Cu") next to the cursor. Look through them to the canvas
    # area. Dialogs (captioned), menus and drop-down lists (not wx classes)
    # still count as "not the board", as do toolbars and side panels, which
    # lie outside the canvas.
    root = user32.GetAncestor(hwnd, GA_ROOT)
    style = user32.GetWindowLongW(root, GWL_STYLE) & 0xFFFFFFFF
    canvas = _canvas_below(root, pt)              # an overlay inside the editor window
    if not canvas and not style & WS_CAPTION and class_name(root).startswith("wx"):
        canvas = _kicad_canvas_under(pt, skip=root)  # a caption-less popup on top of it
    if DEBUG:
        log("%r in %r (%r, style %08x) over the canvas area: %s" % (
            class_name(hwnd), class_name(root), window_text(root), style, "board" if canvas else "not the board"))
    return _canvas_kind(canvas) if canvas else None


EnumWindowsProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, w.LPARAM]


def _kicad_canvas_under(pt, skip):
    """The canvas under pt of the topmost KiCad window with a title bar there."""
    found = []

    def check(top, _):
        if top == skip or not user32.IsWindowVisible(top):
            return True
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(top, ctypes.byref(pid))
        r = w.RECT()
        if (pid.value != KICAD_PID or not user32.GetWindowLongW(top, GWL_STYLE) & WS_CAPTION
                or not user32.GetWindowRect(top, ctypes.byref(r))
                or not (r.left <= pt.x < r.right and r.top <= pt.y < r.bottom)):
            return True
        found.append(_canvas_below(top, pt))  # windows come topmost first; stop at the first
        return False

    user32.EnumWindows(EnumWindowsProc(check), 0)
    return found[0] if found else None


def right_down_injected():
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_RIGHTDOWN, 0, MAGIC)
    return i


def modifiers_held():
    return key_down(VK_SHIFT) or key_down(VK_CONTROL) or key_down(VK_MENU)


def is_drag(press, pt):
    """Whether a press at (x, y, time, ...) has turned into a drag now that the mouse is at pt."""
    moved = abs(pt.x - press[0]) > CLICK_SLOP_PX or abs(pt.y - press[1]) > CLICK_SLOP_PX
    return moved and time.perf_counter() - press[2] >= DRAG_MIN_S


def dragging():
    return left_down or key_down(VK_LBUTTON)


# --- is the router active? -------------------------------------------------
# KiCad doesn't tell other processes which tool is active, but the router
# shows a pencil cursor, and it's the only board cursor whose hotspot (the
# pencil tip) is in the bottom-left corner. Arrows have it top-left, the move
# and placement cursors in the middle.

class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hCursor", w.HANDLE), ("pt", POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", w.WORD), ("bmBitsPixel", w.WORD),
                ("bmBits", ctypes.c_void_p)]


gdi32 = ctypes.WinDLL("gdi32")
gdi32.DeleteObject.argtypes = [w.HANDLE]
gdi32.GetBitmapBits.argtypes = [w.HANDLE, ctypes.c_long, ctypes.c_void_p]
gdi32.GetObjectW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p]
user32.GetIconInfo.argtypes = [w.HANDLE, ctypes.POINTER(ICONINFO)]
_cursor_shapes = {}  # cursor handle -> (hotspot x, y, width, height)


def cursor_shape():
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    if not user32.GetCursorInfo(ctypes.byref(ci)) or not ci.hCursor:
        return None
    if ci.hCursor not in _cursor_shapes:
        ii = ICONINFO()
        if not user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
            return None
        bm = BITMAP()
        gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(bm), ctypes.byref(bm))
        height = bm.bmHeight if ii.hbmColor else bm.bmHeight // 2  # monochrome: AND and XOR masks stacked
        if DEBUG:  # a fingerprint of the image, to tell KiCad's cursors apart in the log
            size = bm.bmWidthBytes * bm.bmHeight
            bits = ctypes.create_string_buffer(size)
            gdi32.GetBitmapBits(ii.hbmMask, size, bits)
            _cursor_prints[ci.hCursor] = "%08x" % zlib.crc32(bits.raw)
        for hbm in (ii.hbmMask, ii.hbmColor):
            if hbm:
                gdi32.DeleteObject(hbm)
        _cursor_shapes[ci.hCursor] = (ii.xHotspot, ii.yHotspot, bm.bmWidth, height)
    return _cursor_shapes[ci.hCursor]


_cursor_prints = {}


def log_click(button, kind):
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    user32.GetCursorInfo(ctypes.byref(ci))
    shape = cursor_shape()
    log("%s click on %s: left held=%s, cursor %s shape %s image %s" % (
        button, kind, dragging(), ci.hCursor, shape, _cursor_prints.get(ci.hCursor)))


# Windows' own cursors (arrow, I-beam, hand...). KiCad shows the plain arrow
# while nothing is being moved, even with a part selected, and its own
# cursors while a tool is busy: moving (M, or a left-button drag), dragging
# (G), placing a new part, routing.
user32.LoadCursorW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = w.HANDLE
SYSTEM_CURSORS = {user32.LoadCursorW(None, ctypes.c_void_p(i))
                  for i in (32512, 32513, 32514, 32515, 32516, 32640, 32641, 32642, 32643, 32644,
                            32645, 32646, 32648, 32649, 32650, 32651, 32671, 32672)} - {None}
# Actions that change a part, so only make sense while one is in hand.
PART_ACTIONS = {"rotate", "flip", "sch_rotate", "sch_mirror_x", "sch_mirror_y"}


def tool_busy():
    """Whether KiCad shows one of its own tool cursors (moving, dragging, placing...)."""
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    return bool(user32.GetCursorInfo(ctypes.byref(ci)) and ci.hCursor and ci.hCursor not in SYSTEM_CURSORS)


def routing():
    shape = cursor_shape()
    if not shape or not shape[2] or not shape[3]:
        return False
    x, y, width, height = shape
    return x < 0.3 * width and y > 0.6 * height


# --- via layer chooser ----------------------------------------------------
# Eagle-style via placement while routing. A middle click opens KiCad's own
# "Select Layer and Place Through Via" chooser (always a through via, never
# blind/buried). KiCad's chooser only reacts to clicks on its rows, so we
# draw our own highlight over it: further middle clicks move the highlight
# one copper layer down, and a left click anywhere clicks the highlighted
# row for the user. Esc still cancels it as usual.
#
# The copper layers and the active layer are read from the layer drop-down
# in KiCad's toolbar; the chooser lists the same copper layers, top to
# bottom, one row each.

WM_APP_CHOOSER_OPEN, WM_APP_CHOOSER_STEP, WM_APP_CHOOSER_PICK = 0x8011, 0x8012, 0x8013
WM_TIMER, WM_LBUTTONDOWN_MSG, WM_LBUTTONUP_MSG, MK_LBUTTON = 0x0113, 0x0201, 0x0202, 0x0001
CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXTLEN, CB_GETLBTEXT = 0x0146, 0x0147, 0x0149, 0x0148
SMTO_ABORTIFHUNG = 0x0002
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x00080000, 0x00000020, 0x00000008
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
LWA_ALPHA = 0x2
SWP_NOACTIVATE, SWP_SHOWWINDOW, SW_HIDE = 0x0010, 0x0040, 0
HWND_TOPMOST = w.HWND(-1)
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
CHOOSER_WAIT_S = 1.5
CHOOSER_HIGHLIGHT_RGB = (0, 120, 215)
# Fixed names of KiCad's technical layers; the copper layers come before them.
TECHNICAL_LAYERS = {"F.Adhesive", "B.Adhesive", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen",
                    "F.Mask", "B.Mask", "Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User",
                    "Edge.Cuts", "Margin", "F.Courtyard", "B.Courtyard", "F.Fab", "B.Fab"}

user32.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
                                       ctypes.POINTER(ULONG_PTR)]
user32.SendMessageTimeoutW.restype = LRESULT
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.ScreenToClient.argtypes = [w.HWND, ctypes.POINTER(POINT)]
user32.IsWindow.argtypes = [w.HWND]
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.SetLayeredWindowAttributes.argtypes = [w.HWND, w.DWORD, ctypes.c_ubyte, w.DWORD]
user32.SetTimer.argtypes = [w.HWND, ULONG_PTR, w.UINT, ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
gdi32.CreateSolidBrush.argtypes = [w.DWORD]
gdi32.CreateSolidBrush.restype = w.HBRUSH


def _send_timeout(hwnd, msg, wparam, lparam):
    """SendMessage that gives up if KiCad is busy, so this thread never hangs."""
    result = ULONG_PTR()
    if not user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, 300, ctypes.byref(result)):
        return None
    return ctypes.c_ssize_t(result.value).value


def _kicad_children(parent, wanted):
    """Descendant windows of parent for which wanted(hwnd) is true."""
    found = []

    def check(child, _):
        if wanted(child):
            found.append(child)
        return True

    user32.EnumChildWindows(parent, EnumChildProc(check), 0)
    return found


def _kicad_tops():
    tops = []

    def check(top, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(top, ctypes.byref(pid))
        if pid.value == KICAD_PID and user32.IsWindowVisible(top):
            tops.append(top)
        return True

    user32.EnumWindows(EnumWindowsProc(check), 0)
    return tops


def read_copper_layers():
    """(copper layer names top to bottom, index of the active one) from the toolbar, or None."""
    for frame in _kicad_tops():
        if not any(f in window_text(frame) for f in BOARD_FRAMES):
            continue
        for combo in _kicad_children(frame, lambda h: class_name(h) == "ComboBox"):
            count = _send_timeout(combo, CB_GETCOUNT, 0, 0)
            if not count or count <= 0 or count > 200:
                continue
            names = []
            for i in range(count):
                length = _send_timeout(combo, CB_GETLBTEXTLEN, i, 0)
                if length is None or length < 0:
                    break
                buf = ctypes.create_unicode_buffer(length + 2)
                _send_timeout(combo, CB_GETLBTEXT, i, ctypes.addressof(buf))
                names.append(buf.value.split(" (")[0])  # "F.Cu (PgUp)" -> "F.Cu"
            technical = [i for i, n in enumerate(names) if n in TECHNICAL_LAYERS]
            if not technical or technical[0] < 2:
                continue  # not the layer selector
            copper = names[:technical[0]]
            active = _send_timeout(combo, CB_GETCURSEL, 0, 0)
            return copper, active if active is not None and 0 <= active < len(copper) else None
    return None


def find_layer_chooser():
    """(dialog, grid window) of KiCad's open layer chooser, or None."""
    for top in _kicad_tops():
        if user32.GetWindowLongW(top, GWL_STYLE) & WS_CAPTION:
            grids = _kicad_children(top, lambda h: window_text(h) == "GridWindow"
                                    and user32.IsWindowVisible(h))
            if len(grids) == 1 and not any(f in window_text(top) for f in BOARD_FRAMES + SCHEMATIC_FRAMES):
                return top, grids[0]
    return None


def _left_click():
    """A left click at the cursor, tagged so our own hook ignores it."""
    down, up = INPUT(type=INPUT_MOUSE), INPUT(type=INPUT_MOUSE)
    down.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, MAGIC)
    up.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, MAGIC)
    arr = (INPUT * 2)(down, up)
    user32.SendInput(2, arr, ctypes.sizeof(INPUT))


class LayerChooser:
    def __init__(self):
        self.open = False        # read by the hook
        self.thread_id = None
        self.dialog = self.grid = None
        self.rows = 0
        self.active = None
        self.index = 0
        self.names = []

    def request(self, msg):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, msg, 0, 0)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self.overlay = self._make_overlay()
        user32.SetTimer(None, 0, 100, None)  # check whether the chooser is still open
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            try:
                if msg.message == WM_APP_CHOOSER_OPEN:
                    self._open()
                elif msg.message == WM_APP_CHOOSER_STEP:
                    self._step()
                elif msg.message == WM_APP_CHOOSER_PICK:
                    self._pick()
                elif msg.message == WM_TIMER and self.open:
                    if not (user32.IsWindow(self.dialog) and user32.IsWindowVisible(self.dialog)):
                        self._close()
            except Exception as e:
                log("layer chooser error: %r" % e)
                self._close()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _make_overlay(self):
        hinst = kernel32.GetModuleHandleW(None)
        self.wndproc = WNDPROC(lambda h, m, wp, lp: user32.DefWindowProcW(h, m, wp, lp))
        r, g, b = CHOOSER_HIGHLIGHT_RGB
        wc = WNDCLASSW(lpfnWndProc=self.wndproc, hInstance=hinst, lpszClassName="KiCadEagleControlsHighlight",
                       hbrBackground=gdi32.CreateSolidBrush(r | g << 8 | b << 16))
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            wc.lpszClassName, "", 0x80000000, 0, 0, 0, 0, None, None, hinst, None)  # WS_POPUP
        user32.SetLayeredWindowAttributes(hwnd, 0, 110, LWA_ALPHA)
        return hwnd

    def _open(self):
        layers = read_copper_layers()
        do_action("via_chooser", "middle click while routing")
        if not layers:
            log("layer chooser: can't read the copper layers; no highlight")
            return
        self.names, self.active = layers
        deadline = time.perf_counter() + CHOOSER_WAIT_S
        found = None
        while not found and time.perf_counter() < deadline:
            time.sleep(0.03)
            found = find_layer_chooser()
        if not found:
            log("layer chooser didn't appear")
            return
        self.dialog, self.grid = found
        self.rows = len(self.names)
        self.index = self.active if self.active is not None else -1
        self.open = True
        self._step()  # start on the next layer down from the active one
        if DEBUG:
            log("layer chooser open: %s, active %s" % (self.names, self.active))

    def _row_rect(self):
        r = w.RECT()
        user32.GetWindowRect(self.grid, ctypes.byref(r))
        row_h = (r.bottom - r.top) / self.rows
        top = round(r.top + self.index * row_h)
        return r.left, top, r.right - r.left, round(r.top + (self.index + 1) * row_h) - top

    def _step(self):
        if not self.open:
            return
        for _ in range(self.rows):
            self.index = (self.index + 1) % self.rows
            if self.index != self.active:
                break
        x, y, width, height = self._row_rect()
        user32.SetWindowPos(self.overlay, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _pick(self):
        if not self.open:
            return
        x, y, width, height = self._row_rect()
        pt = POINT(x + width // 2, y + height // 2)
        if DEBUG:
            log("layer chooser: picking %s" % self.names[self.index])
        user32.ShowWindow(self.overlay, SW_HIDE)
        client = POINT(pt.x, pt.y)
        user32.ScreenToClient(self.grid, ctypes.byref(client))
        lparam = (client.y & 0xFFFF) << 16 | (client.x & 0xFFFF)
        user32.PostMessageW(self.grid, WM_LBUTTONDOWN_MSG, MK_LBUTTON, lparam)
        user32.PostMessageW(self.grid, WM_LBUTTONUP_MSG, 0, lparam)
        deadline = time.perf_counter() + 0.4
        while time.perf_counter() < deadline and user32.IsWindowVisible(self.dialog):
            time.sleep(0.02)
        if user32.IsWindowVisible(self.dialog):
            # The grid ignored posted clicks: click the row for real, then put the cursor back.
            back = POINT()
            user32.GetCursorPos(ctypes.byref(back))
            user32.SetCursorPos(pt.x, pt.y)
            _left_click()
            time.sleep(0.05)
            user32.SetCursorPos(back.x, back.y)
        picked = not user32.IsWindowVisible(self.dialog)
        self._close()
        if picked and user_settings.get()["chooser_places_via"]:
            # KiCad now has the via on the end of the trace; the click that
            # confirmed the layer also places it, where the cursor is.
            time.sleep(0.1)
            _left_click()

    def _close(self):
        self.open = False
        if getattr(self, "overlay", None):
            user32.ShowWindow(self.overlay, SW_HIDE)


chooser = LayerChooser()


def perform(action, trigger):
    """Carry out a click action: press its hotkey, or run the via layer chooser."""
    if action == "via_chooser":
        chooser.request(WM_APP_CHOOSER_OPEN)
    else:
        do_action(action, trigger)


def click_action(s, button, kind):
    """The KiCad action (a settings.KICAD_ACTIONS key) for a right/middle click in
    the current situation, or None to leave the click to KiCad."""
    if kind not in ("board", "schematic"):
        return None
    prefix = "sch_" if kind == "schematic" else ""   # the schematic has its own settings
    if dragging():
        action = s[prefix + button + "_drag"]
    elif routing():   # the router's or the wire tool's pencil cursor
        action = s[prefix + button + "_route"]
    else:
        action = s[prefix + button]
    action = prefix + action
    if action not in settings.KICAD_ACTIONS:
        return None
    if action in PART_ACTIONS and s["parts_only_while_moving"] and not (dragging() or tool_busy()):
        return None   # a part is merely selected: leave the click to KiCad
    return action


def handle(msg, info):
    """Return True to swallow the event."""
    global middle, swallow_right_up, right_pending, left_down, swallow_up, last_middle_down, hide_middle_up

    # While our highlight is over KiCad's via layer chooser, a left click picks
    # the highlighted layer and a middle click moves the highlight.
    if chooser.open and msg in (WM_LBUTTONDOWN, WM_MBUTTONDOWN):
        chooser.request(WM_APP_CHOOSER_PICK if msg == WM_LBUTTONDOWN else WM_APP_CHOOSER_STEP)
        swallow_up = msg + 1  # the matching button-up message
        return True
    if msg == swallow_up:
        swallow_up = None
        return True

    if msg == WM_LBUTTONDOWN:
        left_down = True
        return False

    if msg == WM_LBUTTONUP:
        left_down = False
        return False

    if msg == WM_MOUSEMOVE:
        if middle and is_drag(middle, info.pt):
            middle = None  # it's a pan drag, leave it to KiCad
        if right_pending and is_drag(right_pending, info.pt):
            right_pending = None  # a right drag: give KiCad the press after all, so it pans
            outbox.put([right_down_injected()])
        return False

    if msg == WM_RBUTTONDOWN:
        kind = target(info.pt)
        if DEBUG and kind:
            log_click("right", kind)
        action = click_action(user_settings.get(), "right_click", kind)
        if not action or modifiers_held():
            return False                          # e.g. Shift+right click: KiCad's menu
        if dragging():
            perform(action, "right click while dragging")
            swallow_right_up = True
            return True
        right_pending = (info.pt.x, info.pt.y, time.perf_counter(), action)  # decided on release or movement
        return True

    if msg == WM_RBUTTONUP:
        if swallow_right_up:
            swallow_right_up = False
            return True
        if right_pending:
            perform(right_pending[3], "right click")
            right_pending = None
            return True
        return False

    if msg == WM_MBUTTONDOWN:
        middle = None
        kind = target(info.pt)
        if DEBUG and kind:
            log_click("middle", kind)
        s = user_settings.get()
        action = click_action(s, "middle_click", kind)
        now = time.perf_counter()
        if action:
            middle = (info.pt.x, info.pt.y, now, action)
        # The second press of a double-click: KiCad would zoom to fit. Hide it
        # from KiCad if asked to; our own click action still runs on release.
        scale = CLICK_SLOP_PX / CLICK_SLOP  # the metrics are in 96-dpi pixels, the hook's in physical ones
        second = (last_middle_down is not None
                  and now - last_middle_down[0] <= user32.GetDoubleClickTime() / 1000
                  and abs(info.pt.x - last_middle_down[1]) <= user32.GetSystemMetrics(SM_CXDOUBLECLK) * scale
                  and abs(info.pt.y - last_middle_down[2]) <= user32.GetSystemMetrics(SM_CYDOUBLECLK) * scale)
        last_middle_down = None if second else (now, info.pt.x, info.pt.y)
        if second and kind and s["block_middle_double_click"]:
            hide_middle_up = True
            return True
        return False

    if msg == WM_MBUTTONUP:
        if middle and time.perf_counter() - middle[2] < CLICK_MAX_S:
            perform(middle[3], "middle click")
        middle = None
        if hide_middle_up:
            hide_middle_up = False
            return True
        return False

    if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
        if target(info.pt) is None or not trackpad_layout():
            return False
        s = user_settings.get()
        delta = ctypes.c_short(info.mouseData >> 16).value
        horizontal = msg == WM_MOUSEHWHEEL
        fingers = touchpads.fingers
        if fingers >= 2 or time.perf_counter() < pan.swallow_until:
            return pan.windows_wheel(horizontal, delta, fingers) if s["touchpad_free_pan"] else False
        if horizontal or not s["wheel_zoom"]:
            return False
        kind = classify_wheel(delta, bool(info.flags & LLMHF_INJECTED))
        if DEBUG:
            log("wheel delta=%d flags=%x -> %s" % (delta, info.flags, kind))
        if kind == "mouse" and not modifiers_held():
            zoom_wheel(delta)
            return True
        return False

    return False


def hook_proc(code, wparam, lparam):
    hook_watch.last_call = time.perf_counter()
    if code == 0:
        info = MSLLHOOKSTRUCT.from_address(lparam)
        if info.dwExtraInfo != MAGIC:
            try:
                if handle(wparam, info):
                    return 1
            except Exception as e:
                log("hook error: %r" % e)
    return user32.CallNextHookEx(None, code, wparam, lparam)


WM_APP_REHOOK = 0x8001
THREAD_PRIORITY_TIME_CRITICAL = 15
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
HOOK_SILENT_S = 0.5        # mouse moved but the hook wasn't called for this long: it's gone


class HookWatch:
    def __init__(self):
        self.last_call = self.last_mouse = time.perf_counter()
        self.thread_id = None
        self.last_report = 0.0


hook_watch = HookWatch()
user32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
kernel32.GetCurrentThread.restype = w.HANDLE
kernel32.GetCurrentProcess.restype = w.HANDLE
kernel32.SetThreadPriority.argtypes = [w.HANDLE, ctypes.c_int]
kernel32.SetPriorityClass.argtypes = [w.HANDLE, w.DWORD]
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]


def hook_thread():
    # Get scheduled promptly even when KiCad keeps the CPU busy.
    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL)
    hook_thread.proc = HOOKPROC(hook_proc)  # keep a reference
    hook_watch.thread_id = kernel32.GetCurrentThreadId()
    hook = None

    def install():
        h = user32.SetWindowsHookExW(WH_MOUSE_LL, hook_thread.proc, kernel32.GetModuleHandleW(None), 0)
        if not h:
            log("SetWindowsHookExW failed: %d" % ctypes.get_last_error())
            flush_log()
            os._exit(1)
        hook_watch.last_call = time.perf_counter()
        return h

    hook = install()
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_APP_REHOOK:
            user32.UnhookWindowsHookEx(hook)
            hook = install()


def refresher():
    """Background upkeep: re-read settings, and reinstall the hook if Windows dropped it."""
    while True:
        user_settings.refresh()
        _refresh_layout()
        _refresh_hotkeys()
        silent = hook_watch.last_mouse - hook_watch.last_call
        if silent > HOOK_SILENT_S and hook_watch.thread_id:
            # Also happens while an elevated (admin) window has focus: hooks aren't
            # called then. Reinstalling is harmless; just don't flood the log.
            now = time.perf_counter()
            if now - hook_watch.last_report > 30:
                log("mouse hook went silent for %.1f s while the mouse moved; reinstalling" % silent)
                hook_watch.last_report = now
            hook_watch.last_call = now
            user32.PostThreadMessageW(hook_watch.thread_id, WM_APP_REHOOK, 0, 0)
        time.sleep(0.25)


def pump():
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


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
    global KICAD_PID, COMMON_JSON, CLICK_SLOP_PX
    make_dpi_aware()
    try:
        CLICK_SLOP_PX = round(CLICK_SLOP * user32.GetDpiForSystem() / 96)
    except AttributeError:  # before Windows 10 1607
        pass
    KICAD_PID = int(sys.argv[1])
    COMMON_JSON = sys.argv[2] if len(sys.argv) > 2 else None

    # One helper per KiCad process, even if the plugin is loaded twice.
    kernel32.CreateMutexW(None, False, "Local\\KiCadEagleControls_%d" % KICAD_PID)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        sys.exit(EXIT_DUPLICATE)

    # Record crashes: pythonw has no console to print them to.
    crash_log = open(LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(crash_log)
    threading.excepthook = lambda a: (log("thread %s died: %r" % (a.thread.name, a.exc_value)), flush_log())

    kicad = kernel32.OpenProcess(SYNCHRONIZE, False, KICAD_PID)
    if not kicad:
        log("KiCad process %d not found" % KICAD_PID)
        return

    # The hook blocks all mouse input until it returns, so it must get the GIL
    # quickly; the default 5 ms switch interval lets lag pile up.
    sys.setswitchinterval(0.0002)
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
    _refresh_layout()
    _refresh_hotkeys()
    for fn in (log_writer, sender, pan.sender, raw_input_thread, hook_thread, refresher, chooser.run):
        threading.Thread(target=fn, name=getattr(fn, "__qualname__", "worker"), daemon=True).start()
    log("started for KiCad pid %d" % KICAD_PID)
    if DEBUG:
        log("settings from %s: %s" % (settings.PATH, {k: v for k, v in user_settings.get().items() if "click" in k}))

    kernel32.WaitForSingleObject(w.HANDLE(kicad), w.DWORD(INFINITE))  # exit together with KiCad
    log("KiCad exited, stopping")
    flush_log()


if __name__ == "__main__":
    main()
