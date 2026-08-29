import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type JobDivaWrite, type RunDetail as Run, type StepRun } from '../api'
import { Badge, ErrorBox, Loading, ScoreTable, StatusBadge, scoreTone } from '../components'

/**
 * The run timeline — one card per step, showing what the step was configured with and
 * what it produced. Each module type renders its own body, because a score breakdown
 * and a call transcript want very different presentation.
 */
export default function RunDetail() {
  const { id } = useParams()
  const runId = Number(id)
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    () => api.run(runId).then(setRun).catch((e) => setError(e.message)),
    [runId],
  )
  useEffect(() => { load() }, [load])

  // Poll while the run is still moving — a suspended run resumes when the phone call
  // ends, which happens minutes after the page was opened.
  useEffect(() => {
    if (!run || ['completed', 'failed'].includes(run.status)) return
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [run, load])

  async function decide(approved: boolean) {
    try {
      await api.approve(runId, approved)
      load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (error && !run) return <ErrorBox error={error} />
  if (!run) return <Loading />

  const awaitingApproval = run.steps.find(
    (s) => s.module_id === 'approval_gate' && s.status === 'suspended',
  )

  return (
    <>
      <div className="page-head">
        <Link to={`/jobs/${run.job.id}`} className="dim">← {run.job.title}</Link>
        <div className="spread" style={{ marginTop: 6 }}>
          <div>
            <h1>{run.candidate.name}</h1>
            <p>
              {run.workflow.name} · triggered {run.trigger_source} ·{' '}
              {new Date(run.started_at).toLocaleString()}
              {run.dry_run && ' · DRY RUN'}
            </p>
          </div>
          <StatusBadge status={run.status} />
        </div>
      </div>

      <ErrorBox error={error || run.error} />

      {awaitingApproval && (
        <div className="card" style={{ borderColor: 'var(--warn)' }}>
          <h2>Awaiting your approval</h2>
          <p className="muted">
            {(awaitingApproval.output.note as string) ||
              'This run is paused until you approve outreach.'}
          </p>
          <div className="row">
            <button className="primary" onClick={() => decide(true)}>Approve</button>
            <button className="danger" onClick={() => decide(false)}>Reject</button>
          </div>
        </div>
      )}

      <div className="timeline">
        {run.steps.map((step) => <StepCard key={step.id} step={step} />)}
      </div>
    </>
  )
}

function StepCard({ step }: { step: StepRun }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={`step ${step.status}`}>
      <div className="card" style={{ marginBottom: 0 }}>
        <div className="step-head spread">
          <div className="row">
            <strong>{titleFor(step)}</strong>
            <StatusBadge status={step.status} />
          </div>
          <div className="row">
            {step.duration_ms != null && (
              <span className="dim">{formatMs(step.duration_ms)}</span>
            )}
            <button className="sm" onClick={() => setOpen(!open)}>
              {open ? 'Hide raw' : 'Raw'}
            </button>
          </div>
        </div>

        {step.skip_reason && <div className="dim">Skipped — {step.skip_reason}</div>}
        {step.error && <div className="error" style={{ margin: '8px 0 0' }}>{step.error}</div>}

        <StepBody step={step} />

        {open && (
          <pre className="transcript" style={{ marginTop: 10 }}>
            {JSON.stringify({ config: step.config, output: step.output }, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}

function StepBody({ step }: { step: StepRun }) {
  const out = step.output ?? {}
  if (step.status === 'skipped' || step.status === 'pending') return null

  switch (step.module_id) {
    case 'resume_screening':
      return out.breakdown ? <ScoreTable breakdown={out.breakdown} /> : null

    case 'sms_outreach':
      return (
        <div>
          {out.delivered
            ? <Badge tone="good">delivered to {out.to}</Badge>
            : <Badge tone="warn">not sent — {out.reason}</Badge>}
          {out.body && <pre className="transcript" style={{ marginTop: 8 }}>{out.body}</pre>}
        </div>
      )

    case 'email_notification':
      return (
        <div>
          {out.delivered
            ? <Badge tone="good">sent to {out.to}</Badge>
            : <Badge tone="warn">not sent — {out.reason}</Badge>}
          {out.subject && <div style={{ marginTop: 6 }}><strong>{out.subject}</strong></div>}
          {out.body && <pre className="transcript" style={{ marginTop: 8 }}>{out.body}</pre>}
        </div>
      )

    case 'ai_phone_call':
      return <CallBody out={out} status={step.status} />

    case 'assessment_report':
      return (
        <div>
          <div className="spread">
            <strong style={{ fontSize: 15 }}>{out.headline}</strong>
            <Badge tone={out.recommendation === 'advance' ? 'good' : out.recommendation === 'hold' ? 'warn' : 'bad'}>
              {out.recommendation}
            </Badge>
          </div>
          <p className="muted">{out.narrative}</p>
          <div className="grid two">
            {out.strengths?.length > 0 && (
              <div><h3>Strengths</h3>
                <ul className="tight">{out.strengths.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul>
              </div>
            )}
            {out.concerns?.length > 0 && (
              <div><h3>Concerns</h3>
                <ul className="tight">{out.concerns.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>
              </div>
            )}
          </div>
        </div>
      )

    case 'note_posting':
      return <WriteBackBody out={out} />

    case 'approval_gate':
      if (out.skipped) return <div className="dim">Approval not required for this workflow.</div>
      return out.approved
        ? <Badge tone="good">approved by {out.decided_by ?? 'recruiter'}</Badge>
        : null

    case 'new_applicants':
      return (
        <div className="dim">
          Candidate #{out.candidate_id} applied to job #{out.job_id} · source {out.source}
        </div>
      )

    default:
      return null
  }
}

/**
 * Every JobDiva write attempted by this step, with the exact payload.
 *
 * A suppressed write is not a failure — it means write mode is off, and the payload
 * shown is precisely what would have been sent. That makes this table both the safety
 * mechanism and a way to show a client what the integration does before it does it.
 */
function WriteBackBody({ out }: { out: any }) {
  const writes: JobDivaWrite[] = out.writes ?? []
  const suppressed = out.write_mode !== 'live'

  // The recruiter-facing artifact. It was previously only reachable by expanding a
  // JSON payload, which meant the one thing a recruiter actually reads was the
  // hardest thing on the page to find.
  const note = writes.find((w) => w.op === 'createCandidateNote')
  const noteText = (note?.payload as { note?: string } | undefined)?.note

  return (
    <div>
      <div className="row" style={{ flexWrap: 'wrap', marginBottom: 10 }}>
        {out.note_written && <Badge tone="good">note #{out.note_id}</Badge>}
        {out.submittal_id && <Badge tone="good">submittal #{out.submittal_id}</Badge>}
        {out.screener_written && <Badge tone="good">screener answers written</Badge>}
        {out.write_mode === 'dry_run' && <Badge tone="warn">dry run — nothing written</Badge>}
      </div>

      {suppressed && writes.length > 0 && (
        <div className="notice">
          <strong>Write mode is off.</strong> These are the exact payloads that would
          have been sent to JobDiva. Set <code>JOBDIVA_WRITE_MODE=live</code> to send them.
        </div>
      )}

      {noteText && (
        <div style={{ marginBottom: 14 }}>
          <h3>Recruiter note</h3>
          <pre className="note">{noteText}</pre>
          <div className="dim" style={{ marginTop: 6 }}>
            {suppressed
              ? 'This is the note that would be posted to the candidate\u2019s JobDiva record, linked to this job.'
              : `Posted to JobDiva as note #${out.note_id}.`}
          </div>
        </div>
      )}

      {writes.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Operation</th>
              <th style={{ width: 110 }}>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {writes.map((w, i) => (
              <tr key={i}>
                <td><code>{w.op}</code></td>
                <td>
                  <Badge tone={w.status === 'sent' ? 'good' : w.status === 'suppressed' ? '' : 'warn'}>
                    {w.status}
                  </Badge>
                </td>
                <td className="muted">
                  {w.reason ?? (
                    <details>
                      <summary className="dim" style={{ cursor: 'pointer' }}>payload</summary>
                      <pre className="transcript" style={{ marginTop: 6 }}>
                        {JSON.stringify(w.payload, null, 2)}
                      </pre>
                    </details>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function CallBody({ out, status }: { out: any; status: string }) {
  const plan = out.plan ?? {}
  return (
    <div>
      {status === 'suspended' && (
        <div className="notice">
          Call in progress — this run resumes automatically when the interview ends.
        </div>
      )}

      {plan.questions?.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <h3>Questions prepared for this candidate</h3>
          <ol className="tight">
            {plan.questions.map((q: string, i: number) => <li key={i}>{q}</li>)}
          </ol>
          {plan.rationale?.length > 0 && (
            <div className="dim">Why: {plan.rationale.join(' · ')}</div>
          )}
        </div>
      )}

      {out.interview_score != null && (
        <div className="spread" style={{ marginBottom: 10 }}>
          <div className="row">
            <span className={`score ${scoreTone(out.interview_score * 10)}`} style={{ fontSize: 20 }}>
              {out.interview_score}<span className="dim" style={{ fontSize: 13 }}>/10</span>
            </span>
            {out.recommendation && (
              <Badge tone={out.recommendation === 'advance' ? 'good' : out.recommendation === 'hold' ? 'warn' : 'bad'}>
                {out.recommendation}
              </Badge>
            )}
          </div>

        </div>
      )}

      {out.rationale && <p className="muted">{out.rationale}</p>}

      <div className="row" style={{ flexWrap: 'wrap', marginBottom: 10 }}>
        {out.availability && <Badge>Available: {out.availability}</Badge>}
        {out.rate_expectation && <Badge>Rate: {out.rate_expectation}</Badge>}
        {out.work_authorization && <Badge>Work auth: {out.work_authorization}</Badge>}
      </div>

      {out.answers?.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <h3>Answers</h3>
          {out.answers.map((a: any, i: number) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <div className="dim">{a.question}</div>
              <div>{a.answer}</div>
            </div>
          ))}
        </div>
      )}

      {out.recording_url && (
        <div style={{ marginBottom: 12 }}>
          <h3>Recording</h3>
          <audio controls preload="none" src={out.recording_url} style={{ width: '100%' }}>
            <a href={out.recording_url}>Download the recording</a>
          </audio>
        </div>
      )}

      {out.transcript && <Transcript text={out.transcript} />}
    </div>
  )
}

/**
 * Render the transcript as a conversation rather than a wall of text.
 *
 * VAPI returns it as newline-separated "AI:" / "User:" turns. Splitting on the speaker
 * prefix and styling the two sides differently makes it scannable — and makes it
 * obvious at a glance who said what, which a <pre> block does not.
 */
function Transcript({ text }: { text: string }) {
  const turns = text
    .split(/\n(?=(?:AI|User|Assistant|Customer):)/i)
    .map((chunk) => {
      const match = chunk.match(/^(AI|User|Assistant|Customer):\s*([\s\S]*)$/i)
      if (!match) return { speaker: '', body: chunk.trim() }
      const speaker = /ai|assistant/i.test(match[1]) ? 'ai' : 'candidate'
      return { speaker, body: match[2].trim() }
    })
    .filter((t) => t.body)

  if (turns.length <= 1) {
    return (
      <>
        <h3>Transcript</h3>
        <pre className="transcript">{text}</pre>
      </>
    )
  }

  return (
    <>
      <h3>Transcript</h3>
      <div className="convo">
        {turns.map((t, i) => (
          <div key={i} className={`turn ${t.speaker}`}>
            <span className="who">{t.speaker === 'ai' ? 'Interviewer' : 'Candidate'}</span>
            <div className="said">{t.body}</div>
          </div>
        ))}
      </div>
    </>
  )
}

const TITLES: Record<string, string> = {
  new_applicants: 'New applicant',
  resume_screening: 'Resume screening',
  approval_gate: 'Recruiter approval',
  sms_outreach: 'SMS outreach',
  email_notification: 'Email',
  ai_phone_call: 'AI phone interview',
  assessment_report: 'Assessment report',
  note_posting: 'JobDiva write-back',
}

function titleFor(step: StepRun) {
  return TITLES[step.module_id] ?? step.step_id
}

function formatMs(ms: number) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}
