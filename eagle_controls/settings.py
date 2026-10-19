"""User settings, shared by the plugin (which edits them) and the helper (which reads them)."""

import json
import os

PATH = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__)),
                    "kicad_eagle_controls_prefs.json")

DEFAULTS = {
    "pan_sensitivity": 1.0,    # multiplier on top of the user's Windows touchpad speed
    "pan_acceleration": 0.0,   # 0 = constant speed, 1 = strongly speed-dependent
    "dont_ask_layout": False,  # don't remind about KiCad's trackpad scroll layout
}


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {k: type(v)(data.get(k, v)) for k, v in DEFAULTS.items()}


def save(values):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)
