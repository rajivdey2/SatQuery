const POLL_MS = 600
const POLL_TIMEOUT_MS = 180000

async function json(response) {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      detail = body.detail || detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return response.json()
}

export const health = () => fetch('/health').then(json)
export const registry = () => fetch('/api/registry').then(json)
export const demoInfo = () => fetch('/api/demo').then(json)
export const recentJobs = () => fetch('/api/jobs?limit=12').then(json)

/** Pre-computed showcase runs, rendered instantly through the live-job components. */
export const examplesIndex = () => fetch('/api/examples').then(json)
export const exampleJob = (slug) => fetch(`/api/examples/${encodeURIComponent(slug)}`).then(json)

export async function analyze(files, query, params) {
  const body = new FormData()
  files.forEach((f) => body.append('files', f, f.name))
  body.append('query', query)
  if (params && Object.keys(params).length) {
    body.append('model_params', JSON.stringify(params))
  }
  return json(await fetch('/api/analyze', { method: 'POST', body }))
}

/** Fetch a bundled demo input as a File, so demo runs use the same upload path. */
export async function demoFile(name) {
  const response = await fetch(`/api/demo/files/${encodeURIComponent(name)}`)
  if (!response.ok) throw new Error(`demo file ${name} unavailable`)
  const blob = await response.blob()
  return new File([blob], name, { type: blob.type || 'image/tiff' })
}

export async function pollJob(jobId, { onUpdate } = {}) {
  const started = Date.now()
  for (;;) {
    const job = await json(await fetch(`/api/jobs/${jobId}`))
    if (onUpdate) onUpdate(job)
    if (job.status === 'done' || job.status === 'rejected' || job.status === 'error') return job
    if (Date.now() - started > POLL_TIMEOUT_MS) {
      throw new Error('The controller did not finish within the timeout.')
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS))
  }
}

export const reportUrl = (jobId) => `/api/jobs/${jobId}/report`
export const traceUrl = (jobId) => `/api/jobs/${jobId}/trace`

/** Rendered artefacts are served by basename out of the runtime output directory. */
export function fileUrl(path) {
  if (!path) return ''
  const name = String(path).split('\\').pop().split('/').pop()
  return `/api/files/${encodeURIComponent(name)}`
}
