import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(new URL('../src/components/GlobalTaskBar.jsx', import.meta.url), 'utf8')

test('task cards stay in export context when opened from the export center', () => {
  assert.match(component, /useLocation\(\)/)
  assert.match(component, /location\.pathname\.startsWith\('\/export\/'\)/)
  assert.match(component, /`\/export\/\$\{task\.task_id\}`/)
  assert.match(component, /task\.target_route \|\| `\/workspace\/\$\{task\.task_id\}`/)
})

test('task selection becomes the shared workspace and export context', () => {
  assert.match(component, /projectIdFromPath\(location\.pathname\)/)
  assert.match(component, /selectProject\(task\)/)
  assert.match(component, /task\.task_id === selectedProject\?\.taskId/)
  assert.match(component, /当前项目/)
  assert.match(component, /task-current-context/)
})
