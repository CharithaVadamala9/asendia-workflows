import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type JobSummary } from '../api'
import { ErrorBox, Funnel, Loading } from '../components'

export default function Jobs() {
  const [jobs, setJobs] = useState<JobSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)

  const load = () => api.jobs().then(setJobs).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])

  async function sync() {
    setSyncing(true)
    setError(null)
    try {
      await api.syncJobs()
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSyncing(false)
    }
  }

  return (
    <>
      <div className="page-head spread">
        <div>
          <h1>Jobs</h1>
          <p>Open requisitions and their applicant pipelines.</p>
        </div>
        <button className="primary" onClick={sync} disabled={syncing}>
          {syncing ? 'Syncing…' : 'Sync from JobDiva'}
        </button>
      </div>

      <ErrorBox error={error} />
      {!jobs ? <Loading /> : jobs.length === 0 ? (
        <div className="card empty">
          No jobs yet. Sync from JobDiva to pull in open requisitions and their applicants.
        </div>
      ) : (
        jobs.map((job) => (
          <div className="card" key={job.id}>
            <div className="spread" style={{ marginBottom: 14 }}>
              <div>
                <Link to={`/jobs/${job.id}`}>
                  <h2 style={{ fontSize: 16, marginBottom: 2 }}>{job.title}</h2>
                </Link>
                <div className="dim">
                  {[job.city, job.state].filter(Boolean).join(', ') || 'Remote'}
                  {job.jobdiva_id && ` · JobDiva #${job.jobdiva_id}`}
                </div>
              </div>
              <div className="row">
                <span className="muted">
                  <strong>{job.applicant_count}</strong> applicant
                  {job.applicant_count === 1 ? '' : 's'}
                </span>
                <Link to={`/jobs/${job.id}`}>
                  <button className="sm">Open pipeline</button>
                </Link>
              </div>
            </div>
            <Funnel counts={job.funnel} />
          </div>
        ))
      )}
    </>
  )
}
