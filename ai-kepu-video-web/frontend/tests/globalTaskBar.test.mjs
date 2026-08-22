import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(new URL('../src/components/GlobalTaskBar.jsx', import.meta.url), 'utf8')

test('task cards stay in export context when opened from the export center', () => {
  assert.match(component, /useLocation\(\)/)
  assert.match(component, /location\.pathname\.startsWith\('\/export\/'\)/)
  assert.match(component, /`\/export\/\$\{item\.task_id\}`/)
  assert.match(component, /item\.target_route \|\| `\/workspace\/\$\{item\.task_id\}`/)
})
