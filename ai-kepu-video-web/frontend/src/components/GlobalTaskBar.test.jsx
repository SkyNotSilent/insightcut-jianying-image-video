import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'

import * as taskApi from '../api/task'
import { GlobalTaskBar } from './GlobalTaskBar'

vi.mock('../api/task', async () => {
  const actual = await vi.importActual('../api/task')
  return {
    ...actual,
    getTaskActivity: vi.fn(),
    cancelTask: vi.fn(),
  }
})

const activity = {
  running: [
    { task_id: 'running-1', name: '正在生成的项目', status: 'processing', step: 5, progress: 64 },
  ],
  attention: [
    { task_id: 'confirm-1', name: '等待确认的项目', status: 'awaiting_confirmation', step: 3, progress: 50, activity_label: '等待确认' },
    { task_id: 'finalize-1', name: '素材已经齐全', status: 'awaiting_finalization', step: 6, progress: 100, export_ready: false, activity_label: '素材已齐 · 待构建草稿' },
  ],
  recent: [
    { task_id: 'ready-1', name: '等待导出的项目', status: 'completed', step: 6, progress: 100, export_ready: true, exported_at: null },
    { task_id: 'done-1', name: '已经导出的项目', status: 'completed', step: 6, progress: 100, export_ready: false, exported_at: '2026-08-29 09:00:00' },
  ],
  counts: { running: 1, attention: 1 },
}

describe('GlobalTaskBar', () => {
  beforeEach(() => {
    window.localStorage.clear()
    taskApi.getTaskActivity.mockResolvedValue(activity)
  })

  it('stays expanded, keeps export-ready work, and hides only projects already exported', async () => {
    render(<MemoryRouter><GlobalTaskBar /></MemoryRouter>)

    expect(await screen.findByText('正在生成的项目')).toBeInTheDocument()
    expect(screen.getByText('等待确认的项目')).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByText(/03 · 等待确认/)).toBeInTheDocument()
    expect(screen.getByText('素材已经齐全')).toBeInTheDocument()
    expect(screen.getByText(/素材已齐 · 待构建草稿/)).toBeInTheDocument()
    expect(screen.getByText('等待导出的项目')).toBeInTheDocument()
    expect(screen.getByText(/可导出/)).toBeInTheDocument()
    expect(screen.getAllByText('100%')).toHaveLength(2)
    expect(screen.queryByText('已经导出的项目')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '收起任务活动' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '浏览更多项目' })).toBeInTheDocument()
  })
})
