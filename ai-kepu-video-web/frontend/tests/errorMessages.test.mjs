import test from 'node:test'
import assert from 'node:assert/strict'

import { errorToastMessage, getErrorPresentation } from '../src/lib/errorMessages.js'

test('stable error codes use consistent titles and actionable copy', () => {
  const auth = getErrorPresentation({ error_code: 'auth' })
  const rateLimit = getErrorPresentation({ error_code: 'rate_limit', error_meta: { retry_after_seconds: 12.2 } })

  assert.equal(auth.title, 'API 凭证不可用')
  assert.match(auth.action, /API 配置/)
  assert.equal(rateLimit.title, '服务请求过于频繁')
  assert.match(rateLimit.action, /13 秒/)
  assert.equal(errorToastMessage({ error_code: 'auth' }), `${auth.title}；${auth.action}`)
})

test('content policy failures ask for a prompt edit instead of a blind retry', () => {
  const presentation = getErrorPresentation({
    error_code: 'content_policy',
    error_meta: { retryable: false },
  })

  assert.equal(presentation.code, 'content_policy')
  assert.equal(presentation.title, '画面描述未通过内容检查')
  assert.match(presentation.action, /修改当前分镜的生图提示词/)
  assert.equal(presentation.retryable, false)
})

test('HTTP and network failures map without relying on backend detail', () => {
  assert.equal(getErrorPresentation({ response: { status: 409 } }).code, 'conflict')
  assert.equal(getErrorPresentation({ kind: 'network' }).code, 'network')
  assert.equal(getErrorPresentation({ response: { status: 503 } }).code, 'provider_error')
})

test('unknown backend detail is never rendered directly', () => {
  const secretDetail = 'provider debug: Authorization Bearer SECRET-DO-NOT-RENDER'
  const presentation = getErrorPresentation({ response: { status: 400, data: { detail: secretDetail } } })
  const rendered = `${presentation.title} ${presentation.action} ${errorToastMessage({ response: { status: 400, data: { detail: secretDetail } } })}`

  assert.equal(presentation.code, 'unknown')
  assert.equal(rendered.includes(secretDetail), false)
  assert.equal(rendered.includes('SECRET-DO-NOT-RENDER'), false)
})
