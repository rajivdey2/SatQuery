import { useEffect, useRef, useState } from 'react'
import { fileUrl } from '../api.js'
import { EVIDENCE_LABEL } from '../format.js'

/**
 * Evidence gallery.
 *
 * Boxes are drawn as an SVG overlay in the same 0..100 normalised space the audit
 * trace reports, so what a judge sees on screen is literally the number in the
 * trace rather than a separately-computed rendering.
 */
export default function EvidenceGallery({ trace, boxes }) {
  const items = trace?.outputs?.evidence || []
  const [active, setActive] = useState(0)
  const [showBoxes, setShowBoxes] = useState(true)

  useEffect(() => {
    setActive(0)
  }, [trace?.run_id])

  if (!items.length) return <p className="muted">No rendered evidence for this run.</p>

  const current = items[Math.min(active, items.length - 1)]
  const overlayBoxes = current.role === 'input_preview' || current.role === 'grounding_overlay'
    ? boxes || []
    : []

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Visual evidence</h3>
        <div className="chip-row">
          {items.map((item, i) => (
            <button
              key={`${item.path}-${i}`}
              className={`chip ${i === active ? 'chip-on' : ''}`}
              onClick={() => setActive(i)}
              title={item.caption}
            >
              {EVIDENCE_LABEL[item.role] || item.role}
            </button>
          ))}
          {overlayBoxes.length > 0 && (
            <button className={`chip ${showBoxes ? 'chip-on' : ''}`} onClick={() => setShowBoxes(!showBoxes)}>
              boxes {showBoxes ? 'on' : 'off'}
            </button>
          )}
        </div>
      </div>
      <BoxedImage
        src={fileUrl(current.path)}
        alt={current.caption || current.role}
        boxes={showBoxes ? overlayBoxes : []}
      />
      <p className="caption">{current.caption || current.path}</p>
    </section>
  )
}

function BoxedImage({ src, alt, boxes }) {
  const [natural, setNatural] = useState({ w: 100, h: 100 })
  const imgRef = useRef(null)
  return (
    <figure className="evidence-figure">
      <div className="img-wrap">
        <img
          ref={imgRef}
          src={src}
          alt={alt}
          onLoad={(e) => setNatural({ w: e.target.naturalWidth, h: e.target.naturalHeight })}
        />
        {boxes.length > 0 && (
          <svg className="overlay" viewBox="0 0 100 100" preserveAspectRatio="none">
            {boxes.map((b, i) => {
              const [x1, y1, x2, y2] = b.bbox || []
              if ([x1, y1, x2, y2].some((v) => v === undefined)) return null
              return (
                <g key={i}>
                  <rect
                    x={x1}
                    y={y1}
                    width={Math.max(0, x2 - x1)}
                    height={Math.max(0, y2 - y1)}
                    className="box"
                    vectorEffect="non-scaling-stroke"
                  />
                  <text x={x1 + 0.6} y={Math.max(2.4, y1 - 0.8)} className="box-label">
                    {b.label || b.class_name || 'region'}
                  </text>
                </g>
              )
            })}
          </svg>
        )}
      </div>
      {boxes.length > 0 && (
        <figcaption className="muted">
          {boxes.length} box{boxes.length > 1 ? 'es' : ''} in 0–100 normalised image coordinates
          {natural.w > 1 ? ` · source ${natural.w}×${natural.h}px` : ''}
        </figcaption>
      )}
    </figure>
  )
}
