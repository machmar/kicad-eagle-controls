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
| Right click while holding a part (see below) | Rotates it |
| Middle click while holding a part | Flips it to the other side, like Eagle's mirror |
| Middle click otherwise | Goes to the other side's layer: F.Silkscreen ↔ B.Silkscreen, F.Paste ↔ B.Paste, and likewise Mask, Adhesive, Courtyard and Fab, plus User.Drawings ↔ User.Comments and User.Eco1 ↔ User.Eco2. On copper it goes to the next copper layer down, wrapping from B.Cu back to F.Cu; on User.1, User.2... to the next user layer. Edge.Cuts and Margin have no counterpart. |
| Right click while routing | Switches the bend direction |
| Middle click while routing | Opens KiCad's "Select Layer and Place Through Via" chooser with a highlight on the next layer down. Middle click again moves the highlight; left click places a through via on that layer. Esc cancels. |

Schematic and symbol editor:

| Input | Does |
|---|---|
| Right click while holding a part | Rotates it |
| Middle click while holding a part | Mirrors it left-right, like Eagle's mirror |
| Right click while drawing a wire | Switches the bend direction |

A part is **held** while it moves with the mouse: while you drag it with the left button, from `M`, `G`, `D`, `Ctrl+M`, `Ctrl+D` or `Ctrl+V` (whatever keys KiCad has for move, drag, duplicate and paste), and when a new one comes from the footprint or symbol chooser, until a left click puts it down or Esc. A part that is only selected isn't touched: then right click opens KiCad's menu as usual. With a pen, pressing its side button while the tip is on the screen counts as a right click (Windows itself makes nothing of it), so a part dragged with the pen can be rotated too. If a pen driver does turn that into a right click itself, the part still turns only once. Shift + right click always opens the menu, and middle- and right-button drags still pan. Every one of these can be changed or turned off in the settings.

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
- **Mouse:** wheel zooms on/off (off: it pans), and whether rotate/flip/mirror wait until a part is being moved. Optionally, a middle double-click no longer zooms to fit (off by default; a triple click still does). The **PCB** and **Schematic** tabs set what right and middle click do, separately for normal use, while moving a part and while routing or drawing wires: rotate, flip or mirror, the other side's layer, next layer, toggle the layer pair, switch the bend direction, the via layer chooser, or nothing extra (for right click: KiCad's context menu). "Holding a part" is as described above; "routing" means the pencil cursor is showing. The PCB tab also sets whether the left click in the via chooser places the via.
- **Pen:** tapping with the pen's eraser end deletes what it touches (off by default, the same in every editor). The tap reaches KiCad as a click, which selects what's there, and then the helper presses KiCad's Delete key; a tap on empty space deletes nothing. Where things overlap, KiCad's Clarify Selection menu opens: pick what you meant and it's deleted; cancel and nothing is. Dragging the eraser does nothing. It doesn't act while a part is held, while routing, or with Shift, Ctrl or Alt held, and it needs a pen that reports its eraser end to Windows.
- **Hotkeys** aren't set here: the plugin uses whatever keys KiCad has for rotate, flip, mirror, next layer, layer pair, bend direction and "Select Layer and Place Through Via" (Preferences → Hotkeys, read from KiCad's `user.hotkeys`). The window only warns if a click uses an action that has no key in KiCad.
- **Reminder** about KiCad's trackpad scroll layout on/off.

Changes apply right away. Settings are stored in `%LOCALAPPDATA%\kicad_eagle_controls_prefs.json`. Mouse-wheel zoom speed is KiCad's own setting, in Preferences → Mouse and Touchpad.

## How it works

The helper uses a Windows low-level mouse hook, and only acts on input aimed at the KiCad process that started it.

- **Wheel:** KiCad is set to the trackpad layout, where scrolling pans and Ctrl+scroll zooms. Windows sends touchpad pinches as Ctrl+scroll, so pinching zooms by itself. A real mouse wheel would now pan, so the helper resends wheel events from a mouse with Ctrl held. It tells the mouse from the touchpad by raw input: a mouse produces raw wheel input in 120-unit notches, and touchpad scrolling doesn't. If you hold Ctrl, Shift or Alt while scrolling, the event passes through unchanged.
- **Touchpad panning:** Windows locks two-finger scrolling to one axis. The helper reads finger contacts directly from Precision Touchpads (`touchpad.py`, HID raw input). It decides whether the gesture is a pan (fingers move together) or a pinch (fingers spread or close). Over a KiCad canvas, it swallows Windows' scroll events and pans from the real finger motion, at most once per frame. The speed and direction are calibrated against Windows' own scroll events and saved in `%LOCALAPPDATA%\kicad_eagle_controls.json`.
- **Holding a part:** KiCad's cursor can't tell a held part from a selected one (its move cursor also shows over a selected part, and dragging with `G` or placing a footprint shows the arrow or the pencil). So the helper watches what picks a part up: a left-button drag that shows KiCad's move cursor (a selection box has its own cursors); KiCad's move, drag, duplicate and paste keys, read from `user.hotkeys`, pressed while a canvas has the keyboard focus, for as long as KiCad then shows its move cursor (so an `M` that found nothing to pick up doesn't count; KiCad's router drag with `G` or `D` shows the arrow and is taken on trust); or a Footprint Chooser or Choose Symbol window closing without Cancel. A left click or Esc ends it.
- **Layers:** the active layer and the board's layers come from the layer drop-down in KiCad's toolbar, which stores KiCad's layer number with each entry, so renamed layers don't matter. Picking the counterpart's entry there switches the layer, as if you had chosen it.
- **Clicks:** a right press is held back until release. If the mouse moves first, it's handed to KiCad so right-drag still pans. Middle clicks pass through and the action follows on release. The keys sent are read from KiCad's `user.hotkeys` and re-read when it changes; click timings are constants at the top of `daemon.py`.
- **Panning with a pen or touchscreen:** while you pan or zoom by dragging with the middle or right button, KiCad wraps the cursor round to the canvas' other side when it reaches an edge. With a pen, a touchscreen or a tablet in mouse mode the cursor jumps straight back to where you point, and so does the view. So for those (Windows marks pen and touch mouse input, and raw input says when a device reports positions instead of movement), the helper holds the cursor at the canvas' edge instead, and the pan stops there. A mouse still wraps as usual.

## Code

In `eagle_controls/`, the plugin side (runs inside KiCad):

- `__init__.py`: starts the helper and keeps it running, and registers the menu entry.
- `dialog.py`: the settings window.
- `settings.py`: the settings, their defaults and the click actions, plus reading KiCad's hotkeys and scroll layout. The helper uses it too.

The helper (`pythonw.exe daemon.py <kicad pid>`):

- `daemon.py`: entry point, the mouse hook and what each click and wheel event does, and raw input.
- `kicad.py`: what KiCad is doing, seen from outside (the canvas under the mouse, the tool from its cursor, whether a part is held, the layer drop-down), and pressing its hotkeys.
- `layers.py`: middle click's switch to the other side's layer.
- `eraser.py`: a tap with the pen's eraser end: the click, Delete, and KiCad's Clarify Selection menu.
- `pen.py`: a pen's side button pressed with the tip down, and whether its eraser end is in use, read from the pen's HID reports.
- `pan.py`: touchpad panning and its speed calibration. `touchpad.py` reads the finger contacts for it.
- `via_chooser.py`: the highlight over KiCad's via layer chooser.
- `winapi.py`, `logfile.py`: Win32 declarations, and the log (`%TEMP%\kicad_eagle_controls.log`; an empty file named `debug` in the plugin folder makes it detailed).

## Limitations

- Windows only.
- Diagonal panning needs a Precision Touchpad. Other touchpads still pan, but stay locked to one axis.
- Wheel-to-zoom also applies to the schematic and other editors in the same KiCad process. The helper only runs once the PCB editor has opened, though. If you run the schematic editor alone, the mouse wheel pans there because of the trackpad scroll settings.
- Only the OpenGL canvas is handled. The fallback renderer isn't.
- A move started from KiCad's right-click menu (Move) isn't recognised as holding a part; use `M`.
- While dragging a footprint with `G` or `D` in the PCB editor (KiCad's router drag), KiCad itself ignores rotate and flip.
- The layer numbers are KiCad 9's and later.

## Publishing

KiCad's Plugin and Content Manager lists packages from the [KiCad addon metadata repository](https://gitlab.com/kicad/addons/metadata). To publish a version:

1. Bump `version` in `metadata.json` and `VERSION` in `eagle_controls/settings.py` (the build stops if they differ; the settings window shows it), then run `python build_pcm.py`. It writes the zip and `dist/submission/packages/com.github.machmar.eagle-controls/` (`metadata.json` with download URL, SHA-256 and sizes, plus `icon.png`).
2. Upload the zip from that same run to the GitHub release `v<version>`: `gh release upload v<version> dist/eagle-controls-<version>.zip --clobber`. The SHA-256 must match the uploaded file.
3. The repository must be public and the release must not be a draft, so PCM can download it.
4. Fork the addon metadata repository on GitLab, copy the `packages/...` folder in (for a later version, add the new `versions` entry to the existing file), and open a merge request.

## Troubleshooting

Create an empty file named `debug` in the installed `eagle_controls` folder and restart KiCad. Wheel classification, touchpad gestures and calibration are then logged to `%TEMP%\kicad_eagle_controls.log`. That file always records the helper starting and stopping, and any crashes.
