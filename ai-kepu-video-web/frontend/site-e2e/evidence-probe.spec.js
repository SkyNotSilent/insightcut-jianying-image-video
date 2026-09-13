import { test, expect } from '@playwright/test'

// Temporary release-gate drill; removed after the failed run is inspected.
test('intentional failure verifies screenshot and trace retention', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle('INTENTIONAL CI EVIDENCE PROBE', { timeout: 1000 })
})
