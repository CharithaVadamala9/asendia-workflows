# Design decisions

Why this is built the way it is, including the things that were deliberately not built.

---

## 1. The brief, and how it was interpreted

Asendia asked to move from custom one-off workflow implementations to a repeatable
product platform. The literal deliverable is a workflows page plus seven backend
modules. The actual ask underneath it is *"can a staffing agency configure a new
automation without an engineer?"*

Everything below follows from taking that second question as the real specification.
A system that automates one recruitment pipeline very well but requires a code change
for the second pipeline has not solved the stated problem.

---

## 2. Modules declare their own interface

**Decision.** Every module implements one interface and declares its configuration as a
Pydantic model. The engine derives a JSON Schema from it and serves the module catalog
at `GET /api/modules`. The frontend renders every configuration form from that schema.

```python
class ResumeScreening(BaseModule):
    id = "resume_screening"
    config_model = Config          # a Pydantic model
    async def run(self, ctx, config) -> StepResult: ...
```

**Why.** This is the difference between "seven modules" and "a module system". Adding an
eighth module is a backend-only change: it appears in the workflow builder with its
fields, help text, defaults, and validation bounds, and no frontend code is written.
Field titles and descriptions live next to the code that uses them, so they cannot drift
out of sync with behaviour.

**Cost.** Configuration UI is generic. A module wanting a bespoke editor — a visual
rule builder, say — would need an escape hatch. None needed one here.

---

## 3. Suspension is a step outcome, not a special case

**Decision.** `StepResult.status` includes `SUSPENDED`. A step that hands off to an
external system returns it, the run halts with its cursor and all step outputs in the
database, and an inbound callback resumes execution from where it stopped.

**Why.** An AI phone interview takes minutes and completes via webhook. The naive
approaches are both bad: blocking a request thread for the duration, or treating the
call as fire-and-forget and losing the workflow's thread. Making suspension a normal
outcome means a run survives a process restart mid-call, and the resume path is ordinary
code rather than a callback special case.

The proof that it is general: the **recruiter approval gate uses the identical
mechanism**. Waiting for a human to click a button and waiting for a phone call to end
are the same problem. Nothing in the executor knows about VAPI.

**Cost.** Every asynchronous module must define a correlation key (`external_ref`) and
implement `resume()`. That is one indexed lookup and one method — a fair price.

---

## 4. Scoring is a rubric, not a prompt

**Decision.** The score is a weighted sum of independently computed criteria. Rules
handle what is objectively checkable — skill presence, years, degree level, location.
The LLM is confined to semantic equivalence and overall fit, and contributes one bounded
number rather than the verdict. Every criterion returns `{score, weight, evidence}`.

| Criterion | Weight | Computed by |
|---|---|---|
| Must-have skills | 40% | Exact match, LLM only for synonymy |
| Years of experience | 20% | Rules |
| Semantic fit | 25% | LLM, 0–10 |
| Education | 10% | Rules |
| Location | 5% | Rules |

**Why.** A staffing client will ask *why 72*, and "the model said so" does not survive
that question. It is also not reproducible, not tunable per role, and not auditable if a
rejected candidate ever disputes the process. The rubric gives a defensible answer for
each line, and weights live in module config so a warehouse role and a staff engineer
role score differently without touching code.

Three properties fall out of the structure:

- **Explainable** — the dashboard renders the breakdown as a table with evidence.
- **Tunable** — weights and threshold are per-workflow configuration.
- **Degradable** — if the LLM call fails, the deterministic criteria still produce a
  score, the result is flagged `degraded`, and the workflow continues. The weights of
  the remaining criteria are renormalized, so a missing criterion lowers confidence
  rather than silently capping the achievable score.

**Hard knockouts short-circuit** before any LLM spend: missing work authorization, or an
explicitly excluded skill, scores 0 with the reason recorded. Knockouts fire only on
*stated* facts — silence in a resume never disqualifies anyone.

**Cost.** More code than one prompt, and the rules need per-domain tuning. Worth it: this
is the part of the system a client actually evaluates.

---

## 5. The interview is grounded in the resume, the JD, and the score

**Decision.** Two stages. Before the call, one LLM call turns the full job description,
the full resume, and *the scoring breakdown already computed* into two compressed briefs
and 3–5 tailored questions. The call itself carries only the compact briefs plus the
questions.

**Why two stages.** The VAPI system prompt is loaded before the call connects, and a
bloated prompt hurts time-to-first-token. A laggy interviewer is immediately obvious to
the person on the phone. Preparation has no latency budget; the call does.

**Why not RAG.** A resume (~2–4k tokens) and a job description (~1k) both fit in context
comfortably. Retrieval exists for corpora that do not fit — applying it here would chunk
two short documents and hand the model fragments instead of the complete text, which is
strictly worse grounding plus embedding infrastructure for no gain.

RAG *would* earn its place over a corpus that genuinely exceeds context: a library of
past successful interviews for a role, or a client-specific requirements knowledge base.
That is a real future enhancement, not tonight's mechanism.

**The compounding advantage:** because scoring already ran, we know exactly which
criteria were weak, so the interview probes *those specific gaps* — "your resume shows 3
years of Kubernetes, the role asks for 5, tell me about the depth" — rather than asking
generic questions. The generated questions and the evidence that prompted each one are
persisted, so the run timeline shows *why the AI asked what it asked*.

---

## 6. Polling, because JobDiva has no webhooks

**Decision.** A watermark poller over `CandidateApplicationRecords`, re-querying a small
overlap window behind the mark and deduplicating on `(candidate, job)`.

**Why.** Not a shortcut — a property of the ATS. A case-insensitive search for
hook/callback/subscribe/notify across all 749 endpoints in both public Swagger specs
returns zero results. Polling is the only correct trigger design here.

Three details that are easy to get wrong:

- **The overlap window is deliberate.** The semantics of JobDiva's date filters are
  undocumented (created vs. modified, and in which timezone), so a poll that starts
  exactly at the last watermark will drop records. Overlapping and deduplicating is
  cheaper than being subtly lossy.
- **`CandidateApplicationRecords` is the right stream.** `NewUpdatedCandidateRecords`
  fires on any profile edit, which is a large false-positive rate;
  `NewResumesAdded` covers uploads with no job attached. Only this one means
  "applied to a job", and it is the only one of the four that paginates.
- **Poll cadence comes from `ApiLimits`**, which reports per-endpoint, per-tenant limits,
  rather than from a guessed constant.

---

## 7. Provider abstraction with independent live/mock toggles

**Decision.** Every integration sits behind an interface with live and mock
implementations, selected per provider by environment variable.

**Why.** Three distinct payoffs. The demo cannot be taken down by someone else's
sandbox. The tests exercise the real pipeline with no network. And "can you swap SMS
providers?" is answered by an environment variable rather than a refactor.

This mattered concretely: JobDiva credentials arrived untested, and the build was never
blocked on them.

---

## 8. Confining the untyped API to one file

**Decision.** All JobDiva payload parsing lives in `integrations/jobdiva/adapter.py`,
which normalizes into typed models. The parsing helpers accept several possible envelope
shapes rather than assuming one.

**Why.** 260 of JobDiva's 431 V2 endpoints declare their response as `IBiData` —
`{"type": "object"}` with zero properties. The shapes are genuinely unknowable from the
specification. `scripts/discover_jobdiva.py` probes the endpoints we depend on and
writes down what actually comes back, but until it runs against real credentials the
integration must tolerate surprise. Confining that uncertainty to one file means a shape
surprise degrades one adapter method instead of propagating into every module.

---

## 9. Our own pipeline vocabulary, mapped at the boundary

**Decision.** Internal stages — applied, screened, qualified, contacted, interviewed,
recommended — with `rejected` as a terminal flag rather than a stage. JobDiva's statuses
are fetched at boot and mapped only at the write-back boundary.

**Why.** Our stages map 1:1 onto engine state, so the funnel is a direct readout of what
the executor did and cannot drift from reality. JobDiva's statuses are tenant-configurable
and the spec declares no enums for them, so coupling the UI to them would mean coupling
to a vocabulary that varies per customer. Rejection is a flag because it preserves the
stage a candidate actually reached — a candidate rejected after the interview is a
different fact from one rejected at screening.

---

## 10. Writing back to the right place

**Decision.** Three calls: `createOrUpdateCandidateScreener` for structured interview
answers, `createCandidateNote` with `link2AnOpenJob` for the human-readable summary, and
`updateSubmittal` for the pipeline stage.

**Why.** The screener endpoint is natively candidate×job scoped with structured Q&A —
it is where interview results *belong*, and it is a field recruiters already read.
Writing there rather than dumping prose into a note is the difference between
integrating with their workflow and bolting onto it.

**A failed write-back does not fail the run.** The interview happened and is recorded on
our side; losing the mirror is a degradation, not a reason to discard completed work.

---

## 11. SMS consent is checked before sending

**Decision.** The SMS module reads JobDiva's stored texting consent before sending, and
treats an opt-out as a successful, compliant outcome rather than an error.

**Why.** This is TCPA exposure, and it is exactly the kind of check a one-off automation
script skips. JobDiva stores consent but has no send capability, so the obligation sits
with whoever does the sending — us. Raised here because it is a liability Asendia may not
have accounted for in their current custom implementations.

---

## Deliberately not built

Each of these is a decision, not an oversight.

| Not built | Why |
|---|---|
| Real SMS delivery | Mailjet SMS requires a funded wallet and a documented 48-hour security check after first deposit. It fails at *send* time, so an unactivated wallet breaks the demo live rather than at integration time. The Mailjet call is implemented and one env var from live; Twilio works immediately. |
| JobDiva UDF write-back | `updateCandidateUserfields` takes `overwrite: true`, which the V1 docs describe as overwriting *all* user-defined fields. Untested against a real tenant, so running it risks blanking recruiter data. Needs a sandbox test first. |
| Authentication / multi-tenancy | Real for production, adds nothing to the architectural question being evaluated. The data model is already tenant-shaped. |
| Parallel and looping steps | The executor is deliberately sequential. Branching via `when` covers the actual workflows; a full DAG is a larger design and was not needed to prove the model. |
| Retry with backoff | External calls fail without retry today. The correct design is per-module retry policy in config, which is a natural extension of the module contract. |
| Production hardening | No rate limiting, no structured log shipping, SQLite rather than Postgres (one URL change). |

---

## Proposed enhancements

Beyond the stated scope, in rough order of value:

1. **Bidirectional sync.** Read recruiter actions back out of JobDiva so a human
   decision made in the ATS updates the workflow, not just the other way round.
2. **Scoring calibration.** Persist outcomes and back-test rubric weights against actual
   hires. The rubric already produces per-criterion data; this makes it learn.
3. **Retrieval where it earns its place** — grounding interview questions in past
   successful interviews for a role, which is a corpus that genuinely exceeds context.
4. **Per-module retry and dead-letter handling**, configured like everything else.
5. **A/B testing workflows.** Because a workflow is data, two variants can run against
   the same job and be compared on outcomes.
6. **Candidate-facing scheduling** rather than a cold outbound call — higher answer
   rates, and better consent posture.
