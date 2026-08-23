"""doxa.palette -- DOXA's command-palette provider (Ctrl+P).

Textual's built-in CommandPalette does the modal/fuzzy work; this Provider
feeds it DOXA's commands. The command LIST lives on the app
(``DoxaApp.doxa_commands()``) as plain (name, help, callback) tuples --
data, not palette machinery -- for two reasons: the attach picker's entries
are dynamic (one per live daemon session in the shared peer/daemon
registry, recomputed on every palette open), and the test suite can assert
the registered surface without driving a modal screen.
"""

from __future__ import annotations

from textual.command import DiscoveryHit, Hit, Hits, Provider


class DoxaCommandProvider(Provider):
    """Feeds ``app.doxa_commands()`` into the built-in command palette."""

    def _commands(self):
        supplier = getattr(self.app, "doxa_commands", None)
        return supplier() if callable(supplier) else []

    async def discover(self) -> Hits:
        """The empty-query view: every DOXA command, in registration order
        (attach targets last, newest session first -- the order
        doxa_commands yields them)."""
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)
