import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const frontend = new URL('../', import.meta.url)
const [tokens, appCss, creationCss, manuscriptPage, workspaceCss, deliveryCss] = await Promise.all([
  readFile(new URL('src/styles/tokens.css', frontend), 'utf8'),
  readFile(new URL('src/styles/app.css', frontend), 'utf8'),
  readFile(new URL('src/pages/creation-flow.css', frontend), 'utf8'),
  readFile(new URL('src/pages/ManuscriptPage.jsx', frontend), 'utf8'),
  readFile(new URL('src/pages/workspace-page.css', frontend), 'utf8'),
  readFile(new URL('src/pages/delivery-pages.css', frontend), 'utf8'),
])

test('the shared palette uses restrained brand blue and warm orange tokens', () => {
  assert.match(tokens, /--color-accent:\s*#315fea/i)
  assert.match(tokens, /--color-accent-strong:\s*#2348b8/i)
  assert.match(tokens, /--color-orange:\s*#d46f44/i)
  assert.match(tokens, /--color-orange-strong:\s*#a44627/i)
  assert.match(tokens, /--color-orange-soft:\s*#fff3ec/i)
  assert.match(tokens, /--color-success:\s*#b65432/i)
  assert.match(tokens, /--color-success-soft:\s*#fff3ec/i)
  assert.doesNotMatch(appCss, /#1261ff|#0848ca|#347cff|#0755ed|#55a2ff/i)
  assert.match(appCss, /\.rail-brand-glow[^}]*var\(--color-orange\)/)
  assert.match(appCss, /\.task-activity-card-progress i[^}]*var\(--color-accent\)[^}]*var\(--color-orange\)/)
})

test('manuscript focus moves from the editor shell to one soft canvas perimeter', () => {
  assert.doesNotMatch(creationCss, /\.paper-editor-shell:focus-within/)
  assert.match(creationCss, /\.writing-canvas:focus-within[^}]*border-color:[^}]*var\(--color-accent\)/)
  assert.match(creationCss, /\.writing-canvas:focus-within[^}]*box-shadow:[^}]*var\(--color-orange\)/)
  assert.match(creationCss, /\.paper-editor-shell textarea:focus-visible[^}]*outline:\s*0/)
})

test('manuscript uses one writing canvas and one unified settings panel', () => {
  assert.match(creationCss, /\.manuscript-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(320px, 372px\)/)
  assert.match(creationCss, /\.manuscript-settings\s*\{[^}]*min-height:\s*calc\(100vh - 116px\)[^}]*max-height:\s*calc\(100vh - 116px\)[^}]*position:\s*sticky[^}]*overflow-y:\s*auto/)
  assert.match(creationCss, /@media \(max-width: 960px\)[\s\S]*?\.manuscript-settings\s*\{[^}]*max-height:\s*none[^}]*position:\s*static/)
  assert.equal((manuscriptPage.match(/className="work-panel/g) || []).length, 1)
  assert.match(manuscriptPage, /PanelHeading eyebrow="创作配置" title="文稿设置"/)
  assert.match(manuscriptPage, /<legend>任务来源<\/legend>/)
  assert.doesNotMatch(manuscriptPage, /title="文稿准备"|title="画面设置"/)
})

test('selected cards use a complete perimeter instead of a single colored edge', () => {
  assert.doesNotMatch(workspaceCss, /box-shadow:\s*inset\s+3px\s+0\s+0\s+var\(--color-(?:accent|danger)\)/)
  assert.doesNotMatch(deliveryCss, /\.export-option\.is-preferred[^}]*border-top:/)
})
