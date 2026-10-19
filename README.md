# Eagle-style controls for KiCad

A KiCad (Windows) plugin that makes the PCB, footprint, schematic and symbol editors behave like Eagle.

Everywhere:

| Input | Does |
|---|---|
| Touchpad two-finger swipe, in any direction including diagonal | Pans |
| Touchpad pinch | Zooms |
| Mouse wheel | Zooms |

PCB and footprint editor:

| Input | Does |
|---|---|
| Right click while moving a part (left button held, `M`, `G`, or placing a new one) | Rotates it |
| Middle click while moving a part | Flips it to the other side, like Eagle's mirror |
| Middle click otherwise | Goes to the next copper layer down, wrapping from B.Cu back to F.Cu |
| Right click while routing | Switches the bend direction |
| Middle click while routing | Opens KiCad's "Select Layer and Place Through Via" chooser with a highlight on the next layer down. Middle click again moves the highlight; left click places a through via on that layer. Esc cancels. |

Schematic and symbol editor:

| Input | Does |
|---|---|
| Right click while moving a part | Rotates it |
| Middle click while moving a part | Mirrors it left-right, like Eagle's mirror |
| Right click while drawing a wire | Switches the bend direction |

A part that is only selected isn't touched: then right click opens KiCad's menu as usual. Shift + right click always opens the menu, and middle- and right-button drags still pan. Every one of these can be changed or turned off in the settings.

## Install

**From a package:** run `python build_pcm.py` to get `dist/eagle-controls-<version>.zip`. In KiCad, open **Plugin and Content Manager → Install from File…** and pick the zip. On first start the plugin explains the one KiCad setting it needs: **Preferences → Mouse and Touchpad → Reset to Trackpad Defaults**.

**From this folder:** close KiCad, then run:

```
"C:\Program Files\KiCad\10.0\bin\python.exe" install.py
```

This copies `eagle_controls/` to `Documents\KiCad\10.0\scripting\plugins\` and sets the trackpad scroll layout for you (it backs up `kicad_common.json` first).

The plugin starts automatically when the PCB editor opens, and then also covers the schematic editor in the same KiCad window. (Opened on its own, without the PCB editor, the schematic editor isn't covered.) It runs a helper process (`daemon.py`, using KiCad's own `pythonw.exe`) that exits when KiCad closes. If the helper dies, the plugin restarts it.

## Settings

**PCB Editor → Tools → External Plugins → Eagle-style controls** opens the settings. Each Eagle-style behaviour can be changed or switched back to KiCad's own:

- **Touchpad:** free two-axis panning on/off (off leaves Windows' axis-locked scrolling), **pan sensitivity** (0.25× to 4× your Windows touchpad speed) and **pan acceleration** (off to 100%; slow swipes pan finer, fast swipes further).
- **Mouse:** wheel zooms on/off (off: it pans), and whether rotate/flip/mirror wait until a part is being moved. Optionally, a middle double-click no longer zooms to fit (off by default; a triple click still does). The **PCB** and **Schematic** tabs set what right and middle click do, separately for normal use, while dragging a part and while routing or drawing wires: rotate, flip or mirror, next layer, toggle the layer pair, switch the bend direction, the via layer chooser, or nothing extra (for right click: KiCad's context menu). "Dragging" means holding the left button on a part; "moving" also covers `M`, `G` and placing, recognised by KiCad's tool cursor; "routing" means the pencil cursor is showing. The PCB tab also sets whether the left click in the via chooser places the via.
- **Hotkeys** aren't set here: the plugin uses whatever keys KiCad has for rotate, flip, mirror, next layer, layer pair, bend direction and "Select Layer and Place Through Via" (Preferences → Hotkeys, read from KiCad's `user.hotkeys`). The window only warns if a click uses an action that has no key in KiCad.
- **Reminder** about KiCad's trackpad scroll layout on/off.

Changes apply right away. Settings are stored in `%LOCALAPPDATA%\kicad_eagle_controls_prefs.json`. Mouse-wheel zoom speed is KiCad's own setting, in Preferences → Mouse and Touchpad.

## How it works

The helper uses a Windows low-level mouse hook, and only acts on input aimed at the KiCad process that started it.

- **Wheel:** KiCad is set to the trackpad layout, where scrolling pans and Ctrl+scroll zooms. Windows sends touchpad pinches as Ctrl+scroll, so pinching zooms by itself. A real mouse wheel would now pan, so the helper resends wheel events from a mouse with Ctrl held. It tells the mouse from the touchpad by raw input: a mouse produces raw wheel input in 120-unit notches, and touchpad scrolling doesn't. If you hold Ctrl, Shift or Alt while scrolling, the event passes through unchanged.
- **Touchpad panning:** Windows locks two-finger scrolling to one axis. The helper reads finger contacts directly from Precision Touchpads (`touchpad.py`, HID raw input). It decides whether the gesture is a pan (fingers move together) or a pinch (fingers spread or close). Over a KiCad canvas, it swallows Windows' scroll events and pans from the real finger motion, at most once per frame. The speed and direction are calibrated against Windows' own scroll events and saved in `%LOCALAPPDATA%\kicad_eagle_controls.json`.
- **Clicks:** a right press is held back until release. If the mouse moves first, it's handed to KiCad so right-drag still pans. Middle clicks pass through and the action follows on release. The keys sent are read from KiCad's `user.hotkeys` and re-read when it changes; click timings are constants at the top of `daemon.py`.

## Code

In `eagle_controls/`, the plugin side (runs inside KiCad):

- `__init__.py`: starts the helper and keeps it running, and registers the menu entry.
- `dialog.py`: the settings window.
- `settings.py`: the settings, their defaults and the click actions, plus reading KiCad's hotkeys and scroll layout. The helper uses it too.

The helper (`pythonw.exe daemon.py <kicad pid>`):

- `daemon.py`: entry point, the mouse hook and what each click and wheel event does, and raw input.
- `kicad.py`: what KiCad is doing, seen from outside (the canvas under the mouse, the tool from its cursor), and pressing its hotkeys.
- `pan.py`: touchpad panning and its speed calibration. `touchpad.py` reads the finger contacts for it.
- `via_chooser.py`: the highlight over KiCad's via layer chooser.
- `winapi.py`, `logfile.py`: Win32 declarations, and the log (`%TEMP%\kicad_eagle_controls.log`; an empty file named `debug` in the plugin folder makes it detailed).

## Limitations

- Windows only.
- Diagonal panning needs a Precision Touchpad. Other touchpads still pan, but stay locked to one axis.
- Wheel-to-zoom also applies to the schematic and other editors in the same KiCad process. The helper only runs once the PCB editor has opened, though. If you run the schematic editor alone, the mouse wheel pans there because of the trackpad scroll settings.
- Only the OpenGL canvas is handled. The fallback renderer isn't.

## Publishing

KiCad's Plugin and Content Manager lists packages from the [KiCad addon metadata repository](https://gitlab.com/kicad/addons/metadata). To publish a version:

1. Bump `version` in `metadata.json` and `VERSION` in `eagle_controls/settings.py` (the build stops if they differ; the settings window shows it), then run `python build_pcm.py`. It writes the zip and `dist/submission/packages/com.github.machmar.eagle-controls/` (`metadata.json` with download URL, SHA-256 and sizes, plus `icon.png`).
2. Upload the zip from that same run to the GitHub release `v<version>`: `gh release upload v<version> dist/eagle-controls-<version>.zip --clobber`. The SHA-256 must match the uploaded file.
3. The repository must be public and the release must not be a draft, so PCM can download it.
4. Fork the addon metadata repository on GitLab, copy the `packages/...` folder in (for a later version, add the new `versions` entry to the existing file), and open a merge request.

## Troubleshooting

Create an empty file named `debug` in the installed `eagle_controls` folder and restart KiCad. Wheel classification, touchpad gestures and calibration are then logged to `%TEMP%\kicad_eagle_controls.log`. That file always records the helper starting and stopping, and any crashes.
