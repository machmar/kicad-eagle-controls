"""KiCad plugin: Eagle-style mouse and touchpad controls.

KiCad imports this when the PCB editor starts. It launches daemon.py as a
separate helper process (see that file for what it does), which exits by
itself when KiCad closes, and restarts it if it dies. The plugin's menu entry
opens the settings window (dialog.py).
"""

import ctypes
import os
import subprocess
import sys

import pcbnew

from . import settings

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHDOG_MS = 5000


def _python():
    """KiCad's bundled pythonw.exe (sys.executable is often kicad.exe itself)."""
    dirs = [os.path.dirname(sys.executable), sys.prefix, os.path.join(sys.prefix, "bin")]
    for name in ("pythonw.exe", "python.exe"):
        for d in dirs:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def _common_json():
    try:
        return os.path.join(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath(), "kicad_common.json")
    except Exception:
        return None


def _trackpad_layout():
    try:
        return settings.has_trackpad_layout(_common_json())
    except Exception:
        return True  # can't tell; don't nag


def _helper_running():
    """Whether a helper serves this KiCad: it holds a named mutex while it runs.

    Asking Windows rather than remembering the process we started keeps this
    right when KiCad re-imports the plugin ("Refresh Plugins").
    """
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenMutexW(0x00100000, False, settings.MUTEX_NAME % os.getpid())  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def start_helper():
    """Start the helper unless it's already running. Returns False if it can't run here."""
    if sys.platform != "win32":
        return False
    if _helper_running():
        return True
    python = _python()
    if not python:
        return False
    # The helper also refuses to start twice for the same KiCad process.
    args = [python, "-I", os.path.join(HERE, "daemon.py"), str(os.getpid())]
    if _common_json():
        args.append(_common_json())
    subprocess.Popen(args, cwd=HERE, close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
    return True


def _keep_helper_alive():
    """Restart the helper if it dies, for as long as KiCad runs."""
    import wx

    def tick():
        start_helper()
        sys._eagle_controls_watchdog = wx.CallLater(WATCHDOG_MS, tick)

    # One watchdog per KiCad, even if the plugin is imported again: the pending
    # call is kept on sys, which outlives plugin reloads, and replaced here.
    old = getattr(sys, "_eagle_controls_watchdog", None)
    if old:
        old.Stop()
    sys._eagle_controls_watchdog = wx.CallLater(WATCHDOG_MS, tick)


def _ask_for_trackpad_layout():
    """Explain the one KiCad setting this plugin needs, unless it's set or the user opted out."""
    import wx
    if _trackpad_layout() or settings.load()["dont_ask_layout"]:
        return
    dlg = wx.RichMessageDialog(
        None,
        "Swipe-to-pan and pinch-to-zoom need KiCad's trackpad scroll layout.\n\n"
        "Open Preferences → Preferences… → Common → Mouse and Touchpad, "
        "click “Reset to Trackpad Defaults” and press OK.\n\n"
        "The mouse wheel will still zoom. Right click to rotate and middle click "
        "to switch layers work either way.",
        settings.TITLE, wx.OK | wx.ICON_INFORMATION)
    dlg.ShowCheckBox("Don't show this again")
    dlg.ShowModal()
    if dlg.IsCheckBoxChecked():
        values = settings.load()
        values["dont_ask_layout"] = True
        try:
            settings.save(values)
        except OSError:
            pass
    dlg.Destroy()


class EagleControls(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = settings.TITLE
        self.category = "Navigation"
        self.description = ("Right click rotates, middle click switches layer or mirrors, "
                            "mouse wheel zooms, touchpad swipe pans and pinch zooms. "
                            "Runs automatically; this menu entry opens its settings.")
        self.show_toolbar_button = False
        self.icon_file_name = os.path.join(HERE, "icon.png")

    def Run(self):
        import wx
        from . import dialog
        if not start_helper():
            wx.MessageBox("Could not start the helper (Windows only, needs KiCad's bundled Python).",
                          settings.TITLE, wx.ICON_ERROR)
            return
        dialog.show(_trackpad_layout(), settings.hotkeys_file(_common_json()))


if start_helper():
    try:
        import wx
        wx.CallAfter(_keep_helper_alive)
        wx.CallAfter(_ask_for_trackpad_layout)
    except Exception:
        pass
EagleControls().register()
