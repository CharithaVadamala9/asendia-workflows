import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type JobDetail as Job, type WorkflowSummary } from '../api'
import { Badge, ErrorBox, Funnel, Loading, Score, StatusBadge } from '../components'

/**
 * The recruiter's main screen: one job, its funnel, and every applicant with their
 * score and pipeline stage. "Run workflow" is the manual push the spec asks for.
 */
export default function JobDetail() {
  const { id } = useParams()
  const jobId = Number(id)

  const [job, setJob] = useState<Job | null>(null)
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([])
  const [workflowId, setWorkflowId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(
    () => api.job(jobId).then(setJob).catch((e) => setError(e.message)),
    [jobId],
  )

  useEffect(() => {
    load()
    api.workflows().then((ws) => {
      setWorkflows(ws)
      setWorkflowId(ws.find((w) => w.is_active)?.id ?? ws[0]?.id ?? null)
    })
  }, [load])

  // A run places a phone call and finishes asynchronously, so poll while anything
  // is still in flight rather than leaving the recruiter looking at stale rows.
  useEffect(() => {
    const inFlight = job?.applicants.some(
      (a) => a.latest_run && ['running', 'pending', 'suspended'].includes(a.latest_run.status),
    )
    if (!inFlight) return
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [job, load])

  async function runWorkflow(applicationId: number) {
    if (!workflowId) return
    setBusy(applicationId)
    setError(null)
    try {
      await api.startRun(workflowId, applicationId)
      setTimeout(load, 600)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function runAll() {
    if (!workflowId || !job) return
    setError(null)
    const pending = job.applicants.filter((a) => !a.latest_run)
    for (const a of pending) {
      try {
        await api.startRun(workflowId, a.application_id)
      } catch (e) {
        setError((e as Error).message)
        break
      }
    }
    setTimeout(load, 800)
  }

  if (error && !job) return <ErrorBox error={error} />
  if (!job) return <Loading />

  const unstarted = job.applicants.filter((a) => !a.latest_run).length

  return (
    <>
      <div className="page-head">
        <Link to="/jobs" className="dim">← Jobs</Link>
        <div className="spread" style={{ marginTop: 6 }}>
          <div>
            <h1>{job.title}</h1>
            <p>
              {[job.city, job.state].filter(Boolean).join(', ') || 'Remote'}
              {job.jobdiva_id && ` · JobDiva #${job.jobdiva_id}`}
              {job.experience != null && ` · ${job.experience}+ years`}
            </p>
          </div>
          <div className="row">
            <select
              value={workflowId ?? ''}
              onChange={(e) => setWorkflowId(Number(e.target.value))}
              style={{ width: 200 }}
            >
              {workflows.map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>
            <button className="primary" onClick={runAll} disabled={!unstarted}>
              Run for {unstarted} new
            </button>
          </div>
        </div>
      </div>

      <ErrorBox error={error} />

      <div className="card">
        <h2>Pipeline</h2>
        <Funnel counts={job.funnel} />
      </div>

      <div className="card">
        <h2>Applicants</h2>
        <table>
          <thead>
            <tr>
              <th>Candidate</th>
              <th style={{ width: 110 }}>Stage</th>
              <th style={{ width: 70 }}>Score</th>
              <th style={{ width: 80 }}>Interview</th>
              <th style={{ width: 110 }}>Run</th>
              <th style={{ width: 130 }} />
            </tr>
          </thead>
          <tbody>
            {job.applicants.map((a) => (
              <tr key={a.application_id}>
                <td>
                  <strong>{a.name}</strong>
                  <div className="dim">{a.email ?? a.phone ?? '—'}</div>
                  {a.is_rejected && a.reject_reason && (
                    <div className="dim" style={{ color: 'var(--bad)' }}>
                      {a.reject_reason}
                    </div>
                  )}
                </td>
                <td>
                  <Badge tone={a.is_rejected ? 'bad' : a.stage === 'recommended' ? 'good' : ''}>
                    {a.is_rejected ? 'rejected' : a.stage}
                  </Badge>
                </td>
                <td className="num"><Score value={a.score} /></td>
                <td className="num">
                  {a.interview_score != null
                    ? <span className="score">{a.interview_score}/10</span>
                    : <span className="dim">—</span>}
                </td>
                <td>
                  {a.latest_run
                    ? <Link to={`/runs/${a.latest_run.id}`}>
                        <StatusBadge status={a.latest_run.status} />
                      </Link>
                    : <span className="dim">not started</span>}
                </td>
                <td>
                  {a.latest_run ? (
                    <Link to={`/runs/${a.latest_run.id}`}>
                      <button className="sm">View run</button>
                    </Link>
                  ) : (
                    <button
                      className="sm primary"
                      onClick={() => runWorkflow(a.application_id)}
                      disabled={busy === a.application_id || !workflowId}
                    >
                      {busy === a.application_id ? 'Starting…' : 'Run workflow'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Job description</h2>
        {job.skills && (
          <p className="muted" style={{ marginTop: 0 }}>
            <strong>Required skills:</strong> {job.skills}
          </p>
        )}
        <pre className="transcript">{job.description}</pre>
      </div>
    </>
  )
}
