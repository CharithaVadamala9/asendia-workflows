import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  api, type ModuleSpec, type WorkflowDetail, type WorkflowStep, type WorkflowSummary,
} from '../api'
import { ConfigForm } from '../ConfigForm'
import { Badge, ErrorBox, Loading } from '../components'

export function Workflows() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => { api.workflows().then(setWorkflows).catch((e) => setError(e.message)) }, [])

  async function clone() {
    try {
      const { id } = await api.cloneTemplate()
      navigate(`/workflows/${id}`)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <>
      <div className="page-head spread">
        <div>
          <h1>Workflows</h1>
          <p>Reusable templates. Each one is a sequence of configurable modules.</p>
        </div>
        <button className="primary" onClick={clone}>New from template</button>
      </div>

      <ErrorBox error={error} />
      {!workflows ? <Loading /> : workflows.map((w) => (
        <div className="card" key={w.id}>
          <div className="spread">
            <div>
              <Link to={`/workflows/${w.id}`}>
                <h2 style={{ fontSize: 15, marginBottom: 2 }}>{w.name}</h2>
              </Link>
              <div className="muted">{w.description}</div>
              <div className="dim" style={{ marginTop: 4 }}>
                {w.step_count} steps · {w.run_count} runs
              </div>
            </div>
            <div className="row">
              {w.is_active && <Badge tone="good">active</Badge>}
              <Link to={`/workflows/${w.id}`}><button className="sm">Edit</button></Link>
            </div>
          </div>
        </div>
      ))}
    </>
  )
}

export function WorkflowBuilder() {
  const { id } = useParams()
  const workflowId = Number(id)

  const [wf, setWf] = useState<WorkflowDetail | null>(null)
  const [modules, setModules] = useState<ModuleSpec[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.workflow(workflowId).then(setWf).catch((e) => setError(e.message))
    api.modules().then(setModules)
  }, [workflowId])

  if (error && !wf) return <ErrorBox error={error} />
  if (!wf) return <Loading />

  const steps = wf.definition.steps ?? []
  const specOf = (moduleId: string) => modules.find((m) => m.id === moduleId)

  function update(next: Partial<WorkflowDetail>) {
    setWf({ ...wf!, ...next })
    setSaved(false)
  }

  function updateStep(index: number, patch: Partial<WorkflowStep>) {
    const next = steps.map((s, i) => (i === index ? { ...s, ...patch } : s))
    update({ definition: { ...wf!.definition, steps: next } })
  }

  function move(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= steps.length) return
    const next = [...steps]
    ;[next[index], next[target]] = [next[target], next[index]]
    update({ definition: { ...wf!.definition, steps: next } })
  }

  function remove(index: number) {
    update({
      definition: { ...wf!.definition, steps: steps.filter((_, i) => i !== index) },
    })
  }

  function add(spec: ModuleSpec) {
    // Seed the config from the schema's own defaults, so a newly added step is
    // immediately valid rather than requiring every field to be filled in.
    const config: Record<string, unknown> = {}
    for (const [key, prop] of Object.entries(spec.config_schema.properties ?? {})) {
      if (prop.default !== undefined) config[key] = prop.default
    }
    const step: WorkflowStep = {
      id: uniqueId(spec.id, steps),
      module: spec.id,
      config,
    }
    update({ definition: { ...wf!.definition, steps: [...steps, step] } })
  }

  async function save() {
    setError(null)
    try {
      await api.saveWorkflow(workflowId, {
        name: wf!.name,
        description: wf!.description,
        definition: wf!.definition,
        is_active: wf!.is_active,
      })
      setSaved(true)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <>
      <div className="page-head">
        <Link to="/workflows" className="dim">← Workflows</Link>
        <div className="spread" style={{ marginTop: 6 }}>
          <div>
            <h1>{wf.name}</h1>
            <p>{steps.length} steps · runs top to bottom</p>
          </div>
          <div className="row">
            {saved && <span className="dim">Saved</span>}
            <button className="primary" onClick={save}>Save</button>
          </div>
        </div>
      </div>

      <ErrorBox error={error} />

      <div className="card">
        <div className="field">
          <label>Name</label>
          <input value={wf.name} onChange={(e) => update({ name: e.target.value })} />
        </div>
        <div className="field">
          <label>Description</label>
          <textarea
            value={wf.description}
            onChange={(e) => update({ description: e.target.value })}
          />
        </div>
        <div className="checkbox">
          <input
            type="checkbox" checked={wf.is_active}
            onChange={(e) => update({ is_active: e.target.checked })}
          />
          <label style={{ marginBottom: 0 }}>
            Active — run automatically for new applicants
          </label>
        </div>
      </div>

      {steps.map((step, i) => {
        const spec = specOf(step.module)
        return (
          <div className="card" key={`${step.id}-${i}`}>
            <div className="spread" style={{ marginBottom: 10 }}>
              <div className="row">
                <span className="dim">{i + 1}</span>
                <strong>{spec?.name ?? step.module}</strong>
                {spec && <Badge tone={spec.category === 'ai' ? 'info' : ''}>{spec.category}</Badge>}
                {spec?.is_async && <Badge tone="warn">async</Badge>}
              </div>
              <div className="row">
                <button className="sm" onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
                <button className="sm" onClick={() => move(i, 1)} disabled={i === steps.length - 1}>↓</button>
                <button className="sm danger" onClick={() => remove(i)}>Remove</button>
              </div>
            </div>

            {spec && <p className="dim" style={{ marginTop: 0 }}>{spec.description}</p>}

            <div className="field">
              <label>Run only when</label>
              <input
                placeholder="always — e.g. {{steps.screen.output.score}} >= 70"
                value={step.when ?? ''}
                onChange={(e) => updateStep(i, { when: e.target.value || undefined })}
              />
              <div className="dim">
                Leave empty to always run. References any earlier step's output.
              </div>
            </div>

            {spec && (
              <ConfigForm
                schema={spec.config_schema}
                value={step.config}
                onChange={(config) => updateStep(i, { config })}
              />
            )}
          </div>
        )
      })}

      <div className="card">
        <h2>Add a step</h2>
        <p className="dim" style={{ marginTop: 0 }}>
          Every module below registered itself on the backend. Their forms above are
          generated from each module's own schema — no frontend code per module.
        </p>
        <div className="grid two">
          {modules.filter((m) => m.category !== 'trigger').map((m) => (
            <div key={m.id} className="spread" style={{ gap: 10 }}>
              <div>
                <strong>{m.name}</strong>
                <div className="dim">{m.description}</div>
              </div>
              <button className="sm" onClick={() => add(m)}>Add</button>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}

function uniqueId(base: string, steps: WorkflowStep[]): string {
  const short = base.split('_')[0]
  let candidate = short
  let n = 2
  while (steps.some((s) => s.id === candidate)) candidate = `${short}${n++}`
  return candidate
}
