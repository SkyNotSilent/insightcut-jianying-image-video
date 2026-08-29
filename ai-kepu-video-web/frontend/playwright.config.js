import { defineConfig, devices } from '@playwright/test'

const installedBrowserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL

export default defineConfig({
  testDir: './e2e',
  testIgnore: '**/real-fullstack.spec.js',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:2001',
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
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 2001',
    url: 'http://127.0.0.1:2001',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
