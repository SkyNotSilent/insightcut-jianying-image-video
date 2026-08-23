import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const cssUrl = new URL('../src/pages/workspace-page.css', import.meta.url)
const css = await readFile(cssUrl, 'utf8')
const loaderCss = await readFile(new URL('../src/components/ui/BrandLoader.css', import.meta.url), 'utf8')
const workspaceSource = await readFile(new URL('../src/pages/WorkspacePage.jsx', import.meta.url), 'utf8')

test('workspace loading uses the reusable brand reticle without a generic loading card', () => {
  assert.match(workspaceSource, /<BrandLoader label="正在恢复生产工作台" \/>/)
  assert.doesNotMatch(workspaceSource, /workspace-loading-card|workspace-orbit/)
  assert.match(loaderCss, /\.ui-brand-loader-reticles[^}]*animation:\s*uiBrandLoaderReticles/)
  assert.match(loaderCss, /@media \(prefers-reduced-motion: reduce\)/)
})
const assetCss = await readFile(new URL('../src/components/ui/asset-components.css', import.meta.url), 'utf8')
const uiCss = await readFile(new URL('../src/components/ui/ui.css', import.meta.url), 'utf8')

test('workspace visual layer is composed from the shared semantic token palette', () => {
  assert.match(css, /var\(--color-canvas\)/)
  assert.match(css, /var\(--color-surface\)/)
  assert.match(css, /var\(--color-ink\)/)
  assert.match(css, /var\(--color-accent\)/)
  assert.match(css, /var\(--color-success\)/)
  assert.match(css, /var\(--color-warning\)/)
  assert.match(css, /var\(--color-danger\)/)
  assert.match(css, /font-family:\s*var\(--font-display\)/)
  assert.ok((css.match(/var\(--color-/g) || []).length > 120, 'workspace should consistently consume semantic colors')
  assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b/i)
  assert.doesNotMatch(css, /rgba?\(/i)
  assert.doesNotMatch(assetCss, /#[0-9a-f]{3,8}\b|rgba?\(/i)
  assert.doesNotMatch(uiCss, /#[0-9a-f]{3,8}\b|rgba?\(/i)
})

test('desktop workspace keeps the fixed three-column editor and independent scroll regions', () => {
  assert.match(css, /\.production-workspace\s*\{[^}]*height:\s*calc\(100dvh - 64px\)[^}]*overflow:\s*hidden/s)
  assert.match(css, /\/\* V1\.1[^*]*\*\/[\s\S]*?\.workspace-grid\s*\{[^}]*grid-template-columns:\s*248px minmax\(460px, 1fr\) 336px[^}]*height:\s*calc\(100% - var\(--workspace-guide\)\)[^}]*margin-top:\s*var\(--workspace-guide\)/s)
  assert.match(css, /\.workspace-content\s*\{[^}]*grid-column:\s*1[^}]*overflow-y:\s*auto/s)
  assert.match(css, /\.workspace-preview\s*\{[^}]*grid-column:\s*2[^}]*overflow-y:\s*auto/s)
  assert.match(css, /\.workspace-settings,[\s\S]*?grid-column:\s*3[^}]*overflow:\s*hidden/s)
  assert.match(css, /\.workspace-segment-inspector,[\s\S]*?\.workspace-settings-panel\s*\{[^}]*overflow-y:\s*auto/s)
})

test('desktop stage navigation uses legible two-digit hierarchy', () => {
  assert.match(workspaceSource, /String\(index \+ 1\)\.padStart\(2, '0'\)/)
  assert.match(css, /--workspace-guide:\s*100px/)
  assert.match(css, /\.workspace-stage-navigation li strong\s*\{[^}]*font-size:\s*15px/s)
  assert.match(css, /\.workspace-stage-navigation li small\s*\{[^}]*font-size:\s*11px/s)
  assert.match(css, /\.workspace-stage-navigation li > button > span\s*\{[^}]*width:\s*36px[^}]*height:\s*36px/s)
})

test('mobile panes avoid horizontal overflow and motion has an accessible fallback', () => {
  assert.match(css, /@media \(max-width:\s*780px\)[\s\S]*?\.workspace-grid\s*\{[^}]*display:\s*block[^}]*overflow:\s*hidden/s)
  assert.match(css, /\.production-workspace\[data-mobile-pane='preview'\][\s\S]*?\{\s*display:\s*none/s)
  assert.match(css, /@media \(max-width:\s*780px\)[\s\S]*?\.workspace-content,[\s\S]*?width:\s*100%[^}]*height:\s*100%[^}]*overflow-y:\s*auto/s)
  assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.workspace-save-state\.is-saving svg,[\s\S]*?animation:\s*none !important/s)
  assert.match(css, /\.workspace-inspector-tabs button[^}]*transition:/s)
  assert.match(css, /@keyframes workspaceInspectorIn/)
})
