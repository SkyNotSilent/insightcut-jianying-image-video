import { test } from 'node:test'
import assert from 'node:assert/strict'
import { WorkspaceSaveQueue } from '../src/pages/workspaceSaveQueue.js'

test('a reply for older text cannot erase newer text or another field', async () => {
  let finish, sent = []
  const queue = new WorkspaceSaveQueue({ send: async (index, patch) => {
    sent.push(patch)
    if (sent.length === 1) await new Promise(resolve => { finish = resolve })
    return { plan_version: sent.length }
  } })
  queue.rebase({ plan_version: 0, segments: [{ segment_index: 0, text: 'old', image_prompt: 'prompt' }] })
  queue.edit(0, { text: 'first' })
  const flushing = queue.flush()
  await Promise.resolve()
  queue.edit(0, { text: 'second', image_prompt: 'new prompt' })
  finish()
  await flushing
  assert.equal(sent[1].text, 'second')
  assert.equal(sent[1].image_prompt, 'new prompt')
  assert.deepEqual(queue.patches(), {})
})

test('reload restores and submits pending edits; conflicts retain both values', async () => {
  let saved
  const original = new WorkspaceSaveQueue({})
  original.rebase({ plan_version: 0, segments: [{ segment_index: 0, text: 'old' }] })
  original.edit(0, { text: 'mine' })
  const recovered = new WorkspaceSaveQueue({ pending: original.serialize(), send: async (_, patch) => { saved = patch; return { plan_version: 1 } } })
  recovered.rebase({ plan_version: 0, segments: [{ segment_index: 0, text: 'old' }] })
  await recovered.flush()
  assert.equal(saved.text, 'mine')
  const conflict = new WorkspaceSaveQueue({ pending: original.serialize(), send: () => assert.fail('must not overwrite') })
  conflict.rebase({ plan_version: 1, segments: [{ segment_index: 0, text: 'theirs' }] })
  await assert.rejects(conflict.flush(), /冲突/)
  assert.equal(conflict.conflicts()[0].server, 'theirs')
  assert.equal(conflict.patches()[0].text, 'mine')
})

test('refreshing one page retains its text after another page saved different text', async () => {
  const { readPendingEdits, writePendingEdits } = await import('../src/pages/workspacePendingStorage.js')
  const storage = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) } }
  const local = storage(), pageA = storage(), pageB = storage()
  writePendingEdits('task', { text: 'A unsaved' }, local, pageA)
  readPendingEdits('task', local, pageB)
  writePendingEdits('task', { text: 'B unsaved' }, local, pageB)
  writePendingEdits('task', {}, local, pageB)
  assert.deepEqual(readPendingEdits('task', local, pageA), { text: 'A unsaved' })
  assert.deepEqual(readPendingEdits('task', local, pageB), {})
})
