"""The settings window (PCB Editor → Tools → External Plugins → Eagle-style controls).

Every behaviour that differs from KiCad can be changed or turned off here.
The helper re-reads the settings file, so changes apply right away.
"""

import math

import wx

from . import settings

WARNING_COLOUR = wx.Colour(200, 60, 0)
# The rows of a click tab: (setting suffix, label, whether it's a right click)
CLICK_ROWS = [("right_click", "Right click", True),
              ("right_click_drag", "Right click while moving a part", False),
              ("right_click_route", "Right click while {}", True),
              ("middle_click", "Middle click", False),
              ("middle_click_drag", "Middle click while moving a part", False),
              ("middle_click_route", "Middle click while {}", False)]


def click_options(actions, right):
    """(setting value, label) choices for a click: the actions, then nothing or (right click) KiCad's menu."""
    leave = "menu" if right else "off"
    return [(key, a.label) for key, a in actions.items()] + [(leave, settings.NO_ACTION[leave])]


def show(trackpad_layout, hotkeys_file):
    """Show the window. trackpad_layout: whether KiCad uses it (False shows a warning)."""
    values = settings.load()
    fields = {}  # setting -> (get, set) on its control
    clicks = []  # the click settings among them
    dlg = wx.Dialog(None, title=settings.TITLE)
    outer = wx.BoxSizer(wx.VERTICAL)

    def grid():
        g = wx.FlexGridSizer(0, 3, 8, 10)
        g.AddGrowableCol(1)
        return g

    def section(label):
        box = wx.StaticBoxSizer(wx.VERTICAL, dlg, label)
        outer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        return box, box.GetStaticBox()  # controls in the box must be its children

    def checkbox(parent, sizer, key, label, flags, border, inverted=False):
        ctrl = wx.CheckBox(parent, label=label)
        sizer.Add(ctrl, 0, flags, border)
        fields[key] = ((lambda: not ctrl.GetValue()), (lambda v: ctrl.SetValue(not v))) if inverted \
            else (ctrl.GetValue, ctrl.SetValue)
        return ctrl

    def choice(parent, sizer, key, label, options):
        ctrl = wx.Choice(parent, choices=[text for _, text in options])
        keys = [k for k, _ in options]
        sizer.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(ctrl, 1, wx.EXPAND)
        sizer.AddSpacer(0)
        fields[key] = (lambda: keys[ctrl.GetSelection()]), (lambda v: ctrl.SetSelection(keys.index(v) if v in keys else 0))

    def warning(text):
        label = wx.StaticText(dlg, label=text)
        label.SetForegroundColour(WARNING_COLOUR)
        outer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

    # --- Touchpad
    box, pnl = section("Touchpad")
    free_pan = checkbox(pnl, box, "touchpad_free_pan",
                        "Two-finger swipe pans in any direction (not locked to one axis)", wx.ALL, 6)
    sliders = grid()
    # Sensitivity on a log scale: slider -200..200 -> 0.25x..4x.
    sens = wx.Slider(pnl, minValue=-200, maxValue=200, size=(240, -1))
    accel = wx.Slider(pnl, minValue=0, maxValue=100)
    fields["pan_sensitivity"] = (lambda: 2 ** (sens.GetValue() / 100)), (lambda v: sens.SetValue(round(100 * math.log2(v))))
    fields["pan_acceleration"] = (lambda: accel.GetValue() / 100), (lambda v: accel.SetValue(round(100 * v)))
    sens_label, accel_label = wx.StaticText(pnl, size=(50, -1)), wx.StaticText(pnl, size=(50, -1))
    for label, slider, value in (("Pan sensitivity", sens, sens_label), ("Pan acceleration", accel, accel_label)):
        sliders.Add(wx.StaticText(pnl, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        sliders.Add(slider, 1, wx.EXPAND)
        sliders.Add(value, 0, wx.ALIGN_CENTER_VERTICAL)
    box.Add(sliders, 0, wx.EXPAND | wx.ALL, 6)

    def update(_=None):
        sens_label.SetLabel("%.2f×" % 2 ** (sens.GetValue() / 100))
        accel_label.SetLabel("%d %%" % accel.GetValue() if accel.GetValue() else "off")
        sens.Enable(free_pan.GetValue())
        accel.Enable(free_pan.GetValue())

    for ctrl, event in ((sens, wx.EVT_SLIDER), (accel, wx.EVT_SLIDER), (free_pan, wx.EVT_CHECKBOX)):
        ctrl.Bind(event, update)

    # --- Mouse: the wheel is shared; the clicks differ per editor, one tab each
    box, pnl = section("Mouse")
    checkbox(pnl, box, "wheel_zoom", "Mouse wheel zooms (otherwise it pans)", wx.ALL, 6)
    checkbox(pnl, box, "parts_only_while_moving", "Rotate, flip and mirror only while a part is being moved\n"
             "(left button held, M, G or placing), not when just selected", wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
    checkbox(pnl, box, "block_middle_double_click", "Middle double-click doesn't zoom to fit (triple-click does)",
             wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
    tabs = wx.Notebook(pnl)
    for title, prefix, actions, route_word in (("PCB", "", settings.PCB_ACTIONS, "routing"),
                                               ("Schematic", "sch_", settings.SCHEMATIC_ACTIONS, "drawing wires")):
        page = wx.Panel(tabs)
        page_sizer = wx.BoxSizer(wx.VERTICAL)
        rows = grid()
        for suffix, label, right in CLICK_ROWS:
            clicks.append(prefix + suffix)
            choice(page, rows, prefix + suffix, label.format(route_word), click_options(actions, right))
        page_sizer.Add(rows, 0, wx.EXPAND | wx.ALL, 8)
        if not prefix:
            checkbox(page, page_sizer, "chooser_places_via", "Left click in the via layer chooser also places the via",
                     wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        page.SetSizer(page_sizer)
        tabs.AddPage(page, title)
    box.Add(tabs, 0, wx.EXPAND | wx.ALL, 6)
    box.Add(wx.StaticText(pnl, label="“Moving a part” means holding the left button on it, M, G or\n"
                                     "placing a new one; “routing” and “drawing wires” mean the\n"
                                     "pencil cursor is showing. Shift + right click always opens\n"
                                     "KiCad's menu. Right- and middle-button drags still pan."),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    # --- Keys: they come from KiCad's own hotkey settings; only speak up when
    # one that a click is set to use isn't bound there.
    keys = settings.kicad_hotkeys(hotkeys_file)
    unbound = set()
    for name in clicks:
        sch = name.startswith("sch_")
        action = ("sch_" if sch else "") + values[name]
        if action in keys and not keys[action]:
            label = settings.KICAD_ACTIONS[action].label
            unbound.add("schematic " + label.lower() if sch else label)
    if unbound:
        warning("No hotkey in KiCad for: %s.\nAssign one in KiCad's Preferences → Hotkeys."
                % ", ".join(sorted(unbound)))

    # --- KiCad's scroll layout
    checkbox(dlg, outer, "dont_ask_layout", "Remind me when KiCad isn't using the trackpad scroll layout",
             wx.LEFT | wx.RIGHT | wx.TOP, 12, inverted=True)
    if not trackpad_layout:
        warning("KiCad isn't using the trackpad scroll layout, so the swipe and\n"
                "wheel settings have no effect. See Preferences → Mouse and\n"
                "Touchpad → Reset to Trackpad Defaults.")

    def set_all(source):
        for key, (_, set_) in fields.items():
            set_(source[key])
        update()

    buttons = wx.StdDialogButtonSizer()
    reset = wx.Button(dlg, wx.ID_DEFAULT, "Defaults")
    reset.Bind(wx.EVT_BUTTON, lambda _: set_all(settings.DEFAULTS))
    buttons.Add(reset)
    buttons.AddButton(wx.Button(dlg, wx.ID_OK))
    buttons.AddButton(wx.Button(dlg, wx.ID_CANCEL))
    buttons.Realize()
    bottom = wx.BoxSizer(wx.HORIZONTAL)
    version = wx.StaticText(dlg, label="Version " + settings.VERSION)
    version.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
    bottom.Add(version, 0, wx.ALIGN_CENTER_VERTICAL)
    bottom.AddStretchSpacer()
    bottom.Add(buttons, 0)
    outer.Add(bottom, 0, wx.EXPAND | wx.ALL, 12)
    set_all(values)
    dlg.SetSizerAndFit(outer)
    dlg.CentreOnScreen()

    if dlg.ShowModal() == wx.ID_OK:
        values.update({key: get() for key, (get, _) in fields.items()})
        try:
            settings.save(values)
        except OSError as e:
            wx.MessageBox("Could not save settings: %s" % e, settings.TITLE, wx.ICON_ERROR)
    dlg.Destroy()
