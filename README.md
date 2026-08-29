# Asendia Workflows

> **Copyright © 2026 Charitha Vadamala. All Rights Reserved.**
> Proprietary software, provided solely for evaluation and demonstration.
> No permission is granted to copy, modify, distribute, deploy, host, or
> commercialize this software or any substantial portion of it without prior
> written permission. See [LICENSE](LICENSE).


A template-driven recruitment automation platform integrated with the JobDiva ATS.

When someone applies to a job, the system detects it, pulls their resume, scores them
against that job with an explainable rubric, texts the ones who qualify, interviews them
by AI phone call, and writes the results back into JobDiva — while a recruiter dashboard
shows the funnel per job and lets a human step in at any point.

The point of the build is that none of this is hard-coded. A workflow is a JSON template
of configurable modules, and the modules are the product.

---

## Quick start

```bash
# 1. Backend
cd backend
cp .env.example .env          # fill in credentials; runs fully mocked without them
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload --port 8000

# 2. Frontend (separate terminal)
cd frontend
npm install
npm run dev                   # http://localhost:5173

# 3. Load demo data
curl -X POST localhost:8000/api/jobs/sync
```

Open <http://localhost:5173/jobs>, click into the job, and press **Run workflow** on a
candidate. API docs are at <http://localhost:8000/docs>.

Everything runs with **no credentials at all** — each provider independently falls back
to a mock, so the pipeline is demonstrable offline. Flip providers to live one at a time
as credentials are verified.

```bash
cd backend && .venv/bin/python -m pytest    # 27 tests
```

---

## What it does

```
New applicant (JobDiva poll)
        ↓
Resume screening        weighted rubric, 0-100, per-criterion evidence
        ↓  qualified?
Recruiter approval      optional, pauses the run
        ↓
SMS outreach            consent-checked against JobDiva
        ↓
AI phone interview      VAPI, questions generated from resume + JD + score gaps
        ↓
Assessment report       resume score + interview, combined
        ↓
JobDiva write-back      structured screener answers + note + pipeline stage
```

---

## Write safety

`JOBDIVA_WRITE_MODE` is separate from `JOBDIVA_MODE`, and defaults to `suppressed`.

Reads stay live — real jobs, applicants, resumes, real AI scoring — while every write is
captured and displayed instead of sent. The run timeline shows the exact payload that
would have gone to JobDiva.

That separation exists because the writes are not all equal. A note is additive and
harmless. `createSubmittal` puts a record in a real recruiter's work queue. Mutation
should be a deliberate act, not a side effect of pointing the app at live credentials.

```bash
JOBDIVA_WRITE_MODE=live    # when you actually mean it
```

Write operations implemented: `createCandidateNote` · `createOrUpdateCandidateScreener`
· `createSubmittal` · `updateSubmittal` · `createJobApplication` ·
`updateTextingOptInOut` · `MarkCandidateAsInterested` · `rejectApplicant`.

Two are blocked by tenant configuration rather than by code — the screening module is
disabled, and no reject reasons are configured. Both report that plainly instead of
failing opaquely.

## Architecture

```
backend/app/
  engine/       base.py registry.py executor.py context.py
  scoring/      rubric.py criteria.py extract.py
  modules/      the 8 workflow modules
  integrations/ jobdiva/ vapi.py messaging.py llm.py
  api/routes.py
frontend/src/   pages/ + ConfigForm.tsx (schema-driven forms)
```

**Modules are the unit of reuse.** Each declares its configuration as a Pydantic model.
The engine publishes `model_json_schema()` at `GET /api/modules`, and the frontend
renders every configuration form from that schema. Adding a module is a backend-only
change — it appears in the builder, with its fields, help text, defaults, and bounds,
with no frontend code.

**Suspension is a first-class step outcome.** A step that hands off to the outside world
returns `SUSPENDED`; the run halts with its state in the database and resumes when the
callback arrives. The AI phone call and the recruiter approval gate use the same
mechanism, which is the proof it is general rather than special-cased for voice.

**Every provider has a `live | mock` toggle.** A demo never depends on an external
service being reachable, and swapping SMS providers is one environment variable.

[`docs/PIPELINE.md`](docs/PIPELINE.md) walks the whole flow stage by stage — what
triggers each step, which file implements it, and which APIs it calls. Start there.
[`docs/DESIGN.md`](docs/DESIGN.md) has the reasoning behind each decision, including
what was deliberately left out and why.

---

## Verified against the live JobDiva tenant

Confirmed by calling the real API, not read from the spec. `docs/jobdiva-shapes.md` has
the full payload shapes.

| Endpoint | Result |
|---|---|
| `POST /apiv2/v2/login` | ✅ authenticates, returns `token` + `refreshtoken` |
| Auth header format | ✅ **raw token, no `Bearer` prefix** — resolved empirically |
| `GET /apiv2/bi/JobDetail` | ✅ 109 fields (far more than the spec suggests) |
| `GET /apiv2/bi/JobApplicantsDetail` | ✅ applicants + **`RESUMEID`**, which saves a call |
| `GET /apiv2/bi/ResumesTextDetail` | ✅ real resume text in `PLAINTEXT` |
| `GET /apiv2/bi/CandidatesDetail` | ✅ phone numbers (`CELLPHONE` / `PHONE1`) |
| `POST /apiv2/jobdiva/createCandidateNote` | ✅ returns a note id |
| `POST .../createOrUpdateCandidateScreener` | ❌ 500 *"The screening module is off"* — disabled for this tenant |
| `GET /apiv2/bi/CandidateApplicationRecords` | ❌ 429 *"Request Limit Exceeded"* — quota exhausted |
| `GET /apiv2/bi/ApiLimits` | ⚠️ returns an empty array — no quota figures available |

Three of these changed the code:

- **We ingest via `JobApplicantsDetail`, not `CandidateApplicationRecords`.** It is
  job-scoped (which is the axis we actually want), it carries `RESUMEID` so the
  resume-text lookup drops from two calls to one, and it sits on a *separate quota* —
  it kept serving while the date-range endpoint was rate-limited.
- **The screener write-back degrades to a note.** The structured endpoint is the
  semantically correct home for interview results, but this tenant has the screening
  module switched off. Write-back failures were already non-fatal, so runs complete
  and the summary note still carries everything.
- **Real data is messier than the spec.** `SKILLS` came back as the literal string
  `"Null"`, every scalar is a string (`SECURITY_CLEARANCE: "0"` — truthy in Python),
  `CANDIDATEID` exceeds 32 bits, `RESUMEID` is not numeric, and `JOBDESCRIPTION` is
  HTML with entities. `integrations/jobdiva/shapes.py` documents and handles each.

## Notable findings about the JobDiva API

Worth reading before extending the integration — each of these changed the design.

- **There are no webhooks.** Zero matches for hook/callback/subscribe across all 749
  endpoints in both public Swagger specs. The "new applicant trigger" has to be a
  watermark poll, and that is a property of the ATS, not a shortcut.
- **60% of the V2 API is untyped.** 260 of 431 endpoints declare their response as
  `IBiData` — `{"type": "object"}` with no properties. Shapes are only discoverable by
  calling them, which is what `scripts/discover_jobdiva.py` is for.
- **Jobs have a read/write asymmetry.** `skills`, `experience`, and `securityclearance`
  exist on the write models but not on the typed `Job` read model, and there is **no
  education field at all**. Scoring therefore has a fallback: structured fields when
  present, LLM extraction from the description otherwise.
- **Bad credentials return HTTP 500, not 401.** Error handling matches on the message.
- **Statuses are tenant-configurable with no enums in the spec** — they must be fetched
  at boot, never hardcoded.
- Dates are `MM/dd/yyyy HH:mm:ss`, IDs are int64, and typed models use JSON keys with
  spaces in them (`"submittal status"`, `"job title"`).

---

## Deployment notes

- **VAPI free numbers cannot place outbound calls.** Import a Twilio number first:
  `POST https://api.vapi.ai/phone-number`. A Twilio *trial* account only dials verified
  numbers.
- **The VAPI webhook needs a public URL.** Set `PUBLIC_BASE_URL` to an ngrok or
  cloudflared tunnel in development, or call results never arrive.
- **Mailjet's From address must be a verified sender**, or every email fails.
- **Mailjet SMS needs a funded wallet plus a 48-hour security check** after the first
  deposit. `SMS_MODE=log` is the default for that reason; Twilio works immediately.

---

## Licence

Copyright © 2026 Charitha Vadamala. All Rights Reserved.

This software and associated source code are proprietary and are provided solely
for evaluation and demonstration purposes. No permission is granted to copy,
modify, distribute, sublicense, sell, deploy, host, commercialize, or otherwise
use this software or any substantial portion of it without prior written
permission from the copyright owner.

Viewing or evaluating this repository does not grant any ownership, license, or
commercial usage rights.
