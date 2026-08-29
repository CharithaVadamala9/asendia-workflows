/** Small shared presentational pieces. */

import type { CriterionScore, ScoreBreakdown, Stage } from './api'
import { STAGES } from './api'

export function Badge({ children, tone }: { children: React.ReactNode; tone?: string }) {
  return <span className={`badge ${tone ?? ''}`}>{children}</span>
}

const STATUS_TONE: Record<string, string> = {
  completed: 'good', running: 'info', suspended: 'warn',
  failed: 'bad', skipped: '', pending: '',
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? ''}>{status}</Badge>
}

export function scoreTone(score: number | null | undefined): string {
  if (score == null) return ''
  if (score >= 75) return 'good'
  if (score >= 55) return 'warn'
  return 'bad'
}

export function Score({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="dim">—</span>
  return <span className={`score ${scoreTone(value)}`}>{value.toFixed(0)}</span>
}

export function Funnel({ counts, active }: { counts: Record<string, number>; active?: Stage }) {
  return (
    <div className="funnel">
      {STAGES.map((stage) => (
        <div key={stage} className={`funnel-step ${active === stage ? 'active' : ''}`}>
          <div className="n">{counts[stage] ?? 0}</div>
          <div className="l">{stage}</div>
        </div>
      ))}
      {(counts.rejected ?? 0) > 0 && (
        <div className="funnel-step rejected">
          <div className="n">{counts.rejected}</div>
          <div className="l">rejected</div>
        </div>
      )}
    </div>
  )
}

/**
 * The score breakdown table. This is the answer to "why 72?" — every criterion
 * shows its own points, its weight, and the evidence behind it.
 */
export function ScoreTable({ breakdown }: { breakdown: ScoreBreakdown }) {
  if (breakdown.knockout) {
    return (
      <div className="notice">
        <strong>Disqualified.</strong> {breakdown.knockout}
      </div>
    )
  }
  return (
    <>
      {breakdown.degraded && (
        <div className="notice">{breakdown.degraded_reason}</div>
      )}
      <table>
        <thead>
          <tr>
            <th>Criterion</th>
            <th style={{ width: 70 }}>Weight</th>
            <th style={{ width: 90 }}>Points</th>
            <th style={{ width: 90 }} />
            <th>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {breakdown.criteria.map((c: CriterionScore) => {
            const points = c.normalized * c.weight * 100
            const max = c.weight * 100
            return (
              <tr key={c.key}>
                <td><strong>{c.label}</strong></td>
                <td className="num dim">{(c.weight * 100).toFixed(0)}%</td>
                <td className="num">
                  <span className={`score ${scoreTone(c.normalized * 100)}`}>
                    {points.toFixed(1)}
                  </span>
                  <span className="dim"> / {max.toFixed(0)}</span>
                </td>
                <td>
                  <div className="bar">
                    <i
                      className={scoreTone(c.normalized * 100)}
                      style={{ width: `${Math.round(c.normalized * 100)}%` }}
                    />
                  </div>
                </td>
                <td className="muted">{c.evidence}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="spread" style={{ marginTop: 12 }}>
        <span className="muted">
          Threshold {breakdown.threshold.toFixed(0)} ·{' '}
          {breakdown.qualified ? 'qualified for outreach' : 'below threshold'}
        </span>
        <span className={`score ${scoreTone(breakdown.score)}`} style={{ fontSize: 22 }}>
          {breakdown.score.toFixed(1)}
          <span className="dim" style={{ fontSize: 13, fontWeight: 400 }}> / 100</span>
        </span>
      </div>
    </>
  )
}

export function Loading() {
  return <div className="empty">Loading…</div>
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null
  return <div className="error">{error}</div>
}
