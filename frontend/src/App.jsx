import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { analyze, demoFile, demoInfo, health, pollJob } from './api.js'
import AnswerCard from './components/AnswerCard.jsx'
import AuditPanel from './components/AuditPanel.jsx'
import EvidenceGallery from './components/EvidenceGallery.jsx'
import MeasurementPanel from './components/MeasurementPanel.jsx'
import RegistryPanel from './components/RegistryPanel.jsx'
import {
  ChevronDown,
  Download,
  FileJson,
  Play,
  Satellite,
  Shield,
  Spinner,
  UploadCloud,
} from './components/Icons.jsx'

const ACCEPTED = ['.png', '.jpg', '.jpeg', '.tif', '.tiff']

export default function App() {
  const navigate = useNavigate()
  const inputRef = useRef(null)
  const [files, setFiles] = useState([])
  const [query, setQuery] = useState('')
  const [running, setRunning] = useState(false)
  const [stage, setStage] = useState('')
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const [backend, setBackend] = useState({ status: 'checking' })
  const [demo, setDemo] = useState(null)
  const [paramText, setParamText] = useState('')
  const [showParams, setShowParams] = useState(false)

  useEffect(() => {
    const check = () =>
      health()
        .then((h) => setBackend({ status: 'ok', ...h }))
        .catch(() => setBackend({ status: 'down' }))
    check()
    const timer = setInterval(check, 6000)
    demoInfo().then(setDemo).catch(() => setDemo(null))
    return () => clearInterval(timer)
  }, [])

  const pick = (list) => {
    const chosen = Array.from(list).filter((f) =>
      ACCEPTED.some((ext) => f.name.toLowerCase().endsWith(ext)),
    )
    const skipped = Array.from(list).length - chosen.length
    setFiles(chosen.slice(0, 2))
    setJob(null)
    setError(
      skipped > 0
        ? `${skipped} file(s) ignored: only GeoTIFF/TIFF, or PNG/JPEG for benchmark data, are accepted.`
        : Array.from(list).length > 2
          ? 'Only the first two images are used — the defined input scope is one image or one pair.'
          : null,
    )
  }

  const run = async (overrideFiles, overrideQuery) => {
    const useFiles = overrideFiles || files
    const useQuery = (overrideQuery ?? query).trim()
    if (!useFiles.length) return setError('Upload one image, or a pair.')
    if (!useQuery) return setError('Enter a natural-language query.')

    let params
    if (paramText.trim()) {
      try {
        params = JSON.parse(paramText)
      } catch {
        return setError('Task parameters must be valid JSON, e.g. {"target_class": "water"}')
      }
    }

    setRunning(true)
    setError(null)
    setJob(null)
    setStage('validating input…')
    try {
      const { job_id } = await analyze(useFiles, useQuery, params)
      setStage('routing → executing specialist…')
      const finished = await pollJob(job_id, {
        onUpdate: (j) => setStage(j.status === 'running' ? 'executing specialist…' : j.status),
      })
      setJob(finished)
      if (finished.status === 'error') setError(finished.error)
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
      setStage('')
    }
  }

  const runDemo = async (entry) => {
    try {
      setRunning(true)
      setStage('fetching demo inputs…')
      setError(null)
      const fetched = await Promise.all(entry.inputs.map(demoFile))
      setFiles(fetched)
      setQuery(entry.query)
      await run(fetched, entry.query)
    } catch (e) {
      setError(e.message)
      setRunning(false)
      setStage('')
    }
  }

  return (
    <div className="app">
      <header>
        <div className="brand" onClick={() => navigate('/')} style={{ cursor: 'pointer' }}>
          <span className="brand-mark">
            <Satellite />
          </span>
          <div>
            <h1>SatQuery AI</h1>
            <span className="sub">
              Agentic vision-language assistant for multimodal remote sensing · SIH 26167 (ISRO/SAC)
            </span>
          </div>
        </div>
        <div className={`backend ${backend.status}`}>
          {backend.status === 'ok' ? (
            <>
              <b>{backend.backend}</b> v{backend.version} · {backend.device}
              {backend.adapted_head?.available ? ' · adapted head loaded' : ' · adapted head not trained'}
            </>
          ) : (
            `backend ${backend.status}`
          )}
        </div>
      </header>

      <main className="grid">
        <section className="left">
          <div className="control-panel">
            <div
              className={`upload ${files.length ? 'has-files' : ''}`}
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault()
                pick(e.dataTransfer.files)
              }}
            >
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPTED.join(',')}
                multiple
                hidden
                onChange={(e) => pick(e.target.files)}
              />
              <UploadCloud className="upload-icon" />
              <p className="hint">Drop one image, or a pair</p>
              <p className="subhint">
                GeoTIFF/TIFF for geospatial imagery · PNG/JPEG for benchmark data. A pair is either
                two dates of the same sensor, or a co-registered optical + SAR pair.
              </p>
            </div>

            {files.length > 0 && (
              <ul className="filelist">
                {files.map((f, i) => (
                  <li key={i}>
                    <span className="file-icon">
                      <Satellite />
                    </span>
                    <span className="filename">{f.name}</span>
                    <span className="filemeta">{(f.size / 1024).toFixed(0)} KB</span>
                    <button
                      className="x"
                      aria-label={`remove ${f.name}`}
                      onClick={(e) => {
                        e.stopPropagation()
                        setFiles(files.filter((_, j) => j !== i))
                      }}
                    >
                      <XGlyph />
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <div>
              <label className="control-label" htmlFor="query">Natural-language query</label>
              <textarea
                id="query"
                className="query"
                placeholder="Ask about the image(s) — e.g. “Describe the land-cover and major objects visible in this image.”"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                rows={3}
              />
            </div>

            <div className="row">
              <button className="run" disabled={running} onClick={() => run()}>
                {running ? <Spinner /> : <Play />}
                {running ? 'Running…' : 'Run analysis'}
              </button>
              <button className="btn btn-ghost" onClick={() => setShowParams(!showParams)}>
                parameters <ChevronDown style={{ transform: showParams ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }} />
              </button>
            </div>

            {showParams && (
              <div className="params">
                <textarea
                  className="query mono"
                  rows={2}
                  placeholder='{"target_class": "water", "min_region_pixels": 128}'
                  value={paramText}
                  onChange={(e) => setParamText(e.target.value)}
                />
                <p className="tiny muted">
                  Only parameters the registry permits for the routed task are applied; anything else
                  is listed as rejected in the audit trace. Open the registry panel for the full
                  schema.
                </p>
              </div>
            )}

            {running && (
              <p className="stage">
                <Spinner /> {stage}
              </p>
            )}
            {error && <p className="error">{error}</p>}
          </div>

          {demo?.available ? (
            <div className="control-panel">
              <div className="demo">
                <h4>Problem-statement queries</h4>
                <p className="tiny muted">
                  The five representative queries, verbatim, on bundled demo inputs with known ground
                  truth.
                </p>
                <ul className="demo-list">
                  {demo.queries.map((entry, i) => (
                    <li key={i}>
                      <button className="demo-btn" disabled={running} onClick={() => runDemo(entry)}>
                        <span className="demo-q">
                          <span className="demo-num">{i + 1}</span>
                          {entry.query}
                        </span>
                        <span className="tiny muted">
                          → {entry.expects} · {entry.inputs.join(' + ')}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          ) : demo && !demo.available ? (
            <div className="control-panel">
              <p className="tiny muted">
                Demo inputs not generated yet — run <code>python scripts/make_demo_data.py</code>.
              </p>
            </div>
          ) : null}
        </section>

        <section className="right">
          {!job && !running && (
            <div className="empty">
              <h3>
                <Shield style={{ verticalAlign: '-2px', marginRight: '6px', color: 'var(--accent)' }} />
                How this system answers
              </h3>
              <ol>
                <li><b>Validate</b> — format, modality, georeferencing, co-registration, dates. Unverifiable input is rejected with an explanation.</li>
                <li><b>Classify</b> — the query plus the input configuration select one of six tasks; infeasible tasks are ruled out by the registry, not by wording.</li>
                <li><b>Route</b> — one or more entries from the predefined registry, with only their permitted parameters.</li>
                <li><b>Measure</b> — spectral / radar indices, thresholds with a recorded separability, connected regions, change vectors, cross-modal agreement.</li>
                <li><b>Answer</b> — the text may only restate measured values; confidence is composed from measured signals, or reported as unavailable.</li>
              </ol>
              <p className="tiny muted">
                Everything in step 3–5 is written to the audit trace, which is the artefact the
                problem statement grades.
              </p>
            </div>
          )}

          {running && !job && <LoadingSkeleton />}

          {job && <AnswerCard job={job} />}
          {job && job.status === 'done' && (
            <EvidenceGallery trace={job.trace} boxes={job.result?.boxes} />
          )}
          {job && job.status === 'done' && <MeasurementPanel trace={job.trace} />}
          {job && <AuditPanel job={job} />}
          <RegistryPanel />
        </section>
      </main>
    </div>
  )
}

function XGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function LoadingSkeleton() {
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Analysing…</h3>
      </div>
      <div className="skeleton">
        <div className="skeleton-line w-40" />
        <div className="skeleton-line w-100" />
        <div className="skeleton-line w-70" />
        <div className="skeleton-block" />
      </div>
    </section>
  )
}
