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


CLICK_ACTIONS = [("rotate", "Rotate"), ("flip", "Flip to other side"), ("layer", "Switch layer"),
                 ("off", "Nothing")]
RIGHT_CLICK_ACTIONS = CLICK_ACTIONS[:3] + [("menu", "KiCad's context menu")]


def show_settings():
    """Every behaviour that differs from KiCad can be changed or turned off here.

    The helper re-reads the settings file, so changes apply right away.
    """
    import wx

    values = settings.load()
    dlg = wx.Dialog(None, title=TITLE)
    outer = wx.BoxSizer(wx.VERTICAL)

    def section(label):
        box = wx.StaticBoxSizer(wx.VERTICAL, dlg, label)
        outer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        grid = wx.FlexGridSizer(0, 3, 8, 10)
        grid.AddGrowableCol(1)
        return box, grid, box.GetStaticBox()  # controls in the box must be its children

    def choice(parent, grid, label, options, value):
        ctrl = wx.Choice(parent, choices=[text for _, text in options])
        keys = [key for key, _ in options]
        ctrl.SetSelection(keys.index(value) if value in keys else 0)
        grid.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(ctrl, 1, wx.EXPAND)
        grid.AddSpacer(0)
        return lambda: keys[ctrl.GetSelection()], lambda v: ctrl.SetSelection(keys.index(v))

    # --- Touchpad
    box, grid, pnl = section("Touchpad")
    free_pan = wx.CheckBox(pnl, label="Two-finger swipe pans in any direction (not locked to one axis)")
    free_pan.SetValue(values["touchpad_free_pan"])
    box.Add(free_pan, 0, wx.ALL, 6)
    # Sensitivity on a log scale: slider -200..200 -> 0.25x..4x.
    sens = wx.Slider(pnl, value=round(100 * math.log2(values["pan_sensitivity"])),
                     minValue=-200, maxValue=200, size=(240, -1))
    sens_label = wx.StaticText(pnl, size=(50, -1))
    accel = wx.Slider(pnl, value=round(100 * values["pan_acceleration"]), minValue=0, maxValue=100)
    accel_label = wx.StaticText(pnl, size=(50, -1))

    def update(_=None):
        sens_label.SetLabel("%.2f×" % 2 ** (sens.GetValue() / 100))
        accel_label.SetLabel("%d %%" % accel.GetValue() if accel.GetValue() else "off")
        sens.Enable(free_pan.GetValue())
        accel.Enable(free_pan.GetValue())

    for label, slider, value in (("Pan sensitivity", sens, sens_label), ("Pan acceleration", accel, accel_label)):
        grid.Add(wx.StaticText(pnl, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(slider, 1, wx.EXPAND)
        grid.Add(value, 0, wx.ALIGN_CENTER_VERTICAL)
        slider.Bind(wx.EVT_SLIDER, update)
    free_pan.Bind(wx.EVT_CHECKBOX, update)
    box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)

    # --- Mouse
    box, grid, pnl = section("Mouse")
    wheel = wx.CheckBox(pnl, label="Mouse wheel zooms (otherwise it pans)")
    wheel.SetValue(values["wheel_zoom"])
    box.Add(wheel, 0, wx.ALL, 6)
    right = choice(pnl, grid, "Right click", RIGHT_CLICK_ACTIONS, values["right_click"])
    right_drag = choice(pnl, grid, "Right click while dragging", CLICK_ACTIONS, values["right_click_drag"])
    middle = choice(pnl, grid, "Middle click", CLICK_ACTIONS, values["middle_click"])
    middle_drag = choice(pnl, grid, "Middle click while dragging", CLICK_ACTIONS, values["middle_click_drag"])
    box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
    box.Add(wx.StaticText(pnl, label="“Dragging” means holding the left button on a part. "
                                     "Shift + right click always\nopens KiCad's menu. "
                                     "Right- and middle-button drags still pan."),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    # --- Keys
    box, grid, pnl = section("KiCad hotkeys used")
    keys = {}
    for name, label in (("rotate", "Rotate"), ("flip", "Flip"), ("layer", "Switch layer")):
        keys[name] = wx.TextCtrl(pnl, value=values["key_" + name], size=(120, -1))
        grid.Add(wx.StaticText(pnl, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(keys[name], 0)
        grid.AddSpacer(0)
    box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
    box.Add(wx.StaticText(pnl, label="Only change these if you rebound them in KiCad "
                                     "(e.g. R, Shift+R, PgUp)."),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    # --- KiCad's scroll layout
    remind = wx.CheckBox(dlg, label="Remind me when KiCad isn't using the trackpad scroll layout")
    remind.SetValue(not values["dont_ask_layout"])
    outer.Add(remind, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
    if not _trackpad_layout():
        warn = wx.StaticText(dlg, label="KiCad isn't using the trackpad scroll layout, so the swipe and\n"
                                        "wheel settings have no effect. See Preferences → Mouse and\n"
                                        "Touchpad → Reset to Trackpad Defaults.")
        warn.SetForegroundColour(wx.Colour(200, 60, 0))
        outer.Add(warn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

    def defaults(_):
        d = settings.DEFAULTS
        free_pan.SetValue(d["touchpad_free_pan"])
        sens.SetValue(0)
        accel.SetValue(0)
        wheel.SetValue(d["wheel_zoom"])
        for (_, set_), key in ((right, "right_click"), (right_drag, "right_click_drag"),
                               (middle, "middle_click"), (middle_drag, "middle_click_drag")):
            set_(d[key])
        for name, ctrl in keys.items():
            ctrl.SetValue(d["key_" + name])
        remind.SetValue(True)
        update()

    buttons = wx.StdDialogButtonSizer()
    reset = wx.Button(dlg, wx.ID_DEFAULT, "Defaults")
    reset.Bind(wx.EVT_BUTTON, defaults)
    buttons.Add(reset)
    buttons.AddButton(wx.Button(dlg, wx.ID_OK))
    buttons.AddButton(wx.Button(dlg, wx.ID_CANCEL))
    buttons.Realize()
    outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
    update()
    dlg.SetSizerAndFit(outer)
    dlg.CentreOnScreen()

    while dlg.ShowModal() == wx.ID_OK:
        bad = [ctrl.GetValue() for ctrl in keys.values() if not settings.valid_key(ctrl.GetValue())]
        if bad:
            wx.MessageBox("Not a key KiCad notation can describe: %s" % ", ".join(repr(b) for b in bad),
                          TITLE, wx.ICON_WARNING)
            continue
        values.update({
            "touchpad_free_pan": free_pan.GetValue(),
            "pan_sensitivity": 2 ** (sens.GetValue() / 100),
            "pan_acceleration": accel.GetValue() / 100,
            "wheel_zoom": wheel.GetValue(),
            "right_click": right[0](),
            "right_click_drag": right_drag[0](),
            "middle_click": middle[0](),
            "middle_click_drag": middle_drag[0](),
            "dont_ask_layout": not remind.GetValue(),
        })
        for name, ctrl in keys.items():
            values["key_" + name] = ctrl.GetValue().strip()
        try:
            settings.save(values)
        except OSError as e:
            wx.MessageBox("Could not save settings: %s" % e, TITLE, wx.ICON_ERROR)
        break
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
