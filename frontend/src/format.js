/**
 * Small shared presentation helpers.
 *
 * Formatting lives in one place because the same numbers appear in the answer
 * card, the measurement tables and the audit panel, and a percentage that reads
 * differently in two panels makes a judge distrust both.
 */

export const TASK_LABEL = {
  single_vqa: 'Single-image VQA',
  single_caption: 'Scene description',
  single_grounding: 'Text-guided grounding',
  change_vqa: 'Change VQA',
  change_description: 'Change description',
  sar_optical_fusion: 'Optical + SAR fusion',
  rejected: 'Input rejected',
}

export const TASK_HINT = {
  single_vqa: 'mandatory single-image baseline',
  single_caption: 'second single-image task',
  single_grounding: 'second single-image task — returns visual evidence',
  change_vqa: 'mandatory multi-image change task',
  change_description: 'mandatory multi-image change task',
  sar_optical_fusion: 'mandatory cross-modal task',
}

export const CLASS_COLOR = {
  water: '#2171b5',
  vegetation: '#238b45',
  built_up: '#de2d26',
  bare_soil: '#c49c5a',
  other: '#8c8c8c',
}

export const EVIDENCE_LABEL = {
  input_preview: 'Input',
  class_map: 'Land-cover map',
  grounding_overlay: 'Grounding overlay',
  change_map: 'Change map',
  fusion_map: 'Joint optical+SAR map',
  side_by_side: 'Sensor pair',
}

export function pct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(digits)}%`
}

export function num(value, digits = 3) {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return String(value)
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(digits)
}

export function ha(value) {
  if (value === null || value === undefined) return '—'
  return `${Number(value).toFixed(1)} ha`
}

export function signedPp(value) {
  if (value === null || value === undefined) return '—'
  const v = Number(value) * 100
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)} pp`
}

export function confidenceTone(conf) {
  if (!conf) return 'unknown'
  if (conf.source === 'mock') return 'mock'
  if (conf.value === null || conf.value === undefined) return 'unknown'
  if (conf.value >= 0.7) return 'high'
  if (conf.value >= 0.45) return 'medium'
  return 'low'
}
