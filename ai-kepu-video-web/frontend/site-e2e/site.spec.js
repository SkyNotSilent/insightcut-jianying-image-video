import { test, expect } from '@playwright/test'

for (const width of [390, 768, 1440]) {
  test(`homepage at ${width}px: complete layout and real images`, async ({ page }, testInfo) => {
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`) })
    await page.setViewportSize({ width, height: 950 })
    await page.goto('/')
    await expect(page.getByRole('heading', { level: 1 })).toContainText('让想法')
    await page.screenshot({ path: testInfo.outputPath(`hero-${width}.png`), animations: 'disabled' })
    // Scroll the full page so all lazy screenshot assets are actually requested.
    await page.locator('footer').scrollIntoViewIfNeeded()
    await expect.poll(() => page.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0))).toBe(true)
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
    await page.screenshot({ path: testInfo.outputPath(`home-${width}.png`), fullPage: true, animations: 'disabled' })
    expect(errors).toEqual([])
  })
}

test('videos really play, seek and pause each other', async ({ page }) => {
  await page.goto('/')
  const first = page.locator('video').nth(0)
  await first.scrollIntoViewIfNeeded()
  await first.evaluate(async video => { video.muted = true; await video.play() })
  await expect.poll(() => first.evaluate(video => video.currentTime)).toBeGreaterThan(0.15)
  await first.evaluate(video => { video.currentTime = 12 })
  await expect.poll(() => first.evaluate(video => video.currentTime)).toBeGreaterThan(12)
  const second = page.locator('video').nth(1)
  await second.evaluate(async video => { video.muted = true; await video.play() })
  await expect.poll(() => second.evaluate(video => video.currentTime)).toBeGreaterThan(0.15)
  await expect.poll(() => first.evaluate(video => video.paused)).toBe(true)
})

test('keyboard access, copy and published document routes', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: '跳到正文' })).toBeFocused()
  await page.getByRole('button', { name: '复制命令' }).click()
  await expect(page.getByRole('status')).toContainText('已复制')
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('git clone https://github.com/SkyNotSilent/')
  await page.getByRole('link', { name: '安装与快速开始' }).click()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('开始使用 InsightCut')
  for (const route of ['/engineering-workflow.html', '/batch-video-workflow.html', '/contributing.html', '/license.html', '/showcase/']) {
    const response = await page.goto(route)
    expect(response.status()).toBe(200)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  }
})

test('video Range and HEAD requests remain available', async ({ request }) => {
  const url = '/showcase/videos/book-16x9.mp4'
  const head = await request.head(url)
  expect(head.status()).toBe(200)
  expect(Number(head.headers()['content-length'])).toBeGreaterThan(1000)
  const range = await request.get(url, { headers: { Range: 'bytes=0-1023' } })
  expect(range.status()).toBe(206)
  expect((await range.body()).length).toBe(1024)
})
