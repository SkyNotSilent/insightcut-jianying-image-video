export const SELECTED_PROJECT_KEY = 'insightcut:selected-project'
export const LEGACY_WORKSPACE_KEY = 'insightcut:last-workspace'
export const PROJECT_SELECTION_EVENT = 'insightcut:selected-project'

function safeParse(value) {
  try {
    const parsed = JSON.parse(value || 'null')
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function normalizeProject(project) {
  const taskId = String(project?.taskId || project?.task_id || '').trim()
  if (!taskId) return null
  return {
    taskId,
    name: String(project?.name || project?.theme || '').trim(),
    path: `/workspace/${taskId}`,
  }
}

export function readSelectedProject(storage = globalThis.localStorage) {
  if (!storage) return null
  return normalizeProject(safeParse(storage.getItem(SELECTED_PROJECT_KEY)))
    || normalizeProject(safeParse(storage.getItem(LEGACY_WORKSPACE_KEY)))
}

export function projectIdFromPath(pathname = '') {
  const match = String(pathname).match(/^\/(?:workspace|export|assets)\/([^/?#]+)/)
  if (!match) return ''
  try {
    return decodeURIComponent(match[1])
  } catch {
    return match[1]
  }
}

export function selectProject(project, { storage = globalThis.localStorage, dispatch = true } = {}) {
  const next = normalizeProject(project)
  if (!next || !storage) return null
  storage.setItem(SELECTED_PROJECT_KEY, JSON.stringify(next))
  storage.setItem(LEGACY_WORKSPACE_KEY, JSON.stringify(next))
  if (dispatch && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(PROJECT_SELECTION_EVENT, { detail: next }))
    window.dispatchEvent(new Event('insightcut:workspace'))
  }
  return next
}

export function clearSelectedProject(taskId, { storage = globalThis.localStorage, dispatch = true } = {}) {
  if (!storage) return false
  const selected = readSelectedProject(storage)
  if (taskId && selected?.taskId !== String(taskId)) return false
  storage.removeItem(SELECTED_PROJECT_KEY)
  const legacy = normalizeProject(safeParse(storage.getItem(LEGACY_WORKSPACE_KEY)))
  if (!taskId || legacy?.taskId === String(taskId)) storage.removeItem(LEGACY_WORKSPACE_KEY)
  if (dispatch && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(PROJECT_SELECTION_EVENT, { detail: null }))
    window.dispatchEvent(new Event('insightcut:workspace'))
  }
  return true
}
