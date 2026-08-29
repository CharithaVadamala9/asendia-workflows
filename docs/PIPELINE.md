# How it works, end to end

One stage per section: what starts it, which file does the work, which APIs it calls,
and what it produces. Written to be read without opening the code.

```
JobDiva application
      │  poll (no webhooks exist)
      ▼
1. Trigger ──▶ 2. Enrich ──▶ 3. Score ──▶ 4. Outreach ──▶ 5. Prepare ──▶ 6. Call ──▶ 7. Write back
                                  │                                          │
                            below threshold?                          run suspends here,
                            outreach skipped                          resumes on webhook
```

---

## 1. Trigger — knowing an application arrived

**File:** `app/polling/poller.py` · **Module:** `app/modules/new_applicants.py`

**JobDiva has no webhooks.** A case-insensitive search for hook/callback/subscribe
across all 749 endpoints in both public Swagger specs returns nothing. Polling is not a
shortcut here; it is the only correct design against this ATS.

```
GET /apiv2/bi/JobApplicantsDetail?jobId=…
```

Returns `CANDIDATEID`, `JOBID`, `DATEAPPLIED`, name, email, and — usefully —
`RESUMEID`, which is the resume the candidate actually applied with.

**Why not `CandidateApplicationRecords`,** the date-range stream that looks like the
obvious choice: it returned `429 Request Limit Exceeded` on this tenant, its quota
already exhausted. `JobApplicantsDetail` sits on a separate quota and kept serving. It
is also the better axis — we care about "who applied to *this job*" — and it saves a
call by carrying the resume id.

Three details that make the poll correct rather than merely working:

- **A watermark with overlap.** We re-query ~90 seconds behind the mark, because
  JobDiva's date semantics are undocumented (created vs. modified, and in which
  timezone). A poll starting exactly at the watermark drops records.
- **Dedupe on `(candidate, job)`.** The overlap guarantees replays; the upsert is what
  stops one application starting three runs.
- **Cadence from configuration**, not a guessed constant.

A recruiter can also start a run by hand from the job page, and `POST /api/jobs/sync`
backfills an entire job's applicant history.

> The poller's state is shown on the Integrations page. A trigger that silently is not
> running looks identical to "no new applicants", which is the worst way for this
> feature to fail.

---

## 2. Enrichment — gathering what scoring needs

**File:** `app/integrations/jobdiva/adapter.py` · parsing in `shapes.py`

| Call | Gives us |
|---|---|
| `GET /apiv2/bi/CandidatesDetail?candidateIds=` | phone (`CELLPHONE` / `PHONE1`) — absent from the applicant list |
| `GET /apiv2/bi/ResumesTextDetail?resumeIds=` | resume text, in `PLAINTEXT` |
| `GET /apiv2/bi/JobDetail?jobId=` | title, description, skills, location |

So the inputs to scoring are: **the resume, the job description, and the candidate's
contact details.**

All parsing lives in `shapes.py` because most of JobDiva's V2 API is untyped — 260 of
431 endpoints declare their response as an object with no properties, so the real
shapes were discovered by calling it. That file documents each surprise: keys are
uppercase, every scalar is a string (`SECURITY_CLEARANCE: "0"`, which is truthy in
Python), `CANDIDATEID` exceeds 32 bits, `RESUMEID` is *not* numeric, empty fields are
sometimes the literal string `"Null"`, and `JOBDESCRIPTION` is HTML.

---

## 3. Scoring — our ATS model

**Files, in the order worth reading them:**

| File | What it holds |
|---|---|
| `app/scoring/rubric.py` | the model, the weights, how points combine — **start here** |
| `app/scoring/criteria.py` | how each criterion is actually computed |
| `app/scoring/extract.py` | turning a job and a resume into structured facts |
| `app/modules/resume_screening.py` | the thin wrapper making it a workflow step |

| Criterion | Weight | Decided by |
|---|---|---|
| Must-have skills | 40% | Rules match; the LLM only resolves equivalence ("React" ≈ "front-end framework") |
| Years of experience | 20% | Rules |
| Semantic fit | 25% | The LLM, 0–10 — the one genuine judgment call |
| Education | 10% | Rules (ordinal degree comparison) |
| Location | 5% | Rules |

The split is deliberate. A client will ask *why 72*, and "the model said so" does not
survive that question. Rules handle what is objectively checkable; the LLM contributes
one bounded number rather than the verdict.

- **Passing** (≥ threshold, default 70) → stage `qualified` → outreach proceeds.
- **Failing** → `is_rejected` with the reason; outreach steps skip; the run still
  completes and still writes the note.
- **Knockouts** (no work authorization, an excluded skill) → score 0 immediately, no
  LLM spend. Knockouts fire only on *stated* facts — silence never disqualifies anyone.
- **If the LLM is unavailable**, the deterministic criteria still produce a score, the
  remaining weights renormalize, and the result is flagged `degraded` in the UI.

**Job requirements have two sources.** JobDiva's typed job read model carries no
skills, experience, or education fields at all. Where the structured `SKILLS` field is
populated we use it; where it is not — and on this tenant it holds the string `"Null"`
— we extract requirements from the description with one LLM call. Scoring is never
blocked on which one it gets.

---

## 4. Outreach — SMS

**Files:** `app/modules/outreach.py` → `app/integrations/messaging.py`

Consent is checked first, via `GET /apiv2/bi/CandidatesTextingConsent`. An opt-out is
treated as a successful, compliant outcome rather than an error. This is TCPA exposure,
and it is exactly the check a one-off automation script skips — JobDiva stores consent
but has no send capability, so the obligation sits with whoever sends.

**Currently `SMS_MODE=log`:** the exact message is recorded, nothing is delivered.
Mailjet's SMS API needs a prepaid wallet, a token separate from the email key, and a
documented **48-hour security check after first deposit** — it fails at *send* time, so
an unactivated wallet would break live rather than at integration time. Twilio works
immediately and is needed for the voice call regardless.

---

## 5. Interview preparation — **not RAG**

**File:** `app/modules/ai_phone_call.py`, function `_prepare`

No vector database, no embeddings, no retrieval. One LLM call receives the **full job
description, the full resume, and the scoring breakdown**, and returns a candidate
brief, a role brief, and 3–5 tailored questions.

**Why not RAG.** A resume (~3k tokens) and a job description (~2.4k) both fit in
context comfortably. Retrieval exists for corpora that do not fit; applying it here
would chunk two short documents and hand the model fragments instead of the complete
text — strictly worse grounding, plus embedding infrastructure, for no gain.

*Where RAG would genuinely earn its place, and is worth proposing later: grounding
questions in a library of past successful interviews for a role, or a client-specific
requirements knowledge base. Those are corpora that actually exceed context.*

**Passing the scoring breakdown is what makes this more than templating.** Because
screening already ran, we know which criteria were weak, so the interview probes those
specific gaps — a candidate scoring 0 on location got asked about their commute. The
generated questions and the evidence behind each are persisted, so the run timeline
shows *why the AI asked what it asked*.

Two stages exist because voice latency is real: preparation has no latency budget, the
call does. A bloated system prompt hurts time-to-first-token, and a laggy interviewer
is immediately obvious to whoever picked up.

---

## 6. The call — where our server stops

**File:** `app/integrations/vapi.py`

```
our backend ──[ prompt + questions ]──▶  VAPI          ← handoff; we are now OUT
                                          │
                                  Deepgram STT
                                        ↓
                                  Claude (run BY VAPI)
                                        ↓
                                      TTS
                                          │
                                   Twilio number
                                          │
                                     candidate

            ◀──[ webhook: transcript + score ]──
```

**Our server is not in the audio loop.** VAPI calls Claude itself, using the model
configuration we send in the request. We are involved only before (building the prompt)
and after (receiving the webhook).

The layers are often confused, so plainly:

- **Twilio** is the carrier — it owns the phone number and connects the call.
- **VAPI** is the realtime voice loop — speech-to-text, the model, text-to-speech,
  turn-taking, handling interruptions, all inside ~800ms.
- **Our code** decides what to ask and what the answers mean.

**VAPI's own free numbers cannot place outbound calls**, which is why Twilio is
required: buy the number there, import it into VAPI, and VAPI dials from it.

> **Setup consequence:** since VAPI runs Claude itself, it needs an Anthropic key
> configured *in the VAPI dashboard*. The `ANTHROPIC_API_KEY` in `backend/.env` is used
> by our backend for scoring and question generation; VAPI cannot see it.

The call step returns `SUSPENDED`. The run halts with its state in the database and
resumes when the webhook arrives — which is also how the recruiter approval gate works,
proving the mechanism is general rather than bolted on for voice.

---

## 7. Write-back

**File:** `app/modules/jobdiva_writeback.py`

| Write | Purpose | State |
|---|---|---|
| `createSubmittal` | creates the pipeline record — what actually *moves* a candidate | built |
| `updateSubmittal` | sets interview date and notes | built |
| `createCandidateNote` (`link2AnOpenJob`) | the human-readable summary | ✅ verified live |
| `createOrUpdateCandidateScreener` | structured interview Q&A | ⚠️ tenant's screening module is off |
| `updateTextingOptInOut` | consent trail | built |
| `rejectApplicant` | declines — no reject reasons configured on this tenant | built |

**Writes are suppressed by default.** `JOBDIVA_WRITE_MODE` is separate from
`JOBDIVA_MODE`: reads stay live while every write is captured and displayed instead of
sent. The run timeline shows the exact payload that would have gone to JobDiva.

That separation exists because the writes are not equal. A note is additive and
harmless. A submittal appears in a real recruiter's work queue. Mutation should be a
deliberate act, not a side effect of pointing the app at live credentials.

**A failed write never fails the run.** The interview happened and is recorded on our
side; losing the mirror is a degradation, not a reason to discard completed work.

---

## What is live right now

| | State | To go live |
|---|---|---|
| JobDiva reads | **live** | — |
| Claude scoring & question generation | **live** | — |
| Applicant poller | **running**, watching the configured job | — |
| JobDiva writes | suppressed | `JOBDIVA_WRITE_MODE=live` |
| SMS | log only | Twilio credentials |
| Email | mock | Mailjet key + a verified sender |
| **AI voice call** | **mock** | VAPI key, Twilio number, Anthropic key in VAPI, public tunnel |

Two items are blocked by tenant configuration rather than by code, and need a JobDiva
administrator: **the screening module is disabled**, and **no reject reasons are
configured**. Both report that plainly rather than failing opaquely.
