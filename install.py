"""Install the Eagle-style controls plugin into KiCad (Windows).

Run with KiCad's Python while KiCad is closed:
    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" install.py

  1. copies eagle_controls/ into Documents\\KiCad\\<ver>\\scripting\\plugins
  2. switches KiCad's scroll settings to the trackpad layout
     (scroll = pan, Ctrl+scroll = zoom, Shift+scroll = pan sideways);
     the original kicad_common.json is backed up next to it first.
"""

import json
import os
import shutil
import subprocess
import sys
import time

VERSION = sys.argv[1] if len(sys.argv) > 1 else "10.0"
HERE = os.path.dirname(os.path.abspath(__file__))

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(HERE, "eagle_controls"))
import settings  # noqa: E402

WXK_SHIFT = 306
TRACKPAD = dict(settings.TRACKPAD_LAYOUT, scroll_modifier_pan_h=WXK_SHIFT)  # Shift+scroll pans sideways too


def kicad_running():
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout.lower()
    return any('"%s.exe"' % n in out for n in ("kicad", "pcbnew", "eeschema", "gerbview", "pl_editor"))


def main():
    if kicad_running():
        sys.exit("Close KiCad first (it overwrites its settings on exit).")

    plugins = os.path.join(os.path.expanduser("~"), "Documents", "KiCad", VERSION, "scripting", "plugins")
    dest = os.path.join(plugins, "eagle_controls")
    os.makedirs(plugins, exist_ok=True)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(os.path.join(HERE, "eagle_controls"), dest,
                    ignore=shutil.ignore_patterns("__pycache__", "debug"))
    print("Plugin installed to", dest)

    common = os.path.join(os.environ["APPDATA"], "kicad", VERSION, "kicad_common.json")
    if not os.path.isfile(common):
        sys.exit("%s not found: start KiCad %s once, close it, and run this again." % (common, VERSION))
    with open(common, encoding="utf-8") as f:
        cfg = json.load(f)
    if all(cfg.get("input", {}).get(k) == v for k, v in TRACKPAD.items()):
        print("Scroll settings already set.")
        return
    backup = common + ".before-eagle-controls-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(common, backup)
    cfg.setdefault("input", {}).update(TRACKPAD)
    with open(common, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print("Scroll settings switched to trackpad layout (backup: %s)" % backup)


if __name__ == "__main__":
    main()
