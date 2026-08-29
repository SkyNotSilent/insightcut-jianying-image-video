import { ArrowLeft, ExternalLink, RefreshCw, RotateCcw, Square } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { cancelBatch, getBatch, retryFailedBatchItems } from '../api/task'
import { toast } from '../lib/toast'
import { formatTime, statusCopy } from './BatchListPage'
import './batch-pages.css'

const itemCopy = {
  queued: ['排队', 'queued'], running: ['生成中', 'running'],
  awaiting_confirmation: ['待确认', 'success'], failed: ['失败', 'warning'],
  cancelled: ['已取消', 'muted'],
}
const terminal = new Set(['completed', 'completed_with_errors', 'cancelled'])

export function BatchDetailPage() {
  const { batchId } = useParams()
  const navigate = useNavigate()
  const [batch, setBatch] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const requestActive = useRef(false)

  const load = useCallback(async signal => {
    if (requestActive.current) return
    requestActive.current = true
    try {
      const result = await getBatch(batchId, { signal })
      setBatch(result)
      setError('')
      return result
    } catch (requestError) {
      if (requestError?.name !== 'CanceledError') setError(requestError?.response?.status === 404 ? '批次不存在或已被移除。' : '进度暂时无法连接，页面会保留当前状态。')
      return null
    } finally {
      requestActive.current = false
      setLoading(false)
    }
  }, [batchId])

  useEffect(() => {
    const controller = new AbortController()
    let timer
    const tick = async () => {
      const current = await load(controller.signal)
      if (!controller.signal.aborted && !terminal.has(current?.status)) timer = window.setTimeout(tick, 2000)
    }
    tick()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [load])

  const counts = batch?.counts || {}
  const settled = Number(counts.awaiting_confirmation || 0) + Number(counts.failed || 0) + Number(counts.cancelled || 0)
  const percent = batch?.total_count ? Math.round((settled / batch.total_count) * 100) : 0
  const [statusLabel, tone] = statusCopy[batch?.status] || [batch?.status || '读取中', 'muted']
  const failed = Number(counts.failed || 0)
  const canCancel = batch && ['queued', 'running'].includes(batch.status)
  const canRetry = batch && !batch.cancel_requested && failed > 0
  const shared = useMemo(() => batch?.config || {}, [batch])

  const cancel = async () => {
    setBusy('cancel')
    try { setBatch(await cancelBatch(batchId)); toast.success('已请求取消，运行项会在检查点停止') }
    catch { toast.error('取消批次失败') } finally { setBusy('') }
  }
  const retry = async () => {
    setBusy('retry')
    try { const result = await retryFailedBatchItems(batchId); toast.success(`已重新排队 ${result.retried_count} 项`); await load() }
    catch { toast.error('重试失败项未能启动') } finally { setBusy('') }
  }

  if (loading && !batch) return <main className="batch-page"><section className="batch-empty">正在恢复批次进度…</section></main>
  if (!batch) return <main className="batch-page"><section className="batch-empty" role="alert"><strong>{error || '批次无法打开'}</strong><button className="button button-secondary" type="button" onClick={() => navigate('/batches')}>返回批次列表</button></section></main>

  return <main className="batch-page">
    <header className="batch-detail-heading">
      <button type="button" className="batch-back" onClick={() => navigate('/batches')} aria-label="返回批次列表"><ArrowLeft size={18} /></button>
      <div><p>{batch.batch_id}</p><h1>批次进度</h1><span>创建于 {formatTime(batch.created_at)} · 并发 {batch.concurrency}</span></div>
      <div className="batch-actions">{canRetry ? <button type="button" className="button button-secondary" disabled={Boolean(busy)} onClick={retry}><RotateCcw size={15} />{busy === 'retry' ? '排队中…' : '重试失败项'}</button> : null}{canCancel ? <button type="button" className="button button-secondary batch-cancel" disabled={Boolean(busy)} onClick={cancel}><Square size={14} />{busy === 'cancel' ? '停止中…' : '取消批次'}</button> : null}</div>
    </header>

    {error ? <section className="batch-alert" role="status"><span>{error}</span><button type="button" onClick={() => load()}><RefreshCw size={15} />立即重连</button></section> : null}
    <section className="batch-progress-card" aria-live="polite">
      <div><span className={`batch-status is-${tone}`}>{statusLabel}</span><strong>{settled} / {batch.total_count}</strong><small>{percent}% 已有结果</small></div>
      <progress max={batch.total_count} value={settled} aria-label={`批次已处理 ${settled} 项，共 ${batch.total_count} 项`} />
      <ul><li><b>{counts.running || 0}</b><span>运行中</span></li><li><b>{counts.queued || 0}</b><span>排队</span></li><li><b>{counts.awaiting_confirmation || 0}</b><span>待确认</span></li><li><b>{counts.failed || 0}</b><span>失败</span></li></ul>
    </section>

    <section className="batch-config-strip"><span>共享预设</span><b>{shared.style || '默认风格'}</b><i>{shared.ratio || '16:9'}</i><i>{shared.length || '自动'} 字</i><i>{shared.voice_type || '默认音色'}</i></section>

    <section className="batch-item-table" aria-label="批次项目">
      <header><span>#</span><span>项目主题</span><span>状态</span><span>操作</span></header>
      {batch.items.map((item, index) => {
        const [label, itemTone] = itemCopy[item.status] || [item.status, 'muted']
        return <article key={item.item_id}>
          <span className="batch-item-number">{String(index + 1).padStart(2, '0')}</span>
          <span className="batch-item-title"><strong>{item.name || item.theme}</strong>{item.name ? <small>{item.theme}</small> : null}{item.error ? <em role="alert">{item.error}</em> : null}</span>
          <span><b className={`batch-status is-${itemTone}`}>{label}</b>{item.attempt > 1 ? <small>第 {item.attempt} 次</small> : null}</span>
          <span>{item.task_id ? <button type="button" onClick={() => navigate(`/workspace/${item.task_id}`)} disabled={item.status === 'queued'}>{item.status === 'awaiting_confirmation' ? '打开工作台' : '查看检查点'}<ExternalLink size={13} /></button> : <small>等待创建</small>}</span>
        </article>
      })}
    </section>
  </main>
}
