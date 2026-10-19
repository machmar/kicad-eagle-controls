"""User settings, shared by the plugin (which edits them) and the helper (which reads them)."""

import json
import os

PATH = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__)),
                    "kicad_eagle_controls_prefs.json")

# What a click can do; the helper presses KiCad's hotkey for the matching action.
ACTIONS = ("rotate", "flip", "layer", "next_layer", "posture", "via_chooser", "off")

# The KiCad actions behind them, as named in KiCad's user.hotkeys file, and
# KiCad's default keys for when that file is missing or doesn't list one.
KICAD_ACTIONS = {
    "rotate": ("pcbnew.InteractiveEdit.rotateCcw", "R"),
    "flip": ("pcbnew.InteractiveEdit.flip", "F"),
    "layer": ("pcbnew.Control.layerToggle", "V"),                       # toggle the layer pair
    "next_layer": ("pcbnew.Control.layerNext", "+"),                    # next copper layer down
    "posture": ("pcbnew.InteractiveRouter.SwitchPosture", "/"),
    "via_chooser": ("pcbnew.InteractiveRouter.SelLayerAndPlaceVia", "<"),  # through via, pick layer
}

DEFAULTS = {
    # Touchpad
    "touchpad_free_pan": True,      # pan on both axes from finger motion (else Windows' axis-locked scroll)
    "pan_sensitivity": 1.0,         # multiplier on top of the user's Windows touchpad speed
    "pan_acceleration": 0.0,        # 0 = constant speed, 1 = strongly speed-dependent
    # Mouse
    "wheel_zoom": True,             # mouse wheel zooms (else it pans, as KiCad's trackpad layout does)
    "right_click": "rotate",        # on the board, not dragging: an ACTIONS entry or "menu"
    "right_click_drag": "rotate",   # while dragging a part with the left button
    "right_click_route": "posture",  # while routing (pencil cursor): switch bend direction
    "middle_click": "next_layer",   # middle click without moving
    "middle_click_drag": "flip",    # middle click while dragging a part
    "middle_click_route": "via_chooser",  # middle click while routing: pick the through via's layer
    "chooser_places_via": True,    # the left click that picks the via layer also places the via
    # Misc
    "dont_ask_layout": False,       # don't remind about KiCad's trackpad scroll layout
}


KEY_NAMES = {"pgup", "pgdn", "end", "home", "left", "up", "right", "down", "ins", "insert", "del",
             "delete", "tab", "space", "back", "backspace", "esc", "escape", "return", "enter"}
KEY_NAMES.update("f%d" % n for n in range(1, 25))
MODIFIERS = {"shift", "ctrl", "alt"}


def valid_key(text):
    """Whether text is a hotkey in KiCad's notation that daemon.parse_key understands."""
    text = text.strip()
    if text.endswith("++"):
        mods, key = text[:-2].split("+") if len(text) > 2 else [], "+"
    elif "+" in text[:-1]:
        *mods, key = text.split("+")
    else:
        mods, key = [], text
    key = key.strip()
    return (all(m.strip().lower() in MODIFIERS for m in mods)
            and (len(key) == 1 or key.lower() in KEY_NAMES))


def hotkeys_path(kicad_settings_dir):
    return os.path.join(kicad_settings_dir, "user.hotkeys")


def kicad_hotkeys(kicad_settings_dir):
    """{action: key} as bound in KiCad, in KiCad's notation ("R", "Shift+R", "PgUp"...).

    "" means KiCad has no key for that action.
    """
    keys = {action: default for action, (_, default) in KICAD_ACTIONS.items()}
    try:
        with open(hotkeys_path(kicad_settings_dir), encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (OSError, TypeError):
        return keys
    bound = {}
    for line in lines:  # "<action name>	<key>	<alternate key>"
        parts = line.split("	")
        if len(parts) >= 2:
            bound[parts[0]] = [k.strip() for k in parts[1:3]]
    for action, (name, _) in KICAD_ACTIONS.items():
        if name in bound:
            keys[action] = next((k for k in bound[name] if valid_key(k)), "")
    return keys


def load():
    try:
        with open(PATH, encoding="utf-8-sig") as f:  # tolerate a BOM from hand edits
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if data.get("right_click_rotates_always") is False and "right_click" not in data:
        data["right_click"] = "menu"  # setting from 1.0's checkbox
    values = {}
    for k, default in DEFAULTS.items():
        try:
            values[k] = type(default)(data.get(k, default))
        except (TypeError, ValueError):
            values[k] = default
    return values


def save(values):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)
