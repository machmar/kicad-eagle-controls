"""Eagle-style via placement while routing.

A middle click opens KiCad's own "Select Layer and Place Through Via" chooser
(always a through via, never blind/buried). KiCad's chooser only reacts to
clicks on its rows, so we draw our own highlight over it: further middle
clicks move the highlight one copper layer down, and a left click anywhere
clicks the highlighted row for the user. Esc still cancels it as usual.

The copper layers and the active layer are read from the layer drop-down in
KiCad's toolbar; the chooser lists the same copper layers, top to bottom,
one row each.
"""

import ctypes
import time
from ctypes import wintypes as w

import kicad
import settings
from logfile import DEBUG, log
from winapi import (CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN, HWND_TOPMOST, LWA_ALPHA, MK_LBUTTON,
                    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, POINT, SW_HIDE, SWP_NOACTIVATE, SWP_SHOWWINDOW,
                    WM_APP, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_TIMER, WNDPROC, WS_EX_LAYERED, WS_EX_NOACTIVATE,
                    WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP, class_name, create_window,
                    cursor_pos, find_windows, gdi32, has_caption, kernel32, message_loop, mouse_input, send_input,
                    send_message_timeout, user32, window_rect, window_text)

# Requests to the chooser's thread (see LayerChooser.request).
OPEN, STEP, PICK = WM_APP + 0x11, WM_APP + 0x12, WM_APP + 0x13
APPEAR_S = 1.5                 # wait this long for KiCad's chooser to appear
HIGHLIGHT_RGB = (0, 120, 215)
# Fixed names of KiCad's technical layers; the copper layers come before them.
TECHNICAL_LAYERS = {"F.Adhesive", "B.Adhesive", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen",
                    "F.Mask", "B.Mask", "Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User",
                    "Edge.Cuts", "Margin", "F.Courtyard", "B.Courtyard", "F.Fab", "B.Fab"}


def read_copper_layers():
    """(copper layer names top to bottom, index of the active one or None) from the toolbar, or None."""
    for frame in kicad.windows():
        if kicad.editor(frame) != "board":
            continue
        for combo in find_windows(lambda h: class_name(h) == "ComboBox", parent=frame):
            count = send_message_timeout(combo, CB_GETCOUNT, 0, 0)
            if not count or count <= 0 or count > 200:
                continue
            names = []
            for i in range(count):
                length = send_message_timeout(combo, CB_GETLBTEXTLEN, i, 0)
                if length is None or length < 0:
                    break
                buf = ctypes.create_unicode_buffer(length + 2)
                send_message_timeout(combo, CB_GETLBTEXT, i, ctypes.addressof(buf))
                names.append(buf.value.split(" (")[0])  # "F.Cu (PgUp)" -> "F.Cu"
            technical = [i for i, n in enumerate(names) if n in TECHNICAL_LAYERS]
            if not technical or technical[0] < 2:
                continue  # not the layer selector
            copper = names[:technical[0]]
            active = send_message_timeout(combo, CB_GETCURSEL, 0, 0)
            return copper, active if active is not None and 0 <= active < len(copper) else None
    return None


def find_layer_chooser():
    """(dialog, grid window) of KiCad's open layer chooser, or None."""
    for top in kicad.windows():
        if has_caption(top) and kicad.editor(top) is None:
            grids = find_windows(lambda h: window_text(h) == "GridWindow" and user32.IsWindowVisible(h), parent=top)
            if len(grids) == 1:
                return top, grids[0]
    return None


def left_click():
    """A left click at the cursor."""
    send_input([mouse_input(MOUSEEVENTF_LEFTDOWN), mouse_input(MOUSEEVENTF_LEFTUP)])


class LayerChooser:
    """Runs on its own thread; the hook sends it requests and reads .shown."""

    def __init__(self):
        self.shown = False      # our highlight is over KiCad's chooser
        self.thread_id = None
        self.overlay = None
        self.dialog = self.grid = None
        self.names = []         # copper layers, top to bottom: the chooser's rows
        self.active = None      # index of the active layer, which the chooser skips
        self.index = 0          # the highlighted row

    def request(self, msg):
        """Ask the chooser's thread to OPEN KiCad's chooser, STEP the highlight, or PICK its row."""
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, msg, 0, 0)

    def run(self):
        """Thread: handle requests, and notice when KiCad's chooser closes."""
        self.thread_id = kernel32.GetCurrentThreadId()
        self.overlay = self._make_overlay()
        user32.SetTimer(None, 0, 100, None)

        def on_message(msg):
            try:
                if msg.message == OPEN:
                    self._open()
                elif msg.message == STEP:
                    self._step()
                elif msg.message == PICK:
                    self._pick()
                elif msg.message == WM_TIMER and self.shown and not (
                        user32.IsWindow(self.dialog) and user32.IsWindowVisible(self.dialog)):
                    self._close()
            except Exception as e:
                log("layer chooser error: %r" % e)
                self._close()

        message_loop(on_message)

    def _make_overlay(self):
        self.wndproc = WNDPROC(lambda h, m, wp, lp: user32.DefWindowProcW(h, m, wp, lp))  # keep a reference
        r, g, b = HIGHLIGHT_RGB
        hwnd = create_window("KiCadEagleControlsHighlight", self.wndproc,
                             ex_style=WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                             | WS_EX_NOACTIVATE,
                             style=WS_POPUP, brush=gdi32.CreateSolidBrush(r | g << 8 | b << 16))
        user32.SetLayeredWindowAttributes(hwnd, 0, 110, LWA_ALPHA)
        return hwnd

    def _open(self):
        layers = read_copper_layers()
        kicad.press("via_chooser", "middle click while routing")
        if not layers:
            log("layer chooser: can't read the copper layers; no highlight")
            return
        self.names, self.active = layers
        deadline = time.perf_counter() + APPEAR_S
        found = None
        while not found and time.perf_counter() < deadline:
            time.sleep(0.03)
            found = find_layer_chooser()
        if not found:
            log("layer chooser didn't appear")
            return
        self.dialog, self.grid = found
        self.index = self.active if self.active is not None else -1
        self.shown = True
        self._step()  # start on the next layer down from the active one
        if DEBUG:
            log("layer chooser open: %s, active %s" % (self.names, self.active))

    def _row_rect(self):
        """(x, y, width, height) of the highlighted row on screen."""
        r = window_rect(self.grid) or w.RECT()
        row_h = (r.bottom - r.top) / len(self.names)
        top = round(r.top + self.index * row_h)
        return r.left, top, r.right - r.left, round(r.top + (self.index + 1) * row_h) - top

    def _step(self):
        if not self.shown:
            return
        for _ in self.names:  # the next row down that isn't the active layer
            self.index = (self.index + 1) % len(self.names)
            if self.index != self.active:
                break
        x, y, width, height = self._row_rect()
        user32.SetWindowPos(self.overlay, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _pick(self):
        if not self.shown:
            return
        x, y, width, height = self._row_rect()
        pt = POINT(x + width // 2, y + height // 2)
        if DEBUG:
            log("layer chooser: picking %s" % self.names[self.index])
        user32.ShowWindow(self.overlay, SW_HIDE)
        client = POINT(pt.x, pt.y)
        user32.ScreenToClient(self.grid, ctypes.byref(client))
        lparam = (client.y & 0xFFFF) << 16 | (client.x & 0xFFFF)
        user32.PostMessageW(self.grid, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        user32.PostMessageW(self.grid, WM_LBUTTONUP, 0, lparam)
        deadline = time.perf_counter() + 0.4
        while time.perf_counter() < deadline and user32.IsWindowVisible(self.dialog):
            time.sleep(0.02)
        if user32.IsWindowVisible(self.dialog):
            # The grid ignored posted clicks: click the row for real, then put the cursor back.
            back = cursor_pos()
            user32.SetCursorPos(pt.x, pt.y)
            left_click()
            time.sleep(0.05)
            user32.SetCursorPos(back.x, back.y)
        picked = not user32.IsWindowVisible(self.dialog)
        self._close()
        if picked and settings.prefs.value["chooser_places_via"]:
            # KiCad now has the via on the end of the trace; the click that
            # confirmed the layer also places it, where the cursor is.
            time.sleep(0.1)
            left_click()

    def _close(self):
        self.shown = False
        if self.overlay:
            user32.ShowWindow(self.overlay, SW_HIDE)
