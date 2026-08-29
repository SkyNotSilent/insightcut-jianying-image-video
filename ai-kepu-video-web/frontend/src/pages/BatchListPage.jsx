import { ArrowRight, FileStack, Plus, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { listBatches } from '../api/task'
import './batch-pages.css'

const statusCopy = {
  queued: ['排队中', 'queued'], running: ['生成预案中', 'running'],
  completed: ['全部待确认', 'success'], completed_with_errors: ['部分失败', 'warning'],
  cancelled: ['已取消', 'muted'],
}

function formatTime(value) {
  if (!value) return '—'
  const parsed = new Date(String(value).replace(' ', 'T'))
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function BatchListPage() {
  const navigate = useNavigate()
  const [batches, setBatches] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async signal => {
    try {
      const result = await listBatches({ limit: 100 }, { signal })
      setBatches(result?.items || [])
      setError('')
    } catch (requestError) {
      if (requestError?.name !== 'CanceledError') setError('批次列表暂时无法连接，请检查后端服务。')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let timer
    const tick = async () => {
      await load(controller.signal)
      if (!controller.signal.aborted) timer = window.setTimeout(tick, 4000)
    }
    tick()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [load])

  return <main className="batch-page">
    <header className="batch-page-heading">
      <div><p>Batch planning</p><h1>批量预案</h1><span>每个主题只生成文稿、分镜和画面提示词，统一停在人工确认。</span></div>
      <button type="button" className="button button-primary" onClick={() => navigate('/manuscript?mode=batch')}><Plus size={16} />新建批次</button>
    </header>

    {error ? <section className="batch-alert" role="alert"><span>{error}</span><button type="button" onClick={() => { setLoading(true); load() }}><RefreshCw size={15} />重试</button></section> : null}
    {loading ? <section className="batch-empty" aria-live="polite">正在读取批次…</section> : batches.length === 0 ? <section className="batch-empty">
      <FileStack size={34} aria-hidden="true" /><strong>还没有批量预案</strong><p>准备 2–50 个主题，一次生成可逐项确认的预案。</p><button type="button" className="button button-primary" onClick={() => navigate('/manuscript?mode=batch')}>创建第一个批次</button>
    </section> : <section className="batch-list" aria-label="批次列表">
      {batches.map(batch => {
        const [label, tone] = statusCopy[batch.status] || [batch.status, 'muted']
        const done = Number(batch.awaiting_confirmation_count || 0)
        const failed = Number(batch.failed_count || 0)
        const total = Number(batch.total_count || 0)
        const percent = total ? Math.round(((done + failed + Number(batch.cancelled_count || 0)) / total) * 100) : 0
        return <button type="button" className="batch-list-card" key={batch.batch_id} onClick={() => navigate(`/batches/${batch.batch_id}`)}>
          <span className="batch-list-index">{String(total).padStart(2, '0')}</span>
          <span className="batch-list-main"><span><b className={`batch-status is-${tone}`}>{label}</b><small>{formatTime(batch.created_at)}</small></span><strong>{total} 个主题 · 并发 {batch.concurrency}</strong><i><span style={{ width: `${percent}%` }} /></i></span>
          <span className="batch-list-counts"><b>{done}</b><small>待确认</small>{failed ? <em>{failed} 失败</em> : null}</span>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
      })}
    </section>}
  </main>
}

export { statusCopy, formatTime }
