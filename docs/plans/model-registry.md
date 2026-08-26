# A model registry — properties, provenance, and the spawn seam

Status: **draft for review**. Nothing implemented. `doxa/providers.py`,
`ModelInfo` and `ModelProvider` are exactly as they are today; this spec
proposes fields and a seam, not code.

## What exists today

`ModelInfo` (`doxa/providers.py`) is three fields: `id`, `display_name`,
`source`. `source` is `"api"` or `"fallback"`, one value per
`list_models()` call, describing which of three resolution tiers produced
the *listing* — never anything about what a model in that listing can
actually do. `ModelProvider` is listing-only by its own docstring: "what
the model picker needs from a provider… A second provider is a new class
satisfying this Protocol, never a branch inside the picker's own code."

`docs/plans/plugin-api.md`'s extension point 4 assessed this Protocol
directly (v0.34.0) and recorded the finding in `providers.py`'s own
docstring: right shape for **half** of it. "The catalog half is complete:
the picker asks a provider what it can offer and never branches on who the
provider is. The session half is not here at all — spawn, send, interrupt
and the event stream are what `SessionEngine` and `EngineClient` already
agree on informally… and there is no Protocol naming it." That finding is
this spec's starting point on the spawn side; the properties side starts
from a narrower gap — `ModelInfo` is "enough for a human picker, not
enough for an agent to choose a model for a task" (this spec's brief).

## Why it is worth building

Not a hypothetical gap. DOXA's own connect-time code already wants exactly
one of the facts this registry would hold, and does not have it:

`show_reasoning()` (`doxa/engine.py:802-827`) decides whether to ask for
visible reasoning. It cannot special-case by model, because "self.model is
often still None here (the real model only becomes known from the CLI's
own init message, AFTER connect)" — so OFF means "omit the `thinking` key
entirely" rather than asserting `{"type": "disabled"}`, because "Claude
Fable 5, Claude Mythos 5 and Claude Mythos Preview reject that outright
(thinking cannot be turned off on those models at all)." That is a real,
measured, per-model behavioral difference DOXA's own options-builder
already routes around with a blanket workaround, for lack of a table that
could say *which* models are in that set. A registry entry that answered
"is thinking optional or mandatory for this id" would let that workaround
become a decision instead of a guess.

The picker has already needed a second, smaller version of this problem:
`open_model_picker` (`doxa/session/chips.py:634-658`) shows a
`"model catalog: static fallback — the Anthropic Models API is not
reachable under this session's OAuth auth"` note whenever the listing
itself degraded to tier 3. That note is the one-field precedent for the
whole of this spec's "Visibility" section below — generalized from "the
whole catalog is a guess" to "this specific number you're looking at is a
guess."

## Provenance per field, not per record

`source` on today's `ModelInfo` already distinguishes `"api"` from
`"fallback"` — but per **record**, describing where `id`/`display_name`
came from. A registry rich enough to hold `context_window` or a price
cannot reuse that single field, because those numbers do not share a
source with the listing they sit next to: DOXA's live-API tier
(`_try_api`, `providers.py:153-187`) returns `id` and `display_name` only —
`anthropic.Anthropic().models.list()` carries no price or window size.
Every capability fact this spec adds is necessarily either hand-maintained
(a static table someone typed in and dated) or unknown. Presenting that
next to a genuinely live-fetched `id` under one `source` field would flatten
"measured this run" and "somebody's best guess a year ago" into the same
word — which is exactly the failure `docs/plans/code-graph.md` already
named for a different kind of node ("LORE 0.36.0 records `writer` and `via`
per memory entry precisely so an approved fact and a derived one are never
confused; a node carrying both a parsed edge and a human sentence needs the
same discipline").

So: capability fields carry their own provenance, wrapped rather than bare.

```python
@dataclass(frozen=True)
class Measurement:
    """One fact about a model, with where it came from. Never a bare
    number or bool -- a bare 0 or False cannot be told apart from "checked
    and it's zero" (see ctx_absolute_text's identical reasoning for context
    windows, doxa/ui/labels.py:474: "An unknown LIMIT is `?`, never a
    substituted 200000")."""

    value: "int | float | str | None"
    source: str  # "api" | "static" | "unknown"
    as_of: "str | None" = None  # ISO date the static entry was last checked
```

`source == "api"` never fires today — no live tier returns capability data
— and is defined now so a future Models API surface that *does* (or a
second provider whose API returns pricing) has somewhere to report it
without a schema change. `source == "static"` is a project-maintained
table entry, always carrying `as_of`; a static entry without a date is a
bug, not a degraded-but-valid one. `source == "unknown"` is the honest
default for anything not in the table — an unrecognized id, a
fallback-tier alias with no size ever attached to it, or a field nobody has
typed in yet.

**What an agent does with `source == "unknown"`:** never treats the
`value` as a decision input. Not as zero, not as the smallest known value,
not as a conservative placeholder pulled from a similar-looking model — the
same rule `_as_tokens` already enforces for token counts ("a non-numeric,
negative or absent count is UNKNOWN, and an unknown key is simply absent…
coerced to 0 it would paint as '0 tokens used', which is a confident lie").
An agent that needs the fact and finds it unknown asks the user, or falls
back to whatever conservative default the caller already had before this
registry existed — the registry is additive information, and its absence
must degrade to "as if the registry didn't exist," never to a wrong number
with high confidence attached.

## Which criteria earn their place

A registry nobody can maintain is worse than none — the phrase is
deliberately not hedged. Every field below was tested against "does DOXA,
or a maintainer, have a real, arguable way to fill this in," not "would an
agent like to know this."

| criterion | in / out | why |
|---|---|---|
| `context_window` | **in** | Directly answers "will this task's prompt, tools and history fit" — the single most decision-relevant number for picking between models. Sourced only as `static`/`unknown` today (see above); worth publishing anyway because `unknown` is itself useful information, and a static table beats nothing for the common case (a handful of well-known ids). |
| `input_price_per_mtok`, `output_price_per_mtok` | **in, as a pair** | Needed for the one routing decision this registry actually enables today — "use the cheap model for a grep-shaped subtask." Priced separately because the ratio between them varies by model and collapsing to one number hides exactly the fact a cost-aware chooser needs. Static-table only; no live source exists anywhere in this codebase for pricing. |
| `thinking` (`"unsupported"` \| `"optional"` \| `"mandatory"`) | **in** | Not speculative — see "Why it is worth building" above. This is the one field with a concrete, cited DOXA code path that currently works around not having it. |
| a coarse tier | **in, but not a new taxonomy** | Reuses `short_model()`'s existing alias word (`doxa/ui/labels.py:123-138`: `haiku`/`sonnet`/`opus`/`fable`, already extracted from a model id today for the tab label) rather than inventing a `fast`/`balanced`/`flagship` scale. Anthropic's own naming already carries a rough capability ordering, DOXA already computes the word, and a second taxonomy meaning almost the same thing as the first is exactly the kind of drift this spec is trying to avoid elsewhere. |
| structured tool calls | **out** | Every model DOXA can select today goes through the same `claude_agent_sdk` tool-calling path — the property is constant across every id in the current catalog and therefore has zero discriminating power. It is a fact about the **provider's session engine**, not about an individual model id (a provider whose engine cannot dispatch tools fails at the still-unwritten session Protocol, before `list_models()` is even relevant). If a future provider genuinely varies per-model, this belongs on that provider's own engine description, not resurrected here as a per-model boolean that is `True` for every row that exists today. |
| a benchmark or quality score | **rejected outright** | See below — this is the one candidate worth arguing against explicitly rather than just marking out. |

### Why no benchmark/quality table

Three independent reasons, not one:

1. **Nobody in this project can keep it current.** A benchmark number is
   either the provider's own marketing figure or a third-party eval
   snapshot, and both go stale within a model generation. An unmaintained
   score is worse than none, because it *looks* current — a `Measurement`
   with a six-month-old `as_of` at least says so; a bare quality number
   invites exactly the "confidently wrong" failure this whole spec exists
   to prevent.
2. **A single score conflates task types an agent's actual choice does
   not.** The registry's whole value is letting an agent reason about
   *this* task against context/price/thinking — a benchmark average already
   baked in tasks unlike this one, which is strictly less useful than the
   three concrete fields above, not complementary to them.
3. **This codebase has already watched a three-tier resolution order mostly
   fail** — tier 1 (live API) unreachable under DOXA's normal auth, tier 2
   (SDK catalog) a structural no-op, only tier 3 (four static aliases)
   actually live. A benchmark table would be a *fourth* source of truth
   with weaker grounding than any of the three that already mostly don't
   fire. Adding a source this shaky under a name ("quality") that reads as
   authoritative is a worse bet than the three tiers this project already
   has evidence about.

## The spawn seam

`ModelProvider` lists; it does not run anything. What "picking a model for
a task" needs beyond listing splits into two very different reaches.

**Reachable from here: a session DOXA itself owns.** DOXA already has both
halves of this — a connect-time choice (`model=self.model` in
`SessionEngine._build_options`, `doxa/engine.py:1750`) and a live,
reconnect-free switch (`SessionEngine.set_model`, `doxa/engine.py:2188-2210`,
a control request with "no reconnect: the transcript, the daemon, the
replay ring, the peer presence and every hook stay exactly as they are").
What this spec adds here is not a new mechanism — it is a richer `ModelInfo`
feeding the *existing* one: `open_model_picker` reads
`context_window`/price/`thinking`/tier the same way it reads `id` and
`display_name` today, so a human (or, later, an agent with the same picker
surface) chooses with the properties visible, not just a name.

**Not reachable from here: a Task-spawned subagent.** DOXA does not spawn
subagents today — the `claude` CLI's own `Task` tool does, entirely inside
the spawned subprocess. DOXA only *observes* it (the subagent tracker and
`SubagentTranscriptTab`, `tests/test_subagent_tracker.py`: a `Task` tool
call with `subagent_type: "Explore"` arrives as an event the CLI's own
agent chose to emit, rendered read-only, never issued by DOXA). Two
measured facts make "pick a model for that subagent" unreachable, not
merely unbuilt:

1. **No LORE snapshot reaches it.** DOXA's one context-injection channel is
   `system_prompt={"append": "[LORE SNAPSHOT]\n" + snapshot}` on the
   *parent* session's own `ClaudeAgentOptions` (`doxa/engine.py:1814-1818`).
   A parent's `--append-system-prompt` does not propagate to a
   `Task`-spawned subagent (measured). If the one mechanism DOXA has for
   putting anything into a session's context does not reach a subagent,
   nothing built on top of that mechanism does either.
2. **No addressable session id.** Every DOXA-owned session gets a uuid
   DOXA mints and the CLI is asked to `resume`/`session_id` against
   (`doxa/engine.py:1750-1789`) — the id that makes `set_model`, the peer
   registry and `/resume` all work. A `Task` subagent is a tool call
   inside the CLI's own agent loop, not a second `ClaudeAgentOptions` DOXA
   builds; there is no id surfaced to DOXA to switch, register, or resume.

So "pick a model for a subagent" is not DOXA's design gap to close — it is
outside DOXA's engine boundary as that boundary is measured to exist today.
If the CLI/SDK ever exposes a per-subagent model knob, DOXA's role is to
*surface* the catalog this spec proposes through whatever picker that knob
gets, not to build a second spawn path of its own.

**What this leaves for the still-unwritten session Protocol** (plugin-api's
extension point 4, "spawn, send, interrupt and the event stream… feature
work for multi-provider engines, not something a refactor gets to invent"):
this spec does not write that Protocol — `providers.py`'s own docstring
says the catalog module should "stop at listing and grow a second module
instead," and a second spec inventing a Protocol nobody has built against
yet would be exactly the speculative complexity this house style avoids.
What it does commit to: whatever that Protocol's `spawn(...)` eventually
takes, the `id` and `thinking` fields this registry adds are the two a
multi-provider `_build_options`-equivalent would need immediately (an id to
pass, and whether to send a disabled-thinking key or omit it) — the same
two facts DOXA's own single-provider `_build_options` already needs and
partly lacks today.

## Visibility

A model chosen too small does not error — it produces worse work that
looks like work, and there is no honest way to detect that after the fact
(see the benchmark-table rejection above: scoring the *output* is exactly
the move this spec refuses). Legibility has to live on the **input** side
of the choice, not the output.

Two mechanisms, both extending something that already ships rather than
inventing new UI:

- **The active choice is already always visible.** The model chip and the
  tab label's `short_model()` word show what is running, per tab, at all
  times — this spec changes nothing there.
- **The basis for the choice becomes visible at the moment it is made.**
  `open_model_picker`'s existing degraded-listing note ("model catalog:
  static fallback…") generalizes: whenever a field the picker is showing
  has `source in ("static", "unknown")`, the picker says so next to that
  field, not only when the whole catalog fell back. A human picking a model
  on the strength of a `context_window` dated eight months ago sees the
  date; a human picking on the strength of an `unknown` price sees that
  it's a guess before they act on it, not after.

Explicitly not attempted: inferring, from a finished turn, that a "bigger"
model would have done better. That is the benchmark instinct wearing a
different name, and it has the same maintenance and false-confidence
problems.

## One vocabulary, two directions

`docs/plans/peer-publishing.md` proposes `PeerInfo.provider` and
`PeerInfo.model` — what a session says about *itself*. This spec's catalog
holds `ModelInfo.id` and a provider dimension for what the ecosystem knows
about *that model*. They have to agree on the same strings, or a peer
publishing `model: "claude-sonnet-4-5"` and this catalog's `id` field
silently drift into two dialects describing the same thing.

The two specs share the **vocabulary**, not a type. Neither module imports
the other:

- `provider` is a short id (`"claude"` today) — the same one
  `doxa.ui.labels.PROVIDER_GLYPHS` already keys on. Both `peers.py` (a
  session naming its own provider) and `providers.py` (a catalog entry's
  provider) point at that one existing table rather than either owning it.
- `model` / `id` is whatever string `/model`, `set_model`, and a catalog
  row all agree names one model — an alias or a resolved id, no
  transformation between the two specs' fields.

A peer's self-reported `model` is not guaranteed to resolve in this
catalog — a bespoke alias, a model DOXA has never listed, or (per
peer-publishing.md's trust section) simply a wrong string from an untrusted
self-description. A lookup that finds nothing reports "no catalog data for
this model," the same honesty rule `docs/plans/code-graph.md` already
commits to for a query with no hits ("a query with no answer says so").
It is never an error, and it is never silently treated as
`context_window: unknown` conflated with "this peer is lying" — those are
different failure modes and a reader should be able to tell them apart from
the message, not guess.

If the coupling ever needs to be tighter than "same string convention" —
a shared `ProviderId` type, a single source-of-truth list of valid provider
ids — that is a legitimate later addition, in a third, neutral module
(`doxa/ui/labels.py`'s `PROVIDER_GLYPHS` is the closest existing candidate).
Neither this spec nor `peer-publishing.md` introduces one speculatively.

## Testing bar

- a catalog entry with `context_window.source == "unknown"` is excluded
  from any size-based filter or sort a picker (or future selection helper)
  performs — never coerced to `0` or an average of known entries
- the picker shows a degraded-provenance note whenever a *field it is
  displaying* has `source in ("static", "unknown")`, not only when the
  whole catalog is on the fallback tier — extends the existing
  static-fallback-note test rather than replacing it
- a peer's self-reported `model` (from `peer-publishing.md`) that has no
  entry in this catalog resolves to an explicit "not in catalog" state
  wherever it is looked up — never a `KeyError`, never a fabricated entry
- `SessionEngine.set_model`'s existing reconnect-free behavior
  (`doxa/engine.py:2188-2210`) is unchanged by this spec — the catalog is a
  read-only decision input, never a new mutation path
- `ModelProvider.list_models()`'s existing three-tier resolution order and
  its record-level `source` marking (`"api"` / `"fallback"`) are unchanged
  — the new capability fields degrade to `Measurement(source="unknown")`
  under the fallback tier rather than the tier itself growing guessed
  numbers
- a static-table entry with no `as_of` is treated as a data error worth
  surfacing (a bad table entry), not silently accepted as equivalent to a
  dated one

## Open questions

1. **Where does the static table live, and who keeps it dated?** A
   checked-in literal in `doxa/providers.py`, versus something LORE-adjacent
   that could in principle be updated without a DOXA release. Not settled;
   whichever it is, an entry with a stale `as_of` and no process for
   noticing decays exactly like the thing this spec replaces.
2. **Does the reused tier word actually order anything, once a second
   provider exists?** `short_model()` degrades to a name's first
   dash-segment for anything it doesn't recognize (`labels.py:138`) — fine
   as a *label*, unclear whether that degraded value is usable for ranking
   across providers whose naming carries no such convention.
3. **Per-field provenance notes in the picker, once four or five fields can
   each be independently static/unknown** — does that read as helpful
   disclosure or as visual noise? Needs a real UI to answer, not this spec.
4. **What does the eventual session Protocol (plugin-api's extension point
   4) actually want from `ModelInfo`** — the full `Measurement`-wrapped
   shape, or a plainer, already-resolved options record that has thrown
   provenance away by the time it reaches spawn? That Protocol does not
   exist yet and this spec deliberately does not write it.
5. **Is per-field `Measurement` worth its ceremony for a set this small** —
   four or five capability fields — or would a documented convention
   ("prices and windows are always static; ids are always live-or-fallback")
   get most of the honesty without a wrapper type on every field? Argued
   for the wrapper above; a real implementation attempt could reasonably
   go the other way.

## See also

- `docs/plans/peer-publishing.md` — publishes `provider`/`model` per
  session; "One vocabulary, two directions" above is the other half of
  that spec's argument for keeping `context_window` off `PeerInfo`.
- `docs/plans/plugin-api.md`, extension point 4 — the still-unwritten
  session Protocol this spec's "spawn seam" section stops short of writing.
- `docs/plans/code-graph.md` — "one structure, two provenances" is the
  parallel this spec's `Measurement` wrapper is drawn from, applied to a
  model's capabilities instead of a code graph's nodes.
