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
MUTEX_NAME = "Local\\KiCadEagleControls_%d"  # held by daemon.py while it serves that KiCad pid


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


def _helper_running():
    """Whether a helper serves this KiCad: it holds a named mutex while it runs.

    Asking Windows rather than remembering the process we started keeps this
    right when KiCad re-imports the plugin ("Refresh Plugins").
    """
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME % os.getpid())  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def start_helper():
    """Start the helper unless it's already running. Returns False if it can't run here."""
    if sys.platform != "win32":
        return False
    if _helper_running():
        return True
    python = _python()
    if not python:
        return False
    # The helper also refuses to start twice for the same KiCad process.
    args = [python, "-I", DAEMON, str(os.getpid())]
    if _common_json():
        args.append(_common_json())
    subprocess.Popen(args, cwd=HERE, close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
    return True


def _keep_helper_alive():
    """Restart the helper if it dies, for as long as KiCad runs."""
    import wx

    def tick():
        start_helper()
        sys._eagle_controls_watchdog = wx.CallLater(5000, tick)

    # One watchdog per KiCad, even if the plugin is imported again: the pending
    # call is kept on sys, which outlives plugin reloads, and replaced here.
    old = getattr(sys, "_eagle_controls_watchdog", None)
    if old:
        old.Stop()
    sys._eagle_controls_watchdog = wx.CallLater(5000, tick)


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


CLICK_ACTIONS = [("rotate", "Rotate"), ("flip", "Flip to other side"),
                 ("next_layer", "Next layer down (via while routing)"),
                 ("layer", "Toggle layer pair (via while routing)"),
                 ("posture", "Switch bend direction"),
                 ("via_chooser", "Choose via layer (click again: next, left click: place)"),
                 ("off", "Nothing")]
RIGHT_CLICK_ACTIONS = CLICK_ACTIONS[:6] + [("menu", "KiCad's context menu")]


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
    right_route = choice(pnl, grid, "Right click while routing", RIGHT_CLICK_ACTIONS, values["right_click_route"])
    middle = choice(pnl, grid, "Middle click", CLICK_ACTIONS, values["middle_click"])
    middle_drag = choice(pnl, grid, "Middle click while dragging", CLICK_ACTIONS, values["middle_click_drag"])
    middle_route = choice(pnl, grid, "Middle click while routing", CLICK_ACTIONS, values["middle_click_route"])
    box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
    places_via = wx.CheckBox(pnl, label="Left click in the via layer chooser also places the via")
    places_via.SetValue(values["chooser_places_via"])
    box.Add(places_via, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
    box.Add(wx.StaticText(pnl, label="“Dragging” means holding the left button on a part; “routing”\n"
                                     "means the router's pencil cursor is showing. Shift + right click\n"
                                     "always opens KiCad's menu. Right- and middle-button drags still pan."),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    # --- Keys
    # The keys come from KiCad's own hotkey settings; only speak up when one
    # that a click is set to use isn't bound there.
    folder = os.path.dirname(_common_json() or "")
    kicad_keys = settings.kicad_hotkeys(folder if folder else None)
    used = {values[k] for k in ("right_click", "right_click_drag", "right_click_route",
                                "middle_click", "middle_click_drag", "middle_click_route")}
    unbound = [label for name, label in CLICK_ACTIONS if name in used and name in kicad_keys
               and not kicad_keys[name]]
    if unbound:
        warn_keys = wx.StaticText(dlg, label="No hotkey in KiCad for: %s.\nAssign one in KiCad's "
                                             "Preferences → Hotkeys." % ", ".join(unbound))
        warn_keys.SetForegroundColour(wx.Colour(200, 60, 0))
        outer.Add(warn_keys, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

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
        places_via.SetValue(d["chooser_places_via"])
        for (_, set_), key in ((right, "right_click"), (right_drag, "right_click_drag"),
                               (right_route, "right_click_route"), (middle, "middle_click"),
                               (middle_drag, "middle_click_drag"), (middle_route, "middle_click_route")):
            set_(d[key])
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

    if dlg.ShowModal() == wx.ID_OK:
        values.update({
            "touchpad_free_pan": free_pan.GetValue(),
            "pan_sensitivity": 2 ** (sens.GetValue() / 100),
            "pan_acceleration": accel.GetValue() / 100,
            "wheel_zoom": wheel.GetValue(),
            "right_click": right[0](),
            "right_click_drag": right_drag[0](),
            "right_click_route": right_route[0](),
            "middle_click": middle[0](),
            "middle_click_drag": middle_drag[0](),
            "middle_click_route": middle_route[0](),
            "chooser_places_via": places_via.GetValue(),
            "dont_ask_layout": not remind.GetValue(),
        })
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
