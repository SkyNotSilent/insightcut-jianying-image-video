import { test, expect } from './fixtures'

test('opens the manuscript workspace without contacting real model providers', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('link', { name: 'InsightCut 文稿首页' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /(主题输入|文稿编辑)/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /(生成预案|插入示例并继续)/ })).toBeVisible()
})
