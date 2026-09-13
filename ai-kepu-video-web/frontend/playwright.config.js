import { defineConfig, devices } from '@playwright/test'

const installedBrowserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL

const port = process.env.INSIGHTCUT_BROWSER_PORT || '2103'
export default defineConfig({
  outputDir: 'test-results-browser',
  testDir: './e2e',
  testIgnore: '**/real-fullstack.spec.js',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        ...(installedBrowserChannel ? { channel: installedBrowserChannel } : {}),
      },
    },
  ],
  webServer: {
    command: `npm run build && npm run preview -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
