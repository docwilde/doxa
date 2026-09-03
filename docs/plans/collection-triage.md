# Collection triage — colour, default labels, and ordering by urgency

Status: **Parts 0, 1 and 1b SHIPPED in v1.2.0. Parts 2 and 3 are NOT
implemented.**

What shipped: the two status glyphs and the named `CTX_GLYPH_PCT`
threshold (Part 0); project colour assigned by a stable hash of
`repo_root` into a fixed named palette, overridable by name in
`~/.doxa/config.toml`, with auto-grouping by project (Part 1); and the
rail entry as a PANE GROUP with most-urgent-wins aggregation over its
tabs, a count chip that says how many it holds and which of them the
state came from, and age as a dim rather than a recolour (Part 1b). The
module is `doxa/triage.py`; the tests are `tests/test_triage.py`.

What did NOT ship, deliberately:

* **Part 2 (default labels)** — nothing of it. `/collection new` still
  takes a name and nothing derives one.
* **Part 3 (ordering by urgency)** — the RANKING shipped
  (`triage.urgency`), because Part 1b's aggregation needs it. The
  REORDERING did not, and it is the half with the hazard this spec was
  written for: a list that moves under your click, at the needs-input
  blink's 2 Hz. It ships with its settling boundary and its
  `collection_sort` setting or it does not ship.
* **Glyphs on the tab header** — the scope question this spec leaves
  open, answered: deferred, for the width reason set out in
  `tests/test_triage.py::test_the_glyphs_are_deliberately_NOT_on_the_tab_header_yet`.

The check this spec owes itself (bottom of this file) is answered YES and
is asserted by
`tests/test_triage.py::test_the_rail_reads_with_every_colour_stripped`.

Written before the work because the third part (automatic ordering) is
the one that can make the rail worse rather than better, and the failure
mode is not obvious until someone is using it.

## What provoked it

Requested 2026-09-03: *"color coded pane/tab groups, default label
customer+project+task, automatic sorting of pane/tab groups to color coded
customer and urgency (which tab needs input/confirmation/approaches ctx
limit)"*

Three features that interlock. They are separable and should ship
separately, in this order, because each is useful without the next and the
third is the one that needs the most care.

## What exists to build on

v1.0.0's **collections** (`doxa/collections.py`, the `collections` key in
the tabset record): a name, an ordered list of session ids, collapsed
state. The rail (`doxa/ui/sidebar.py`) already renders one `SidebarLine`
per session, reusing lines in place rather than remounting, and already
carries the marks this feature wants to sort by — `-done-unseen`, the
needs-input blink, the `-staged` tint — derived once from the tab header's
own logic rather than a second time.

Context percentage exists per session (`doxa/ui/labels.py`'s ctx chip
path). It is **the CLI's own accounting**, and an unreported limit reads
`?` and stays `?` — see `/context`'s three honesty rules. Anything sorting
by "approaching the context limit" inherits that: a session whose limit was
never reported cannot be ranked by it, and must not be silently treated as
0%.

## Part 0 — glyphs, and why they come first

Added to the request 2026-09-03: *"session status indicated by glyphs
(needs input, >= 50% ctx full)"*.

This is not a fourth feature bolted on; it **answers the check this spec
set itself** and reorders the rest. A glyph is a status channel that
survives a monochrome terminal, a colour-blind reader and a screenshot, so
once status is glyphed, colour (Part 1) is genuinely redundancy rather
than a channel carrying meaning alone.

Two states are named, and they are the two worth interrupting for:

| state | means |
|---|---|
| **needs input** | the session is stopped, waiting for a human — DOXA is idle *because of you* |
| **context ≥ 50%** | half the window is gone; still recoverable, and only before it is not |

`50%` is the owner's number, and it is a **named constant**, not a
literal: it is the first threshold anyone will want to tune, and a second
one (75%? 90%?) is a plausible follow-up. One glyph either way — a scale
of five glyphs is a gauge, and the ctx chip already is one.

Constraints the project has already paid for:

- **Font coverage is a real risk, and DOXA has been burned by it.** The
  banner work rejected Geometric Shapes for tofu risk; v0.81.0's draughts
  glyphs (⛀ ⛁ ⛶) ship only with an `ascii` fallback behind a setting
  because the same risk was accepted knowingly. The status glyphs must
  either come from the narrow set already proven in this codebase (`⎇`,
  `⧉`, `⚒`, `⏳` all ship today) or carry the same fallback. **Decide
  which, and do not silently introduce a fifth codepoint class.**
- **The glyph is not a second derivation.** The rail already computes
  these marks from the tab header's own logic; the glyph renders what is
  already known. v1.0.0 measured what re-deriving costs (+22% layout
  time).
- **Unknown context is not `< 50%`.** A session whose limit was never
  reported gets **no** ctx glyph — not the absence-of-warning that reads
  as "plenty of room". `/context`'s `?` rule, one level down.
- Where they appear: the rail's rows, and — since the same facts drive
  it — consider the tab header, so a user with the rail hidden is not
  blind to the thing the rail exists to show. Say whether that is in
  scope or deliberately deferred.

## Part 1 — colour, keyed to the PROJECT

Refined by the owner 2026-09-03: *"the color coding and grouping should be
per project, with an accent color/glyph for its state"*.

This is a better design than the draft above it and it changes two things.

**Two channels, two jobs, and they must not be the same channel:**

| channel | carries | derived from |
|---|---|---|
| **base colour** | *which project* — identity | `repo_root`, already on every pane and every `PeerInfo` |
| **accent (glyph, and colour on the glyph)** | *what state* — urgency | Part 0's marks |

Identity is stable and says nothing about urgency; state changes minute to
minute and says nothing about identity. Painting both into one hue is how
a rail stops being readable — a session cannot be "the red project" and
"the red because it needs you" at once.

**Grouping becomes derivable.** v1.0.0's collections are user-named and
manual, and the draft assumed that stayed the only grouping. Per-project
grouping needs no naming at all: `repo_root` is the same key `peers.py`
already uses for `scope_key`, so two sessions on one repo already agree
they are related. **A project group is the default grouping; a manual
collection overrides it** for the case the user genuinely wants (three
repos that are one piece of work). Neither replaces the other, and a
session belongs to exactly one of them — the collection if it is in one,
its project otherwise.

**The colour must be assigned, not configured.** Asking a user to pick a
colour per repo is a chore that will not be done, and an unconfigured
project would fall back to no colour, which is where most projects would
stay. Derive it: a stable hash of `repo_root` into the fixed palette, so
the same repo is the same colour on every machine and across restarts with
nothing stored. Keep an override in `~/.doxa/config.toml` for the person
who wants `doxa` to be blue, and store the palette NAME, never a hex.

**Collision is expected and must not be hidden.** A small palette and a
hash means two projects eventually share a colour. The name is still the
primary channel (Part 0's rule), so a collision costs redundancy, not
meaning — but the rail must not imply two same-coloured groups are
related. Say what happens at collision rather than discovering it.

This also **shrinks Part 2**: with grouping keyed to the project, the
default label's `project` half is the group itself, and `customer` becomes
an optional prefix on a project — one line in `~/.doxa/config.toml`, absent
when unset, exactly as Part 2 already recommends.

- **Colour is never the only channel.** DOXA has decided this twice
  already: `/context`'s grid ships an ASCII fallback because colour is
  load-bearing there, and the `sub:` chip carries a glyph as well as a
  tint. A colour-blind operator, a monochrome terminal, and a screenshot
  in a bug report must all still convey which project a row belongs to —
  the project's **name** is the primary channel and colour is redundancy,
  not a replacement for it.
- **A fixed palette, not free hex.** Terminals vary; a user-chosen
  `#3a3a3a` is unreadable on half of them. Offer a small named set chosen
  against `theme.tcss`'s existing ramp, and store the NAME in the record,
  not the resolved colour — so a future theme change re-resolves rather
  than stranding a colour that no longer contrasts.
- Default: **no colour**. A collection without one renders exactly as it
  does today. Hide-at-zero, again.

## Part 1b — what a rail entry IS, and where its state comes from

Refined by the owner 2026-09-03: *"the entries in the session pane on the
left should each hold the actual panes states, each can contain a session
with potentially multiple tabs, the session state needs to be fed by any of
the sessions and of each tab it includes. Old sessions fade color to grey.
Uncategorized entries are grey by default. Entries should be groupable,
with editable group labels, autogrouped by association to a project."*

This changes the row model v1.0.0 shipped, and the change is right.

### An entry is a pane, not a session

v1.0.0's rail renders **one row per session**. Since v0.97.0 the window is
a tree of `PaneGroup`s and **each group owns its own tabs**, so a single
visible pane can hold three sessions of which two are invisible. A rail
that lists sessions flat cannot show that structure; a rail that lists
*panes* can, and the pane is the thing the user actually navigates to.

So: **one entry per pane group**, and the tabs it holds are its members.
Whether an entry expands to show its tabs, or shows only an aggregate with
a count, is an implementation choice — but the entry must be able to say
*how many* it holds, or a three-tab pane looks identical to a one-tab pane.

### State aggregates upward, and the rule must be "most urgent wins"

*"fed by any of the sessions and of each tab it includes"* — an entry's
state is the **maximum urgency over its members**, using Part 3's ranking:
needs input > context ≥ 50% > staged > done-unseen > nothing.

Two consequences to implement deliberately:

- **A hidden tab's state must reach the entry.** That is the whole point:
  the invisible tab needing input is exactly what today's rail cannot
  surface. v1.0.0 already ORs marks over a group's leaves for the tab
  header — reuse that derivation, do not write a second one.
- **The entry must say the state is not its visible tab's.** Otherwise a
  user opens the pane, sees a calm active tab, and concludes the rail lied.
  A count, a second glyph, or the member row itself — decide, and pin it.

### Grey means two different things, and that is a collision

The request puts two facts on one appearance:

- *"Old sessions fade color to grey"* — an age/liveness statement
- *"Uncategorized entries are grey by default"* — a grouping statement

A grey row is then ambiguous: **old, or ungrouped?** Both are true of some
rows and neither of others, and the user cannot tell which they are
looking at. This is the same failure Part 1 avoids by splitting identity
from urgency, appearing one level down.

**Resolution: grey is the ABSENCE of a project colour, and nothing else.**
An ungrouped entry has no project, so it has no colour — grey is what "no
colour" looks like, not a colour that means something. Age is then a
*separate* channel: **dim** the row (reduce contrast) rather than recolour
it, so an old entry in a coloured project stays that project's colour,
faded — which is what "fade to grey" actually describes, and it composes
with grouping instead of competing with it.

**"Old" needs a definition, and it is not obvious.** Candidates, and the
spec does not settle it: ended (`_ended_this_run`), detached with no
client, or idle for N minutes with the session still live. These are
different facts with different usefulness — a detached session doing work
is not old, and an idle attached one might be. **Pick one, name the
constant, and say what it is not.**

### Grouping: automatic by project, editable by hand

Both, and they compose the way Part 1 already sets out: **auto-grouped by
project** (`repo_root`, derivable, no naming needed), with a **manual
collection overriding it** where the user says so, and group labels
editable in either case. An auto-group's label defaults to the project
name and stays editable — and editing it must not convert the group into
something that stops tracking the project, or the next session on that repo
lands in a second group with the same meaning.

## Part 2 — the default label

Today `/collection new` takes a name. The request is that a new
collection's default name be **customer + project + task**.

**Only one of those three is derivable, and that must be said plainly.**

- **project** — yes. `repo_root`'s basename, already on every `PeerInfo`
  and every pane.
- **task** — partly. The session's first prompt is already the source of
  `display_name()`/`title`, capped at 72 chars. It is a reasonable *task*
  proxy and nothing better exists without asking.
- **customer** — **no.** Nothing in DOXA knows a customer. It is a human
  fact about why the work exists. Three ways to get one, and the spec
  should pick the least magical:
  1. a per-repo mapping in `~/.doxa/config.toml` (`customer = "acme"`
     under a repo key) — explicit, and the config is already the trusted
     non-repo-local source (`plugin-api.md`'s rule);
  2. a path convention (`~/work/<customer>/<repo>`) — zero configuration
     and wrong the moment someone's layout differs;
  3. ask on first use and remember it.

  **Recommendation: (1), with the field simply absent when unset.** A
  label that reads `acme · doxa · fix the picker` when configured and
  `doxa · fix the picker` when not is honest; one that guesses a customer
  from a directory name and gets it wrong is worse than no customer at
  all, because a mislabelled collection is acted on.

The default is a **starting point, not a lock**: `/collection rename`
already exists and the name stays user-editable. Deriving it must never
overwrite a name the user has set.

## Part 3 — ordering by urgency

The one with a real hazard.

### The hazard

**A list that reorders itself while you are looking at it loses your
click.** You see the row you want, you reach for it, a background session
starts needing input, the order changes, and you select the wrong session.
This is not hypothetical: the needs-input blink runs at 2 Hz, so a naive
"re-sort whenever a mark changes" reorders the rail twice a second.

Three ways to have ordering without that, and the spec should pick one
rather than discovering the problem in use:

- **Sort on a settling boundary, not on every change.** Re-sort when the
  rail is opened, when a collection is expanded, and after N seconds of no
  mark changes — never mid-blink.
- **Never move a row under the pointer/selection.** Compute the new order,
  apply it only when the rail is not being interacted with.
- **Sort collections, not rows within them.** Coarser, much less
  disruptive, and it still answers "which of my customers needs me now".

**Recommendation: sort collections, and leave row order alone.** It
delivers the actual ask — *which group needs attention* — at a fraction of
the disruption, and row order inside a collection is something the user
deliberately set.

### The ranking

Urgency is a total order over four signals, and they are not equal:

1. **needs input** — a session is stopped, waiting for a human. Nothing
   else competes; this is the only state where DOXA is idle *because of
   you*.
2. **approaching the context limit** — real, time-sensitive, and
   recoverable only before it hits. Needs a threshold; make it a named
   constant, and see the honesty note above about `?`.
3. **staged proposals** — work waiting for approval, not blocking.
4. **done, unseen** — finished, nothing at risk.

A collection ranks by its **most urgent member**, ties broken by the
existing order so the ranking is stable rather than arbitrary.

**A session whose context percentage is unknown does not rank at 2.** It
ranks as if the signal is absent, and the rail must not imply it was
measured. This is `/context`'s rule, one level up.

### Off by default

Ordering changes what the user sees without being asked. `collection_sort`
(`manual` | `urgency`), default **manual** — the order the user set. The
setting lives in the settings registry beside `context_grid`, and
`/collection sort` toggles it.

## Risks, named

1. **Colour as the only channel** — addressed above; the guard is that
   every test asserting a collection is identifiable must pass with colour
   stripped.
2. **Reordering under the user** — the reason Part 3 recommends sorting
   collections only, on a settling boundary.
3. **A derived label that lies** — a guessed customer is worse than none.
4. **Sorting makes the rail's own cost per-mark-change**, and the rail
   already learned this: v1.0.0 found `refresh_sidebar_marks` running a
   full derivation on every mark toggle of every hidden window, costing
   +22% layout time. Any ranking must be computed from the marks the rail
   already has, not by re-deriving, and must not run for a hidden rail.
5. **`?` treated as 0%** — the failure that turns an honesty rule into a
   wrong answer.

## Scope

**In:** status glyphs for *needs input* and *context ≥ 50%*; a fixed
colour palette per collection stored by name; a derived
default label with customer from config only; collection-level ordering by
most-urgent member, off by default, on a settling boundary.

**Out:** per-row sorting inside a collection; user-defined hex colours;
inferring a customer from anything; cross-window or cross-machine
collections; auto-creating collections.

## The check this spec owes

Every spec since v0.91.0 has owed itself one, and each found something.
**Can a collection AND every session's status be identified with colour
stripped entirely?** Render the rail monochrome — a screenshot, a
colour-blind operator, a terminal that ignores styling. Part 0 is what
makes the answer yes for status; the collection's own name is what makes
it yes for grouping. If either needs colour to be legible, colour is doing
work a glyph or a name should be doing.
