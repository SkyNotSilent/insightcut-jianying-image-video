import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const workspacePage = readFileSync(new URL('../src/pages/WorkspacePage.jsx', import.meta.url), 'utf8')
const actionBar = readFileSync(new URL('../src/components/WorkspaceActionBar.jsx', import.meta.url), 'utf8')
const inspector = readFileSync(new URL('../src/components/WorkspaceInspector.jsx', import.meta.url), 'utf8')

test('the bottom confirmation action saves the selected voice instead of only reopening settings', () => {
  assert.match(workspacePage, /<WorkspaceActionBar[\s\S]*?onConfirmVoice=\{confirmVoice\}/)
  assert.doesNotMatch(workspacePage, /onConfirmVoice=\{\(\) => \{ setWorkspaceSettingsOpen\(true\)/)
  assert.match(actionBar, /onClick=\{onConfirmVoice\}/)
  assert.match(actionBar, /正在验证音色/)
})

test('voice confirmation validates the TTS path and is reflected immediately', () => {
  assert.match(workspacePage, /const next = \{[\s\S]*?\.\.\.current,[\s\S]*?\.\.\.patch,[\s\S]*?plan_version:/)
  assert.match(workspacePage, /workspaceRef\.current = next/)
  assert.match(workspacePage, /getVoices\(\{ include_disabled: true \}\)\.catch/)
  assert.match(workspacePage, /await previewVoice\([\s\S]*?voice_type: selectedVoice[\s\S]*?silent: true/)
  assert.match(workspacePage, /该音色当前无法生成配音/)
})

test('current segment exposes a speed override and previews the effective settings', () => {
  assert.match(inspector, /aria-label="当前分镜语速"/)
  assert.match(inspector, /audio_tts_options: event\.target\.value \? \{ \.\.\.segmentVoiceSettings\.override, speed_level: event\.target\.value \} : \{\}/)
  assert.match(inspector, /试听当前设置/)
  assert.match(workspacePage, /previewVoice\(\{ voice_type: voice\.id, tts_options: optionsOverride \}\)/)
  assert.match(workspacePage, /pendingEdits=\{Boolean\(savingCount \|\| Object\.keys\(pendingRef\.current\)\.length\)\}/)
})
