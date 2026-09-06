import { ArrowLeft, ExternalLink, RefreshCw, RotateCcw, Square } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { archiveBatch, cancelBatch, getBatch, retryFailedBatchItems } from '../api/task'
import { toast } from '../lib/toast'
import { formatTime, statusCopy } from './BatchListPage'
import { confirmBatchProduction, cancelBatchProduction, cancelProduction, cancelTask } from '../api/task'
import { WorkspaceSaveQueue } from './workspaceSaveQueue'
import { readPendingEdits, writePendingEdits } from './workspacePendingStorage'
import { getTaskWorkspace, updateSegment } from '../api/task'
import { BatchProjectPreview } from './BatchProjectPreview'
import './batch-pages.css'

const itemCopy = {
  queued: ['排队', 'queued'], running: ['生成中', 'running'],
  awaiting_confirmation: ['待确认', 'success'], failed: ['失败', 'warning'],
  cancelled: ['已取消', 'muted'],
}
const productionCopy = { queued:['等待生产','queued'], generating_assets:['素材生成','running'], render_queued:['排队渲染','queued'], rendering:['渲染视频','running'], completed:['视频完成','success'], failed:['生产失败','warning'], stale:['待重新确认','warning'], cancelled:['生产已停止','muted'], cancelling:['正在停止','muted'] }

export function BatchDetailPage() {
  const { batchId } = useParams()
  const navigate = useNavigate()
  const [batch, setBatch] = useState(null)
  const [selected, setSelected] = useState({})
  const [previewId, setPreviewId] = useState(() => sessionStorage.getItem(`batch-preview:${batchId}`) || '')
  const [outcomes, setOutcomes] = useState([])
  const previewRef = useRef(null)
  const showPreview = id => { setPreviewId(id); sessionStorage.setItem(`batch-preview:${batchId}`, id) }
  const selectItem = item => setSelected(s => { const n={...s}; if(n[item.task_id]) delete n[item.task_id]; else n[item.task_id]={task_id:item.task_id,snapshot_key:item.snapshot_key,plan_version:item.plan_version}; return n })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [pollRevision, setPollRevision] = useState(0)
  const requestActive = useRef(null)

  const load = useCallback(async signal => {
    if (signal?.aborted) return
    const active = requestActive.current
    if (active && active.batchId === batchId && !active.signal?.aborted) return
    const request = { batchId, signal }
    requestActive.current = request
    const isCurrent = () => requestActive.current === request && !signal?.aborted
    try {
      const result = await getBatch(batchId, { signal })
      if (!isCurrent()) return
      setBatch(result)
      setError('')
      return result
    } catch (requestError) {
      if (isCurrent() && requestError?.kind !== 'cancelled' && requestError?.name !== 'CanceledError' && requestError?.code !== 'ERR_CANCELED') setError(requestError?.response?.status === 404 ? '批次不存在或已被移除。' : '进度暂时无法连接，正在自动重连，页面会保留当前状态。')
      return null
    } finally {
      if (isCurrent()) setLoading(false)
      if (requestActive.current === request) requestActive.current = null
    }
  }, [batchId])

  useEffect(() => {
    const controller = new AbortController()
    let timer
    const tick = async () => {
      await load(controller.signal)
      if (!controller.signal.aborted) timer = window.setTimeout(tick, 2000)
    }
    tick()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [load, pollRevision])

  const produce = async entries => {
    setBusy('produce'); setOutcomes([])
    try {
      const candidates=[], rejected=[]
      for (const entry of entries || Object.values(selected)) {
        let q
        try {
          let fresh=entry
          if (entry.task_id === previewId && previewRef.current) fresh=await previewRef.current.flush()
          else {
            const bodyKey=`insightcut:batch-fulltext:${entry.task_id}`
            if (sessionStorage.getItem(bodyKey)||localStorage.getItem(bodyKey)) throw Error('全文修改尚未保存，请先打开预览处理')
            const pending=readPendingEdits(entry.task_id)
            if (Object.keys(pending).length) {
              q=new WorkspaceSaveQueue({pending,send:(i,p)=>updateSegment(entry.task_id,i,p),refresh:()=>getTaskWorkspace(entry.task_id),onChange:(_,serial)=>writePendingEdits(entry.task_id,serial)})
              q.rebase(await getTaskWorkspace(entry.task_id));await q.flush()
              const d=await getTaskWorkspace(entry.task_id)
              fresh={task_id:entry.task_id,snapshot_key:d.snapshot_key,plan_version:d.plan_version}
            }
          }
          candidates.push(fresh)
        } catch(e) { rejected.push({task_id:entry.task_id,outcome:'conflict',error:e.message||'编辑保存失败'}) }
        finally { q?.stop() }
      }
      const result = candidates.length ? await confirmBatchProduction(batchId, candidates) : {items:[]}
      setOutcomes([...rejected,...(result.items || [])])
      setSelected(previous=>Object.fromEntries(Object.entries(previous).filter(([id])=>!result.items?.some(r=>r.task_id===id&&['accepted','already_running','already_completed'].includes(r.outcome)))))
      await load(); setPollRevision(v=>v+1)
    } catch(e){setError(e.message || '确认失败，编辑已保留')} finally{setBusy('')}
  }
  const stopProduction = async () => {setBusy('stop-video');try{await cancelBatchProduction(batchId);await load()}catch{setError('停止成片生产失败')}finally{setBusy('')}}
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
    try { const result = await retryFailedBatchItems(batchId); toast.success(`已重新排队 ${result.retried_count} 项`); await load(); setPollRevision(value => value + 1) }
    catch { toast.error('重试失败项未能启动') } finally { setBusy('') }
  }
  const toggleArchive = async () => {
    setBusy('archive')
    try {
      const archived = !batch.archived_at
      await archiveBatch(batchId, archived)
      toast.success(archived ? '批次已归档，项目和素材仍然保留' : '批次已恢复到当前列表')
      await load(); setPollRevision(v=>v+1)
    } catch { toast.error('归档状态保存失败，请重试') }
    finally { setBusy('') }
  }

  if (loading && !batch) return <main className="batch-page"><section className="batch-empty">正在恢复批次进度…</section></main>
  if (!batch) return <main className="batch-page"><section className="batch-empty" role="alert"><strong>{error || '批次无法打开'}</strong><button className="button button-secondary" type="button" onClick={() => navigate('/batches')}>返回批次列表</button></section></main>

  return <main className="batch-page">
    <header className="batch-detail-heading">
      <button type="button" className="batch-back" onClick={() => navigate('/batches')} aria-label="返回批次列表"><ArrowLeft size={18} /></button>
      <div><p>{batch.batch_id}</p><h1>批次进度</h1><span>创建于 {formatTime(batch.created_at)} · 共 {batch.total_count} 项 · 同时运行 {batch.concurrency} 项</span></div>
      <div className="batch-actions">{canRetry ? <button type="button" className="button button-secondary" disabled={Boolean(busy)} onClick={retry}><RotateCcw size={15} />{busy === 'retry' ? '排队中…' : '重试失败项'}</button> : null}{canCancel ? <button type="button" className="button button-secondary batch-cancel" disabled={Boolean(busy)} onClick={cancel}><Square size={14} />{busy === 'cancel' ? '停止中…' : '停止预案生成'}</button> : null}</div>
    </header>
    {error ? <section className="batch-alert" role="status"><span>{error}</span><button type="button" onClick={() => load()}><RefreshCw size={15} />立即重连</button></section> : null}
    <section className="batch-progress-card" aria-live="polite"><h2>预案进度</h2><p>实际运行 {batch.runtime_counts?.running||0} · 等待名额 {batch.runtime_counts?.queued||0} · 等待服务商限流或重试 {batch.runtime_counts?.provider_wait||0}</p>
      <div><span className={`batch-status is-${tone}`}>{statusLabel}</span><strong>{settled} / {batch.total_count}</strong><small>{percent}% 已有结果</small></div>
      <progress max={batch.total_count} value={settled} aria-label={`批次已处理 ${settled} 项，共 ${batch.total_count} 项`} />
      <ul><li><b>{counts.running || 0}</b><span>运行中</span></li><li><b>{counts.queued || 0}</b><span>排队</span></li><li><b>{counts.awaiting_confirmation || 0}</b><span>预案就绪</span></li><li><b>{counts.failed || 0}</b><span>失败</span></li></ul>
    </section>

    <section className="batch-progress-card"><h2>成片进度</h2><p>完成 {batch.production_counts?.completed||0} · 排队生产 {batch.production_counts?.queued||0} · 素材生成 {batch.production_counts?.generating_assets||0} · 等待渲染 {batch.production_counts?.render_queued||0} · 渲染中 {batch.production_counts?.rendering||0} · 失败 {batch.production_counts?.failed||0}</p><p>预案完成后才可确认生产；MP4 同时渲染 1 个，剪映草稿按需导出。</p></section>
    <div className="batch-selection"><button disabled={Boolean(busy)||!batch.items.some(i=>i.can_produce)} onClick={()=>setSelected(Object.fromEntries(batch.items.filter(i=>i.can_produce).map(i=>[i.task_id,{task_id:i.task_id,snapshot_key:i.snapshot_key,plan_version:i.plan_version}])))}>一键全选可生产项</button><button onClick={()=>setSelected({})}>取消全选</button><button className="button button-primary" disabled={Boolean(busy)||!Object.keys(selected).length} onClick={()=>produce()}>确认并生成 {Object.keys(selected).length} 个视频</button><button disabled={Boolean(busy)} onClick={stopProduction}>停止成片生产</button><span className="batch-selection-divider" aria-hidden="true"/><button className="button button-secondary" disabled={Boolean(busy)} onClick={toggleArchive}>{batch.archived_at?'恢复批次':'归档批次'}</button><small>{batch.archived_at?'项目和素材仍然保留':'只收起批次，不删除内容'}</small></div>
    {outcomes.length>0&&<ul aria-live="polite">{outcomes.map((r,i)=><li key={i}>{batch.items.find(x=>x.task_id===r.task_id)?.name||batch.items.find(x=>x.task_id===r.task_id)?.theme}：{r.outcome==='accepted'?'已加入生产':r.outcome==='already_running'?'已在生产':r.outcome==='already_completed'?'视频已完成':typeof r.error==='string'?r.error:r.error?.message||'无法启动'}</li>)}</ul>}
    <section className="batch-config-strip"><span>本批设置快照</span><b>{shared.style || '默认风格'}</b><i>{shared.ratio || '16:9'}</i><i>{shared.length || '自动'} 字</i><i>{shared.voice_type || '默认音色'}</i></section>

    <div className={`batch-detail-grid ${previewId ? 'has-preview' : ''}`}><section className="batch-item-table" aria-label="批次项目">
      <header><span>#</span><span>项目主题</span><span>状态</span><span>操作</span></header>
      {batch.items.map((item, index) => {
        const [label, itemTone] = productionCopy[item.production?.state] || itemCopy[item.status] || [item.status, 'muted']
        return <article key={item.item_id}>
          <span className="batch-item-number"><input type="checkbox" aria-label={`选择项目 ${index+1}`} disabled={!item.can_produce} checked={Boolean(selected[item.task_id])} onChange={()=>selectItem(item)}/>{String(index + 1).padStart(2, '0')}</span>
          <span className="batch-item-title"><strong>{item.name || item.theme}</strong>{item.name ? <small>{item.theme}</small> : null}<small>{item.input_mode==='script'?'完整文稿':'主题'} · {item.segments_count??0} 个分镜</small>{item.unavailable_reason&&<small>{item.unavailable_reason}</small>}{item.production&&<small>{({queued:'排队生产',generating_assets:'生成素材',render_queued:'排队渲染',rendering:'生成 MP4',completed:'视频已完成',failed:'生产失败',cancelled:'生产已取消',stale:'预案已变化',cancelling:'停止中'})[item.production.state]} {item.production.error}</small>}{item.error ? <em role="alert">{item.error}</em> : null}</span>
          <span><b className={`batch-status is-${itemTone}`}>{label}</b>{item.attempt > 1 ? <small>第 {item.attempt} 次</small> : null}</span>
          <span className="batch-item-actions">{item.task_id&&<button onClick={()=>showPreview(item.task_id)}>预览与编辑</button>}{item.can_produce&&<button disabled={Boolean(busy)} onClick={()=>produce([{task_id:item.task_id,snapshot_key:item.snapshot_key,plan_version:item.plan_version}])}>确认并生成视频</button>}{item.production&&['queued','generating_assets','render_queued','rendering'].includes(item.production.state)&&<button onClick={async()=>{await cancelProduction(item.task_id);load()}}>停止本项生产</button>}{item.status==='running'&&<button onClick={async()=>{await cancelTask(item.task_id);load()}}>停止本项预案</button>}{item.task_id ? <button type="button" onClick={() => navigate(`/workspace/${item.task_id}`)} disabled={item.status === 'queued'}>{item.status === 'awaiting_confirmation' ? '打开工作台' : '查看检查点'}<ExternalLink size={13} /></button> : <small>等待创建</small>}</span>
        </article>
      })}
    </section>{previewId&&<BatchProjectPreview key={previewId} ref={previewRef} taskId={previewId} onBack={()=>showPreview('')} onUpdated={()=>setPollRevision(v=>v+1)}/>}</div>
  </main>
}
