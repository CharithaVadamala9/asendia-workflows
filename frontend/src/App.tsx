import { NavLink, Route, Routes } from 'react-router-dom'
import Overview from './pages/Overview'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import RunDetail from './pages/RunDetail'
import { Runs, Integrations } from './pages/Misc'
import { Workflows, WorkflowBuilder } from './pages/Workflows'

export default function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          Asendia
          <small>Recruitment automation</small>
        </div>
        <nav className="nav">
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/jobs">Jobs</NavLink>
          <NavLink to="/workflows">Workflows</NavLink>
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/integrations">Integrations</NavLink>
        </nav>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:id" element={<JobDetail />} />
          <Route path="/workflows" element={<Workflows />} />
          <Route path="/workflows/:id" element={<WorkflowBuilder />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/integrations" element={<Integrations />} />
        </Routes>
      </main>
    </div>
  )
}
