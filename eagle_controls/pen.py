"""The pen's side button while its tip is on the screen.

Windows turns a pen into mouse input: the tip is the left button, and the
side (barrel) button held while tapping is a right click. With the tip
already down, pressing the side button makes no mouse input at all, so the
mouse hook never sees a right click while a part is dragged with the pen.
Eagle reads the pen itself and treats that as a right click; so does this,
from the pen's HID reports (raw input, HID digitizer page).
"""

import ctypes
from ctypes import wintypes as w

from touchpad import HIDP_CAPS, HIDP_STATUS_SUCCESS, HidP_Input, PAGE_DIGITIZER, USAGE_TIP, hid
from winapi import RIDI_PREPARSEDDATA, user32

USAGE_DIGITIZER, USAGE_PEN, USAGE_BARREL = 0x01, 0x02, 0x44
PEN_USAGES = (USAGE_DIGITIZER, USAGE_PEN)  # top-level collections pens report in


class Pen:
    """Watches pens; on_side_button() is called when a side button goes down with the tip on the screen."""

    def __init__(self, on_side_button, log):
        self.on_side_button = on_side_button
        self.log = log
        self.devices = {}  # hDevice -> preparsed data if it's a pen, else None
        self.barrel = {}   # hDevice -> whether its side button was down in the last report

    def is_pen(self, hdevice):
        """Whether this HID device (raw input handle) is a pen. Looked up once per device."""
        if hdevice not in self.devices:
            self.devices[hdevice] = None
            size = w.UINT(0)
            user32.GetRawInputDeviceInfoW(hdevice, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
            preparsed = ctypes.create_string_buffer(max(size.value, 1))
            caps = HIDP_CAPS()
            if (user32.GetRawInputDeviceInfoW(hdevice, RIDI_PREPARSEDDATA, preparsed, ctypes.byref(size))
                    not in (0, 0xFFFFFFFF)
                    and hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) == HIDP_STATUS_SUCCESS
                    and caps.UsagePage == PAGE_DIGITIZER and caps.Usage in PEN_USAGES):
                self.devices[hdevice] = preparsed
                self.log("pen found")
        return self.devices[hdevice] is not None

    def feed(self, hdevice, data):
        """data: the RAWHID part of a RAWINPUT (dwSizeHid, dwCount, reports...) from a pen."""
        preparsed = self.devices.get(hdevice)
        if preparsed is None or len(data) < 8:
            return
        size = int.from_bytes(data[0:4], "little")
        count = int.from_bytes(data[4:8], "little")
        for i in range(count):
            report = data[8 + i * size:8 + (i + 1) * size]
            usages = (w.USHORT * 32)()
            n = w.ULONG(32)
            if len(report) != size or hid.HidP_GetUsages(HidP_Input, PAGE_DIGITIZER, 0, usages, ctypes.byref(n),
                                                         preparsed, report, len(report)) != HIDP_STATUS_SUCCESS:
                continue
            down = set(usages[:n.value])
            barrel = USAGE_BARREL in down
            if barrel and not self.barrel.get(hdevice) and USAGE_TIP in down:
                self.on_side_button()
            self.barrel[hdevice] = barrel
