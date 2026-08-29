const POLL_MS = 700

export async function health() {
  const r = await fetch('/health')
  if (!r.ok) throw new Error('backend unreachable')
  return r.json()
}

export async function analyze(files, query) {
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f, f.name))
  fd.append('query', query)
  const r = await fetch('/api/analyze', { method: 'POST', body: fd })
  if (!r.ok) throw new Error((await r.json()).detail || 'analyze failed')
  return r.json()
}

export async function pollJob(jobId, onDone, onError) {
  const r = await fetch(`/api/jobs/${jobId}`)
  if (!r.ok) throw new Error('job fetch failed')
  const job = await r.json()
  if (job.status === 'done' || job.status === 'rejected' || job.status === 'error') {
    onDone(job)
    return
  }
  setTimeout(() => pollJob(jobId, onDone, onError), POLL_MS)
}

export function reportUrl(jobId) {
  return `/api/jobs/${jobId}/report`
}

export function fileUrl(name) {
  return `/api/files/${encodeURIComponent(name.split('\\').pop().split('/').pop())}`
}