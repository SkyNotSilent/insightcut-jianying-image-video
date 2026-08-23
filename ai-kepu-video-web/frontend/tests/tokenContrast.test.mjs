import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const tokens = await readFile(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')

function token(name) {
  const match = tokens.match(new RegExp(`--color-${name}:\\s*(#[0-9a-f]{6})`, 'i'))
  assert.ok(match, `missing hex token --color-${name}`)
  return match[1]
}

function luminance(hex) {
  const values = hex.slice(1).match(/../g).map(value => Number.parseInt(value, 16) / 255)
  const linear = values.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
}

function contrast(foreground, background) {
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

test('primary editorial text and action colors meet WCAG AA contrast', () => {
  const pairs = [
    ['ink', 'canvas'],
    ['ink-soft', 'surface'],
    ['ink-muted', 'surface'],
    ['accent', 'surface'],
    ['orange-strong', 'surface'],
    ['success', 'surface'],
    ['warning', 'surface'],
    ['danger', 'surface'],
    ['on-media', 'accent'],
  ]
  pairs.forEach(([foreground, background]) => {
    const ratio = contrast(token(foreground), token(background))
    assert.ok(ratio >= 4.5, `${foreground} on ${background} contrast ${ratio.toFixed(2)} is below 4.5`)
  })
})
