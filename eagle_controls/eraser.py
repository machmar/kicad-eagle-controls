"""Deleting with a tap of the pen's eraser end (pen_eraser_deletes); daemon.Mouse decides what's a tap.

KiCad's Delete deletes the selection, or with nothing selected, what's under
the cursor. Its other tools (the router, the wire tool, the drawing tools...)
pass a Delete they don't use on to it, and have nothing selected while idle.
So in another tool the tap only presses Delete, and the tool stays active.
Except while a track is being routed: then the router takes Delete for
"undo the last segment". KiCad doesn't say whether one is, so this guesses
from what the user does (drawing): with the pencil cursor, a click starts a
track or a wire, and Esc, End, a double-click or a click off the canvas ends
it. While one is being drawn the tap presses Esc first, which drops what
follows the cursor and keeps the tool. (If the guess is wrong, the Esc leaves
the idle tool for the selection tool, and the Delete still deletes.)
In the selection tool something else may be selected: there the tap is a
click, which selects what's there, and then Delete.

Where several things overlap, KiCad opens its "Clarify Selection" menu. After
a Delete, KiCad deletes what's picked in it by itself. After a click, it
swallows the Delete that follows; so for a moment after such a tap this
watches for a KiCad menu to open, and if one does, waits until it closes and,
if an item was picked (clicked, or chosen with Enter or its number key),
presses Delete again. Cancelling the menu deletes nothing.
"""

import queue
import time

import kicad
from logfile import log
from winapi import (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, VK_ESCAPE, WM_LBUTTONDOWN, WM_LBUTTONUP,
                    WM_MBUTTONDOWN, WM_RBUTTONDOWN, WM_RBUTTONUP, class_name, contains, find_windows, key_input,
                    mouse_input, send_input_later, user32, window_rect)

MENU_CLASS = "#32768"  # Windows' popup menus
MENU_WAIT_S = 1.0      # a menu opening this soon after the tap is its Clarify Selection menu...
POLL_S = 0.015
MENU_MAX_S = 120       # ...and is given up on if it's still open after this long
VK_RETURN, VK_END = 0x0D, 0x23
# Keys that choose an item: Enter, and the menu's own 1-9 and "Select All" (A), also on the number pad.
PICK_KEYS = {VK_RETURN, 0x41} | set(range(0x31, 0x3A)) | set(range(0x61, 0x6A))
CLICKS = (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP)


def kicad_menu():
    """A popup menu KiCad is showing, or None."""
    return find_windows(lambda h: class_name(h) == MENU_CLASS and user32.IsWindowVisible(h) and kicad.is_kicad(h),
                        first=True)


class EraserTap:
    """Runs its menu watch on its own thread: the hook mustn't wait on KiCad."""

    def __init__(self):
        self._taps = queue.Queue()
        self._watch_until = 0.0  # a KiCad menu opening before then is the tap's
        self.menu = None         # that menu's rectangle while it's open
        self.picked = False      # whether the user has chosen an item in it
        self.drawing = False     # whether a track or wire is probably being drawn (see above)

    def tap(self):
        """The eraser tapped the canvas where the cursor is (hook thread)."""
        self._watch_until = time.perf_counter() + MENU_WAIT_S
        self._taps.put(None)

    def left_press(self, on_canvas, pencil, double):
        """The user pressed the left button (hook thread): on an editor's canvas or not, with KiCad
        showing the pencil cursor (and no part in hand), as the second press of a double-click."""
        self.drawing = on_canvas and pencil and not double

    def button(self, msg, pt):
        """A mouse button message the user made (hook thread)."""
        if self.menu:
            if msg in CLICKS:
                self.picked = contains(self.menu, pt)  # a click outside the menu closes it
        elif msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            self._watch_until = 0.0  # a click of the user's own: a menu opening now is theirs

    def key_pressed(self, vk):
        """A key went down (raw input thread)."""
        if vk in (VK_ESCAPE, VK_END):  # cancel or finish the track or wire
            self.drawing = False
        if self.menu:
            if vk == VK_ESCAPE:
                self.picked = False
            elif vk in PICK_KEYS:
                self.picked = True

    def run(self):
        """Thread: erase at each tap, then watch for its Clarify Selection menu."""
        while True:
            self._taps.get()
            try:
                if self._erase():
                    self._watch()
            except Exception as e:
                log("eraser menu watch error: %r" % e)
                self.menu = None

    def _erase(self):
        """Delete at the tap. Returns whether it clicked: then a Clarify Selection menu needs watching."""
        if kicad.selection_tool():
            send_input_later([mouse_input(MOUSEEVENTF_LEFTDOWN), mouse_input(MOUSEEVENTF_LEFTUP)])
            kicad.press("delete", "pen eraser tap")
            return True
        # Without a click first, the key goes wherever the keyboard focus is. KiCad's
        # canvas takes it when the pointer comes over it, unless KiCad is in the background.
        if kicad.focused_canvas():
            if self.drawing:
                send_input_later([key_input(VK_ESCAPE), key_input(VK_ESCAPE, up=True)])
                self.drawing = False
            kicad.press("delete", "pen eraser tap in a tool")
        else:
            log("pen eraser tap in a tool, but KiCad's canvas doesn't have the keyboard focus")
        return False

    def _watch(self):
        menu = None
        while not menu and time.perf_counter() < self._watch_until:
            time.sleep(POLL_S)
            menu = kicad_menu()
        if not menu or time.perf_counter() >= self._watch_until:
            return  # no menu, or the user clicked before it opened: then it's theirs
        self.picked, self.menu = False, window_rect(menu)
        log("eraser tap opened KiCad's selection menu")
        give_up = time.perf_counter() + MENU_MAX_S
        while user32.IsWindowVisible(menu) and time.perf_counter() < give_up:
            time.sleep(POLL_S)
        picked, self.menu = self.picked, None
        if picked:
            kicad.press("delete", "pick in the selection menu after an eraser tap")
