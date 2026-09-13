import { expect, test } from '@playwright/test'

const apiBaseUrl = `http://127.0.0.1:${process.env.INSIGHTCUT_E2E_API_PORT || '2102'}/ai/native/video/kepu`

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
  await page.getByLabel('同时运行项目数').fill('2')
  await page.getByRole('button', { name: '创建 2 个预案' }).click()

  await expect(page).toHaveURL(/\/batches\/batch_/)
  await expect(page.getByRole('heading', { name: '批次进度' })).toBeVisible()
  await expect(page.getByText(first)).toBeVisible()
  await expect(page.getByText(second)).toBeVisible()
  await expect(page.getByText('2 / 2')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('待确认', { exact: true })).toHaveCount(2)

  const batchId = page.url().split('/batches/')[1]
  await expect.poll(async () => (await readBatch(page.request, batchId)).status).toBe('completed')
  const batch = await readBatch(page.request, batchId)
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
  await expect(page.getByText('50/50 个项目')).toBeVisible()
  await page.getByLabel('同时运行项目数').fill('10')
  await page.getByRole('button', { name: '创建 50 个预案' }).click()

  await expect(page).toHaveURL(/\/batches\/batch_/)
  await expect(page.getByText('50 / 50')).toBeVisible({ timeout: 90_000 })
  const batchId = page.url().split('/batches/')[1]
  await expect.poll(async () => (await readBatch(page.request, batchId)).status, { timeout: 90_000 }).toBe('completed')
  const batch = await readBatch(page.request, batchId)
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

test('three intact manuscript cards confirm without preview and automatically produce MP4', async ({page})=>{
 test.setTimeout(180000)
 const content='第一段，保留换行。\n\n第二段，保持原文。'
 await page.goto('/manuscript?mode=batch')
 await page.getByRole('button',{name:'完整文稿模式'}).click()
 await page.getByLabel('完整正文 1').fill(content)
 await page.getByLabel('完整正文 2').fill(content)
 await page.getByRole('button',{name:'复制文稿 1'}).click()
 await page.getByRole('button',{name:'创建 3 个预案'}).click()
 await expect(page).toHaveURL(/\/batches\/batch_/)
 const bid=page.url().split('/batches/')[1]
 await expect.poll(async()=> (await readBatch(page.request,bid)).counts.awaiting_confirmation,{timeout:30000}).toBe(3)
 const batch=await readBatch(page.request,bid)
 for(const item of batch.items){
  const workspace=await (await page.request.get(`${apiBaseUrl}/tasks/${item.task_id}/workspace`)).json()
  expect(workspace.script_text).toBe(content)
 }
 await expect(page.getByRole('button',{name:'确认并生成视频',exact:true})).toHaveCount(3)
 await page.getByRole('button',{name:'一键全选可生产项'}).click()
 await page.getByRole('button',{name:'确认并生成 3 个视频'}).click()
 // Leave the page: progression belongs to the server.
 await page.goto('/templates')
 await expect.poll(async()=> (await readBatch(page.request,bid)).production_counts.completed,{timeout:120000}).toBe(3)
 await page.goto(`/batches/${bid}`)
 await page.getByRole('button',{name:'预览与编辑'}).first().click()
 await page.getByRole('button',{name:'成片',exact:true}).click()
 await expect(page.getByLabel('完整成片预览')).toBeVisible()
 for(const item of batch.items){
  const state=await (await page.request.get(`${apiBaseUrl}/tasks/${item.task_id}/export-state`)).json()
  expect(state.preview.valid).toBeTruthy()
  expect(state.outputs.draft.available).toBeFalsy()
 }
})

test('single-project confirmation automatically produces a playable video without voice preview',async({page})=>{
 test.setTimeout(90000)
 const response=await page.request.post(`${apiBaseUrl}/tasks`,{data:{name:'单项目自动成片',theme:'一只猫坐在窗边。',input_mode:'script',script_policy:'verbatim',execution_mode:'review_first',voice_type:'mimo:冰糖'}})
 expect(response.ok()).toBeTruthy();const {task_id}=await response.json()
 await page.goto(`/workspace/${task_id}`)
 await page.getByRole('button',{name:'确认并生成视频',exact:true}).click({timeout:30000})
 await expect(page.getByLabel('完整视频预览',{exact:true})).toBeVisible({timeout:60000})
 const state=await (await page.request.get(`${apiBaseUrl}/tasks/${task_id}/export-state`)).json()
 expect(state.preview.valid).toBeTruthy();expect(state.outputs.draft.available).toBeFalsy()
 const video=page.getByLabel('完整视频预览',{exact:true})
 await video.evaluate(el=>{el.muted=true;return el.play()})
 await expect.poll(()=>video.evaluate(el=>el.currentTime)).toBeGreaterThan(0.1)
})

test('batch composer and preview remain usable on a narrow phone',async({page})=>{
 await page.setViewportSize({width:390,height:844})
 await page.goto('/manuscript?mode=batch')
 await page.getByRole('button',{name:'完整文稿模式'}).click()
 await page.getByLabel('完整正文 1').fill('手机文稿第一篇。')
 await page.getByLabel('完整正文 2').fill('手机文稿第二篇。')
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy()
 await page.getByRole('button',{name:'创建 2 个预案'}).click()
 await expect(page).toHaveURL(/\/batches\/batch_/)
 await page.getByRole('button',{name:'预览与编辑'}).first().click({timeout:30000})
 await page.getByRole('button',{name:'编辑预案',exact:true}).click()
 await expect(page.getByLabel('完整文稿正文')).toBeVisible()
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy()
 await page.getByRole('button',{name:'关闭预览',exact:true}).click()
 await expect(page.getByRole('button',{name:'一键全选可生产项'})).toBeVisible()
})
