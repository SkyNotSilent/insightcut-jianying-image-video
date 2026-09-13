import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './site-e2e',
  outputDir: 'test-results-site',
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:2104', ...devices['Desktop Chrome'], screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: {
    command: 'node ../../scripts/serve_site.mjs',
    url: 'http://127.0.0.1:2104',
    reuseExistingServer: false,
    timeout: 10000,
  },
})
