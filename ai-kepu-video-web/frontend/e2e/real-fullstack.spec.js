import { expect, test } from '@playwright/test'

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
  const response = await page.request.get(`http://127.0.0.1:2002/ai/native/video/kepu/batches/${batchId}`)
  expect(response.ok()).toBeTruthy()
  const batch = await response.json()
  expect(batch.status).toBe('completed')
  expect(batch.items).toHaveLength(2)
  expect(batch.items.every(item => item.status === 'awaiting_confirmation')).toBeTruthy()
  expect(batch.items.every(item => item.task_id)).toBeTruthy()
})
