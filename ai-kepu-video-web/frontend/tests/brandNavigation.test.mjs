import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(new URL('../src/components/BrandNavigation.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../src/styles/app.css', import.meta.url), 'utf8')
test('brand navigation reuses the canonical centered AIPM reticle structure', () => {
  assert.doesNotMatch(component, /\bClapperboard\b/)
  assert.match(component, /className="rail-brand-mark"/)
  assert.match(component, /className="rail-brand-glow"/)
  assert.match(component, /className="rail-brand-reticles"/)
  assert.match(component, /className="rail-brand-dot"/)
})

test('brand icon restores stepped pauses and reduced-motion behavior', () => {
  assert.match(css, /\.rail-brand-glow\s*\{[^}]*conic-gradient[^}]*animation:\s*rail-brand-glow-spin 6\.4s linear infinite/s)
  assert.match(css, /@keyframes rail-brand-reticle-snap\s*\{\s*0%, 23%\s*\{[^}]*rotate\(0deg\)[^}]*\}\s*28%, 48%\s*\{[^}]*rotate\(90deg\)/s)
  assert.match(css, /\.rail-brand-dot\s*\{[^}]*animation:\s*rail-brand-dot-pulse 2\.4s linear infinite/s)
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)\s*\{[^}]*\.rail-brand-glow,[\s\S]*\.rail-brand-dot,[\s\S]*\.brand-glow/s)
})

test('toast states use a complete low-saturation border instead of a one-sided accent stripe', () => {
  assert.doesNotMatch(css, /\.toast-(?:success|warning|error|info)\s*\{[^}]*border-left/s)
  assert.match(css, /\.toast\s*\{[^}]*border:\s*1px solid color-mix/s)
})
