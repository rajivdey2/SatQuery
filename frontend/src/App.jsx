import { useState, useRef, useEffect } from 'react'
import { analyze, fileUrl, health, pollJob, reportUrl } from './api.js'

const TASK_LABEL = {
  single_vqa: 'Single-Image VQA',
  single_caption: 'Captioning',
  single_grounding: 'Grounding (boxes)',
  change_vqa: 'Change-VQA',
  change_description: 'Change Description',
  sar_optical_fusion: 'Optical + SAR Fusion',
  rejected: 'Input Rejected',
}

function EvidencePreview({ job }) {
  const result = job.result || {}
  const paths = result.evidence || []
  const boxes = result.boxes || []
  if (!paths.length) return <p className="muted">No evidence rendered.</p>
  return (
    <div className="evidence-grid">
      {paths.map((p, i) => (
        <EvidenceBox key={i} src={fileUrl(p)} boxes={boxes} label={`Image ${i + 1}`} />
      ))}
    </div>
  )
}

function EvidenceBox({ src, boxes, label }) {
  const [natural, setNatural] = useState({ w: 1, h: 1 })
  const [view, setView] = useState({ w: 1, h: 1 })
  const ref = useRef(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      if (el) setView({ w: el.clientWidth, h: el.clientHeight })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return (
    <figure className="evidence">
      <div className="img-wrap" ref={ref}>
        <img src={src} alt={label} onLoad={(e) => setNatural({ w: e.target.naturalWidth, h: e.target.naturalHeight })} />
        <svg viewBox={`0 0 ${natural.w} ${natural.h}`} className="overlay" preserveAspectRatio="none">
          {boxes.map((b, i) =>
            b.bbox.length === 4 ? (
              <rect
                key={i}
                x={(b.bbox[0] / 100) * natural.w}
                y={(b.bbox[1] / 100) * natural.h}
                width={((b.bbox[2] - b.bbox[0]) / 100) * natural.w}
                height={((b.bbox[3] - b.bbox[1]) / 100) * natural.h}
                fill="none" stroke="#d50000" strokeWidth={Math.max(2, natural.w / 400)}
              />
            ) : null,
          )}
        </svg>
      </div>
      <figcaption>{label}</figcaption>
    </figure>
  )
}

function ConfBadge({ conf }) {
  if (!conf) return null
  const cls = conf.source === 'not_available' ? 'badge warn' : 'badge ok'
  return (
    <span className={cls}>
      confidence: {conf.value ?? 'n/a'} <small>({conf.source})</small>
    </span>
  )
}

function AuditPanel({ job }) {
  const [open, setOpen] = useState(true)
  const trace = job?.trace
  return (
    <section className="panel">
      <button className="panel-toggle" onClick={() => setOpen(!open)}>
        Audit trace (evaluation artifact) {open ? '▾' : '▸'}
      </button>
      {open && trace && (
        <>
          <div className="trace-grid">
            <TraceCell k="task" v={String(trace.task)} />
            <TraceCell k="classifier" v={String((trace.classification || {}).method)} />
            <TraceCell k="registry" v={String((trace.registry_entries_used || [])[0]?.id)} />
            <TraceCell k="model" v={String((trace.registry_entries_used || [])[0]?.model_id)} />
            <TraceCell k="adapter" v={String((trace.registry_entries_used || [])[0]?.adapter ?? 'none')} />
            <TraceCell k="execution_ms" v={String(trace.execution_time_ms)} />
            <TraceCell k="co_registered" v={String(trace.input_config?.co_registered ?? '—')} />
            <TraceCell k="bi_temporal" v={String(trace.input_config?.bi_temporal ?? '—')} />
            <TraceCell k="modality" v={(trace.input_config?.modality_signature || []).join(', ') || '—'} />
            <TraceCell k="validation" v={String(((trace.validation || {}).checks || []).map((c) => c.status).join(', '))} />
          </div>
          <pre className="json">{JSON.stringify(trace, null, 1).slice(0, 6000)}</pre>
        </>
      )}
    </section>
  )
}

function TraceCell({ k, v }) {
  return (
    <div className="cell">
      <span className="cell-k">{k}</span>
      <span className="cell-v">{v}</span>
    </div>
  )
}

function App() {
  const inputRef = useRef(null)
  const [files, setFiles] = useState([])
  const [query, setQuery] = useState('')
  const [running, setRunning] = useState(false)
  const [job, setJob] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [backend, setBackend] = useState({ status: 'checking' })

  useEffect(() => {
    health().then(h => setBackend({ status: 'ok', ...h })).catch(() => setBackend({ status: 'down' }))
    const t = setInterval(() => health().then(h => setBackend({ status: 'ok', ...h })).catch(() => setBackend({ status: 'down' })), 4000)
    return () => clearInterval(t)
  }, [])

  const pick = (list) => {
    const arr = Array.from(list).filter(f => ['.png', '.jpg', '.jpeg', '.tif', '.tiff'].some(e => f.name.toLowerCase().endsWith(e)))
    setFiles(arr)
    setJob(null)
    setError(null)
  }

  const run = async () => {
    if (!files.length) { setError('Upload at least one image.'); return }
    if (!query.trim()) { setError('Enter a query.'); return }
    setRunning(true); setError(null)
    try {
      const { job_id } = await analyze(files, query)
      setBusy(job_id)
      pollJob(job_id, (j) => { setJob(j); setBusy(null); setRunning(false) }, (e) => { setError(e.message); setRunning(false) })
    } catch (e) { setError(e.message); setRunning(false) }
  }

  const final = job && (job.status === 'done' || job.status === 'rejected')
  const taskLabel = job ? (TASK_LABEL[job.trace?.task] || job.trace?.task || job.status) : ''

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className="logo">⊕</span>
          <h1>SatQuery AI</h1>
          <span className="sub">Agentic remote-sensing VLM · SIH-26167</span>
        </div>
        <span className={`backend ${backend.status}`}>
          backend : {backend.status === 'ok' ? `${backend.backend} v${backend.version}` : backend.status}
        </span>
      </header>

      <main className="grid">
        <section className="left">
          <div className="upload" onClick={() => inputRef.current?.click()}
               onDragOver={(e) => e.preventDefault()}
               onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files) }}>
            <input ref={inputRef} type="file" accept=".png,.jpg,.jpeg,.tif,.tiff" multiple hidden
                   onChange={(e) => pick(e.target.files)} />
            <p className="hint">Drop images here or click to upload</p>
            <p className="subhint">
              optical / SAR GeoTIFF, or PNG/JPEG (benchmarks). Pairs: bi-temporal or optical+SAR.
            </p>
          </div>

          {files.length > 0 && (
            <ul className="filelist">
              {files.map((f, i) => (
                <li key={i}>
                  <span className="file-icon">{f.type.includes('image') ? '🖼' : '📄'}</span>
                  <span className="filename">{f.name}</span>
                  <span className="filemeta">{(f.size / 1024).toFixed(0)} KB</span>
                </li>
              ))}
            </ul>
          )}

          <textarea
            className="query"
            placeholder="Ask about the image(s), e.g. “Describe the land-cover and major objects visible in this image.”"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={3}
          />
          <button className="run" disabled={running} onClick={run}>
            {running ? 'Running…' : 'Run analysis'}
          </button>
          {error && <p className="error">{error}</p>}
        </section>

        <section className="right">
          <div className="statusbar">
            <span>{job ? `job ${job.id}` : 'idle'}</span>
            {running && <span className="spinner">▚ routing…</span>}
          </div>

          {busy && !job && <p className="muted">Job {busy}: controller running (validate → classify → route → execute)…</p>}

          {final && job.status === 'rejected' && (
            <div className="reject">
              <h3>⛔ {job.result?.rejected?.title}</h3>
              {job.result.rejected?.validation_failed?.map((f, i) => <p key={i} className="error">{f}</p>)}
            </div>
          )}

          {final && job.status === 'done' && (
            <>
              <div className="answer card">
                <div className="answer-head">
                  <span className="badge task">{taskLabel}</span>
                  <ConfBadge conf={job.result?.confidence} />
                </div>
                <p className="text">{job.result?.text}</p>
                <div className="answer-actions">
                  <a className="btn" href={reportUrl(job.id)} target="_blank" rel="noreferrer">⬇ Download report (PDF)</a>
                  <span className="muted">{job.result?.model_id} {job.result?.adapter ? `· adapter ${job.result.adapter}` : ''}</span>
                </div>
              </div>
              <EvidencePreview job={job} />
            </>
          )}

          <AuditPanel job={job} />
        </section>
      </main>
    </div>
  )
}

export default App