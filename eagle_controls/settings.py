"""User settings, shared by the plugin (which edits them) and the helper (which reads them)."""

import json
import os

PATH = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__)),
                    "kicad_eagle_controls_prefs.json")

# What a click can do; the helper presses the matching key setting.
ACTIONS = ("rotate", "flip", "layer", "off")

DEFAULTS = {
    # Touchpad
    "touchpad_free_pan": True,      # pan on both axes from finger motion (else Windows' axis-locked scroll)
    "pan_sensitivity": 1.0,         # multiplier on top of the user's Windows touchpad speed
    "pan_acceleration": 0.0,        # 0 = constant speed, 1 = strongly speed-dependent
    # Mouse
    "wheel_zoom": True,             # mouse wheel zooms (else it pans, as KiCad's trackpad layout does)
    "right_click": "rotate",        # on the board, not dragging: an ACTIONS entry or "menu"
    "right_click_drag": "rotate",   # while dragging a part with the left button
    "middle_click": "layer",        # middle click without moving
    "middle_click_drag": "flip",    # middle click while dragging a part
    # KiCad hotkeys the actions press, in KiCad's notation ("R", "Shift+R", "PgUp", ...)
    "key_rotate": "R",
    "key_flip": "F",
    "key_layer": "V",
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


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
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
