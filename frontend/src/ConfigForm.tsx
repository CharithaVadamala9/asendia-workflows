/**
 * Renders a configuration form directly from a module's JSON Schema.
 *
 * This is the piece that makes the platform claim real: a module declares its config
 * as a Pydantic model on the backend, the engine publishes `model_json_schema()`, and
 * this component turns that into a form. Adding a new module requires no frontend
 * change at all — its fields appear here automatically, with their titles, help text,
 * defaults, and bounds intact.
 */

import type { JsonSchema } from './api'

interface Props {
  schema: JsonSchema
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
}

export function ConfigForm({ schema, value, onChange }: Props) {
  const properties = schema.properties ?? {}
  const keys = Object.keys(properties)

  if (keys.length === 0) {
    return <div className="dim">This module has no configuration.</div>
  }

  return (
    <>
      {keys.map((key) => (
        <Field
          key={key}
          name={key}
          schema={properties[key]}
          value={value[key]}
          onChange={(v) => onChange({ ...value, [key]: v })}
        />
      ))}
    </>
  )
}

function Field({
  name, schema, value, onChange,
}: {
  name: string
  schema: JsonSchema
  value: unknown
  onChange: (v: unknown) => void
}) {
  const label = schema.title ?? name
  const type = resolveType(schema)
  const current = value ?? schema.default

  // Weights and other free-form object fields get a nested set of number inputs
  // rather than a JSON blob — recruiters tune these.
  if (type === 'object' && current && typeof current === 'object') {
    return (
      <div className="field">
        <label>{label}</label>
        {schema.description && <div className="dim">{schema.description}</div>}
        <div style={{ paddingLeft: 12, marginTop: 6 }}>
          {Object.entries(current as Record<string, number>).map(([k, v]) => (
            <div className="field" key={k} style={{ marginBottom: 6 }}>
              <label style={{ textTransform: 'capitalize' }}>{k.replace(/_/g, ' ')}</label>
              <input
                type="number" step="0.05" min={0} max={1} value={v}
                onChange={(e) =>
                  onChange({ ...(current as object), [k]: Number(e.target.value) })
                }
              />
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (type === 'boolean') {
    return (
      <div className="field">
        <div className="checkbox">
          <input
            type="checkbox"
            checked={Boolean(current)}
            onChange={(e) => onChange(e.target.checked)}
          />
          <label style={{ marginBottom: 0 }}>{label}</label>
        </div>
        {schema.description && <div className="dim">{schema.description}</div>}
      </div>
    )
  }

  if (schema.enum) {
    return (
      <div className="field">
        <label>{label}</label>
        <select value={String(current ?? '')} onChange={(e) => onChange(e.target.value)}>
          {schema.enum.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        {schema.description && <div className="dim">{schema.description}</div>}
      </div>
    )
  }

  if (type === 'integer' || type === 'number') {
    return (
      <div className="field">
        <label>{label}</label>
        <input
          type="number"
          value={current == null ? '' : String(current)}
          min={schema.minimum}
          max={schema.maximum}
          step={type === 'integer' ? 1 : 'any'}
          onChange={(e) =>
            onChange(e.target.value === '' ? null : Number(e.target.value))
          }
        />
        {schema.description && <div className="dim">{schema.description}</div>}
      </div>
    )
  }

  // Long text (message templates) gets a textarea; short strings get an input.
  const isLong = String(current ?? '').length > 60 || /template|body|note|message/i.test(name)
  return (
    <div className="field">
      <label>{label}</label>
      {isLong ? (
        <textarea value={String(current ?? '')} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input value={String(current ?? '')} onChange={(e) => onChange(e.target.value)} />
      )}
      {schema.description && <div className="dim">{schema.description}</div>}
    </div>
  )
}

/** Pydantic emits optional fields as anyOf[T, null]; unwrap to the real type. */
function resolveType(schema: JsonSchema): string | undefined {
  if (schema.type) return schema.type
  const concrete = schema.anyOf?.find((s) => s.type && s.type !== 'null')
  return concrete?.type
}
