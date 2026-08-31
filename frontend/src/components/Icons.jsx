/**
 * Inline SVG icon set (Lucide-style strokes) — consistent sizing + viewBox,
 * replaces unicode/emoji glyphs used as UI icons.
 */

const base = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  width: '1em',
  height: '1em',
  viewBox: '0 0 24 24',
}

function Svg({ children, ...rest }) {
  return (
    <svg {...base} {...rest} aria-hidden="true">
      {children}
    </svg>
  )
}

export const UploadCloud = (props) => (
  <Svg {...props}>
    <path d="M4 14.9A7 7 0 1 1 15.7 8h1.8a4.5 4.5 0 0 1 2.7 8.1" />
    <path d="M12 12v9" />
    <path d="m16 16-4-4-4 4" />
  </Svg>
)

export const Satellite = (props) => (
  <Svg {...props}>
    <path d="M13 7 9 3 5 7l4 4" />
    <path d="m17 11 4 4-4 4-4-4" />
    <path d="m8 12 4 4 6-6-4-4Z" />
    <path d="m16 8 3-3" />
    <path d="M9 21a6 6 0 0 0-6-6" />
  </Svg>
)

export const Play = (props) => (
  <Svg {...props}>
    <polygon points="6 3 20 12 6 21 6 3" fill="currentColor" stroke="none" />
  </Svg>
)

export const Download = (props) => (
  <Svg {...props}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </Svg>
)

export const FileJson = (props) => (
  <Svg {...props}>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <path d="M10 12a1 1 0 0 0-1 1v1a1 1 0 0 1-1 1 1 1 0 0 1 1 1v1a1 1 0 0 0 1 1" />
    <path d="M14 18a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1 1 1 0 0 1-1-1v-1a1 1 0 0 0-1-1" />
  </Svg>
)

export const ChevronDown = (props) => (
  <Svg {...props}>
    <polyline points="6 9 12 15 18 9" />
  </Svg>
)

export const Spinner = (props) => (
  <Svg {...props} className={`spin ${props.className || ''}`}>
    <path d="M21 12a9 9 0 1 1-6.2-8.56" />
  </Svg>
)

export const XCircle = (props) => (
  <Svg {...props}>
    <circle cx="12" cy="12" r="10" />
    <line x1="14.5" y1="9.5" x2="9.5" y2="14.5" />
    <line x1="9.5" y1="9.5" x2="14.5" y2="14.5" />
  </Svg>
)

export const Shield = (props) => (
  <Svg {...props}>
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
  </Svg>
)

export const Layers = (props) => (
  <Svg {...props}>
    <polygon points="12 2 2 7 12 12 22 7 12 2" />
    <polyline points="2 17 12 22 22 17" />
    <polyline points="2 12 12 17 22 12" />
  </Svg>
)
