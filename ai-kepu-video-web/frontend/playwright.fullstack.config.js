import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'

const frontendDir = path.dirname(fileURLToPath(import.meta.url))
const serverDir = path.resolve(frontendDir, '../../ai-kepu-video-server')
const runtimeDir = path.resolve(serverDir, '.e2e-runtime')
const bundledPython = path.join(serverDir, 'venv', 'bin', 'python')
const python = process.env.INSIGHTCUT_PYTHON || (fs.existsSync(bundledPython) ? bundledPython : 'python')

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/real-fullstack.spec.js',
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:2001',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: `"${python}" -m uvicorn api_server:app --host 127.0.0.1 --port 2002`,
      cwd: serverDir,
      env: {
        ...process.env,
        INSIGHTCUT_FAKE_PROVIDERS: '1',
        INSIGHTCUT_DATA_ROOT: runtimeDir,
        INSIGHTCUT_DB_PATH: path.join(runtimeDir, 'data', 'e2e.db'),
        TASK_SWEEPER_INTERVAL_SECONDS: '1',
      },
      url: 'http://127.0.0.1:2002/health',
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 2001',
      cwd: frontendDir,
      env: { ...process.env, VITE_API_BASE_URL: 'http://127.0.0.1:2002' },
      url: 'http://127.0.0.1:2001',
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
})
