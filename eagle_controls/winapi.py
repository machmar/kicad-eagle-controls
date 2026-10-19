"""The parts of the Win32 API the helper uses: ctypes declarations and small wrappers."""

import ctypes
import queue
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

# --- constants ---------------------------------------------------------------

WM_NULL, WM_INPUT, WM_TIMER, WM_APP = 0x0000, 0x00FF, 0x0113, 0x8000
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
MK_LBUTTON = 0x0001
CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN = 0x0146, 0x0147, 0x0148, 0x0149

WH_MOUSE_LL, LLMHF_INJECTED = 14, 0x01
VK_LBUTTON, VK_SHIFT, VK_CONTROL, VK_MENU = 0x01, 0x10, 0x11, 0x12
INPUT_MOUSE, INPUT_KEYBOARD, KEYEVENTF_KEYUP = 0, 1, 0x0002
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTDOWN = 0x0002, 0x0004, 0x0008
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
RID_INPUT, RIDI_PREPARSEDDATA, RIDEV_INPUTSINK = 0x10000003, 0x20000005, 0x00000100
RIM_TYPEMOUSE, RIM_TYPEHID, RI_MOUSE_WHEEL = 0, 2, 0x0400
SM_CXDOUBLECLK, SM_CYDOUBLECLK = 36, 37

GA_ROOT, GWL_STYLE = 2, -16
WS_POPUP, WS_CAPTION = 0x80000000, 0x00C00000
WS_EX_TRANSPARENT, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00000020, 0x00000008, 0x00000080
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x00080000, 0x08000000
HWND_TOPMOST, HWND_MESSAGE = w.HWND(-1), w.HWND(-3)
SWP_NOACTIVATE, SWP_SHOWWINDOW, SW_HIDE = 0x0010, 0x0040, 0
LWA_ALPHA, SMTO_ABORTIFHUNG = 0x2, 0x0002

SYNCHRONIZE, INFINITE, ERROR_ALREADY_EXISTS = 0x00100000, 0xFFFFFFFF, 183
THREAD_PRIORITY_TIME_CRITICAL, ABOVE_NORMAL_PRIORITY_CLASS = 15, 0x8000

# Tag on everything we inject, so our own hook lets it through.
INJECTED_TAG = 0x4541474C  # "EAGL"

# --- structures ----------------------------------------------------------------


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


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hCursor", w.HANDLE), ("pt", POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", w.WORD), ("bmBitsPixel", w.WORD),
                ("bmBits", ctypes.c_void_p)]


# --- prototypes ------------------------------------------------------------------
# Only where ctypes' defaults (int arguments and result) would be wrong, mostly
# for handles, which are 64 bits wide.


def _proto(dll, name, restype, argtypes=None):
    f = getattr(dll, name)
    f.restype = restype
    if argtypes is not None:
        f.argtypes = argtypes


_proto(user32, "SetWindowsHookExW", w.HHOOK, [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD])
_proto(user32, "UnhookWindowsHookEx", w.BOOL, [w.HHOOK])
_proto(user32, "CallNextHookEx", LRESULT, [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM])
_proto(user32, "DefWindowProcW", LRESULT, [w.HWND, w.UINT, w.WPARAM, w.LPARAM])
_proto(user32, "CreateWindowExW", w.HWND, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID])
_proto(user32, "GetRawInputData", w.UINT, [w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT), w.UINT])
_proto(user32, "GetRawInputDeviceInfoW", w.UINT, [w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT)])
_proto(user32, "WindowFromPoint", w.HWND, [POINT])
_proto(user32, "GetAncestor", w.HWND, [w.HWND, w.UINT])
_proto(user32, "GetWindowLongW", ctypes.c_long, [w.HWND, ctypes.c_int])
_proto(user32, "GetWindowRect", w.BOOL, [w.HWND, ctypes.POINTER(w.RECT)])
_proto(user32, "IsWindow", w.BOOL, [w.HWND])
_proto(user32, "IsWindowVisible", w.BOOL, [w.HWND])
_proto(user32, "EnumWindows", w.BOOL, [WNDENUMPROC, w.LPARAM])
_proto(user32, "EnumChildWindows", w.BOOL, [w.HWND, WNDENUMPROC, w.LPARAM])
_proto(user32, "SetWindowPos", w.BOOL, [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        w.UINT])
_proto(user32, "ShowWindow", w.BOOL, [w.HWND, ctypes.c_int])
_proto(user32, "SetLayeredWindowAttributes", w.BOOL, [w.HWND, w.DWORD, ctypes.c_ubyte, w.DWORD])
_proto(user32, "ScreenToClient", w.BOOL, [w.HWND, ctypes.POINTER(POINT)])
_proto(user32, "SendMessageTimeoutW", LRESULT, [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
                                                ctypes.POINTER(ULONG_PTR)])
_proto(user32, "PostMessageW", w.BOOL, [w.HWND, w.UINT, w.WPARAM, w.LPARAM])
_proto(user32, "PostThreadMessageW", w.BOOL, [w.DWORD, w.UINT, w.WPARAM, w.LPARAM])
_proto(user32, "SetTimer", ULONG_PTR, [w.HWND, ULONG_PTR, w.UINT, ctypes.c_void_p])
_proto(user32, "GetMessageTime", ctypes.c_long)
_proto(user32, "GetAsyncKeyState", ctypes.c_short)
_proto(user32, "SendInput", w.UINT, [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int])
_proto(user32, "SetCursorPos", w.BOOL, [ctypes.c_int, ctypes.c_int])
_proto(user32, "LoadCursorW", w.HANDLE, [w.HINSTANCE, ctypes.c_void_p])
_proto(user32, "GetIconInfo", w.BOOL, [w.HANDLE, ctypes.POINTER(ICONINFO)])
_proto(kernel32, "GetModuleHandleW", w.HMODULE)
_proto(kernel32, "OpenProcess", w.HANDLE)
_proto(kernel32, "CreateMutexW", w.HANDLE)
_proto(kernel32, "GetCurrentThread", w.HANDLE)
_proto(kernel32, "GetCurrentProcess", w.HANDLE)
_proto(kernel32, "SetThreadPriority", w.BOOL, [w.HANDLE, ctypes.c_int])
_proto(kernel32, "SetPriorityClass", w.BOOL, [w.HANDLE, w.DWORD])
_proto(gdi32, "CreateSolidBrush", w.HBRUSH, [w.DWORD])
_proto(gdi32, "DeleteObject", w.BOOL, [w.HANDLE])
_proto(gdi32, "GetObjectW", ctypes.c_int, [w.HANDLE, ctypes.c_int, ctypes.c_void_p])
_proto(gdi32, "GetBitmapBits", ctypes.c_long, [w.HANDLE, ctypes.c_long, ctypes.c_void_p])

# --- windows -------------------------------------------------------------------


def window_text(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def window_pid(hwnd):
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def has_caption(hwnd):
    return bool(user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CAPTION)


def window_rect(hwnd):
    """The window's screen rectangle, or None."""
    r = w.RECT()
    return r if user32.GetWindowRect(hwnd, ctypes.byref(r)) else None


def contains(rect, pt):
    return rect is not None and rect.left <= pt.x < rect.right and rect.top <= pt.y < rect.bottom


def find_windows(match, parent=None, first=False):
    """The top-level windows (topmost first), or parent's descendants, for which match(hwnd) is true.

    With first=True, stop at the first one and return it (or None).
    """
    found = []

    def check(hwnd, _):
        if match(hwnd):
            found.append(hwnd)
            return not first
        return True

    proc = WNDENUMPROC(check)
    if parent:
        user32.EnumChildWindows(parent, proc, 0)
    else:
        user32.EnumWindows(proc, 0)
    return (found[0] if found else None) if first else found


def create_window(name, wndproc, ex_style=0, style=0, parent=None, brush=None):
    """Register a window class with a Python wndproc (kept alive by the caller) and create one window."""
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName=name, hbrBackground=brush)
    user32.RegisterClassW(ctypes.byref(wc))
    return user32.CreateWindowExW(ex_style, name, "", style, 0, 0, 0, 0, parent, None, hinst, None)


def send_message_timeout(hwnd, msg, wparam, lparam, timeout_ms=300):
    """SendMessage that gives up if the window's thread is busy or hung. The result, or None."""
    result = ULONG_PTR()
    if not user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms,
                                      ctypes.byref(result)):
        return None
    return ctypes.c_ssize_t(result.value).value


def message_loop(on_message=None):
    """Run this thread's message loop, calling on_message(msg) for each message first."""
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if on_message:
            on_message(msg)
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# --- input -----------------------------------------------------------------------


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def modifiers_held():
    return key_down(VK_SHIFT) or key_down(VK_CONTROL) or key_down(VK_MENU)


def cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt


def key_input(vk, up=False):
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), KEYEVENTF_KEYUP if up else 0, 0, INJECTED_TAG)
    return i


def mouse_input(flags, data=0):
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, INJECTED_TAG)
    return i


def wheel_input(delta, horizontal=False):
    return mouse_input(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, delta)


def send_input(inputs):
    """Inject input now. Not from inside the mouse hook: use send_input_later() there."""
    user32.SendInput(len(inputs), (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT))


_outbox = queue.Queue()


def send_input_later(inputs):
    """Have the input_sender() thread inject input."""
    _outbox.put(inputs)


def input_sender():
    """Thread: inject what send_input_later() queued."""
    while True:
        send_input(_outbox.get())
