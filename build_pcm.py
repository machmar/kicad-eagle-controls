"""Build the KiCad Plugin and Content Manager package.

    python build_pcm.py

Writes dist/eagle-controls-<version>.zip (installable via PCM's
"Install from File...") and prints the download_sha256 / download_size /
install_size values needed for a submission to the KiCad addon repository.
"""

import hashlib
import json
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_FILES = ["__init__.py", "daemon.py", "touchpad.py", "settings.py", "icon.png"]


def main():
    with open(os.path.join(HERE, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)
    version = meta["versions"][0]["version"]
    meta["versions"] = meta["versions"][:1]

    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    out = os.path.join(HERE, "dist", "eagle-controls-%s.zip" % version)
    install_size = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(meta, indent=2))
        for name in PLUGIN_FILES:
            src = os.path.join(HERE, "eagle_controls", name)
            z.write(src, "plugins/" + name)
            install_size += os.path.getsize(src)
        z.write(os.path.join(HERE, "resources", "icon.png"), "resources/icon.png")
        install_size += os.path.getsize(os.path.join(HERE, "resources", "icon.png"))

    with open(out, "rb") as f:
        data = f.read()
    print(out)
    print("download_sha256:", hashlib.sha256(data).hexdigest())
    print("download_size:  ", len(data))
    print("install_size:   ", install_size)


if __name__ == "__main__":
    main()
