# CLAUDE.md

Guidance for Claude Code when working in this repository.

This project is built live, in-session. Three read-only subagents do the investigation;
the main session does the deciding and the editing. Use them — do not answer from memory
or from a quick grep when one of them is the right tool.

## The agents

| Agent | Use it for | Tools |
|---|---|---|
| `code-explorer` | How **this codebase** works — locating and explaining what already exists | Read, Grep, Glob, Bash |
| `researcher` | Open questions that need **evidence** — options, edge cases, external docs | + WebSearch, WebFetch |
| `code-reviewer` | Judging a **completed change** before it ships | Read, Grep, Glob, Bash |

All three are read-only. They report; you edit.

## Routing

**`code-explorer`** — any question about existing code:
- Where does X live? Which file/function handles Y? What calls what?
- How does <feature> work end to end, and why is it built this way?
- What breaks if I change Z?
- Getting oriented in an unfamiliar area before planning or editing.

**`researcher`** — any question where the answer isn't in this repo, or isn't settled:
- Which library/approach should we use? (it evaluates against *this* project's constraints)
- What are all the cases this feature must handle — empty input, boundaries, concurrency,
  partial failure, migration, existing callers?
- Does this API/version actually behave the way we're assuming?
- Is this feasible, and what's the blast radius?

**`code-reviewer`** — a unit of work is done:
- On request: "review this", "second pair of eyes", "ready to ship?"
- Proactively once a feature, bugfix, or refactor is written and working. Once per unit of
  work, not after every edit.

Boundary: "how does our auth work?" → `code-explorer`. "which auth library should we use,
and what cases must it cover?" → `researcher`. Unsure which? Send both, in parallel.

## The live loop

For each piece of work:

1. **Before deciding** — `researcher` for the open question (options, edge cases,
   feasibility). Skip if the path is already settled.
2. **Before editing** — `code-explorer` on the code you're about to touch, unless you
   already read it this session.
3. **Build** — make the edits yourself in the main session.
4. **After building** — `code-reviewer` on the completed change.
5. **Fix** — apply the findings yourself, confirming with the user first if they're
   more than mechanical.

Steps 1 and 2 are independent: dispatch both agents in a single message so they run
concurrently. Don't stall the session waiting on serial round-trips.

## Working with their output

- Relay findings with the `path:line` and source citations they return. Don't dump the raw
  report, and don't drop the citations — they're what makes the answer checkable.
- `researcher` separates **verified** / **inferred** / **unknown**. Preserve that
  distinction when you relay it; don't promote an inference to a fact.
- If an agent says something doesn't exist or the docs don't say, report that as the
  finding. Don't fill the gap with a guess.
- Agents can be wrong. If a finding contradicts something you've directly verified, say so
  and check rather than deferring.

## Reuse and skipping

Continue an already-running or recently-finished agent with `SendMessage` rather than
spawning a duplicate for a follow-up on the same thread.

Skip delegation when:
- The answer is a single fact from a file already read this session.
- The change is trivial and mechanical (typo, rename, comment).
- The user explicitly asks you to handle it yourself, or names a different agent.

Live-session pacing overrides thoroughness: if the user is waiting on a two-line change,
make the change.
