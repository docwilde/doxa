"""doxa.palette -- DOXA's command palette (Ctrl+P): its provider, its
entries, and the ordering all three command surfaces share.

Textual's built-in CommandPalette does the modal/fuzzy work; this module
feeds it and disciplines the ORDER. The entry list lives on the app
(``DoxaApp.doxa_commands()``) as plain :class:`PaletteEntry` data -- not
palette machinery -- for two reasons: the tab and attach entries are
dynamic (one per open tab, one per live daemon in the shared registry,
recomputed on every palette open, so a tab opened or closed while the
palette is up can never desync it), and the test suite can assert the
registered surface without driving a modal screen.

The order, unfiltered, is:

1. **New tab** -- always first, always visible.
2. **Open tabs** -- every open tab, in LEFT-TO-RIGHT TAB ORDER (never
   alphabetical): the palette mirrors the tab bar, so the spatial map the
   user already has from Ctrl+←/→ carries over. The active one is marked.
3. **Commands** -- the registry's own grouping, from
   ``doxa.commands.GROUPS`` / ``ordered()``. There is no second ordering
   here: the slash dropdown, ``/help`` and this list all read that one
   sequence, and the app-level entries that have no registry row (Close
   tab, Quit, the inspector) sort into the same groups.
4. **Attach** -- live daemon sessions not open in any tab.

Filtered, the headers collapse and everything ranks by match score, with
open tabs kept ABOVE commands on a tie (a user who opens the palette
mid-work is usually switching tabs, not running a command) -- which is
just yield order plus a stable sort.

Ordering is enforced through the hit SCORE rather than through yield
order, because the palette gathers from several providers concurrently
(Textual's own system commands are in there too) and sorts the pooled
result: a rank carried on the hit is the only thing arrival order cannot
scramble.
"""

from __future__ import annotations

from dataclasses import dataclass
from operator import attrgetter
from typing import Any, Callable

from textual.command import (
    CommandPalette,
    DiscoveryHit,
    Hit,
    Hits,
    Provider,
)
from textual.widgets.option_list import Option

from . import commands as commands_mod

# The two dynamic sections that bracket the registry's own groups. Only
# these three names are declared here; the middle of the list is
# doxa.commands.GROUPS verbatim, so a new command group is added in ONE
# place (the registry) and this list follows.
SECTION_NEW = "New"
SECTION_TABS = "Open tabs"
SECTION_ATTACH = "Attach"

SECTIONS: tuple[str, ...] = (
    (SECTION_NEW, SECTION_TABS) + commands_mod.GROUPS + (SECTION_ATTACH,)
)


@dataclass(frozen=True)
class PaletteEntry:
    """One palette row: which section it belongs to, what it says, what it
    does. Ordering lives in the section plus the sort key -- never in the
    order some caller happened to append."""

    section: str
    label: str
    help: str
    callback: "Callable[[], Any]"
    sort_key: "tuple[int, str] | None" = None
    """Within-section order. ``None`` means "sort alphabetically by label";
    a tuple lets a section impose its own (tabs use tab-bar position, and
    registry commands keep ``commands.ordered()``'s sequence)."""

    def key(self) -> "tuple[int, tuple[int, str]]":
        section = (
            SECTIONS.index(self.section) if self.section in SECTIONS
            else len(SECTIONS)
        )
        return section, (self.sort_key or (1, self.label.lower()))


def ordered_entries(entries: "list[PaletteEntry]") -> "list[PaletteEntry]":
    """The palette's one sort. Section first (:data:`SECTIONS`), then the
    section's own within-order."""
    return sorted(entries, key=PaletteEntry.key)


@dataclass
class SectionHit(DiscoveryHit):
    """A discovery hit that knows its section and its rank.

    ``DiscoveryHit.score`` is a fixed 0.0 and the palette sorts the pooled
    hits of every provider by score, so an unfiltered list yielded in the
    right order can still come out interleaved with another provider's.
    The rank IS the score here: DOXA's entries sort among themselves in
    exactly the order they were ranked, and above Textual's own system
    commands, which score 0."""

    section: str = ""
    rank: float = 0.0

    @property
    def score(self) -> float:  # type: ignore[override]
        return self.rank


class DoxaCommandProvider(Provider):
    """Feeds ``app.doxa_commands()`` into the built-in command palette."""

    def _entries(self) -> "list[PaletteEntry]":
        supplier = getattr(self.app, "doxa_commands", None)
        entries = supplier() if callable(supplier) else []
        return ordered_entries(list(entries))

    async def discover(self) -> Hits:
        """The empty-query view: every entry, in section order, each
        carrying its section (the palette draws a header when the section
        changes) and a rank that pins it there."""
        entries = self._entries()
        total = len(entries)
        for index, entry in enumerate(entries):
            yield SectionHit(
                entry.label,
                entry.callback,
                help=entry.help,
                section=entry.section,
                # Strictly descending, strictly positive: DOXA's own rows
                # keep their order and stay above the score-0 hits of
                # Textual's built-in providers.
                rank=float(total - index),
            )

    async def search(self, query: str) -> Hits:
        """Filtered: no sections, no headers -- match quality decides.

        Entries are yielded in section order and the palette's sort is
        stable, so a tab and a command that match equally well keep the
        tab first."""
        matcher = self.matcher(query)
        for entry in self._entries():
            score = matcher.match(entry.label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(entry.label),
                    entry.callback,
                    help=entry.help,
                )


class DoxaPalette(CommandPalette):
    """The built-in palette, plus section headers.

    Textual's palette has no notion of a header row, so this overrides the
    one method that turns gathered hits into list options and inserts a
    DISABLED option whenever the section changes -- disabled being exactly
    what the slash dropdown's group headers already are: dim, unselectable,
    skipped by arrow navigation. Hits without a section (a filtered search,
    or another provider's rows) produce no headers at all, which is what
    makes the filtered list collapse to pure ranking."""

    def _refresh_command_list(self, command_list, commands, clear_current) -> None:
        ranked = sorted(commands, key=attrgetter("hit.score"), reverse=True)
        rows: list[Any] = []
        section = None
        for command in ranked:
            hit_section = getattr(command.hit, "section", "") or ""
            if hit_section and hit_section != section:
                rows.append(Option(hit_section, disabled=True))
                section = hit_section
            rows.append(command)
        command_list.clear_options().add_options(rows)
        first = next(
            (i for i, row in enumerate(rows) if not row.disabled), None
        )
        if first is not None:
            command_list.highlighted = first
        self._list_visible = bool(command_list.option_count)
        # The hit count is what "no matches" and the busy indicator read:
        # headers are chrome, not hits.
        self._hit_count = len(ranked)
