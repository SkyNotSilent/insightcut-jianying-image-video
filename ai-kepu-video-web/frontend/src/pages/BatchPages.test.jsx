import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { BatchDetailPage } from './BatchDetailPage'
import { BatchListPage } from './BatchListPage'
import { parseTopics } from './ManuscriptPage'
import * as taskApi from '../api/task'

vi.mock('../api/task', async () => {
  const actual = await vi.importActual('../api/task')
  return {
    ...actual,
    listBatches: vi.fn(),
    getBatch: vi.fn(),
    cancelBatch: vi.fn(),
    retryFailedBatchItems: vi.fn(),
  }
})

const completedBatch = {
  batch_id: 'batch-1', status: 'completed', concurrency: 2, total_count: 2,
  created_at: '2026-08-28 10:00:00', cancel_requested: false,
  config: { style: '知识科普|电影质感', ratio: '16:9', length: 300 },
  counts: { queued: 0, running: 0, awaiting_confirmation: 2, failed: 0, cancelled: 0 },
  items: [
    { item_id: 'item-1', theme: '极光如何形成', status: 'awaiting_confirmation', task_id: 'task-1', attempt: 1 },
    { item_id: 'item-2', theme: '海水为什么是咸的', status: 'awaiting_confirmation', task_id: 'task-2', attempt: 1 },
  ],
}

describe('batch planning pages', () => {
  it('detects duplicates after Unicode and whitespace normalization', () => {
    const parsed = parseTopics('ＡＩ   助手\nAI 助手\n\n极光')
    expect(parsed.topics).toHaveLength(3)
    expect(parsed.duplicates).toEqual(['AI 助手'])
  })

  it('restores a completed detail view from its persisted API state', async () => {
    taskApi.getBatch.mockResolvedValue(completedBatch)
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: '批次进度' })).toBeInTheDocument()
    expect(screen.getByText('2 / 2')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /打开工作台/ })).toHaveLength(2)
    expect(taskApi.getBatch).toHaveBeenCalledWith('batch-1', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('requests cancellation and exposes the persisted cancelled state', async () => {
    const running = {
      ...completedBatch, status: 'running', counts: { queued: 1, running: 1, awaiting_confirmation: 0, failed: 0, cancelled: 0 },
      items: [
        { ...completedBatch.items[0], status: 'running' },
        { ...completedBatch.items[1], status: 'queued', task_id: null },
      ],
    }
    const cancelled = {
      ...running, status: 'cancelled', cancel_requested: true,
      counts: { queued: 0, running: 0, awaiting_confirmation: 0, failed: 0, cancelled: 2 },
      items: running.items.map(item => ({ ...item, status: 'cancelled' })),
    }
    taskApi.getBatch.mockResolvedValue(running)
    taskApi.cancelBatch.mockResolvedValue(cancelled)
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)

    await user.click(await screen.findByRole('button', { name: '取消批次' }))
    await waitFor(() => expect(taskApi.cancelBatch).toHaveBeenCalledWith('batch-1'))
    expect((await screen.findAllByText('已取消')).length).toBeGreaterThanOrEqual(1)
  })

  it('keeps the list usable when the API returns an empty persisted history', async () => {
    taskApi.listBatches.mockResolvedValue({ items: [] })
    render(<MemoryRouter><BatchListPage /></MemoryRouter>)
    expect(await screen.findByText('还没有批量预案')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '创建第一个批次' })).toBeEnabled()
  })
})
