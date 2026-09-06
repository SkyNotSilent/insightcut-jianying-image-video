// Each open page keeps its own recovery copy, so another page cannot replace its
// unsaved text on refresh. The local copy also supports reopening the browser.
export function readPendingEdits(taskId, local = localStorage, session = sessionStorage) {
  const key = `insightcut:workspace-pending:${taskId}`
  const raw = session.getItem(key) ?? local.getItem(key) ?? '{}'
  const parsed = JSON.parse(raw)
  const value = parsed && typeof parsed === 'object' ? parsed : {}
  session.setItem(key, JSON.stringify(value))
  return value
}

export function writePendingEdits(taskId, value, local = localStorage, session = sessionStorage) {
  const key = `insightcut:workspace-pending:${taskId}`
  const previous = session.getItem(key)
  const serialized = JSON.stringify(value)
  session.setItem(key, serialized)
  if (Object.keys(value).length) local.setItem(key, serialized)
  else if (local.getItem(key) === previous) local.removeItem(key)
}
