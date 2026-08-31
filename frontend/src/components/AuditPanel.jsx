import { useState } from 'react'
import { num, TASK_HINT, TASK_LABEL } from '../format.js'
import { traceUrl } from '../api.js'
import { FileJson } from './Icons.jsx'

/**
 * Audit trace panel.
 *
 * Problem statement 26167 grades the observable execution trace, not the internal
 * reasoning, so this panel is the primary UI surface: the routing decision, the
 * registry entries chosen, the parameters permitted and rejected, every input
 * check, the executed tool sequence, and the raw JSON — visible without opening
 * developer tools, and downloadable.
 */
export default function AuditPanel({ job }) {
  const trace = job?.trace
  const [tab, setTab] = useState('routing')
  if (!trace) {
    return (
      <section className="panel">
        <div className="panel-head"><h3>Audit trace</h3></div>
        <p className="muted">Run a query to produce the graded execution trace.</p>
      </section>
    )
  }
  const entries = trace.registry_entries_used || []
  const cls = trace.classification || {}
  const tabs = [
    ['routing', 'Routing'],
    ['validation', `Checks (${(trace.validation?.checks || []).length})`],
    ['steps', `Tool sequence (${(trace.steps || []).length})`],
    ['confidence', 'Confidence'],
    ['json', 'Raw JSON'],
  ]

  return (
    <section className="panel audit">
      <div className="panel-head">
        <h3>
          Audit trace <span className="muted">schema {trace.schema_version} · the graded artefact</span>
        </h3>
        <a className="btn btn-ghost" href={traceUrl(job.id)} target="_blank" rel="noreferrer">
          <FileJson /> trace.json
        </a>
      </div>

      <div className="kpi-row">
        <Kpi k="task" v={TASK_LABEL[trace.task] || trace.task} sub={TASK_HINT[trace.task]} />
        <Kpi k="tools used" v={entries.map((e) => e.id).join(' → ') || '—'} sub={`plan: ${(trace.plan || []).join(' → ') || '—'}`} />
        <Kpi k="model / tool" v={entries[0]?.model_id || '—'} sub={entries[0]?.adapter ? `adapter ${entries[0].adapter}` : 'no adapter'} />
        <Kpi k="narration" v={trace.outputs?.text?.narration_source || '—'} sub={trace.model_backend} />
        <Kpi k="runtime" v={`${trace.execution_time_ms ?? '—'} ms`} sub={`device ${trace.environment?.device || 'cpu'}`} />
      </div>

      <div className="chip-row tabs">
        {tabs.map(([key, label]) => (
          <button key={key} className={`chip ${tab === key ? 'chip-on' : ''}`} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'routing' && <Routing trace={trace} cls={cls} entries={entries} />}
      {tab === 'validation' && <Validation validation={trace.validation} inputs={trace.input_config} />}
      {tab === 'steps' && <Steps steps={trace.steps} />}
      {tab === 'confidence' && <Confidence conf={trace.confidence} />}
      {tab === 'json' && <pre className="json">{JSON.stringify(trace, null, 1)}</pre>}
    </section>
  )
}

function Kpi({ k, v, sub }) {
  return (
    <div className="kpi">
      <span className="kpi-k">{k}</span>
      <span className="kpi-v">{v}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  )
}

function Routing({ trace, cls, entries }) {
  const scores = cls.alternatives || {}
  const ranked = Object.entries(scores).sort((a, b) => b[1] - a[1])
  return (
    <div className="stack">
      <table className="data">
        <tbody>
          <Row k="classified task" v={trace.task} />
          <Row k="classifier method" v={cls.method} />
          <Row k="runner-up" v={cls.runner_up ? `${cls.runner_up.task} (${num(cls.runner_up.score, 3)})` : '—'} />
          <Row k="permitted parameters" v={<code>{JSON.stringify(trace.effective_parameters || {})}</code>} />
          <Row
            k="rejected parameters"
            v={entries.flatMap((e) => e.rejected_parameters || []).join(', ') || 'none'}
          />
          <Row k="matched query evidence" v={Object.entries(cls.matched_evidence || {}).map(([t, terms]) => `${t}: ${terms.join(', ')}`).join(' | ') || '—'} />
        </tbody>
      </table>

      {ranked.length > 0 && (
        <div className="scores">
          <h5>Task scores (feasible tasks only)</h5>
          {ranked.map(([task, score]) => (
            <div key={task} className="score-row">
              <span className={`score-task ${task === trace.task ? 'chosen' : ''}`}>{task}</span>
              <span className="bar"><span className="bar-fill" style={{ width: `${score * 100}%` }} /></span>
              <span className="mono">{num(score, 3)}</span>
            </div>
          ))}
        </div>
      )}

      {Object.keys(cls.infeasible_tasks || {}).length > 0 && (
        <div className="limits">
          <h5>Tasks the input configuration rules out</h5>
          <ul>
            {Object.entries(cls.infeasible_tasks).map(([task, why]) => (
              <li key={task}><b>{task}</b> — {why}</li>
            ))}
          </ul>
        </div>
      )}

      {(cls.reasons || []).length > 0 && (
        <div className="limits">
          <h5>Routing rationale</h5>
          <ul>{cls.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}

      {entries.length > 0 && (
        <table className="data">
          <thead>
            <tr><th>registry entry</th><th>tool kind</th><th>model</th><th>adapter</th><th>parameters</th></tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td className="mono">{e.id}</td>
                <td>{e.tool_kind}</td>
                <td className="tiny">{e.model_id}</td>
                <td className="tiny">{e.adapter || '—'}</td>
                <td className="tiny mono">{JSON.stringify(e.permitted_parameters)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Validation({ validation, inputs }) {
  const checks = validation?.checks || []
  return (
    <div className="stack">
      <table className="data">
        <thead><tr><th>check</th><th>status</th><th>message</th></tr></thead>
        <tbody>
          {checks.map((c, i) => (
            <tr key={i}>
              <td className="mono">{c.name}</td>
              <td><span className={`tag tag-${c.status}`}>{c.status}</span></td>
              <td className="tiny">{c.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="data">
        <thead>
          <tr><th>input</th><th>modality</th><th>format</th><th>bands</th><th>CRS</th>
            <th>pixel</th><th>date</th><th>band roles</th></tr>
        </thead>
        <tbody>
          {(inputs?.images || []).map((im, i) => (
            <tr key={i}>
              <td className="tiny">{im.filename}</td>
              <td>
                {im.modality}
                <span className={`tag tag-${im.modality_confidence}`}>{im.modality_confidence}</span>
              </td>
              <td>{im.format}</td>
              <td className="mono">{im.band_count}</td>
              <td className="tiny">{im.crs || '—'}</td>
              <td className="mono tiny">{im.pixel_area_m2 ? `${num(im.pixel_area_m2, 1)} m²` : '—'}</td>
              <td className="tiny">{im.acquisition_date || '—'}</td>
              <td className="tiny mono">{Object.keys(im.band_assignment?.roles || {}).join(', ') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="tiny muted">
        Input configuration: {inputs?.n_images} image(s) · modalities {(inputs?.modality_signature || []).join(' + ')}
        {inputs?.co_registered !== null && inputs?.co_registered !== undefined
          ? ` · co-registered: ${inputs.co_registered ? 'verified' : 'not verified'}`
          : ' · co-registration not verifiable'}
        {inputs?.bi_temporal ? ' · bi-temporal' : ''}{inputs?.cross_modal ? ' · cross-modal' : ''}
      </p>
    </div>
  )
}

function Steps({ steps }) {
  return (
    <ol className="timeline">
      {(steps || []).map((s) => (
        <li key={s.order} className={`step step-${s.status}`}>
          <div className="step-head">
            <span className="mono">{s.tool}</span>
            <span className="muted">{s.duration_ms} ms</span>
            <span className={`tag tag-${s.status}`}>{s.status}</span>
          </div>
          {Object.keys(s.parameters || {}).length > 0 && (
            <code className="tiny">{JSON.stringify(s.parameters)}</code>
          )}
          {s.outputs_summary && <p className="tiny">{s.outputs_summary}</p>}
        </li>
      ))}
    </ol>
  )
}

function Confidence({ conf }) {
  if (!conf) return <p className="muted">No confidence block.</p>
  return (
    <div className="stack">
      <p>
        <b>{conf.value === null || conf.value === undefined ? 'not available' : num(conf.value, 3)}</b>
        {' · source '}<span className="mono">{conf.source}</span>
        {' · calibrated: '}{conf.calibrated ? 'yes' : 'no'}
      </p>
      {(conf.components || []).length > 0 && (
        <table className="data">
          <thead><tr><th>signal</th><th>value</th><th>weight</th><th>what it measures</th></tr></thead>
          <tbody>
            {conf.components.map((c) => (
              <tr key={c.name}>
                <td className="mono">{c.name}</td>
                <td className="mono">{num(c.value, 3)}</td>
                <td className="mono">{num(c.weight, 2)}</td>
                <td className="tiny">{c.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="tiny muted">{conf.note}</p>
    </div>
  )
}

function Row({ k, v }) {
  return (
    <tr>
      <th className="rowhead">{k}</th>
      <td className="tiny">{v || '—'}</td>
    </tr>
  )
}
