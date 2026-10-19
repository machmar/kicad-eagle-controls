"""Build the KiCad Plugin and Content Manager package.

    python build_pcm.py

Writes:
  dist/eagle-controls-<version>.zip     installable via PCM's "Install from File..."
                                        and uploaded as the GitHub release asset
  dist/submission/packages/<id>/        the entry for KiCad's addon repository
                                        (gitlab.com/kicad/addons/metadata): metadata.json
                                        with download_url/sha256/sizes, plus icon.png

The zip is not byte-for-byte reproducible, so upload the zip from the same run
as the submission files (gh release upload ... --clobber).
"""

import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
RELEASE_URL = "https://github.com/machmar/kicad-eagle-controls/releases/download/v{0}/eagle-controls-{0}.zip"


def main():
    with open(os.path.join(HERE, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)
    meta["versions"] = meta["versions"][:1]
    version = meta["versions"][0]["version"]
    with open(os.path.join(HERE, "eagle_controls", "settings.py"), encoding="utf-8") as f:
        plugin_version = re.search(r'^VERSION = "(.*)"', f.read(), re.M).group(1)
    if plugin_version != version:
        sys.exit("metadata.json says %s but eagle_controls/settings.py says %s" % (version, plugin_version))
    icon = os.path.join(HERE, "resources", "icon.png")

    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    out = os.path.join(dist, "eagle-controls-%s.zip" % version)
    install_size = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(meta, indent=2))
        plugin = os.path.join(HERE, "eagle_controls")
        for name in sorted(n for n in os.listdir(plugin) if n.endswith((".py", ".png"))):
            src = os.path.join(plugin, name)
            z.write(src, "plugins/" + name)
            install_size += os.path.getsize(src)
        z.write(icon, "resources/icon.png")
        install_size += os.path.getsize(icon)

    with open(out, "rb") as f:
        data = f.read()
    meta["versions"][0].update({
        "download_url": RELEASE_URL.format(version),
        "download_sha256": hashlib.sha256(data).hexdigest(),
        "download_size": len(data),
        "install_size": install_size,
    })

    sub = os.path.join(dist, "submission", "packages", meta["identifier"])
    shutil.rmtree(os.path.join(dist, "submission"), ignore_errors=True)
    os.makedirs(sub)
    with open(os.path.join(sub, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")
    shutil.copy2(icon, os.path.join(sub, "icon.png"))

    print(out)
    print(sub)


if __name__ == "__main__":
    main()
