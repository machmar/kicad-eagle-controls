"""KiCad plugin: Eagle-style mouse and touchpad controls.

KiCad imports this when the PCB editor starts. It launches daemon.py as a
separate helper process (see that file for what it does), which exits by
itself when KiCad closes.
"""

import json
import math
import os
import subprocess
import sys

import pcbnew

from . import settings

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "daemon.py")
TRACKPAD_LAYOUT = {"scroll_modifier_zoom": 308, "scroll_modifier_pan_v": 0, "horizontal_pan": True}
TITLE = "Eagle-style controls"
EXIT_DUPLICATE = 3  # daemon.py's exit code when another helper serves this KiCad


def _python():
    """KiCad's bundled pythonw.exe (sys.executable is often kicad.exe itself)."""
    dirs = [os.path.dirname(sys.executable), sys.prefix, os.path.join(sys.prefix, "bin")]
    for name in ("pythonw.exe", "python.exe"):
        for d in dirs:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def _common_json():
    try:
        return os.path.join(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath(), "kicad_common.json")
    except Exception:
        return None


helper = None


def start_helper():
    """Start the helper unless it's already running. Returns False if it can't run here."""
    global helper
    if sys.platform != "win32":
        return False
    if helper and helper.poll() in (None, EXIT_DUPLICATE):
        return True
    python = _python()
    if not python:
        return False
    # The helper also refuses to start twice for the same KiCad process.
    args = [python, "-I", DAEMON, str(os.getpid())]
    if _common_json():
        args.append(_common_json())
    helper = subprocess.Popen(args, cwd=HERE, close_fds=True,
                              creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
    return True


def _keep_helper_alive():
    """Restart the helper if it dies, for as long as KiCad runs."""
    import wx

    class Watchdog(wx.Timer):
        def Notify(self):
            start_helper()

    _keep_helper_alive.timer = Watchdog()
    _keep_helper_alive.timer.Start(5000)


def _trackpad_layout():
    try:
        with open(_common_json(), encoding="utf-8") as f:
            inp = json.load(f).get("input", {})
        return all(inp.get(k) == v for k, v in TRACKPAD_LAYOUT.items())
    except (OSError, ValueError, TypeError):
        return True  # can't tell; don't nag


def ask_for_trackpad_layout(force=False):
    """Explain the one KiCad setting this plugin needs, unless it's set or the user opted out."""
    import wx
    if _trackpad_layout() or (not force and settings.load()["dont_ask_layout"]):
        return
    dlg = wx.RichMessageDialog(
        None,
        "Swipe-to-pan and pinch-to-zoom need KiCad's trackpad scroll layout.\n\n"
        "Open Preferences → Preferences… → Common → Mouse and Touchpad, "
        "click “Reset to Trackpad Defaults” and press OK.\n\n"
        "The mouse wheel will still zoom. Right click to rotate and middle click "
        "to switch layers work either way.",
        TITLE, wx.OK | wx.ICON_INFORMATION)
    dlg.ShowCheckBox("Don't show this again")
    dlg.ShowModal()
    if dlg.IsCheckBoxChecked():
        values = settings.load()
        values["dont_ask_layout"] = True
        try:
            settings.save(values)
        except OSError:
            pass
    dlg.Destroy()


def show_settings():
    """Sensitivity and acceleration sliders; the helper picks changes up on the next swipe."""
    import wx

    values = settings.load()
    dlg = wx.Dialog(None, title=TITLE)
    sizer = wx.BoxSizer(wx.VERTICAL)
    grid = wx.FlexGridSizer(2, 3, 10, 10)
    grid.AddGrowableCol(1)

    # Sensitivity on a log scale: slider -200..200 -> 0.25x..4x.
    sens = wx.Slider(dlg, value=round(100 * math.log2(values["pan_sensitivity"])),
                     minValue=-200, maxValue=200, size=(260, -1))
    sens_label = wx.StaticText(dlg, size=(50, -1))
    accel = wx.Slider(dlg, value=round(100 * values["pan_acceleration"]), minValue=0, maxValue=100)
    accel_label = wx.StaticText(dlg, size=(50, -1))

    def update(_=None):
        sens_label.SetLabel("%.2f×" % 2 ** (sens.GetValue() / 100))
        accel_label.SetLabel("%d %%" % accel.GetValue() if accel.GetValue() else "off")

    for label, slider, value in (("Pan sensitivity", sens, sens_label), ("Pan acceleration", accel, accel_label)):
        grid.Add(wx.StaticText(dlg, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(slider, 1, wx.EXPAND)
        grid.Add(value, 0, wx.ALIGN_CENTER_VERTICAL)
        slider.Bind(wx.EVT_SLIDER, update)
    update()

    sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 12)
    rotate = wx.CheckBox(dlg, label="Right click always rotates (Shift+right click opens the menu)")
    rotate.SetValue(values["right_click_rotates_always"])
    rotate.SetToolTip("Off: right click rotates only while you drag a part with the left button.")
    sizer.Add(rotate, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
    sizer.Add(wx.StaticText(dlg, label="Touchpad panning speed follows your Windows touchpad settings;\n"
                                       "sensitivity scales it. With acceleration, slow swipes pan finer\n"
                                       "and fast swipes pan further. Mouse-wheel zoom speed is set in\n"
                                       "KiCad's Preferences → Mouse and Touchpad."),
              0, wx.LEFT | wx.RIGHT, 12)
    if not _trackpad_layout():
        warn = wx.StaticText(dlg, label="KiCad isn't using the trackpad scroll layout, so swipe and\n"
                                        "wheel remapping are off. See Preferences → Mouse and Touchpad\n"
                                        "→ Reset to Trackpad Defaults.")
        warn.SetForegroundColour(wx.Colour(200, 60, 0))
        sizer.Add(warn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

    buttons = wx.StdDialogButtonSizer()
    reset = wx.Button(dlg, wx.ID_DEFAULT, "Defaults")
    reset.Bind(wx.EVT_BUTTON, lambda _: (sens.SetValue(0), accel.SetValue(0), rotate.SetValue(True), update()))
    buttons.Add(reset)
    buttons.AddButton(wx.Button(dlg, wx.ID_OK))
    buttons.AddButton(wx.Button(dlg, wx.ID_CANCEL))
    buttons.Realize()
    sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
    dlg.SetSizerAndFit(sizer)
    dlg.CentreOnScreen()

    if dlg.ShowModal() == wx.ID_OK:
        values["pan_sensitivity"] = 2 ** (sens.GetValue() / 100)
        values["pan_acceleration"] = accel.GetValue() / 100
        values["right_click_rotates_always"] = rotate.GetValue()
        try:
            settings.save(values)
        except OSError as e:
            wx.MessageBox("Could not save settings: %s" % e, TITLE, wx.ICON_ERROR)
    dlg.Destroy()


class EagleControls(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = TITLE
        self.category = "Navigation"
        self.description = ("Right click rotates, middle click switches layer, "
                            "mouse wheel zooms, touchpad swipe pans and pinch zooms. "
                            "Runs automatically; this menu entry opens its settings.")
        self.show_toolbar_button = False
        self.icon_file_name = os.path.join(HERE, "icon.png")

    def Run(self):
        import wx
        if not start_helper():
            wx.MessageBox("Could not start the helper (Windows only, needs KiCad's bundled Python).",
                          TITLE, wx.ICON_ERROR)
            return
        show_settings()


if start_helper():
    try:
        import wx
        wx.CallAfter(_keep_helper_alive)
        wx.CallAfter(ask_for_trackpad_layout)
    except Exception:
        pass
EagleControls().register()
