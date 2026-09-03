import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Satellite, Shield, Layers } from './components/Icons.jsx'
import './landing.css'

const FEATURES = [
  {
    icon: 'vqa',
    title: 'Visual Question Answering',
    desc: 'Ask natural-language questions about satellite imagery — land-cover, object detection, scene classification — and get grounded, auditable answers.',
    tasks: ['single_vqa', 'single_caption'],
  },
  {
    icon: 'change',
    title: 'Change Detection',
    desc: 'Upload bi-temporal pairs to detect, describe, and localise land-cover change across dates with change-VQA and spatial change maps.',
    tasks: ['change_vqa', 'change_description'],
  },
  {
    icon: 'fusion',
    title: 'Optical–SAR Fusion',
    desc: 'Fuse co-registered optical and SAR imagery for joint analysis — built-up extraction, water-body mapping, and multi-modal land-cover classification.',
    tasks: ['sar_optical_fusion'],
  },
  {
    icon: 'grounding',
    title: 'Visual Grounding',
    desc: 'Highlight and localise specific regions of interest with bounding-box evidence overlaid directly on the image.',
    tasks: ['single_grounding'],
  },
]

const PIPELINE = [
  { step: '01', title: 'Validate', desc: 'Format, modality, georeferencing, co-registration, temporal metadata — all checked before any model runs.' },
  { step: '02', title: 'Classify', desc: 'Query + input configuration selects one of six tasks from the predefined registry.' },
  { step: '03', title: 'Route', desc: 'One or more specialist models with only their permitted parameters — nothing free-form.' },
  { step: '04', title: 'Measure', desc: 'Spectral indices, radar backscatter, thresholds, connected regions, change vectors.' },
  { step: '05', title: 'Answer', desc: 'Text grounded in measured values. Confidence from real signals — or reported as unavailable.' },
]

const STATS = [
  { value: '6', label: 'Task Types', sub: 'Covering all input regimes' },
  { value: '3', label: 'Specialist Models', sub: 'VLM · Change · Fusion' },
  { value: '100%', label: 'Audit Trace', sub: 'Every decision logged' },
  { value: '7B', label: 'Parameters', sub: 'Qwen2-VL backbone' },
]

function Stars() {
  const canvasRef = useRef(null)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    let animId
    let stars = []
    const resize = () => {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
      stars = Array.from({ length: 200 }, () => ({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        r: Math.random() * 1.2 + 0.3,
        a: Math.random(),
        da: (Math.random() - 0.5) * 0.01,
      }))
    }
    resize()
    window.addEventListener('resize', resize)
    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      for (const s of stars) {
        s.a += s.da
        if (s.a > 1 || s.a < 0.1) s.da *= -1
        ctx.beginPath()
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(255,255,255,${s.a})`
        ctx.fill()
      }
      animId = requestAnimationFrame(draw)
    }
    draw()
    return () => { cancelAnimationFrame(animId); window.removeEventListener('resize', resize) }
  }, [])
  return <canvas ref={canvasRef} className="stars-canvas" />
}

function OrbitRing() {
  return (
    <div className="orbit-container">
      <div className="orbit-center">
        <div className="sat-icon-glow">
          <Satellite style={{ width: 48, height: 48 }} />
        </div>
      </div>
      <div className="orbit-ring ring-1"><div className="orbit-dot dot-1" /></div>
      <div className="orbit-ring ring-2"><div className="orbit-dot dot-2" /></div>
      <div className="orbit-ring ring-3"><div className="orbit-dot dot-3" /></div>
    </div>
  )
}

export default function LandingPage() {
  const navigate = useNavigate()
  const [scrollY, setScrollY] = useState(0)
  const [visible, setVisible] = useState(new Set())

  useEffect(() => {
    const onScroll = () => setScrollY(window.scrollY)
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            setVisible((prev) => new Set([...prev, e.target.dataset.reveal]))
          }
        })
      },
      { threshold: 0.15 }
    )
    document.querySelectorAll('[data-reveal]').forEach((el) => observer.observe(el))
    return () => observer.disconnect()
  }, [])

  const goToApp = () => navigate('/app')

  return (
    <div className="landing">
      <Stars />

      {/* Nav */}
      <nav className={`lp-nav ${scrollY > 60 ? 'scrolled' : ''}`}>
        <div className="lp-nav-inner">
          <div className="lp-brand">
            <span className="lp-brand-icon"><Satellite style={{ width: 22, height: 22 }} /></span>
            <span className="lp-brand-text">SatQuery<span className="lp-brand-ai">AI</span></span>
          </div>
          <div className="lp-nav-links">
            <a href="#features">Capabilities</a>
            <a href="#pipeline">Pipeline</a>
            <a href="#about">About</a>
            <button className="lp-nav-cta" onClick={goToApp}>Launch App</button>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <section className="lp-hero">
        <div className="lp-hero-content">
          <div className="lp-hero-badge">
            <Shield style={{ width: 14, height: 14 }} />
            SIH 26167 · ISRO / SAC
          </div>
          <h1 className="lp-hero-title">
            <span className="lp-hero-line-1">Agentic Vision-Language</span>
            <span className="lp-hero-line-2">for <em>Remote Sensing</em></span>
          </h1>
          <p className="lp-hero-sub">
            SatQuery AI routes natural-language queries to specialised remote-sensing
            models — VQA, change detection, optical–SAR fusion — with full audit trails
            and honest confidence scoring.
          </p>
          <div className="lp-hero-actions">
            <button className="lp-btn-primary" onClick={goToApp}>
              <span>Open Application</span>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>
            </button>
            <a href="#features" className="lp-btn-ghost">Explore Capabilities</a>
          </div>
          <div className="lp-hero-tags">
            <span>Qwen2-VL</span>
            <span>BigEarthNet</span>
            <span>GeoTIFF</span>
            <span>CD-VQA</span>
          </div>
        </div>
        <div className="lp-hero-visual">
          <OrbitRing />
        </div>
      </section>

      {/* Stats */}
      <section className="lp-stats">
        {STATS.map((s, i) => (
          <div key={i} className={`lp-stat ${visible.has(`stat-${i}`) ? 'revealed' : ''}`} data-reveal={`stat-${i}`}>
            <div className="lp-stat-value">{s.value}</div>
            <div className="lp-stat-label">{s.label}</div>
            <div className="lp-stat-sub">{s.sub}</div>
          </div>
        ))}
      </section>

      {/* Features */}
      <section id="features" className="lp-features">
        <div className={`lp-section-header ${visible.has('feat-header') ? 'revealed' : ''}`} data-reveal="feat-header">
          <span className="lp-section-tag">Capabilities</span>
          <h2>Four Specialist Models,<br/>One Unified Interface</h2>
          <p>Each input regime — single image, bi-temporal pair, or cross-modal pair —
            is handled by a dedicated specialist behind a common interface.</p>
        </div>
        <div className="lp-features-grid">
          {FEATURES.map((f, i) => (
            <div
              key={i}
              className={`lp-feature-card ${visible.has(`feat-${i}`) ? 'revealed' : ''}`}
              data-reveal={`feat-${i}`}
              style={{ transitionDelay: `${i * 100}ms` }}
            >
              <div className={`lp-feature-icon lp-fi-${f.icon}`}>
                {f.icon === 'vqa' && <Layers style={{ width: 28, height: 28 }} />}
                {f.icon === 'change' && <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>}
                {f.icon === 'fusion' && <Satellite style={{ width: 28, height: 28 }} />}
                {f.icon === 'grounding' && <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>}
              </div>
              <h3>{f.title}</h3>
              <p>{f.desc}</p>
              <div className="lp-feature-tasks">
                {f.tasks.map((t) => <span key={t} className="lp-task-chip">{t}</span>)}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Pipeline */}
      <section id="pipeline" className="lp-pipeline">
        <div className={`lp-section-header ${visible.has('pipe-header') ? 'revealed' : ''}`} data-reveal="pipe-header">
          <span className="lp-section-tag">Pipeline</span>
          <h2>How Every Query Is Answered</h2>
          <p>Five independently testable stages, each logged in the audit trace.</p>
        </div>
        <div className="lp-pipeline-track">
          {PIPELINE.map((p, i) => (
            <div
              key={i}
              className={`lp-pipe-step ${visible.has(`pipe-${i}`) ? 'revealed' : ''}`}
              data-reveal={`pipe-${i}`}
              style={{ transitionDelay: `${i * 120}ms` }}
            >
              <div className="lp-pipe-num">{p.step}</div>
              <div className="lp-pipe-line" />
              <h4>{p.title}</h4>
              <p>{p.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* About / ISRO */}
      <section id="about" className="lp-about">
        <div className="lp-about-inner" data-reveal="about">
          <div className="lp-about-text">
            <span className="lp-section-tag">About</span>
            <h2>Built for SIH 26167<br/>ISRO / SAC Challenge</h2>
            <p>
              SatQuery AI is an agentic controller that routes natural-language queries
              to specialised remote-sensing models across three input regimes: single
              optical/SAR images, bi-temporal change pairs, and cross-modal optical–SAR
              pairs. Every decision — input validation, task classification, model routing,
              parameter selection, and confidence estimation — is captured in a structured
              JSON audit trace.
            </p>
            <p>
              The system is evaluated on VRSBench, RSVQA, and CDVQA, and fine-tuned on
              BigEarthNet-MM. Designed to generalise to unseen ISRO Cartosat-2S and
              RISAT imagery without silent hallucination — the system says <em>"I don't know"</em>
              {' '}when confidence is insufficient.
            </p>
            <div className="lp-about-logos">
              <div className="lp-logo-card">
                <span className="lp-logo-text">ISRO</span>
                <span className="lp-logo-sub">Indian Space Research Organisation</span>
              </div>
              <div className="lp-logo-card">
                <span className="lp-logo-text">SAC</span>
                <span className="lp-logo-sub">Space Applications Centre</span>
              </div>
              <div className="lp-logo-card">
                <span className="lp-logo-text">SIH</span>
                <span className="lp-logo-sub">Smart India Hackathon 2024</span>
              </div>
            </div>
          </div>
          <div className="lp-about-visual">
            <div className="lp-about-globe">
              <div className="lp-globe-ring r1" />
              <div className="lp-globe-ring r2" />
              <div className="lp-globe-ring r3" />
              <div className="lp-globe-core" />
            </div>
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="lp-cta">
        <div className="lp-cta-inner" data-reveal="cta">
          <h2>Ready to Analyse<br/>Satellite Imagery?</h2>
          <p>Upload GeoTIFF, PNG, or JPEG — ask a question — get a grounded, auditable answer.</p>
          <button className="lp-btn-primary lp-btn-lg" onClick={goToApp}>
            <Satellite style={{ width: 20, height: 20 }} />
            <span>Launch SatQuery AI</span>
          </button>
        </div>
      </section>

      {/* Footer */}
      <footer className="lp-footer">
        <div className="lp-footer-inner">
          <div className="lp-footer-brand">
            <span className="lp-brand-icon" style={{ width: 32, height: 32 }}><Satellite style={{ width: 18, height: 18 }} /></span>
            <span>SatQuery AI</span>
          </div>
          <div className="lp-footer-links">
            <span>SIH Problem Statement 26167</span>
            <span>·</span>
            <span>ISRO / SAC</span>
            <span>·</span>
            <span>Agentic RS-VLM</span>
          </div>
        </div>
      </footer>
    </div>
  )
}
