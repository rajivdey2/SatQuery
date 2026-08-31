import { confidenceTone, num, TASK_HINT, TASK_LABEL } from '../format.js'
import { reportUrl, traceUrl } from '../api.js'
import { Download, FileJson } from './Icons.jsx'

/** The answer, its confidence, and the download actions the problem statement expects. */
export default function AnswerCard({ job }) {
  const result = job.result || {}
  const trace = job.trace || {}
  const conf = result.confidence || trace.confidence
  const tone = confidenceTone(conf)
  const task = trace.task || result.task

  if (job.status === 'rejected') {
    const rejected = result.rejected || {}
    return (
      <section className="card reject">
        <h3>Input rejected — the controller refused to answer</h3>
        <ul>
          {(rejected.validation_failed || []).map((f, i) => <li key={i}>{f}</li>)}
        </ul>
        <p className="tiny muted">
          Refusing an input it cannot verify is deliberate behaviour: on unfamiliar products a
          stated limitation is worth more than a confident guess. The full check list is in the
          audit trace below.
        </p>
        <div className="card-actions">
          <a className="btn btn-ghost" href={traceUrl(job.id)} target="_blank" rel="noreferrer"><FileJson /> trace.json</a>
          <a className="btn btn-ghost" href={reportUrl(job.id)} target="_blank" rel="noreferrer"><Download /> report (PDF)</a>
        </div>
      </section>
    )
  }

  if (job.status === 'error') {
    return (
      <section className="card reject">
        <h3>Run failed</h3>
        <p className="error">{job.error}</p>
      </section>
    )
  }

  return (
    <section className="card answer">
      <div className="card-head">
        <div>
          <span className="badge task">{TASK_LABEL[task] || task}</span>
          {TASK_HINT[task] && <span className="tiny muted"> {TASK_HINT[task]}</span>}
        </div>
        <span className={`badge conf conf-${tone}`}>
          confidence {conf?.value === null || conf?.value === undefined ? 'n/a' : num(conf.value, 2)}
          <small> {conf?.source}</small>
        </span>
      </div>

      <p className="answer-text">{result.text}</p>

      <div className="card-meta">
        <span className="mono">{result.model_id}</span>
        {result.adapter && <span className="mono"> · adapter {result.adapter}</span>}
        <span className="muted"> · narration: {result.narration_source}</span>
        {(result.tools_used || []).length > 0 && (
          <span className="muted"> · tools: {result.tools_used.join(' → ')}</span>
        )}
      </div>

      {conf?.source === 'not_available' && (
        <p className="tiny warn-text">
          No calibrated signal was available for this task, so no number is reported rather than an
          invented one.
        </p>
      )}

      <div className="card-actions">
        <a className="btn" href={reportUrl(job.id)} target="_blank" rel="noreferrer"><Download /> Download report (PDF)</a>
        <a className="btn btn-ghost" href={traceUrl(job.id)} target="_blank" rel="noreferrer"><FileJson /> Audit trace (JSON)</a>
      </div>
    </section>
  )
}
