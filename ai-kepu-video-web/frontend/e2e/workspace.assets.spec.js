import { test, expect } from './fixtures'

const image = color => `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="${color}"/><circle cx="800" cy="450" r="220" fill="#fff" fill-opacity=".28"/></svg>`)}`

const segments = [
  {
    id: 'seg-0', segment_index: 0, text: '第一段分镜文案', image_prompt: '暖色纸张上的岛屿与灯塔', prompt_status: 'completed',
    image_status: 'completed', audio_status: 'completed', image_url: image('#7a9caf'), image_path: '/tmp/current-0.png',
    audio_url: 'data:audio/wav;base64,UklGRg==', duration: 3.2,
  },
  {
    id: 'seg-1', segment_index: 1, text: '第二段分镜文案', image_prompt: '海面上的贸易航线与帆船', prompt_status: 'completed',
    image_status: 'completed', audio_status: 'completed', image_url: image('#a47f62'), image_path: '/tmp/current-1.png',
    audio_url: 'data:audio/wav;base64,UklGRg==', duration: 4.1,
  },
]

const workspace = {
  task_id: 'ui-assets',
  name: '小岛经济学',
  stage: 'ready',
  planning_step: null,
  input_mode: 'script',
  script_text: '第一段分镜文案。第二段分镜文案。',
  visual_style: '电影质感',
  ratio: '16:9',
  text_style: '知识科普',
  voice_confirmed: true,
  voice_type: 'mimo:mimo_default',
  tts_options: { speed_level: 'normal' },
  plan_version: 3,
  snapshot_key: 'fixture-snapshot-key-1234567890',
  segments_count: 2,
  segments,
  estimated_duration: 7.3,
  duration_is_estimate: false,
  generation_estimate: { min_seconds: 20, max_seconds: 45 },
  progress: { prompts_ready: 2, prompts_total: 2, prompts_failed: 0, images_ready: 2, audio_ready: 2 },
  capabilities: { enter_export: true, full_video: true, retry_failed_assets: false, retry_selected_asset: true, finalize: false },
  recovery: { allowed: false, mode: null, targets: [] },
  health: { missing_prompts: 0, missing_images: 0, missing_audio: 0 },
  storage_warnings: [],
}

let activeExportJob = null
let createdExportRequests = 0

test.beforeEach(async ({ page }) => {
  activeExportJob = null
  createdExportRequests = 0
  await page.addInitScript(() => localStorage.setItem('insightcut:workspace-tour:v1', 'seen'))
  await page.route('http://localhost:2002/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path.endsWith('/tasks/ui-assets/workspace')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(workspace) })
      return
    }
    if (path.endsWith('/tasks/ui-assets/export-state')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: activeExportJob ? [activeExportJob] : [], preview: { valid: false }, outputs: {} }) })
      return
    }
    if (path.endsWith('/tasks/ui-assets/exports/render-active/cancel')) {
      activeExportJob = { ...activeExportJob, cancel_requested: true, message: '正在取消' }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(activeExportJob) })
      return
    }
    if (path.endsWith('/tasks/ui-assets/exports/render-active')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(activeExportJob) })
      return
    }
    if (path.endsWith('/tasks/ui-assets/exports') && route.request().method() === 'POST') {
      createdExportRequests += 1
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      return
    }
    if (path.endsWith('/voices')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 'mimo:mimo_default', name: '默认讲解', provider: 'mimo', selectable: true, enabled: true }]) })
      return
    }
    if (path.endsWith('/config')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ generation: { prompt_concurrency: 4, image_concurrency: 8, tts_concurrency: 1, retry_count: 2, retry_interval_seconds: 5 } }) })
      return
    }
    if (path.endsWith('/tasks/ui-assets/assets')) {
      const segmentIndex = Number(url.searchParams.get('segment_index') || 0)
      const assets = [
        { asset_id: `current-${segmentIndex}`, segment_index: segmentIndex, source: 'generated', path: `/tmp/current-${segmentIndex}.png`, file_url: image('#7a9caf'), has_file: true, label: '当前生成版本', created_at: '2026-08-19 14:20:00' },
        { asset_id: `old-${segmentIndex}`, segment_index: segmentIndex, source: 'regenerated', path: `/tmp/old-${segmentIndex}.png`, file_url: image('#675b52'), has_file: true, label: '重生成版本', created_at: '2026-08-19 14:10:00' },
      ]
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(assets) })
      return
    }
    if (path.endsWith('/tasks/ui-assets/segments/0/select-image')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ image_path: '/tmp/old-0.png', image_url: image('#675b52'), image_prompt: segments[0].image_prompt }) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
})

test('keeps the center preview focused and exposes image history plus lightbox from the inspector', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/workspace/ui-assets')

  await expect(page.getByRole('region', { name: '分镜导航' })).toBeVisible()
  await expect(page.locator('.workspace-current-assets')).toHaveCount(0)
  await expect(page.getByText('当前分镜素材')).toHaveCount(0)
  const inspector = page.getByRole('complementary', { name: '生产设置' })
  await expect(inspector.getByRole('button', { name: '上传替换' })).toBeVisible()
  await expect(inspector.getByRole('button', { name: '历史版本' })).toBeVisible()

  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    workspace: document.querySelector('.production-workspace').scrollHeight - document.querySelector('.production-workspace').clientHeight,
  }))
  expect(overflow.document).toBe(0)
  expect(overflow.workspace).toBe(0)

  await inspector.getByRole('button', { name: '历史版本' }).click()
  await expect(inspector.getByRole('region', { name: '素材版本' })).toBeVisible()
  await expect(inspector.getByText('2 个版本')).toBeVisible()
  await page.getByRole('button', { name: /^重生成版本/ }).click()
  await expect(page.getByText('已切换为这个图片版本')).toBeVisible()

  await inspector.getByRole('button', { name: '查看画面' }).click()
  await expect(page.getByRole('dialog', { name: '分镜 1' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: '分镜 1' }).getByText('暖色纸张上的岛屿与灯塔')).toBeVisible()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('dialog', { name: '分镜 2' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('has no horizontal overflow at supported workspace breakpoints', async ({ page }) => {
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/workspace/ui-assets')
    await expect(page.getByRole('main')).toBeVisible()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow, `viewport ${width}px`).toBe(0)
  }
})

test('restores the selected segment and pane scroll after refresh and API overlay use', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/workspace/ui-assets')

  await page.getByRole('button', { name: '选择分镜 2，素材完成' }).click()
  await page.getByRole('tab', { name: '全片设置' }).click()

  const saved = await page.evaluate(() => {
    const preview = document.querySelector('.workspace-preview')
    const settings = document.querySelector('.workspace-settings-panel')
    const previewTop = Math.min(260, Math.max(0, preview.scrollHeight - preview.clientHeight))
    // Open API config from its real visible position. Playwright, like a user,
    // scrolls an off-screen trigger into view before clicking it.
    const settingsTop = Math.max(0, settings.scrollHeight - settings.clientHeight)
    preview.scrollTop = previewTop
    settings.scrollTop = settingsTop
    return { previewTop, settingsTop }
  })
  expect(saved.previewTop).toBeGreaterThan(0)
  expect(saved.settingsTop).toBeGreaterThan(0)
  await page.waitForTimeout(180)

  const apiOpener = page.getByRole('button', { name: '打开 API 配置' })
  await apiOpener.click()
  await expect(page.getByRole('dialog', { name: 'API 配置' })).toBeVisible()
  await expect(page.getByRole('button', { name: '关闭配置' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'API 配置' })).toHaveCount(0)
  await expect(apiOpener).toBeFocused()

  const afterOverlay = await page.evaluate(() => ({
    previewTop: document.querySelector('.workspace-preview').scrollTop,
    settingsTop: document.querySelector('.workspace-settings-panel').scrollTop,
  }))
  expect(afterOverlay.previewTop).toBe(saved.previewTop)
  expect(afterOverlay.settingsTop).toBe(saved.settingsTop)

  await page.reload()
  await expect(page.getByRole('button', { name: '选择分镜 2，素材完成' })).toHaveAttribute('aria-current', 'true')
  await expect(page.getByRole('tab', { name: '全片设置' })).toHaveAttribute('aria-selected', 'true')
  await expect.poll(() => page.evaluate(() => ({
    previewTop: document.querySelector('.workspace-preview')?.scrollTop || 0,
    settingsTop: document.querySelector('.workspace-settings-panel')?.scrollTop || 0,
  }))).toEqual(saved)
})

test('cancels the retained full-video job without creating a second render', async ({ page }) => {
  activeExportJob = {
    job_id: 'render-active',
    task_id: 'ui-assets',
    target: 'mp4',
    status: 'processing',
    message: '正在生成',
    cancel_requested: false,
  }
  await page.goto('/workspace/ui-assets')

  const cancel = page.getByRole('button', { name: '取消生成' })
  await expect(cancel).toBeVisible()
  await cancel.click()
  await expect(page.getByRole('button', { name: '正在取消…' })).toBeDisabled()
  expect(activeExportJob.cancel_requested).toBe(true)
  expect(createdExportRequests).toBe(0)
})
