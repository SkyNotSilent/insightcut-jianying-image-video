import assert from 'node:assert/strict'
import test from 'node:test'

import {
  LEGACY_WORKSPACE_KEY,
  SELECTED_PROJECT_KEY,
  clearSelectedProject,
  projectIdFromPath,
  readSelectedProject,
  selectProject,
} from '../src/lib/projectSelection.js'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('selected project persists independently and mirrors the legacy workspace cache', () => {
  const storage = memoryStorage()
  const selected = selectProject({ task_id: 'task-a', name: '项目 A' }, { storage, dispatch: false })

  assert.deepEqual(selected, { taskId: 'task-a', name: '项目 A', path: '/workspace/task-a' })
  assert.deepEqual(JSON.parse(storage.getItem(SELECTED_PROJECT_KEY)), selected)
  assert.deepEqual(JSON.parse(storage.getItem(LEGACY_WORKSPACE_KEY)), selected)
  assert.deepEqual(readSelectedProject(storage), selected)
})

test('legacy workspace is a fallback and deleting another task does not clear the selection', () => {
  const storage = memoryStorage({
    [LEGACY_WORKSPACE_KEY]: JSON.stringify({ taskId: 'legacy-task', name: '历史项目' }),
  })

  assert.equal(readSelectedProject(storage).taskId, 'legacy-task')
  assert.equal(clearSelectedProject('another-task', { storage, dispatch: false }), false)
  assert.equal(readSelectedProject(storage).taskId, 'legacy-task')
  assert.equal(clearSelectedProject('legacy-task', { storage, dispatch: false }), true)
  assert.equal(readSelectedProject(storage), null)
})

test('workspace, export, and project asset detail routes resolve the same project context', () => {
  assert.equal(projectIdFromPath('/workspace/task-a/settings'), 'task-a')
  assert.equal(projectIdFromPath('/export/task-b'), 'task-b')
  assert.equal(projectIdFromPath('/assets/task-c'), 'task-c')
  assert.equal(projectIdFromPath('/assets'), '')
  assert.equal(projectIdFromPath('/manuscript/draft-a'), '')
})
