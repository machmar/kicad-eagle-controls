"""Reads finger contacts from Windows Precision Touchpads via raw input.

Windows turns two-finger touchpad swipes into wheel events itself, but it
locks them to one axis ("scroll rails"), which makes diagonal panning
nearly impossible. Reading the contacts lets daemon.py pan along both axes.

Every Precision Touchpad reports contacts the same way (HID digitizer page,
usage 0x05), so this works without per-device code: the report layout is
read from the device's HID descriptor with the HidP_* parser.
"""

import ctypes
from ctypes import wintypes as w

hid = ctypes.WinDLL("hid")
user32 = ctypes.WinDLL("user32", use_last_error=True)

PAGE_GENERIC, PAGE_DIGITIZER = 0x01, 0x0D
USAGE_X, USAGE_Y = 0x30, 0x31
USAGE_TOUCHPAD, USAGE_TIP, USAGE_CONTACT_ID, USAGE_CONTACT_COUNT = 0x05, 0x42, 0x51, 0x54
HidP_Input = 0
HIDP_STATUS_SUCCESS = 0x00110000
RIDI_PREPARSEDDATA = 0x20000005
RIM_TYPEHID = 2


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", w.USHORT), ("UsagePage", w.USHORT), ("InputReportByteLength", w.USHORT),
                ("OutputReportByteLength", w.USHORT), ("FeatureReportByteLength", w.USHORT),
                ("Reserved", w.USHORT * 17), ("NumberLinkCollectionNodes", w.USHORT),
                ("NumberInputButtonCaps", w.USHORT), ("NumberInputValueCaps", w.USHORT),
                ("NumberInputDataIndices", w.USHORT), ("NumberOutputButtonCaps", w.USHORT),
                ("NumberOutputValueCaps", w.USHORT), ("NumberOutputDataIndices", w.USHORT),
                ("NumberFeatureButtonCaps", w.USHORT), ("NumberFeatureValueCaps", w.USHORT),
                ("NumberFeatureDataIndices", w.USHORT)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", w.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", w.USHORT), ("LinkCollection", w.USHORT), ("LinkUsage", w.USHORT),
                ("LinkUsagePage", w.USHORT), ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("HasNull", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte), ("BitSize", w.USHORT),
                ("ReportCount", w.USHORT), ("Reserved2", w.USHORT * 5), ("UnitsExp", w.ULONG),
                ("Units", w.ULONG), ("LogicalMin", ctypes.c_long), ("LogicalMax", ctypes.c_long),
                ("PhysicalMin", ctypes.c_long), ("PhysicalMax", ctypes.c_long),
                ("Usage", w.USHORT), ("Reserved3", w.USHORT * 7)]  # NotRange.Usage is first in the union


hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = ctypes.c_ulong
hid.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.POINTER(HIDP_VALUE_CAPS), ctypes.POINTER(w.USHORT), ctypes.c_void_p]
hid.HidP_GetValueCaps.restype = ctypes.c_ulong
hid.HidP_GetUsageValue.argtypes = [ctypes.c_int, w.USHORT, w.USHORT, w.USHORT, ctypes.POINTER(w.ULONG),
                                   ctypes.c_void_p, ctypes.c_char_p, w.ULONG]
hid.HidP_GetUsageValue.restype = ctypes.c_ulong
hid.HidP_GetUsages.argtypes = [ctypes.c_int, w.USHORT, w.USHORT, ctypes.POINTER(w.USHORT), ctypes.POINTER(w.ULONG),
                               ctypes.c_void_p, ctypes.c_char_p, w.ULONG]
hid.HidP_GetUsages.restype = ctypes.c_ulong
user32.GetRawInputDeviceInfoW.argtypes = [w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT)]
user32.GetRawInputDeviceInfoW.restype = w.UINT


def _mm_per_unit(cap, fallback_mm):
    """Size of one logical unit in mm, from the descriptor's physical range."""
    logical = cap.LogicalMax - cap.LogicalMin
    physical = cap.PhysicalMax - cap.PhysicalMin
    if logical <= 0:
        return 0.0
    if physical <= 0:
        return fallback_mm / logical
    exp = cap.UnitsExp & 0xF
    exp = exp - 16 if exp > 7 else exp
    unit = cap.Units & 0xFFFF
    per_unit = 25.4 if unit == 0x13 else 10.0  # inch or cm (0x11)
    return physical * (10.0 ** exp) * per_unit / logical


class Device:
    """Report layout of one touchpad."""

    def __init__(self, hdevice):
        size = w.UINT(0)
        user32.GetRawInputDeviceInfoW(hdevice, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
        self.preparsed = ctypes.create_string_buffer(max(size.value, 1))
        if user32.GetRawInputDeviceInfoW(hdevice, RIDI_PREPARSEDDATA, self.preparsed, ctypes.byref(size)) in (0, 0xFFFFFFFF):
            raise OSError("no preparsed data")
        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(self.preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            raise OSError("HidP_GetCaps failed")
        n = w.USHORT(caps.NumberInputValueCaps)
        vcaps = (HIDP_VALUE_CAPS * max(n.value, 1))()
        if hid.HidP_GetValueCaps(HidP_Input, vcaps, ctypes.byref(n), self.preparsed) != HIDP_STATUS_SUCCESS:
            raise OSError("HidP_GetValueCaps failed")

        xs, ys = {}, {}
        self.count_collection = None
        for cap in vcaps[:n.value]:
            if cap.IsRange:
                continue
            if cap.UsagePage == PAGE_GENERIC and cap.Usage == USAGE_X:
                xs[cap.LinkCollection] = cap
            elif cap.UsagePage == PAGE_GENERIC and cap.Usage == USAGE_Y:
                ys[cap.LinkCollection] = cap
            elif cap.UsagePage == PAGE_DIGITIZER and cap.Usage == USAGE_CONTACT_COUNT:
                self.count_collection = cap.LinkCollection
        # One link collection per finger slot.
        self.slots = sorted(set(xs) & set(ys))
        if not self.slots:
            raise OSError("no finger collections")
        cap_x, cap_y = xs[self.slots[0]], ys[self.slots[0]]
        self.mm_x = _mm_per_unit(cap_x, 100.0)
        self.mm_y = _mm_per_unit(cap_y, 65.0)
        self.prev = {}        # contact id -> (x_mm, y_mm), last complete frame
        self.last_pair = {}   # the last complete frame with exactly two fingers
        self.frame = {}       # contacts seen so far in the frame being assembled
        self.remaining = 0    # contacts still to come in this frame (hybrid reporting)

    def value(self, report, collection, page, usage):
        v = w.ULONG(0)
        ok = hid.HidP_GetUsageValue(HidP_Input, page, collection, usage, ctypes.byref(v),
                                    self.preparsed, report, len(report))
        return v.value if ok == HIDP_STATUS_SUCCESS else None

    def tip(self, report, collection):
        usages = (w.USHORT * 16)()
        n = w.ULONG(16)
        ok = hid.HidP_GetUsages(HidP_Input, PAGE_DIGITIZER, collection, usages, ctypes.byref(n),
                                self.preparsed, report, len(report))
        return ok == HIDP_STATUS_SUCCESS and USAGE_TIP in usages[:n.value]


def _dist(p, q):
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


class Touchpads:
    """Tracks fingers on all touchpads and reports two-finger motion.

    Call feed() with every WM_INPUT lParam that came from a touchpad.
    `fingers` is the number of fingers down. on_motion(dx_mm, dy_mm, dspread_mm)
    is called for each frame in which exactly two fingers moved: the average
    motion (+y is towards the user) and the change in distance between them.
    """

    def __init__(self, on_motion, on_fingers_changed, log):
        self.on_motion = on_motion
        self.on_fingers_changed = on_fingers_changed
        self.log = log
        self.devices = {}       # hDevice -> Device, or None if unusable
        self.fingers = 0

    def feed(self, hdevice, data):
        """data: the RAWHID part of a RAWINPUT (dwSizeHid, dwCount, reports...)."""
        dev = self.devices.get(hdevice, False)
        if dev is False:
            try:
                dev = Device(hdevice)
                self.log("touchpad found: %d finger slots, %.4f x %.4f mm/unit" % (len(dev.slots), dev.mm_x, dev.mm_y))
            except OSError as e:
                self.log("ignoring HID device: %s" % e)
                dev = None
            self.devices[hdevice] = dev
        if dev is None or len(data) < 8:
            return
        size = int.from_bytes(data[0:4], "little")
        count = int.from_bytes(data[4:8], "little")
        for i in range(count):
            report = data[8 + i * size:8 + (i + 1) * size]
            if len(report) == size:
                self._report(dev, report)

    def _report(self, dev, report):
        # A frame may be split over several reports: the first carries the
        # total contact count, the rest carry 0.
        total = dev.value(report, dev.count_collection, PAGE_DIGITIZER, USAGE_CONTACT_COUNT) \
            if dev.count_collection is not None else None
        if total is None:
            dev.frame, dev.remaining = {}, len(dev.slots)
        elif total > 0:
            dev.frame, dev.remaining = {}, total
        elif dev.remaining <= 0:
            return

        for slot in dev.slots[:dev.remaining]:
            cid = dev.value(report, slot, PAGE_DIGITIZER, USAGE_CONTACT_ID)
            x = dev.value(report, slot, PAGE_GENERIC, USAGE_X)
            y = dev.value(report, slot, PAGE_GENERIC, USAGE_Y)
            if cid is None or x is None or y is None:
                continue
            if dev.tip(report, slot):
                dev.frame[cid] = (x * dev.mm_x, y * dev.mm_y)
        dev.remaining -= min(dev.remaining, len(dev.slots))
        if dev.remaining <= 0 or total is None:
            self._finish_frame(dev)

    def _finish_frame(self, dev):
        now = dev.frame
        dev.prev, dev.frame = now, {}
        if len(now) != self.fingers:
            self.fingers = len(now)
            self.on_fingers_changed(self.fingers)
        # Compare with the last frame that had the same two fingers, so a frame
        # where the touchpad briefly reports one finger loses no movement.
        prev, dev.last_pair = dev.last_pair, (now if len(now) == 2 else dev.last_pair if now else {})
        if len(now) == 2 and set(now) == set(prev):
            dx = sum(now[c][0] - prev[c][0] for c in now) / 2
            dy = sum(now[c][1] - prev[c][1] for c in now) / 2
            if dx or dy:
                a, b = sorted(now)
                self.on_motion(dx, dy, _dist(now[a], now[b]) - _dist(prev[a], prev[b]))
