# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui -- the widget layer, one module per surface.

Every class here used to live in ``doxa/app.py``; none of them changed in
the move. They are grouped by what a plugin would extend (docs/plans/plugin-api.md
names four such surfaces) rather than by widget taxonomy:

* :mod:`doxa.ui.labels` -- the pure formatters and the constants they read.
  No widget, no ``self``, no I/O; everything else here imports from it and
  it imports from nothing in this package.
* :mod:`doxa.ui.transcript` -- the blocks a turn renders into.
* :mod:`doxa.ui.statusline` -- the status bar and the chips it paints.
* :mod:`doxa.ui.dialogs` -- the modal/popup surfaces.
* :mod:`doxa.ui.prompt` -- the prompt input and its key routing.

Textual matches CSS TYPE selectors on the class NAME, never the module
path, so ``doxa/theme.tcss`` needed no edit for any of this.
"""
