import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Overview as OverviewData } from '../api'
import { Badge, ErrorBox, Funnel, Loading, Score } from '../components'

/**
 * The landing screen: what the automation has actually done.
 *
 * Every number here is computed from the same tables the run timeline reads, so the
 * summary can never disagree with the detail someone drills into — which is the
 * failure mode that makes dashboards untrustworthy.
 */
export default function Overview() {
  const [data, setData] = useState<OverviewData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = () => api.overview().then(setData).catch((e) => setError(e.message))
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  if (error && !data) return <ErrorBox error={error} />
  if (!data) return <Loading />

  const hours = Math.round(data.minutes_saved / 6) / 10

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <p>Automated screening across every open requisition.</p>
      </div>

      <div className="stats">
        <Stat label="Applicants" value={data.applicants} sub={`${data.jobs} job${data.jobs === 1 ? '' : 's'}`} />
        <Stat label="Screened" value={data.scored} sub={data.average_score != null ? `avg ${data.average_score}` : '—'} />
        <Stat label="Qualified" value={data.qualified} sub={`${data.rejected} screened out`} tone="good" />
        <Stat label="Interviewed" value={data.interviewed} sub={data.average_interview != null ? `avg ${data.average_interview}/10` : '—'} />
        <Stat label="Recruiter hours saved" value={hours} sub="10 min/screen · 20 min/call" tone="accent" />
      </div>

      <div className="card">
        <div className="spread" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Pipeline</h2>
          <span className="dim">
            {data.runs.total} runs · {data.runs.completed} completed
            {data.runs.suspended > 0 && ` · ${data.runs.suspended} in progress`}
            {data.runs.failed > 0 && ` · ${data.runs.failed} failed`}
          </span>
        </div>
        <Funnel counts={{ ...data.stages, rejected: data.rejected }} />
      </div>

      <div className="card">
        <div className="spread" style={{ marginBottom: 14 }}>
          <h2 style={{ margin: 0 }}>Requisitions</h2>
          <Link to="/jobs" className="dim">All jobs →</Link>
        </div>
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th style={{ width: 110 }}>Applicants</th>
              <th style={{ width: 100 }}>Screened</th>
              <th style={{ width: 100 }}>Qualified</th>
              <th style={{ width: 90 }}>Top score</th>
            </tr>
          </thead>
          <tbody>
            {data.jobs_breakdown.map((j) => (
              <tr key={j.id}>
                <td><Link to={`/jobs/${j.id}`}><strong>{j.title}</strong></Link></td>
                <td className="num">{j.applicants}</td>
                <td className="num">
                  {j.screened > 0
                    ? j.screened
                    : <span className="dim">not screened</span>}
                </td>
                <td className="num">
                  {j.screened > 0 ? j.qualified : <span className="dim">—</span>}
                </td>
                <td className="num"><Score value={j.top_score} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Top candidates</h2>
        {data.top_candidates.length === 0 ? (
          <div className="dim">
            No one scored yet. Sync a job and run the workflow to populate this.
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Candidate</th>
                <th>Job</th>
                <th style={{ width: 90 }}>Stage</th>
                <th style={{ width: 70 }}>Resume</th>
                <th style={{ width: 80 }}>Interview</th>
              </tr>
            </thead>
            <tbody>
              {data.top_candidates.map((c) => (
                <tr key={c.application_id}>
                  <td><strong>{c.name}</strong></td>
                  <td className="muted">
                    <Link to={`/jobs/${c.job_id}`}>{c.job}</Link>
                  </td>
                  <td><Badge tone={c.stage === 'recommended' ? 'good' : ''}>{c.stage}</Badge></td>
                  <td className="num"><Score value={c.score} /></td>
                  <td className="num">
                    {c.interview_score != null
                      ? <span className="score">{c.interview_score}/10</span>
                      : <span className="dim">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}

function Stat({
  label, value, sub, tone,
}: { label: string; value: number; sub?: string; tone?: string }) {
  return (
    <div className="stat">
      <div className={`stat-value ${tone ?? ''}`}>{value}</div>
      <div className="stat-label">{label}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}
