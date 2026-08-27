# Your Agent Can't Read Your Database

*Why slowstore is the storage layer for prototyping with LLMs — ~10 min read*

LLM-assisted development runs at the speed of one loop: propose, run, observe, correct. Slowstore makes the *observe* step a file read — and that changes everything about prototyping with an agent.

Here is the entire persistence layer of a working prototype:

```python
from slowstore import Store
from pydantic import BaseModel

class User(BaseModel):
    name: str
    age: int = 0

store = Store[User](User, "mydata", key_selector=lambda u: u.name)

user = store.set(User(name="ada", age=36))  # writes mydata/ada.json, immediately
user.age += 1                               # persisted on assignment
```

No server. No connection string. No migration. Your Pydantic model *is* the schema, and your data is a directory of JSON files you can open in any editor. Slowstore is deliberately slow, deliberately single-threaded, and deliberately plain — because it's built for the phase of a project where **developer experience is the only performance metric that matters**.

That phase looks different in 2026. Increasingly, the "developer" in the loop is a coding agent — Claude Code, or something like it — and it turns out the properties that make slowstore pleasant for humans make it something stronger for agents: **legible**. We've been building on that observation for a year now, across multiple projects — this post is what that year taught us, sharpened by one build we actually measured.

## An agent's world is made of files

Watch a coding agent work and you'll see the same loop, thousands of times: propose a change, run something, *observe what happened*, correct. The tools it observes with are text tools — read a file, grep a directory, diff a commit. That's the agent's entire sensory range, and the speed of the whole collaboration is set by how fast that loop closes.

A database sits outside that range. The data is opaque state behind a socket; every inspection must be composed as a query, executed through a CLI, and parsed back out of tabular output. The agent can do it — but each observation becomes a little translation project, and translation layers are where agents burn time and occasionally lose the plot.

A slowstore collection sits *inside* that range — and three consequences follow from that. They're the whole argument.

## Advantage 1 — Iteration: the schema is the code you're already editing

At prototype stage, the domain model is wrong. Not badly wrong — *usefully* wrong, and it gets less wrong precisely as fast as you can restructure it. Every layer of ceremony between "I understand the domain better now" and "the code reflects that" is drag on the only thing a prototype is for.

With slowstore, a structural rewrite is just editing the Pydantic class:

```python
# Monday: a shipment is a date range
class Shipment(BaseModel):
    departs: date
    arrives: date

# Wednesday: no — it's a chain of stay/transport segments that must tile
class Segment(BaseModel):
    kind: Literal["stay", "transport"]
    location: str
    starts: date
    ends: date

class Shipment(BaseModel):
    segments: list[Segment] = []
    manually_adjusted: bool = False

# migration plan: there isn't one. Edit the class, reseed, keep moving.
```

No DDL to mirror the change, no migration file to generate, no ORM metadata to reconcile. The model *is* the schema; the JSON on disk simply follows it. In the build measured below, the domain model went through **four structural rewrites in two days, each one a pure Python edit** — and for an LLM this matters double, because keeping two representations in sync (model + migrations) is a classic agent failure mode, while editing one artifact it can see completely is what an agent does best.

And when an app outlives the reseed-and-move-on phase, migrations stay just as legible. Our longest-running slowstore app — a year old and deployed — handles them as idempotent Python functions that mutate the JSON directly, tracked in a ledger file and applied at startup. No Alembic, no DDL diffing: a migration is twenty lines of ordinary code the agent writes in one prompt and you review like any other code.

## Advantage 2 — Inspection: your data is textual context

Checking what got persisted is the same motion as reading source code:

```bash
$ cat mydata/ada.json          # the record, human- and agent-readable
$ grep -rl '"age": 37' mydata  # find records by content, no query language
$ git diff -- mydata/          # what changed since the last checkpoint?
```

For an LLM this is more than convenience — it's the difference between data the agent *queries* and data the agent *knows*. A JSON record is textual context: it can be read straight into the agent's context window, in the exact vocabulary of the Pydantic model the agent just wrote. Debugging stops being "compose a query, run it, parse the rows, guess" and becomes "open the file and look." In our measured build, debug loops routinely ended with the agent reading the store file to confirm what was actually persisted — one tool call, same as reading any source file.

It's writable context, too. The agent can fix a bad record with its ordinary edit tools, hand-craft a fixture, or reseed a demo state — because the data is just text, every tool the agent has for code works on the data.

> The store stops being infrastructure the agent talks to, and becomes part of the codebase the agent can see.

## Advantage 3 — Change detection: the data explains itself

Every record carries its own change log — with undo, redo, dirty tracking, and an optional actor trail. Open any record and the history is sitting right there in the JSON:

```json
{
  "name": "ada",
  "age": 37,
  "__changes__": [
    { "kind": "UPDATE", "prop_name": "age", "prev_val": 36, "new_val": 37,
      "date": "2026-08-26T09:14:02", "actor": "claude-code" }
  ]
}
```

When the agent breaks something, the record can roll itself back — no snapshot restore, no transaction log spelunking:

```python
user.is_dirty        # True — uncommitted changes exist
user.__reset__(1)    # undo the last change
user.__reset__()     # undo the entire recorded history

# attribute changes to whoever is driving — human, agent, or service:
store = Store[User](User, "mydata", get_identity=lambda: current_actor())
```

History isn't a feature you build — it's already in the file. In the build described below, this log became a user-facing History panel in the UI for exactly zero backend work.

Change detection runs in the other direction too. If a file changes *underneath* the store — you hand-edit the JSON, or an agent fixes a record with its edit tool while the app is running — slowstore notices. Every record is stamped with its file's `(mtime, size)` at load and after each write, and that stamp is checked before overwriting:

```python
store.reload()                  # pick up external edits (keep_dirty=True preserves local work)
store.reload("ada")             # refresh one record

Store[User](User, "mydata", on_conflict=ConflictPolicy.RAISE)
# a stale write now raises StaleWriteError instead of silently
# clobbering whatever you — or your agent — changed on disk
```

Put those together and you get a workflow no database offers a prototype: **the human edits data in the app, the agent edits data on disk, and the store keeps both honest** — every mutation logged, every external change detected, every mistake undoable.

## Receipts from a real build

First, the practice: slowstore has been our default starting point for **every new ideation for the past year**, across multiple projects — domain experiments, client prototypes, and an internal org-management app (auth, external syncs, a React frontend, Docker) that the "prototype store" quietly carried all the way to deployment, where it has run for months for a real team. Its change log powers the app's audit view; its store calls sit behind one seam; single process, modest load, no drama. Slowstore keeps earning the default spot for one reason: nothing we've tried iterates faster in the discovery phase, with or without an agent in the loop.

But "works great for us" is cheap testimony, so for one recent build we did something stricter: we measured. We built a full logistics-planning prototype — pydantic domain model, FastAPI backend, Next.js frontend with a custom timeline visualization, issue-resolution engine, 1000-record load tests — in one continuous 28-hour Claude Code session, on slowstore. Then we went back through the session transcript and measured what storage actually cost.

| Measure | Result |
|---|---|
| Total storage friction, out of a 28-hour build | **~15 min** |
| Structural model rewrites — each a pure Python edit, zero migrations | **4** |
| The entire storage layer: one repository class as the seam | **1 file** |
| Time to ship a user-facing History panel (read straight from the change log) | **0 min** |

For roughly thirty consecutive prompts in the middle of that build — the UI, the domain logic, the hard parts — the transcript mentions the store about once an hour, in passing. **The best storage layer for a prototype is the one that stops appearing in the transcript.**

The parts that did appear were the good kind. The domain model was rewritten four times in two days as understanding of the problem deepened — and each rewrite was just editing Pydantic classes and reseeding. Data states worth keeping were *git commits in the data directory*: diffable, restorable, reviewable. When a serialization bug did surface, the error was a Pydantic validation message naming the exact field path — the same vocabulary as the code being edited — and the fix took one edit and two minutes.

## Prototype with guardrails

None of this is an argument for scattering `Store[T]` calls through your codebase. Slowstore earns its keep *because* you treat it as disposable — and that discipline is three cheap guardrails, set up in the first hour.

### Guardrail 1 — one seam: the repository

Exactly one class touches slowstore. Everything else — services, API handlers, checks — talks to a protocol it doesn't know the implementation of:

```python
from typing import Protocol, TypeVar, Iterator, Callable

T = TypeVar("T")

class Repository(Protocol[T]):
    def get(self, key: str) -> T | None: ...
    def save(self, item: T) -> T: ...
    def delete(self, key: str) -> None: ...
    def all(self) -> Iterator[T]: ...
    def find(self, pred: Callable[[T], bool]) -> Iterator[T]: ...


class SlowstoreRepository:
    """The ONLY code in the app that imports slowstore."""

    def __init__(self, cls: type[T], data_dir: str, key: Callable[[T], str]):
        self._store = Store[T](cls, data_dir, key_selector=key)

    def get(self, key): return self._store.get(key)
    def save(self, item): return self._store.set(item)
    def delete(self, key): self._store.delete(key)
    def all(self): yield from self._store.values()
    def find(self, pred): yield from self._store.filter(pred)
```

That's the entire storage layer — and the graduation path in the same breath. When the prototype earns a real database, `PostgresRepository` implements the same five methods and the rest of the codebase never learns the news. In our build this class was called `ModelRepository[T]`, and it's the reason "swap in Postgres later" is a sentence and not a rewrite. It's also an agent-friendly guardrail in itself: an LLM told **"storage goes through the repository, nothing else touches slowstore"** in its agents file will respect that boundary far more reliably than one navigating ad-hoc store calls in forty files.

### Guardrail 2 — the data directory is a git repo

Initialize the data directory as its own repository and commit whenever a data state is worth keeping — a good demo, a reproduced bug, a seeded test fixture:

```bash
$ git -C data/ add -A && git -C data/ commit -m "resolver demo: 5 items, all gap fixes"
$ git -C data/ diff HEAD~1        # what did that refactor do to the data?
$ git -C data/ checkout demo-tag  # restore the exact demo state, instantly
```

Data states become checkpoints you can diff, restore, and review — for you *and* for the agent, which can now answer "what changed in the data since yesterday" with its ordinary git tools. Try expressing `git diff` against a `.sqlite` binary.

### Guardrail 3 — put the agents file in context

Copy slowstore's `CLAUDE.md` into your project's agent context (or reference it from your own), and add one line of house rules:

```markdown
# your project's CLAUDE.md
- Storage: slowstore (see docs/slowstore-agents.md for the full API).
  `SlowstoreRepository` in repository.py is the ONLY slowstore-touching
  code — keep it that way; it is the seam for a real DB later.
```

This closes the familiarity gap (more on that below) and enforces guardrail 1 at the same time. Every future session — yours or the agent's — starts fluent and stays inside the lines.

## "But LLMs are trained on SQL"

Fair fight time. The case for prototyping on SQLite instead is real, and it deserves to be stated at full strength before it's answered.

> **The SQLite case:** The model has read millions of SQLAlchemy apps and effectively all of SQL ever written. It has read your bespoke file store approximately once. Familiarity is the whole ballgame with LLMs — why fight it?

**The answer:** Because familiarity is a **context problem**, and context problems are solved by files — which is, conveniently, the whole point. Slowstore ships an agents file (`CLAUDE.md`) that teaches a coding agent the entire library: the module map, the public API, the invariants, the sharp edges. Drop those ~160 lines into your project's agent context and every session starts fluent — no source-diving, no hallucinated methods. In our build, the agent was using slowstore as idiomatically as any ORM within the first hour, and every session after that started warm.

Meanwhile, SQL familiarity isn't free either. The model knows SQL — but your *schema* isn't in its training data. It lives in a second artifact (DDL, ORM metadata, a migrations directory) that must be kept in sync with your Python model, and coordinating two representations that must agree is a classic agent failure mode: edit the model, forget the migration, burn a debug cycle on the stale-schema error. With slowstore there is one representation. The model is the schema. There is nothing to drift.

> **The SQLite case:** SQL is a real query language. The agent writes excellent SQL instantly; your `filter()` is a linear scan with a lambda.

**The answer:** True — and mostly beside the point at prototype time. Early-stage domain models are aggregate-shaped, or at most modestly relational: you read a document, mutate it, write it back, and the occasional join is a loop. `store.filter(lambda x: x.age > 30)` covers what a prototype actually asks, and it's plain Python the agent reads and writes natively. Our deployed org app is the stress test here: people, groups, processes, roles, and assignments referencing each other across collections — a genuinely relational shape — served for a year with in-Python joins and projections, and no query language missed. The day your product's questions truly outgrow that — heavy reporting, ad-hoc aggregation over large data — that's not a prototyping signal, it's a graduation signal. Keep one repository class as your storage seam (you should anyway) and the swap to a real database is a refactor, not a rewrite.

> **The SQLite case:** SQLite is one of the most battle-tested pieces of software on Earth. A young library will have bugs.

**The answer:** Granted, with a wry smile — our build found two, both fixed within minutes and both gone as of 2.3 (which added atomic writes and write-locking while it was at it). But notice what a storage bug *looks like* on each substrate: on slowstore it's a readable JSON file plus a validation error naming the broken field; the agent diagnoses it the way it diagnoses any code. A corrupted or mis-migrated binary database is a far darker room to debug in — for a human or an agent.

> **The SQLite case:** No cross-process safety. You can't even run multiple workers.

**The answer:** Correct, and it says so on the tin — slowstore is *deliberately* slow and single-threaded. A prototype is demoed from one process by one team. The moment a second concurrent writer genuinely appears, you haven't outgrown slowstore's implementation; you've outgrown the prototyping phase. Graduate through the seam.

## What you actually get

- **Schema = code.** Pydantic models (or dataclasses, or plain classes) map straight to JSON files. Restructure the model, reseed, keep moving — no migration ceremony while the domain is still being discovered.
- **An agent-legible store.** Every record is a file your agent can read, grep, and diff with its native tools. Data states are git commits; checkpoints are diffable.
- **History built in.** Per-record change logs with undo/redo, dirty tracking, and an actor trail — an audit feature and a debugging tool you didn't have to build.
- **Transparent persistence.** `user.age += 1` hits disk. Or turn auto-commit off and commit manually — your call.
- **Blobs and references** keep binary content and cross-collection relationships out of your way, resolved lazily.
- **An agents file included.** The `CLAUDE.md` makes any coding agent fluent in the library from the first prompt — the setup step for LLM-driven work is copying one file.

### When not to use it — and the sharp edges we've earned

Slowstore is not a production database and doesn't want to be. Query-heavy domains, concurrent writers, tens of thousands of hot records, multi-tenant anything: use a real database — that's what the repository seam is for. Slowstore's job is to make the road *to* that database fast, legible, and honestly cheap.

A year of daily use has also produced a short list of real sharp edges. Change detection is identity-based: fetch a model, mutate it in place, `set()` it back, and the write looks like "no change" and is silently skipped — write through `model_copy(update=...)` instead. And store proxies log every attribute access, so hot read paths should iterate raw models (`raw_values()`); switching gave us a measured ~10× on two endpoints in the deployed app. Both pitfalls now live in our agents file — which means the agent steers around them automatically. That's the point of the file: with a small library, a year of scar tissue fits in a paragraph the agent actually reads.

## The pattern, in one line

**Schema in code. Data as diffable text under git. Storage behind one seam.** Any store with those three properties is a good substrate for building with an LLM — slowstore just ships them as defaults, with change history thrown in and an agents file in the box.

Your next prototype's storage layer is six lines long. Let your agent see the data.

## Start slow

```bash
pip install "slowstore[pydantic]"
```

→ [github.com/42dotmk/slowstore](https://github.com/42dotmk/slowstore)
→ [pypi.org/project/slowstore](https://pypi.org/project/slowstore)

---

*The build figures come from a real, measured Claude Code session (Aug 2026): a project-logistics planning prototype built end-to-end on slowstore, with the transcript mined afterward for storage-attributable cost. That's one measured data point on an aggregate-shaped domain — behind it stands a year of daily practice across multiple projects that keeps reaching the same conclusion, and the affordance argument itself is structural: it holds anywhere an agent's tools are text tools. Which is everywhere.*
