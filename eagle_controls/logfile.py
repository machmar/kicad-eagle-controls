"""The helper's log, %TEMP%\\kicad_eagle_controls.log (pythonw has no console).

Windows silently removes a low-level mouse hook that is slow to return, so
nothing the hook calls may wait on the disk: log() only queues the line and
the writer() thread writes it.

An empty file named "debug" next to this one turns on detailed logging.
"""

import os
import queue
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG = os.path.exists(os.path.join(HERE, "debug"))
PATH = os.path.join(os.environ.get("TEMP", HERE), "kicad_eagle_controls.log")

_lines = queue.Queue()


def log(msg):
    _lines.put("%s [%d] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))


def flush(first=None):
    """Write out everything queued (plus first, a line already taken off the queue)."""
    lines = [first] if first else []
    while True:
        try:
            lines.append(_lines.get_nowait())
        except queue.Empty:
            break
    if lines:
        try:
            with open(PATH, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass


def writer():
    """Thread: write lines as they come."""
    while True:
        flush(_lines.get())
