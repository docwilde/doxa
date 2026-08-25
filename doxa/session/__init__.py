# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.session -- one session tab's behaviour, split by surface.

:class:`~doxa.session.pane.SessionPane` is still one widget and still one
class; what changed in v0.34.0 is where its 95 methods are WRITTEN. Three
mixins carry the three groups that kept colliding in ``doxa/app.py``:

* :class:`~doxa.session.commands.PaneCommandsMixin` -- every ``/command``
  and the text each one prints.
* :class:`~doxa.session.chips.PaneChipsMixin` -- the status line and every
  picker a chip opens.
* :class:`~doxa.session.runtime.PaneRuntimeMixin` -- boot, the turn loop,
  the event dispatch, the peer pump, stop.

Mixins and not helper objects, deliberately: every one of these methods
reads and writes pane state through ``self``, so a mixin moves the code
with zero call-site churn. A helper object would have meant rewriting
hundreds of call sites, which is how a refactor stops being reviewable.

Textual's own machinery is unaffected by the extra bases: ``_css_bases``
walks ``__bases__`` for the first DOMNode subclass, and these mixins are
plain objects, so the pane's CSS type names are exactly what they were.
The one thing a plain mixin CANNOT carry is an ``@on``-decorated handler
(``MessagePumpMeta`` collects those from the class body it is building),
so every decorated handler stayed in
:class:`~doxa.session.pane.SessionPane` itself.
"""
