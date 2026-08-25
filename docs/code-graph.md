# A queryable code graph — specification

Status: **draft for review**. Nothing implemented.

## The idea

An AST-derived graph of the working repository — files and symbols as nodes,
imports and calls as edges — that the agent **queries through tools**, with the
curated file map's *purpose* text living on the file nodes as an attribute.

**One structure, two provenances.** This is the design decision the rest
follows from:

| on a node | comes from | who may write it |
|---|---|---|
| definitions, imports, call edges, `file:line` | the parser | derivation only |
| `purpose` — "tool allowlisting, session-scoped" | a human | the write gate |

An earlier draft of this spec kept the file map and the graph as separate
things, on the reasoning that no parser can produce a judgement about
importance. That reasoning is right about *derivation* and wrong about
*storage*: the judgement is an attribute of a node the parser already found.
Keeping them apart would mean two indexes of the same files, drifting.

What that unification buys, concretely: **the file map becomes a projection of
the graph** — the file nodes that carry a `purpose`, rendered in map order,
still hard-capped at 4400 chars, still injected. Nothing about the injected map
changes. But `purpose` stops being a standalone list and starts sitting on a
node that also knows what imports it, so *"what depends on a load-bearing
file"* becomes one query instead of a grep informed by a hint.

**Provenance must distinguish the two halves.** LORE 0.36.0 records `writer`
and `via` per memory entry precisely so an approved fact and a derived one are
never confused; a node carrying both a parsed edge and a human sentence needs
the same discipline. A `purpose` is memory — it goes through the write gate,
it survives re-indexing, and re-parsing a file must never touch it. A parsed
attribute is disposable and is rebuilt whenever the file's stamp moves.

**Storage: the existing SQLite store, not `networkx`.** The graph shape is
right and `networkx` would express it comfortably, but LORE is stdlib-only by
design — that property is why it installs anywhere, and it has been declined
for heavier reasons than convenience. Nodes, edges and attributes are three
tables beside the FTS5 session index, and the queries this needs (one hop out,
one hop in, symbol lookup) are indexed SQL rather than graph algorithms. If a
genuine traversal need appears later — transitive closure, cycles, centrality —
that is the moment to argue for a dependency, with the use case in hand.

## What it is not

It is not a replacement for the belief store or the session index. The graph
answers *where* and *what depends on what*. Why a thing is the way it is lives
in beliefs and past sessions — which is why the retrieval ladder has four rungs
and this adds a fifth rather than collapsing one.

Explicitly NOT held: types, inferred flow, anything requiring execution. A
dynamic dispatch is not a call edge; a string that happens to match a symbol
name is not a reference.

## Why it is worth building

LORE's own retrieval ladder already positions the map as the thing to consult
*"before any find/grep hunt"* — an admission that grep is the fallback and a
poor one. Concrete work from a single day that grep answered badly:

- *"which code paths set a canonical COMP name?"* — answered by reading five
  files and reasoning about call order; the honest answer turned out to be
  **2 of 7 live paths were covered** by the guard, and finding the other five
  was the entire task.
- *"every `e.<prop> = r.<field>` in `ON CREATE` that `ON MATCH` drops"* — done
  by hand, and the resulting test is the most valuable one in that branch.
- *"is `_lore_slug` resolving through the worktree or the main repo?"* — a
  one-hop caller question that cost a defect in production.

Each is a graph query. None is a grep.

## Scope

**Python only, via the stdlib `ast` module.** LORE is stdlib-only by design and
that is why it installs anywhere; `tree-sitter` would buy multi-language
support at the cost of the property that makes LORE portable. Python covers
this operator's corpus (DOXA, FINCH, LORE itself) completely. A second
language is a later decision with a real dependency argument attached, not a
detail to slip in.

What the graph holds, per module:

- **definitions** — module, class, function, method, assignment targets at
  module scope, with `file:line` and the enclosing scope
- **imports** — what a module pulls in, and from where, including the
  deferred-import-inside-a-function pattern this codebase uses deliberately
- **references** — name uses resolved to a definition **where that is
  decidable**, and left unresolved where it is not (see Honesty below)
- **call edges** — caller → callee, same decidability rule


## The staleness contract — the hard part

A code index that lags the tree gives confidently wrong answers, which is
strictly worse than grep, because grep is always current. This is the property
to get right; parsing is the easy half.

- **Per-file mtime + size**, the same key `labels.memory_fill` already uses.
  A file whose stamp has moved is re-parsed on demand, not on a timer.
- **A query that touches a stale file re-parses that file first.** Freshness is
  per-answer, not per-index; there is never a background sweep to fall behind.
- **A file that cannot be parsed is recorded as unparseable, with its error** —
  syntax errors are normal in a repo an agent is editing, and a file silently
  missing from the index is a lie of omission.
- **Every answer carries its basis**: which files it consulted and when they
  were last parsed. An answer that cannot state that is not returned.
- **Never a background daemon or watcher.** DOXA already has a documented
  no-timer, no-per-frame rule and one daemon per session; a file watcher is a
  second lifecycle to get wrong.

## Honesty about what a parser cannot know

This is where such tools usually oversell, and where this one must not.

- **Unresolved references stay unresolved.** `getattr(obj, name)`, a method on
  a parameter with no annotation, anything through `**kwargs` — the honest
  answer is "not decidable from the AST", not a guess ranked by name match.
- **Same-name collisions are reported as several candidates**, never
  disambiguated by heuristic. `render` exists on many classes.
- **A query with no answer says so** and names the fallback (`lore_session_search`,
  or grep) — the same lesson as the belief search that returned `count: 0` and
  stopped, letting a model conclude LORE knew nothing when the larger corpus sat
  one rung away.
- **The index never claims completeness it does not have.** If 3 of 210 files
  failed to parse, every answer derived from that module set says so.

## Operator surface

Following the existing four (`lore_belief_search`, `lore_belief_show`,
`lore_memory_list`, `lore_session_search`) and the `Operator` shape in
`doxa/operators.py` — `cost`, `read_only`, `is_configured`:

- **`code_symbol_find(name, kind=None)`** — definitions matching a name. Cheap.
- **`code_symbol_refs(name, file=None)`** — uses of a symbol, resolved and
  unresolved reported separately. Medium.
- **`code_module_imports(module, direction="out"|"in")`** — what it imports, or
  what imports it. Cheap. The `direction="in"` half is what "what breaks if I
  change this" actually needs.
- **`code_callers(qualname)` / `code_callees(qualname)`** — one hop, not a
  transitive closure. A closure is a research project and an unbounded reply.

All read-only. All bounded by the 64KB frame cap through the shared `_fit_page`
budget — three RPCs already share it, and a fourth caller must not become a
fourth budget.

## The viewer — a `dependencies` chip and a graph tab

Requested alongside the graph itself: a status-line chip opening the graph in
its own tab, drawn in the terminal.

**The chip.** Beside the beliefs, memory-fill and proposals chips, showing what
it holds — file count, or the count of files carrying a `purpose`. Hidden when
the repo has not been indexed, the same hide-at-zero convention the subagent
and peer chips follow. Name it for what it opens: `deps` or `graph` reads
better than `file-graph` at the width the status row actually has, and the
tooltip carries the long form. Note the row is already contended enough that
v0.50.0's mode chip had to be measured at 40 columns before it could claim a
place — this one must survive the same test or stand down.

**The tab.** A non-session tab, which DOXA now has three precedents for:
`SubagentTranscriptTab`, `ArchivedSessionTab`, and v0.46.0's beliefs browser.
Reuse that shape rather than inventing a fourth; note Ctrl+W and Ctrl+Q both
had to be taught about each new tab kind (v0.46.0 and v0.54.0), so a new one
inherits that obligation — a tab that cannot be closed is a defect that has
already shipped twice here.

**Drawing a graph in cells is the hard part, and this project has just learned
how that goes.** Four independent attempts at a small block-drawn logo hit four
different walls: half blocks leave seams, some fonts render `█` at reduced
height, Geometric Shapes risk tofu, and detail dies at small sizes. A general
graph layout is strictly harder. So:

- **Start with a tree, not a diagram.** An indented dependency tree from a
  focus node — what it imports, what imports it, one hop expandable — is
  legible in any font, works at 80 columns, and is what the questions in *Why
  it is worth building* actually ask for. `SessionSearch`'s expandable folds
  (v0.21.0) are the working precedent for exactly this interaction.
- **A drawn layout is a second, optional mode**, and it must degrade to the
  tree rather than to a mess. If it ships at all it should be measured on a
  real repo the size of FINCH before anyone commits to it, because a
  force-directed layout of 200 modules in a terminal is a smear whatever the
  font does.
- **Never a picture where a list would answer.** The failure mode is a diagram
  that looks impressive and cannot be read, next to a list that could have been
  read at a glance.

**What the viewer must not become.** A second source of truth. It renders what
the operators return; if the tab can show something the tools cannot, the tools
are missing a query and the fix belongs there. The same rule kept the beliefs
browser and the beliefs chip agreeing.

**Purpose is editable from here, or it is not editable at all.** The `purpose`
attribute is memory and goes through the write gate — so if the tab offers
editing, it stages a proposal exactly as v0.46.0's per-row approve does, with
the same arming discipline on anything destructive. Read-only is an acceptable
first version; a silent write is not.

## Where it lives

`lore_core`, not DOXA — the same reasoning that put beliefs and the session
index there. A code graph is memory about a repository, it is useful to the
LORE CLI and to any client, and DOXA already projects `lore_core`'s operators
onto its own tool surface (`operators_mod.to_sdk_tools`). Building it in DOXA
would make it a DOXA feature that LORE users cannot reach.

Storage: the existing SQLite store (`lore_core.store`), a new table set beside
the FTS5 session index. Not a second database.

## What this does not fix

- It does not tell you what matters. That is the file map, and it stays human.
- It does not survive into other languages without a dependency decision.
- It does not answer "why is this here" — that is the belief store and the
  session index, which is why the retrieval ladder has four rungs and this adds
  a fifth rather than replacing one.

## Testing bar

- a symbol defined in three places returns three candidates, not one ranked guess
- an unparseable file is reported as unparseable and does not vanish
- an answer names the files it consulted and when they were parsed
- a file edited after indexing is re-parsed before it is answered from —
  asserted by editing a file mid-test, not by trusting mtime logic
- an undecidable reference (`getattr`, `**kwargs`) is returned as undecidable
- a query with no hits names the fallback rung
- the reply fits the frame cap with a repo the size of FINCH

## Open questions

1. **Does it index the worktree or the main repo?** Every DOXA session runs in
   a worktree (v0.17.0), and `_lore_slug` had to be fixed to resolve through
   `peers.main_repo_root_of` for exactly this class of confusion. A graph
   indexed per-worktree would be rebuilt per session; one indexed per main repo
   would be wrong about a session's uncommitted edits. This needs deciding
   before anything is built.
2. **How much of a repo is worth indexing?** `.venv`, `node_modules` and
   vendored trees are most of the AST and none of the value.
3. **Does the agent get told the graph exists?** The retrieval ladder is
   injected prose; adding a fifth rung costs snapshot characters, and today
   that prose still names CLI commands rather than the operator names the model
   actually holds — a gap worth fixing in the same pass.
