import { expect, test } from '@playwright/test'

const apiBaseUrl = 'http://127.0.0.1:2002/ai/native/video/kepu'

async function readBatch(request, batchId) {
  const response = await request.get(`${apiBaseUrl}/batches/${batchId}`)
  expect(response.ok()).toBeTruthy()
  return response.json()
}

test('creates a real persisted batch and stops both projects at confirmation', async ({ page }) => {
  const marker = Date.now()
  const first = `全栈测试极光 ${marker}`
  const second = `全栈测试海水 ${marker}`

  await page.goto('/manuscript?mode=batch')
  await expect(page.getByRole('button', { name: '批量预案' })).toHaveAttribute('aria-current', 'page')
  await page.getByRole('textbox', { name: '批量主题，每行一个' }).fill(`${first}\n${second}`)
  await page.getByRole('button', { name: '2', exact: true }).click()
  await page.getByRole('button', { name: '创建 2 个预案' }).click()

  await expect(page).toHaveURL(/\/batches\/batch_/)
  await expect(page.getByRole('heading', { name: '批次进度' })).toBeVisible()
  await expect(page.getByText(first)).toBeVisible()
  await expect(page.getByText(second)).toBeVisible()
  await expect(page.getByText('2 / 2')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('待确认', { exact: true })).toHaveCount(3)

  const batchId = page.url().split('/batches/')[1]
  const batch = await readBatch(page.request, batchId)
  expect(batch.status).toBe('completed')
  expect(batch.items).toHaveLength(2)
  expect(batch.items.every(item => item.status === 'awaiting_confirmation')).toBeTruthy()
  expect(batch.items.every(item => item.task_id)).toBeTruthy()
})

test('processes the maximum 50-topic batch with a durable global concurrency cap', async ({ page }) => {
  test.setTimeout(120_000)
  const marker = Date.now()
  const topics = Array.from({ length: 50 }, (_, index) => `五十项压力主题 ${marker} ${String(index + 1).padStart(2, '0')}`)

  await page.goto('/manuscript?mode=batch')
  await page.getByRole('textbox', { name: '批量主题，每行一个' }).fill(topics.join('\n'))
  await expect(page.getByText('50 / 50 个主题')).toBeVisible()
  await page.getByRole('button', { name: '3', exact: true }).click()
  await page.getByRole('button', { name: '创建 50 个预案' }).click()

  await expect(page).toHaveURL(/\/batches\/batch_/)
  await expect(page.getByText('50 / 50')).toBeVisible({ timeout: 90_000 })
  const batchId = page.url().split('/batches/')[1]
  const batch = await readBatch(page.request, batchId)
  expect(batch.status).toBe('completed')
  expect(batch.counts).toMatchObject({ awaiting_confirmation: 50, failed: 0, cancelled: 0 })
  expect(new Set(batch.items.map(item => item.task_id)).size).toBe(50)
  expect(batch.items.every(item => item.attempt === 1)).toBeTruthy()
})

test('cancels a large queued batch without launching its remaining projects', async ({ page }) => {
  test.setTimeout(60_000)
  const marker = Date.now()
  const createResponse = await page.request.post(`${apiBaseUrl}/batches`, {
    data: {
      items: Array.from({ length: 50 }, (_, index) => ({ theme: `取消竞态主题 ${marker} ${index + 1}` })),
      concurrency: 1,
      style: '知识科普|电影质感',
      ratio: '16:9',
      length: 80,
    },
  })
  expect(createResponse.status()).toBe(201)
  const created = await createResponse.json()
  const cancelResponse = await page.request.post(`${apiBaseUrl}/batches/${created.batch_id}/cancel`)
  expect(cancelResponse.ok()).toBeTruthy()

  await expect.poll(async () => (await readBatch(page.request, created.batch_id)).status, {
    timeout: 30_000,
  }).toBe('cancelled')
  const batch = await readBatch(page.request, created.batch_id)
  expect(batch.cancel_requested).toBeTruthy()
  expect(batch.counts.queued).toBe(0)
  expect(batch.counts.running).toBe(0)
  expect(batch.counts.cancelled).toBeGreaterThan(0)

  await page.goto(`/batches/${created.batch_id}`)
  await expect(page.getByText('已取消', { exact: true }).first()).toBeVisible()
})
