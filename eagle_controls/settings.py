"""The plugin's settings, and the KiCad settings files it reads.

Shared by the plugin (__init__.py and dialog.py, which edit the settings) and
the helper (daemon.py and its modules, which act on them). Plain Python: no
KiCad, wx or Windows imports.
"""

import collections
import json
import os

TITLE = "Eagle-style controls"
VERSION = "1.1.0"  # must match metadata.json; build_pcm.py checks
PATH = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__)),
                    "kicad_eagle_controls_prefs.json")
MUTEX_NAME = "Local\\KiCadEagleControls_%d"  # held by the helper while it serves that KiCad pid

# --- what a click can do ---------------------------------------------------
# Each action presses KiCad's hotkey for it. The hotkey is looked up by
# KiCad's action name in its user.hotkeys file, with KiCad's default key for
# when that file is missing or doesn't list it.

Action = collections.namedtuple("Action", "kicad_name default_key label")

PCB_ACTIONS = {
    "rotate": Action("pcbnew.InteractiveEdit.rotateCcw", "R", "Rotate"),
    "flip": Action("pcbnew.InteractiveEdit.flip", "F", "Flip to other side"),
    "next_layer": Action("pcbnew.Control.layerNext", "+", "Next layer down (via while routing)"),
    "layer": Action("pcbnew.Control.layerToggle", "V", "Toggle layer pair (via while routing)"),
    "posture": Action("pcbnew.InteractiveRouter.SwitchPosture", "/", "Switch bend direction"),
    # Opens KiCad's "Select Layer and Place Through Via"; see via_chooser.py.
    "via_chooser": Action("pcbnew.InteractiveRouter.SelLayerAndPlaceVia", "<",
                          "Choose via layer (click again: next, left click: place)"),
}
SCHEMATIC_ACTIONS = {  # stored without a prefix, known to the helper as "sch_<name>"
    "rotate": Action("eeschema.InteractiveEdit.rotateCCW", "R", "Rotate"),
    "mirror_x": Action("eeschema.InteractiveEdit.mirrorH", "X", "Mirror left-right (Eagle's mirror)"),
    "mirror_y": Action("eeschema.InteractiveEdit.mirrorV", "Y", "Mirror top-bottom"),
    "posture": Action("eeschema.InteractiveDrawingLineWireBus.switchPosture", "\\", "Switch wire bend direction"),
}
# Click settings that leave the click to KiCad. "menu" is offered for right clicks.
NO_ACTION = {"off": "Nothing", "menu": "KiCad's context menu"}

# Every action the helper can press, by its helper name.
KICAD_ACTIONS = dict(PCB_ACTIONS, **{"sch_" + name: a for name, a in SCHEMATIC_ACTIONS.items()})
# Actions that change a part, so only make sense while one is in hand.
PART_ACTIONS = {"rotate", "flip", "sch_rotate", "sch_mirror_x", "sch_mirror_y"}

# --- the settings ----------------------------------------------------------

DEFAULTS = {
    # Touchpad
    "touchpad_free_pan": True,      # pan on both axes from finger motion (else Windows' axis-locked scroll)
    "pan_sensitivity": 1.0,         # multiplier on top of the user's Windows touchpad speed
    "pan_acceleration": 0.0,        # 0 = constant speed, 1 = strongly speed-dependent
    # Mouse
    "wheel_zoom": True,             # mouse wheel zooms (else it pans, as KiCad's trackpad layout does)
    # Rotate/flip/mirror only while a part is in hand (left-button drag, M, G,
    # placing), not when it's merely selected; then the click is KiCad's.
    "parts_only_while_moving": True,
    "block_middle_double_click": False,  # hide middle double-clicks from KiCad (no zoom to fit;
                                         # a triple click still reaches KiCad as a double-click)
    # PCB and footprint editors: a PCB_ACTIONS or NO_ACTION key for each click.
    # "_drag": while moving a part (left-button drag, M, G, placing); "_route": while routing (pencil cursor).
    "right_click": "rotate",
    "right_click_drag": "rotate",
    "right_click_route": "posture",
    "middle_click": "next_layer",
    "middle_click_drag": "flip",
    "middle_click_route": "via_chooser",
    "chooser_places_via": True,     # the left click that picks the via layer also places the via
    # Schematic and symbol editors: a SCHEMATIC_ACTIONS or NO_ACTION key ("_route": drawing a wire)
    "sch_right_click": "rotate",
    "sch_right_click_drag": "rotate",
    "sch_right_click_route": "posture",
    "sch_middle_click": "mirror_x",      # Eagle's mirror, also while moving with M
    "sch_middle_click_drag": "mirror_x",
    "sch_middle_click_route": "off",
    # Misc
    "dont_ask_layout": False,       # don't remind about KiCad's trackpad scroll layout
}
RANGES = {"pan_sensitivity": (0.25, 4.0), "pan_acceleration": (0.0, 1.0)}


def _clean(key, value):
    """value if it's valid for the setting key, else its default."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # NaN
            return default
        low, high = RANGES[key]
        return min(max(float(value), low), high)
    actions = SCHEMATIC_ACTIONS if key.startswith("sch_") else PCB_ACTIONS
    return value if value in actions or value in NO_ACTION else default


def load(path=PATH):
    """The saved settings, with the default for anything missing or invalid."""
    try:
        with open(path, encoding="utf-8-sig") as f:  # tolerate a BOM from hand edits
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("right_click_rotates_always") is False and "right_click" not in data:
        data["right_click"] = "menu"  # setting from 1.0's checkbox
    return {key: _clean(key, data.get(key, default)) for key, default in DEFAULTS.items()}


def save(values):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)


class Watched:
    """A value read from a file, and read again by refresh() when the file changes.

    Reading a file can be slow, so the helper calls refresh() from a background
    thread and everything else only looks at .value (None until the first read).
    """

    def __init__(self, read, path=None):
        self.read, self.path = read, path
        self.value = None
        self._mtime = ()  # never a real mtime, so the first refresh() reads

    def refresh(self):
        """Re-read the file if it changed. Returns whether .value changed."""
        try:
            mtime = os.path.getmtime(self.path)
        except (OSError, TypeError):
            mtime = None
        if mtime == self._mtime:
            return False
        try:
            value = self.read(self.path)
        except Exception:  # e.g. caught halfway through being written: try again next time
            return False
        self._mtime, old, self.value = mtime, self.value, value
        return value != old


prefs = Watched(load, PATH)  # the helper's copy of the settings; the plugin uses load() and save()

# --- KiCad's files -----------------------------------------------------------

# KiCad's "trackpad" scroll layout in kicad_common.json: plain scroll pans,
# Ctrl+scroll (308 = WXK_CONTROL) zooms, horizontal scroll pans sideways.
TRACKPAD_LAYOUT = {"scroll_modifier_zoom": 308, "scroll_modifier_pan_v": 0, "horizontal_pan": True}


def has_trackpad_layout(common_json):
    """Whether KiCad's kicad_common.json at that path sets the trackpad layout. Raises if unreadable."""
    with open(common_json, encoding="utf-8") as f:
        inp = json.load(f).get("input", {})
    return all(inp.get(k) == v for k, v in TRACKPAD_LAYOUT.items())


def hotkeys_file(common_json):
    """KiCad's user.hotkeys, which sits next to its kicad_common.json."""
    return os.path.join(os.path.dirname(common_json), "user.hotkeys") if common_json else None


# Windows virtual-key codes of the key names KiCad uses in its hotkey notation.
NAMED_KEYS = {"pgup": 0x21, "pgdn": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
              "right": 0x27, "down": 0x28, "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
              "tab": 0x09, "space": 0x20, "back": 0x08, "backspace": 0x08, "esc": 0x1B,
              "escape": 0x1B, "return": 0x0D, "enter": 0x0D}
NAMED_KEYS.update({"f%d" % n: 0x6F + n for n in range(1, 25)})
MODIFIER_KEYS = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12}


def split_hotkey(text):
    """KiCad hotkey notation ("R", "Shift+R", "Ctrl++", "PgUp") -> ([modifiers], key).

    Modifiers come back lower case. None if it isn't a hotkey we can press.
    """
    text = text.strip()
    if text.endswith("++"):
        mods, key = (text[:-2].split("+") if len(text) > 2 else []), "+"
    elif "+" in text[:-1]:
        *mods, key = text.split("+")
    else:
        mods, key = [], text
    mods = [m.strip().lower() for m in mods if m.strip()]
    key = key.strip()
    if any(m not in MODIFIER_KEYS for m in mods) or not (len(key) == 1 or key.lower() in NAMED_KEYS):
        return None
    return mods, key


def kicad_hotkeys(path):
    """{action: key} for KICAD_ACTIONS as bound in KiCad's user.hotkeys at path.

    Keys are in KiCad's notation ("R", "Shift+R", "PgUp"...); "" means KiCad
    has no usable key for that action.
    """
    keys = {action: a.default_key for action, a in KICAD_ACTIONS.items()}
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (OSError, TypeError):
        return keys
    bound = {}
    for line in lines:  # "<action name>\t<key>\t<alternate key>"
        name, *line_keys = line.split("\t")
        if line_keys:
            bound[name] = [k.strip() for k in line_keys[:2]]
    for action, a in KICAD_ACTIONS.items():
        if a.kicad_name in bound:
            keys[action] = next((k for k in bound[a.kicad_name] if split_hotkey(k)), "")
    return keys
