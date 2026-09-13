import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'

const frontendDir = path.dirname(fileURLToPath(import.meta.url))
const serverDir = path.resolve(frontendDir, '../../ai-kepu-video-server')
const runtimeDir = process.env.INSIGHTCUT_E2E_RUNTIME_DIR
  ? path.resolve(process.env.INSIGHTCUT_E2E_RUNTIME_DIR)
  : path.resolve(serverDir, '.e2e-runtime')
const bundledPython = path.join(serverDir, 'venv311', ...(process.platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python']))
const python = process.env.INSIGHTCUT_PYTHON || (fs.existsSync(bundledPython) ? bundledPython : 'python')

const webPort = process.env.INSIGHTCUT_E2E_WEB_PORT || '2101'
const apiPort = process.env.INSIGHTCUT_E2E_API_PORT || '2102'

export default defineConfig({
  outputDir: 'test-results-fullstack',
  testDir: './e2e',
  testMatch: '**/real-fullstack.spec.js',
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: `"${python}" scripts/e2e_server.py`,
      cwd: serverDir,
      env: {
        ...process.env,
        INSIGHTCUT_FAKE_PROVIDERS: '1',
        INSIGHTCUT_E2E_API_PORT: apiPort,
        INSIGHTCUT_E2E_WEB_PORT: webPort,
        INSIGHTCUT_SKIP_DOTENV: '1',
        INSIGHTCUT_DATA_ROOT: runtimeDir,
        INSIGHTCUT_DB_PATH: path.join(runtimeDir, 'data', 'e2e.db'),
        TASK_SWEEPER_INTERVAL_SECONDS: '1',
      },
      url: `http://127.0.0.1:${apiPort}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `npm run build && npm run preview -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      cwd: frontendDir,
      env: { ...process.env, VITE_API_BASE_URL: `http://127.0.0.1:${apiPort}` },
      url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
})
