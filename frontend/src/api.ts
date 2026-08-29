/** Typed client for the backend. Vite proxies /api to FastAPI in development. */

export type Stage =
  | 'applied' | 'screened' | 'qualified' | 'contacted' | 'interviewed' | 'recommended'

export const STAGES: Stage[] = [
  'applied', 'screened', 'qualified', 'contacted', 'interviewed', 'recommended',
]

export interface ModuleSpec {
  id: string
  name: string
  description: string
  category: 'trigger' | 'action' | 'ai'
  config_schema: JsonSchema
  output_schema: JsonSchema
  is_async: boolean
}

export interface JsonSchema {
  type?: string
  title?: string
  description?: string
  default?: unknown
  properties?: Record<string, JsonSchema>
  required?: string[]
  minimum?: number
  maximum?: number
  enum?: string[]
  anyOf?: JsonSchema[]
}

export interface Applicant {
  application_id: number
  candidate_id: number
  name: string
  email: string | null
  phone: string | null
  stage: Stage
  is_rejected: boolean
  reject_reason: string | null
  score: number | null
  interview_score: number | null
  applied_at: string | null
  latest_run: { id: number; status: string } | null
}

export interface JobSummary {
  id: number
  jobdiva_id: number | null
  title: string
  city: string | null
  state: string | null
  applicant_count: number
  funnel: Record<string, number>
}

export interface JobDetail extends Omit<JobSummary, 'applicant_count'> {
  description: string
  skills: string
  experience: number | null
  applicants: Applicant[]
}

export interface CriterionScore {
  key: string
  label: string
  normalized: number
  weight: number
  evidence: string
  detail: Record<string, unknown>
}

export interface ScoreBreakdown {
  score: number
  qualified: boolean
  threshold: number
  criteria: CriterionScore[]
  knockout: string | null
  degraded: boolean
  degraded_reason: string | null
}

/** One attempted write to JobDiva, as reported by the write-back step. */
export interface JobDivaWrite {
  op: string
  ok: boolean
  suppressed: boolean
  status: 'sent' | 'suppressed' | 'blocked'
  payload: Record<string, unknown>
  result: unknown
  reason: string | null
}

export interface StepRun {
  id: number
  step_id: string
  module_id: string
  status: 'pending' | 'running' | 'completed' | 'suspended' | 'failed' | 'skipped'
  config: Record<string, unknown>
  output: Record<string, any>
  error: string | null
  skip_reason: string | null
  duration_ms: number | null
  started_at: string | null
  ended_at: string | null
}

export interface RunDetail {
  id: number
  status: string
  cursor: number
  error: string | null
  dry_run: boolean
  trigger_source: string
  started_at: string
  ended_at: string | null
  workflow: { id: number; name: string | null }
  candidate: { id: number; name: string | null; email: string | null; phone: string | null }
  job: { id: number; title: string | null }
  steps: StepRun[]
}

export interface RunSummary {
  id: number
  status: string
  workflow: string | null
  candidate: string | null
  job: string | null
  trigger_source: string
  started_at: string
  ended_at: string | null
}

export interface WorkflowSummary {
  id: number
  name: string
  description: string
  is_active: boolean
  step_count: number
  run_count: number
}

export interface WorkflowStep {
  id: string
  module: string
  config: Record<string, unknown>
  when?: string
}

export interface WorkflowDetail {
  id: number
  name: string
  description: string
  is_active: boolean
  definition: { trigger?: { module: string; config: Record<string, unknown> }; steps: WorkflowStep[] }
}

export interface Overview {
  jobs: number
  applicants: number
  scored: number
  qualified: number
  rejected: number
  interviewed: number
  recommended: number
  average_score: number | null
  average_interview: number | null
  runs: { total: number; completed: number; suspended: number; failed: number }
  stages: Record<string, number>
  minutes_saved: number
  top_candidates: {
    name: string; job: string; job_id: number; score: number | null
    interview_score: number | null; stage: Stage; application_id: number
  }[]
  jobs_breakdown: {
    id: number; title: string; applicants: number
    screened: number; qualified: number; top_score: number | null
  }[]
}

export interface IntegrationStatus {
  jobdiva: { mode: string; configured: boolean; base_url: string; write_mode: string }
  vapi: { mode: string; configured: boolean; phone_number_id: boolean; note: string }
  sms: { mode: string; configured: boolean }
  email: { mode: string; configured: boolean }
  llm: { mode: string; configured: boolean; model: string }
  poller: { running: boolean; job_id: number | null; poll_seconds: number | null; reason: string }
  dry_run: boolean
  jobdiva_write_mode: string
  public_base_url: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    const body = await resp.text()
    let detail = body
    try { detail = JSON.parse(body).detail ?? body } catch { /* plain text */ }
    throw new Error(detail || `${resp.status} ${resp.statusText}`)
  }
  return resp.json() as Promise<T>
}

export const api = {
  overview: () => request<Overview>('/overview'),

  modules: () => request<ModuleSpec[]>('/modules'),

  jobs: () => request<JobSummary[]>('/jobs'),
  job: (id: number) => request<JobDetail>(`/jobs/${id}`),
  syncJobs: () => request<{ synced: number }>('/jobs/sync', { method: 'POST' }),

  runs: () => request<RunSummary[]>('/runs'),
  run: (id: number) => request<RunDetail>(`/runs/${id}`),
  startRun: (workflow_id: number, application_id: number, dry_run = false) =>
    request<{ run_id: number; status: string }>('/runs', {
      method: 'POST',
      body: JSON.stringify({ workflow_id, application_id, dry_run }),
    }),
  approve: (runId: number, approved: boolean, comment?: string) =>
    request<{ run_id: number; status: string }>(`/runs/${runId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ approved, comment, decided_by: 'recruiter' }),
    }),

  workflows: () => request<WorkflowSummary[]>('/workflows'),
  workflow: (id: number) => request<WorkflowDetail>(`/workflows/${id}`),
  saveWorkflow: (id: number, body: Omit<WorkflowDetail, 'id'>) =>
    request<{ id: number }>(`/workflows/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  cloneTemplate: () =>
    request<{ id: number }>('/workflows/from-template', { method: 'POST' }),

  integrations: () => request<IntegrationStatus>('/integrations'),
  testJobDiva: () =>
    request<{ ok: boolean; auth_scheme?: string; error?: string; api_limits?: unknown }>(
      '/integrations/jobdiva/test', { method: 'POST' },
    ),
}
