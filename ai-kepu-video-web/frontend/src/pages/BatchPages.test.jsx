import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { SafeApiError } from '../lib/apiErrorSafety'
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
    archiveBatch: vi.fn(),
    confirmBatchProduction: vi.fn(),
    getTaskWorkspace: vi.fn(),
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
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('detects duplicates after Unicode and whitespace normalization', () => {
    const parsed = parseTopics('ＡＩ   助手\nAI 助手\n\n极光')
    expect(parsed.topics).toHaveLength(3)
    expect(parsed.duplicates).toEqual(['AI 助手'])
  })

  it('recovers polling automatically after a network failure', async () => {
    taskApi.getBatch.mockRejectedValueOnce(new SafeApiError()).mockResolvedValue(completedBatch)
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)
    expect(await screen.findByText(/进度暂时无法连接/)).toBeInTheDocument()
    expect(await screen.findByText('2 / 2', {}, { timeout: 3500 })).toBeInTheDocument()
    expect(screen.queryByText(/进度暂时无法连接/)).not.toBeInTheDocument()
  })

  it('replaces an aborted StrictMode request without showing a connection error', async () => {
    taskApi.getBatch.mockImplementationOnce((_id, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new SafeApiError({ cancelled: true })))
    })).mockResolvedValue(completedBatch)
    render(<StrictMode><MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter></StrictMode>)
    expect(await screen.findByText('2 / 2')).toBeInTheDocument()
    expect(screen.queryByText(/进度暂时无法连接/)).not.toBeInTheDocument()
    expect(taskApi.getBatch).toHaveBeenCalledTimes(2)
  })

  it('uses the same version-stable folding boundaries as the backend', () => {
    expect(parseTopics('ASCII\tSPACE\nascii space').duplicates).toEqual(['ascii space'])
    expect(parseTopics('Straße\nSTRASSE').duplicates).toEqual([])
    expect(parseTopics('ς\nΣ').duplicates).toEqual([])
    expect(parseTopics('A B\nA\u0085B\nA\ufeffB').duplicates).toEqual([])
  })

  it('restores a completed detail view from its persisted API state', async () => {
    taskApi.getBatch.mockResolvedValue(completedBatch)
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: '批次进度' })).toBeInTheDocument()
    expect(screen.getByText('2 / 2')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /打开工作台/ })).toHaveLength(2)
    expect(taskApi.getBatch).toHaveBeenCalledWith('batch-1', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('selects and confirms all eligible projects without opening any preview', async () => {
    taskApi.getBatch.mockResolvedValue({...completedBatch, items: completedBatch.items.map(i=>({...i,can_produce:true,snapshot_key:i.task_id,plan_version:2}))})
    taskApi.confirmBatchProduction.mockResolvedValue({items:[{task_id:'task-1',outcome:'accepted'},{task_id:'task-2',outcome:'conflict',error:'预案已变化'}]})
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button',{name:'一键全选可生产项'}))
    fireEvent.click(screen.getByRole('button',{name:'确认并生成 2 个视频'}))
    await waitFor(()=>expect(taskApi.confirmBatchProduction).toHaveBeenCalledWith('batch-1',[
      {task_id:'task-1',snapshot_key:'task-1',plan_version:2},{task_id:'task-2',snapshot_key:'task-2',plan_version:2}
    ]))
    expect(taskApi.getTaskWorkspace).not.toHaveBeenCalled()
    expect(await screen.findByText(/预案已变化/)).toBeInTheDocument()
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
    taskApi.cancelBatch.mockImplementation(async () => {
      taskApi.getBatch.mockResolvedValue(cancelled)
      return cancelled
    })
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)

    await user.click(await screen.findByRole('button', { name: '停止预案生成' }))
    await waitFor(() => expect(taskApi.cancelBatch).toHaveBeenCalledWith('batch-1'))
    expect((await screen.findAllByText('已取消')).length).toBeGreaterThanOrEqual(1)
  })

  it('keeps the list usable when the API returns an empty persisted history', async () => {
    taskApi.listBatches.mockResolvedValue({ items: [] })
    render(<MemoryRouter><BatchListPage /></MemoryRouter>)
    expect(await screen.findByText('还没有批量预案')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '创建第一个批次' })).toBeEnabled()
  })

  it('filters archived batches and restores a batch without deleting it', async () => {
    taskApi.listBatches.mockResolvedValue({items:[]})
    const list = render(<MemoryRouter><BatchListPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button',{name:'已归档'}))
    expect(await screen.findByText('还没有已归档批次')).toBeInTheDocument()
    expect(taskApi.listBatches).toHaveBeenLastCalledWith({limit:100,archived:true},expect.anything())
    list.unmount()
    taskApi.getBatch.mockResolvedValue({...completedBatch,archived_at:'2026-09-07 02:00:00'})
    taskApi.archiveBatch.mockResolvedValue(completedBatch)
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button',{name:'恢复批次'}))
    await waitFor(()=>expect(taskApi.archiveBatch).toHaveBeenCalledWith('batch-1',false))
  })

  it('restarts polling after retrying a terminal batch with failures', async () => {
    const failedBatch = {
      ...completedBatch,
      status: 'completed_with_errors',
      counts: { queued: 0, running: 0, awaiting_confirmation: 1, failed: 1, cancelled: 0 },
      items: [
        completedBatch.items[0],
        { ...completedBatch.items[1], status: 'failed', error: '处理未完成，请稍后重试或检查配置。' },
      ],
    }
    const runningBatch = {
      ...failedBatch,
      status: 'running',
      counts: { queued: 0, running: 1, awaiting_confirmation: 1, failed: 0, cancelled: 0 },
      items: [completedBatch.items[0], { ...completedBatch.items[1], status: 'running' }],
    }
    taskApi.getBatch
      .mockResolvedValueOnce(failedBatch)
      .mockResolvedValueOnce(runningBatch)
      .mockResolvedValue(completedBatch)
    taskApi.retryFailedBatchItems.mockResolvedValue({ batch_id: 'batch-1', retried_count: 1 })
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchDetailPage />} /></Routes></MemoryRouter>)

    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }))

    await waitFor(() => expect(taskApi.retryFailedBatchItems).toHaveBeenCalledWith('batch-1'))
    await waitFor(() => expect(taskApi.getBatch).toHaveBeenCalledTimes(3), { timeout: 3500 })
    expect(await screen.findByText('2 / 2')).toBeInTheDocument()
  })
})
