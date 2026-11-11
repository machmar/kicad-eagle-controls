"""The "other layer" click action (PCB and footprint editors).

Most layers come in a front and back pair, and a click switches to the other
one: F.Silkscreen <-> B.Silkscreen, F.Paste <-> B.Paste, F.Mask, F.Adhesive,
F.Courtyard and F.Fab likewise, and User.Drawings <-> User.Comments,
User.Eco1 <-> User.Eco2. Kinds of layer with more than two take turns:
on copper it presses KiCad's "next layer" key (F.Cu, In1.Cu, ... B.Cu, F.Cu),
and the board's User.1, User.2... go round the same way. Edge.Cuts and
Margin have no counterpart; a click there does nothing.

The active layer and the board's layers are read from the layer drop-down in
KiCad's toolbar, and picking an entry there switches the layer (kicad.py).
"""

import queue

import kicad
from logfile import DEBUG, log
from winapi import POINT

# KiCad's layer numbers (PCB_LAYER_ID) since KiCad 9: copper layers are the
# even numbers, F.Cu 0, B.Cu 2, In1.Cu 4, In2.Cu 6...; the rest are odd.
COPPER_END = 64           # In30.Cu is 62
FIRST_USER = 39           # User.1; User.n is 37 + 2n
PAIRS = {1: 3,            # F.Mask, B.Mask
         5: 7,            # F.Silkscreen, B.Silkscreen
         9: 11,           # F.Adhesive, B.Adhesive
         13: 15,          # F.Paste, B.Paste
         17: 19,          # User.Drawings, User.Comments
         21: 23,          # User.Eco1, User.Eco2
         31: 29,          # F.Courtyard, B.Courtyard
         35: 33}          # F.Fab, B.Fab
PAIRS.update({b: a for a, b in PAIRS.items()})
NEXT_COPPER = "next copper"


def is_copper(layer):
    return layer < COPPER_END and layer % 2 == 0


def is_user(layer):
    return layer >= FIRST_USER and layer % 2 == 1


def target(box):
    """Where to go from box's active layer: an index into box, NEXT_COPPER, or None to stay."""
    layer = box.layers[box.active]
    if is_copper(layer):
        return NEXT_COPPER
    if layer in PAIRS:
        other = PAIRS[layer]
        return box.layers.index(other) if other in box.layers else None  # e.g. B.Adhesive switched off
    if is_user(layer):
        users = [i for i, n in enumerate(box.layers) if is_user(n)]
        return users[(users.index(box.active) + 1) % len(users)] if len(users) > 1 else None
    return None


class Switcher:
    """Does the switching on its own thread: the hook mustn't wait on KiCad."""

    def __init__(self):
        self._requests = queue.Queue()

    def request(self, pt, trigger):
        """Switch the layer in the editor under pt (screen coordinates); trigger is for the log."""
        self._requests.put((pt.x, pt.y, trigger))

    def run(self):
        """Thread: carry out the requests."""
        while True:
            x, y, trigger = self._requests.get()
            try:
                self._switch(POINT(x, y), trigger)
            except Exception as e:
                log("layer switch error: %r" % e)

    def _switch(self, pt, trigger):
        box = kicad.layer_box(kicad.board_frame(pt))
        if not box or box.active is None:
            log("can't read KiCad's layer drop-down; going to the next copper layer instead")
            kicad.press("other_layer", trigger)
            return
        to = target(box)
        if DEBUG:
            log("%s: on %s, going to %s" % (trigger, box.names[box.active],
                                            box.names[to] if isinstance(to, int) else to))
        if to == NEXT_COPPER:
            kicad.press("other_layer", trigger)  # KiCad's "next layer": also right while routing
        elif to is not None and not kicad.select_layer(box, to):
            log("couldn't switch to %s" % box.names[to])
