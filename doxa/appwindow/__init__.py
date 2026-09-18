# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow -- DoxaApp's method families, one mixin per module.

The window itself stays in :mod:`doxa.app`, and stays a MODULE. Textual's
``_MessagePumpMeta`` registers ``@on`` handlers by scanning the class body
it constructs, and it never meets a plain mixin's -- so BINDINGS, every
``@on`` handler, ``compose`` and ``on_mount`` have to be declared in the
body that metaclass builds. What a family can do without being seen there
lives here instead, one mixin per module, the way :mod:`doxa.session`
already holds SessionPane's three families.

The name is ``appwindow`` and not ``window`` because :mod:`doxa.window` is
taken -- it writes the terminal's own window title -- and a package of
that name would shadow the module outright.
"""
