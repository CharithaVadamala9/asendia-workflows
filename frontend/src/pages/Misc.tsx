import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type IntegrationStatus, type RunSummary } from '../api'
import { Badge, ErrorBox, Loading, StatusBadge } from '../components'

export function Runs() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = () => api.runs().then(setRuns).catch((e) => setError(e.message))
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  return (
    <>
      <div className="page-head">
        <h1>Runs</h1>
        <p>Every workflow execution, with a full audit trail of each step.</p>
      </div>
      <ErrorBox error={error} />
      {!runs ? <Loading /> : runs.length === 0 ? (
        <div className="card empty">No runs yet. Start one from a job's pipeline.</div>
      ) : (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th style={{ width: 50 }}>#</th>
                <th>Candidate</th>
                <th>Job</th>
                <th>Workflow</th>
                <th style={{ width: 90 }}>Trigger</th>
                <th style={{ width: 100 }}>Status</th>
                <th style={{ width: 150 }}>Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td className="dim">{r.id}</td>
                  <td><Link to={`/runs/${r.id}`}><strong>{r.candidate ?? '—'}</strong></Link></td>
                  <td className="muted">{r.job ?? '—'}</td>
                  <td className="muted">{r.workflow ?? '—'}</td>
                  <td><span className="dim">{r.trigger_source}</span></td>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="dim">{new Date(r.started_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

export function Integrations() {
  const [status, setStatus] = useState<IntegrationStatus | null>(null)
  const [test, setTest] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)

  useEffect(() => { api.integrations().then(setStatus) }, [])

  async function testJobDiva() {
    setTesting(true)
    setTest(null)
    try {
      const r = await api.testJobDiva()
      setTest(
        r.ok
          ? `Connected. Authorization header format: ${r.auth_scheme}.`
          : `Failed — ${r.error}`,
      )
    } catch (e) {
      setTest(`Failed — ${(e as Error).message}`)
    } finally {
      setTesting(false)
    }
  }

  if (!status) return <Loading />

  const rows: [string, { mode: string; configured: boolean }, string?][] = [
    ['JobDiva ATS', status.jobdiva,
      `${status.jobdiva.base_url} · writes ${status.jobdiva.write_mode}`],
    ['VAPI (voice)', status.vapi, status.vapi.phone_number_id ? 'outbound number set' : 'no outbound number'],
    ['SMS', status.sms],
    ['Email (Mailjet)', status.email],
    ['LLM (Claude)', status.llm, status.llm.model],
  ]

  return (
    <>
      <div className="page-head">
        <h1>Integrations</h1>
        <p>Every provider can run live or mocked, independently.</p>
      </div>

      {status.jobdiva_write_mode !== 'live' && (
        <div className="notice">
          <strong>JobDiva writes are suppressed.</strong> Reads are live — real jobs,
          applicants, resumes and AI scoring — but nothing is written back. Each run
          records the exact payload it would have sent. Set{' '}
          <code>JOBDIVA_WRITE_MODE=live</code> in <code>backend/.env</code> to enable
          writes.
        </div>
      )}

      {status.dry_run && (
        <div className="notice">
          <strong>Dry run is on.</strong> Every step executes and records what it would
          have sent, but nothing leaves the system.
        </div>
      )}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th style={{ width: 90 }}>Mode</th>
              <th style={{ width: 130 }}>Credentials</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, s, detail]) => (
              <tr key={name}>
                <td><strong>{name}</strong></td>
                <td>
                  <Badge tone={s.mode === 'live' ? 'good' : ''}>{s.mode}</Badge>
                </td>
                <td>
                  {s.configured
                    ? <Badge tone="good">configured</Badge>
                    : <Badge tone="warn">missing</Badge>}
                </td>
                <td className="dim">{detail ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Applicant trigger</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          JobDiva has no webhooks, so new applications are detected by polling with a
          watermark. A trigger that quietly is not running looks exactly like "no new
          applicants", so its state is shown here rather than only in the logs.
        </p>
        <div className="row">
          {status.poller.running
            ? <Badge tone="good">running</Badge>
            : <Badge tone="warn">not running</Badge>}
          <span className="muted">
            {status.poller.running
              ? `watching job ${status.poller.job_id} every ${status.poller.poll_seconds}s`
              : status.poller.reason}
          </span>
        </div>
      </div>

      <div className="card">
        <h2>JobDiva connection</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Authenticates and reads this tenant's per-endpoint rate limits. The API
          declares its auth header without specifying whether it wants a bearer prefix,
          so the client resolves that on the first call and reports which form worked.
        </p>
        <div className="row">
          <button onClick={testJobDiva} disabled={testing}>
            {testing ? 'Testing…' : 'Test connection'}
          </button>
          {test && <span className="muted">{test}</span>}
        </div>
      </div>

      <div className="card">
        <h2>Webhook endpoint</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          VAPI posts call results here. During local development this must be a public
          tunnel, not localhost.
        </p>
        <pre className="transcript">{status.public_base_url}/api/webhooks/vapi</pre>
      </div>
    </>
  )
}
