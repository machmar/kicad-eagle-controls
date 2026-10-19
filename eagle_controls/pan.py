"""Touchpad panning in any direction.

Windows turns two-finger swipes into wheel events locked to one axis. Over a
KiCad canvas we swallow those and pan from the actual finger motion (read by
touchpad.py), so diagonal swipes work. Whether a gesture is a pan or a pinch
is decided from the fingers themselves (moving together = pan, spreading =
pinch), so the pan starts right away and doesn't depend on Windows' events;
a pinch is left to Windows, which sends it as Ctrl+wheel (= zoom in KiCad).

The pan is sent to KiCad as wheel events, which its trackpad layout pans on.
How many wheel units a mm of finger travel is worth, sign included, is
learned from the events Windows sends, so speed and direction follow the
user's touchpad settings.
"""

import json
import os
import threading
import time
import winreg

import kicad
import settings
import touchpad
from logfile import DEBUG, log
from winapi import VK_CONTROL, cursor_pos, key_down, send_input, wheel_input

CALIBRATION = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__)),
                           "kicad_eagle_controls.json")
PAN_START_MM = 1.0         # finger travel before a gesture counts as a pan...
PINCH_RATIO = 0.6          # ...unless the fingers spread/closed by this fraction of it
MOMENTUM_S = 0.3           # keep swallowing Windows' momentum scrolling this long
DEFAULT_GAIN = 25.0        # wheel units per mm until calibrated
LEARN_MIN_MM = 15.0        # calibrate only from swipes at least this long
STALE_INPUT_S = 0.1        # touchpad reports that waited this long only update the finger count
PAN_INTERVAL_S = 1 / 60    # send at most one pan update per frame; every injected
                           # event passes through the hook, so flooding it adds lag
PAN_SPEED = 0.3            # at sensitivity 1x, pan this fraction of Windows' own scroll speed;
                           # matching it fully pans far too fast for board work
ACCEL_REF_MM_S = 30.0      # with acceleration on, finger speed that pans at 1x
ACCEL_RANGE = (0.25, 4.0)  # limits of the acceleration factor


def windows_scroll_sign():
    """+1 or -1 from Windows' touchpad "scrolling direction" setting, or None if unknown.

    0 is Windows' default, "down motion scrolls up" (content follows the fingers).
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad") as k:
            return 1 if winreg.QueryValueEx(k, "ScrollDirection")[0] == 0 else -1
    except OSError:
        return None


def _saved_gain():
    gain = DEFAULT_GAIN
    try:
        with open(CALIBRATION, encoding="utf-8") as f:
            gain = float(json.load(f)["gain"]) or DEFAULT_GAIN
    except (OSError, ValueError, KeyError, TypeError):
        pass
    sign = windows_scroll_sign()
    return abs(gain) * sign if sign else gain


class Pan:
    """Turns two-finger touchpad motion into pan steps for KiCad.

    feed() takes the touchpad's raw input (raw input thread), windows_wheel()
    Windows' scroll events (hook thread), and run() sends the pan (own thread).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.touchpads = touchpad.Touchpads(self._on_motion, self._on_fingers_changed, log)
        # Fingers moving down by 1 mm = this many units of vertical wheel;
        # horizontal wheel uses the negated value (Windows' sign convention).
        self.gain = _saved_gain()
        self.flip_votes = 0          # swipes in a row that suggested the other direction
        self.momentum_ours = False   # the last gesture was ours, so swallow its momentum scrolling
        self.swallow_until = 0.0     # swallow Windows' scrolling until then (read by the hook)
        self.stale = False           # the report being handled waited too long in our queue
        self.pending = threading.Event()
        self._new_gesture()

    def _new_gesture(self):
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

    @property
    def fingers(self):
        return self.touchpads.fingers

    def feed(self, hdevice, data, age_s):
        """A touchpad's raw input, which waited age_s in our message queue."""
        self.stale = age_s > STALE_INPUT_S
        self.touchpads.feed(hdevice, data)

    # --- the fingers (raw input thread)

    def _speed_factor(self, dist, prefs):
        now = time.perf_counter()
        if self.last_t is not None:
            dt = min(max(now - self.last_t, 0.004), 0.05)
            self.speed = 0.5 * self.speed + 0.5 * dist / dt
        self.last_t = now
        k = PAN_SPEED * prefs["pan_sensitivity"]
        if prefs["pan_acceleration"] <= 0:
            return k
        accel = (max(self.speed, 0.1) / ACCEL_REF_MM_S) ** prefs["pan_acceleration"]
        return k * min(max(accel, ACCEL_RANGE[0]), ACCEL_RANGE[1])

    def _on_motion(self, dx, dy, dspread):
        if self.stale:
            self.stats["stale"] += 1
            return  # we fell behind (e.g. a throttled CPU): don't replay old movement
        prefs = settings.prefs.value
        dist = (dx * dx + dy * dy) ** 0.5
        self.stats["last_motion"] = time.perf_counter()
        self.stats["motion_mm"] += dist
        with self.lock:
            k = self._speed_factor(dist, prefs)
            self.acc_dx += dx * k
            self.acc_dy += dy * k
            if self.windows_started:
                self.dx += dx
                self.dy += dy
            if self.state is None:
                self.travel += dist
                self.spread += dspread
                if abs(self.spread) > PINCH_RATIO * max(self.travel, PAN_START_MM):
                    self.state = "pinch"
                elif self.travel >= PAN_START_MM:
                    ours = (kicad.canvas_at(cursor_pos()) and kicad.trackpad_layout.value
                            and prefs["touchpad_free_pan"])
                    self.state = "pan" if ours else "ignore"
                    if DEBUG:
                        log("touchpad gesture: %s" % self.state)
            if self.state == "pan":
                self._flush()

    def _on_fingers_changed(self, n):
        self.stats["max_fingers"] = max(self.stats["max_fingers"], n)
        if n:
            # Fingers landing or lifting one at a time, or the touchpad briefly
            # losing one: the gesture goes on (restarting it caused stalls).
            self.momentum_ours = False  # fingers are down: no momentum scrolling now
            return
        with self.lock:
            if DEBUG:
                self._log_gesture()
            self._learn()
            self.momentum_ours = self.state == "pan"
            self._new_gesture()

    def _log_gesture(self):
        st, now = self.stats, time.perf_counter()
        if self.state == "pan":
            waits = st["wait"] or [0]
            log("pan: %d steps, KiCad wait avg %.0f / max %.0f ms, %d stale reports dropped, "
                "last step %.0f ms after last motion, fingers up %.0f ms after last step" % (
                    st["steps"], 1000 * sum(waits) / len(waits), 1000 * max(waits), st["stale"],
                    1000 * ((st["last_send"] or now) - (st["last_motion"] or now)),
                    1000 * (now - (st["last_send"] or now))))
        elif st["motion_mm"] > 0 or st["win_events"]:
            log("swipe without pan: state %s, most fingers %d, two-finger motion %.1f mm, "
                "%d Windows scroll events (%d passed to KiCad), %.0f ms long" % (
                    self.state, st["max_fingers"], st["motion_mm"], st["win_events"], st["passed"],
                    1000 * (now - st["start"])))

    # --- Windows' scrolling (hook thread)

    def windows_wheel(self, horizontal, delta, fingers):
        """A touchpad wheel event over a canvas. Returns True to swallow it."""
        with self.lock:
            self.stats["win_events"] += 1
            pinch = key_down(VK_CONTROL)  # Windows sends pinches with Ctrl held, for KiCad to zoom
            if pinch:
                self.state = "pinch"
            lost_finger = fingers != 2 and self.state != "pan" and not self.momentum_ours
            if pinch or self.state == "ignore" or lost_finger:
                # A pinch, a gesture that started outside the canvas, or one we
                # can't pan ourselves because the touchpad lost a finger: let
                # Windows' scrolling through.
                self.stats["passed"] += 1
                return False
            self._windows_scrolled(horizontal, delta, fingers)
            return True

    def _windows_scrolled(self, horizontal, delta, fingers):
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

    # --- sending the pan (own thread)

    def _flush(self):
        """Wake run() to send the accumulated motion."""
        if self.acc_dx or self.acc_dy:
            self.pending.set()

    def run(self):
        """Thread: turn accumulated finger motion into wheel events, at most once per PAN_INTERVAL_S."""
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
            events = [wheel_input(d, horizontal) for d, horizontal in ((iv, False), (ih, True)) if d]
            if events:
                send_input(events)
                started = time.perf_counter()
                kicad.wait_until_drawn()
                self.stats["steps"] += 1
                self.stats["wait"].append(time.perf_counter() - started)
                self.stats["last_send"] = started
            time.sleep(PAN_INTERVAL_S)

    # --- calibration

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
