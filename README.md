# Eagle-style controls for KiCad

A KiCad (Windows) plugin that makes the PCB and footprint editors behave like Eagle:

| Input | Does |
|---|---|
| Touchpad two-finger swipe, in any direction including diagonal | Pans the board |
| Touchpad pinch | Zooms |
| Mouse wheel | Zooms |
| Right click on the board | Rotates (sends `R`) the part you're placing, moving (`M`) or dragging, or the selection, instead of opening the context menu. Shift + right click still opens the menu. |
| Middle click while dragging a part | Flips it to the other side (sends `F`), like Eagle's mirror |
| Middle click otherwise | Switches layer (sends `V`: toggles the active layer pair; while routing it places a via and changes layer) |

Middle- and right-button drags still pan. Every one of these can be changed or turned off in the settings.

## Install

**From a package:** run `python build_pcm.py` to get `dist/eagle-controls-<version>.zip`. In KiCad, open **Plugin and Content Manager → Install from File…** and pick the zip. On first start the plugin explains the one KiCad setting it needs: **Preferences → Mouse and Touchpad → Reset to Trackpad Defaults**.

**From this folder:** close KiCad, then run:

```
"C:\Program Files\KiCad\10.0\bin\python.exe" install.py
```

This copies `eagle_controls/` to `Documents\KiCad\10.0\scripting\plugins\` and sets the trackpad scroll layout for you (it backs up `kicad_common.json` first).

The plugin starts automatically when the PCB editor opens. It runs a helper process (`daemon.py`, using KiCad's own `pythonw.exe`) that exits when KiCad closes. If the helper dies, the plugin restarts it.

## Settings

**PCB Editor → Tools → External Plugins → Eagle-style controls** opens the settings. Each Eagle-style behaviour can be changed or switched back to KiCad's own:

- **Touchpad:** free two-axis panning on/off (off leaves Windows' axis-locked scrolling), **pan sensitivity** (0.25× to 4× your Windows touchpad speed) and **pan acceleration** (off to 100%; slow swipes pan finer, fast swipes further).
- **Mouse:** wheel zooms on/off (off: it pans). **Right click**, **right click while dragging**, **middle click** and **middle click while dragging** can each rotate, flip, switch layer, or do nothing extra. For plain right click, "nothing extra" means KiCad's context menu. "Dragging" means holding the left button on a part.
- **KiCad hotkeys used:** the keys sent for rotate, flip and switch layer, in KiCad's notation (`R`, `Shift+R`, `PgUp`, …). Change them only if you rebound them in KiCad.
- **Reminder** about KiCad's trackpad scroll layout on/off.

Changes apply right away. Settings are stored in `%LOCALAPPDATA%\kicad_eagle_controls_prefs.json`. Mouse-wheel zoom speed is KiCad's own setting, in Preferences → Mouse and Touchpad.

## How it works

The helper uses a Windows low-level mouse hook, and only acts on input aimed at the KiCad process that started it.

- **Wheel:** KiCad is set to the trackpad layout, where scrolling pans and Ctrl+scroll zooms. Windows sends touchpad pinches as Ctrl+scroll, so pinching zooms by itself. A real mouse wheel would now pan, so the helper resends wheel events from a mouse with Ctrl held. It tells the mouse from the touchpad by raw input: a mouse produces raw wheel input in 120-unit notches, and touchpad scrolling doesn't. If you hold Ctrl, Shift or Alt while scrolling, the event passes through unchanged.
- **Touchpad panning:** Windows locks two-finger scrolling to one axis. The helper reads finger contacts directly from Precision Touchpads (`touchpad.py`, HID raw input). It decides whether the gesture is a pan (fingers move together) or a pinch (fingers spread or close). Over a KiCad canvas, it swallows Windows' scroll events and pans from the real finger motion, at most once per frame. The speed and direction are calibrated against Windows' own scroll events and saved in `%LOCALAPPDATA%\kicad_eagle_controls.json`.
- **Clicks:** a right press is held back until release. If the mouse moves first, it's handed to KiCad so right-drag still pans. Middle clicks pass through and the action follows on release. The keys come from the settings; click timings are constants at the top of `daemon.py`.

## Limitations

- Windows only.
- Diagonal panning needs a Precision Touchpad. Other touchpads still pan, but stay locked to one axis.
- Wheel-to-zoom also applies to the schematic and other editors in the same KiCad process. The helper only runs once the PCB editor has opened, though. If you run the schematic editor alone, the mouse wheel pans there because of the trackpad scroll settings.
- Only the OpenGL canvas is handled. The fallback renderer isn't.

## Publishing

KiCad's Plugin and Content Manager lists packages from the [KiCad addon metadata repository](https://gitlab.com/kicad/addons/metadata). To publish a version:

1. Bump `version` in `metadata.json`, then run `python build_pcm.py`. It writes the zip and `dist/submission/packages/com.github.machmar.eagle-controls/` (`metadata.json` with download URL, SHA-256 and sizes, plus `icon.png`).
2. Upload the zip from that same run to the GitHub release `v<version>`: `gh release upload v<version> dist/eagle-controls-<version>.zip --clobber`. The SHA-256 must match the uploaded file.
3. The repository must be public and the release must not be a draft, so PCM can download it.
4. Fork the addon metadata repository on GitLab, copy the `packages/...` folder in (for a later version, add the new `versions` entry to the existing file), and open a merge request.

## Troubleshooting

Create an empty file named `debug` in the installed `eagle_controls` folder and restart KiCad. Wheel classification, touchpad gestures and calibration are then logged to `%TEMP%\kicad_eagle_controls.log`. That file always records the helper starting and stopping, and any crashes.
